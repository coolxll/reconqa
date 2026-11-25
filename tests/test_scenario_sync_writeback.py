"""Comprehensive tests for R1: Online Chain / TryIt Scenario L3 Writeback & Zero-JDK L0->L4 Loop."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.mcp_server import create_mcp_server
from reconqa.models.schemas import AssertionEvalResult
from reconqa.models.tables import Run, ScenarioModel
from reconqa.server.routers.chains import (
    _apply_uses_to_step,
    _merge_recorded_and_bound_path,
    _replace_bound_tokens_in_path,
)
from reconqa.server.routers.scenarios import (
    canonical_path_shape,
    clean_endpoint_path,
    endpoint_matches_target,
    match_endpoint_scenario_assertions,
    sync_assertion_evals_to_scenarios,
)
from tests.test_deep_assertions_and_chains import StatefulOrderServer, _extract_mcp_json


def _slot_map(cap_item: dict[str, Any]) -> dict[str, str]:
    return {item["slot"]: item["status"] for item in cap_item["case_matrix"]}


def _setup_isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_name: str
) -> Path:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / db_name
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    return ws_dir


def _build_three_step_chain(base_url: str, pay_code: str = "VALID-PAY") -> list[dict[str, Any]]:
    return [
        {
            "step": 1,
            "name": "Create Order",
            "method": "POST",
            "path": "/orders",
            "raw_url": f"{base_url}/orders",
            "request_body": {"amount": 50},
            "expected_status": 201,
            "extracts": {"order_id": "$.data.order_id"},
        },
        {
            "step": 2,
            "name": "Pay Order",
            "method": "POST",
            "path": "/orders/{{order_id}}/pay",
            "raw_url": f"{base_url}/orders/{{{{order_id}}}}/pay",
            "request_body": {"pay_code": pay_code},
            "expected_status": 200,
        },
        {
            "step": 3,
            "name": "Get Order",
            "method": "GET",
            "path": "/orders/{{order_id}}",
            "raw_url": f"{base_url}/orders/{{{{order_id}}}}",
            "expected_status": 200,
        },
    ]


async def _seed_project_and_cred(
    client: httpx.AsyncClient,
    srv_url: str,
    required_token: str,
    name: str = "R1 Sync Project",
) -> tuple[str, str]:
    proj = (await client.post("/api/v1/projects", json={"name": name})).json()
    pid = proj["id"]
    await client.post(
        f"/api/v1/projects/{pid}/targets",
        json={"root_domain": "127.0.0.1", "base_url": srv_url},
    )
    cred = (
        await client.post(
            f"/api/v1/projects/{pid}/auth-profiles",
            json={
                "name": "Order Auth",
                "role_tag": "buyer",
                "auth_type": "headers",
                "extra_headers_ref": {"Authorization": required_token},
            },
        )
    ).json()
    return pid, cred["id"]


async def _seed_completed_merge_run(
    client: httpx.AsyncClient,
    pid: str,
    srv_url: str,
    scenario_slug: str,
) -> str:
    run_resp = await client.post(
        f"/api/v1/projects/{pid}/runs?auto_start=false",
        json={"name": "Merge Discovery Run", "target_url": srv_url, "modules": ["merge"]},
    )
    assert run_resp.status_code == 201
    run_data = run_resp.json()
    run_id = run_data["id"]

    hub_dir = Path(run_data["workspace_dir"]) / "hub"
    hub_dir.mkdir(parents=True, exist_ok=True)
    openapi_doc = {
        "openapi": "3.0.3",
        "servers": [{"url": srv_url}],
        "paths": {
            "/orders": {
                "post": {
                    "summary": "Create order",
                    "x-discovery-source": "har-live",
                    "x-scenarios": [scenario_slug],
                    "responses": {"201": {"description": "Created"}},
                }
            },
            "/orders/{id}/pay": {
                "post": {
                    "summary": "Pay order",
                    "x-discovery-source": "har-live",
                    "x-scenarios": [scenario_slug],
                    "responses": {"200": {"description": "Paid"}},
                }
            },
            "/orders/{id}": {
                "get": {
                    "summary": "Get order detail",
                    "x-discovery-source": "har-live",
                    "x-scenarios": [scenario_slug],
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }
    (hub_dir / "openapi.json").write_text(json.dumps(openapi_doc, indent=2), encoding="utf-8")

    sf = db_mod.get_session_factory()
    async with sf() as session:
        run_row = await session.get(Run, run_id)
        assert run_row is not None
        run_row.status = "completed"
        run_row.started_at = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
        run_row.finished_at = datetime(2026, 11, 24, 10, 2, tzinfo=UTC)
        run_row.summary_metrics = {"modules_executed": ["merge"]}
        await session.commit()

    return run_id


# ---------------------------------------------------------------------------
# Case 1 & 2: Chain Execute (pass/fail/skip/transport-error) & Chain Resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_execute_and_resume_sync_scenarios_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_chain_sync.db")
    db_path = tmp_path / "orders_chain.db"
    required_token = "Bearer super-secret-chain-token-999"

    from reconqa.server.app import create_app

    with StatefulOrderServer(db_path=db_path, required_token=required_token) as order_srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, cred_id = await _seed_project_and_cred(client, order_srv.base_url, required_token)
                await _seed_completed_merge_run(client, pid, order_srv.base_url, "01_checkout")

                sc_resp = await client.post(
                    f"/api/v1/projects/{pid}/scenarios",
                    json={
                        "slug": "01_checkout",
                        "name": "01 Checkout",
                        "priority": "P0",
                        "assertions": [
                            {
                                "id": "sc-create",
                                "title": "Order created with PENDING",
                                "target_endpoint": "POST /orders",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PENDING",
                            },
                            {
                                "id": "sc-pay",
                                "title": "Payment returns PAID",
                                "target_endpoint": "POST /orders/{id}/pay",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PAID",
                            },
                            {
                                "id": "sc-get",
                                "title": "Order detail tags verified",
                                "target_endpoint": "GET /orders/{id}",
                                "jsonpath": "$.data.tags",
                                "operator": "array_contains",
                                "expected": "verified",
                            },
                        ],
                    },
                )
                assert sc_resp.status_code == 201
                sc_id = sc_resp.json()["id"]

                # Case 1: Execute with failing step 2 (step 3 skipped) -> step 1 passed, step 2 failed, step 3 stays pending
                exec_resp = await client.post(
                    f"/api/v1/projects/{pid}/chains/execute",
                    json={
                        "chain_name": "Checkout Sync Chain",
                        "steps": _build_three_step_chain(order_srv.base_url, pay_code="BAD-PAY"),
                        "base_url": order_srv.base_url,
                        "credential_id": cred_id,
                        "stop_on_failure": True,
                    },
                )
                assert exec_resp.status_code == 200, exec_resp.text
                r1 = exec_resp.json()
                assert r1["status"] == "failed"
                assert r1["step_results"][0]["status"] == "passed"
                assert r1["step_results"][1]["status"] == "failed"
                assert r1["step_results"][2]["status"] == "skipped"
                assert r1["synced_scenarios"] == 1
                assert r1["synced_assertions"] == 2

                # Checkpoint GET reflects synced counts too
                cp_resp = await client.get(f"/api/v1/projects/{pid}/chain-runs/{r1['chain_run_id']}")
                assert cp_resp.status_code == 200
                assert cp_resp.json()["synced_scenarios"] == 1
                assert cp_resp.json()["synced_assertions"] == 2

                sc1 = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                statuses1 = {a["id"]: a["status"] for a in sc1["assertions"]}
                assert statuses1 == {
                    "sc-create": "passed",
                    "sc-pay": "failed",
                    "sc-get": "pending",
                }
                assert sc1["assertions_passed_count"] == 1

                # Capability map reflects failed happy_path before resume
                cap1 = (await client.get(f"/api/v1/projects/{pid}/capabilities")).json()
                assert _slot_map(cap1["capabilities"][0])["happy_path"] == "failed"

                # Pre-assertion network transport error must NOT clobber sc-create ("passed") or sc-pay ("failed")
                err_resp = await client.post(
                    f"/api/v1/projects/{pid}/chains/execute",
                    json={
                        "chain_name": "Unreachable Chain",
                        "steps": [
                            {
                                "step": 1,
                                "method": "POST",
                                "path": "/orders",
                                "raw_url": "http://127.0.0.1:1/orders",
                            }
                        ],
                        "base_url": "http://127.0.0.1:1",
                    },
                )
                assert err_resp.status_code == 200
                assert err_resp.json()["status"] == "failed"
                assert err_resp.json()["step_results"][0]["assertions"] == []
                assert err_resp.json()["synced_scenarios"] == 0
                assert err_resp.json()["synced_assertions"] == 0
                sc_after_err = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert {a["id"]: a["status"] for a in sc_after_err["assertions"]} == statuses1

                # Case 2: Resume from failed step 2 with VALID-PAY -> sc-pay (failed->passed), sc-get (pending->passed)
                resume_resp = await client.post(
                    f"/api/v1/projects/{pid}/chain-runs/{r1['chain_run_id']}/resume",
                    json={
                        "override_step_body": {"pay_code": "VALID-PAY"},
                        "credential_id": cred_id,
                    },
                )
                assert resume_resp.status_code == 200, resume_resp.text
                r2 = resume_resp.json()
                assert r2["status"] == "passed"
                assert r2["synced_scenarios"] == 1
                assert r2["synced_assertions"] == 2

                cp2 = (await client.get(f"/api/v1/projects/{pid}/chain-runs/{r1['chain_run_id']}")).json()
                assert cp2["synced_scenarios"] == 1
                assert cp2["synced_assertions"] == 2

                sc2 = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert {a["id"]: a["status"] for a in sc2["assertions"]} == {
                    "sc-create": "passed",
                    "sc-pay": "passed",
                    "sc-get": "passed",
                }
                assert sc2["assertions_passed_count"] == 3

                cap2 = (await client.get(f"/api/v1/projects/{pid}/capabilities")).json()
                assert _slot_map(cap2["capabilities"][0])["happy_path"] == "covered"
                assert cap2["metrics"]["invariant_pass_rate_pct"] == 100.0

                # Idempotent re-run of passing chain returns (0, 0) without changing anything
                rerun_resp = await client.post(
                    f"/api/v1/projects/{pid}/chains/execute",
                    json={
                        "chain_name": "Idempotent Passing Chain",
                        "steps": _build_three_step_chain(order_srv.base_url, pay_code="VALID-PAY"),
                        "base_url": order_srv.base_url,
                        "credential_id": cred_id,
                    },
                )
                assert rerun_resp.status_code == 200
                assert rerun_resp.json()["status"] == "passed"
                assert rerun_resp.json()["synced_scenarios"] == 0
                assert rerun_resp.json()["synced_assertions"] == 0


# ---------------------------------------------------------------------------
# Case 3 & 4: sync_scenarios=False Opt-Out & TryIt Replay Auto-Binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_opt_out_and_tryit_replay_auto_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_replay_optout.db")
    db_path = tmp_path / "orders_replay.db"
    required_token = "Bearer replay-token-123"

    from reconqa.server.app import create_app

    with StatefulOrderServer(db_path=db_path, required_token=required_token) as order_srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, cred_id = await _seed_project_and_cred(client, order_srv.base_url, required_token)

                sc_resp = await client.post(
                    f"/api/v1/projects/{pid}/scenarios",
                    json={
                        "slug": "replay_sc",
                        "name": "Replay Scenario",
                        "priority": "P0",
                        "assertions": [
                            {
                                "id": "rp-create",
                                "title": "Create order status PENDING",
                                "target_endpoint": "POST /orders",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PENDING",
                            },
                            {
                                "id": "rp-pay",
                                "title": "Pay order status PAID",
                                "target_endpoint": "POST /orders/{id}/pay",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PAID",
                            },
                            {
                                "id": "rp-get",
                                "title": "Get order tags verified",
                                "target_endpoint": "GET /orders/{id}",
                                "jsonpath": "$.data.tags",
                                "operator": "array_contains",
                                "expected": "verified",
                            },
                        ],
                    },
                )
                assert sc_resp.status_code == 201
                sc_id = sc_resp.json()["id"]

                # Case 3a: Execute with sync_scenarios=False (even with include_scenario_assertions=True)
                exec_no_sync = await client.post(
                    f"/api/v1/projects/{pid}/chains/execute",
                    json={
                        "chain_name": "OptOut Chain",
                        "steps": _build_three_step_chain(order_srv.base_url, pay_code="BAD-PAY"),
                        "base_url": order_srv.base_url,
                        "credential_id": cred_id,
                        "sync_scenarios": False,
                        "include_scenario_assertions": True,
                    },
                )
                assert exec_no_sync.status_code == 200
                r_exec = exec_no_sync.json()
                assert r_exec["step_results"][0]["assertions"][0]["passed"] is True
                assert r_exec["synced_scenarios"] == 0
                assert r_exec["synced_assertions"] == 0

                # Case 3b: Resume with sync_scenarios=False
                res_no_sync = await client.post(
                    f"/api/v1/projects/{pid}/chain-runs/{r_exec['chain_run_id']}/resume",
                    json={
                        "override_step_body": {"pay_code": "VALID-PAY"},
                        "credential_id": cred_id,
                        "sync_scenarios": False,
                    },
                )
                assert res_no_sync.status_code == 200
                r_res = res_no_sync.json()
                assert r_res["status"] == "passed"
                assert r_res["synced_scenarios"] == 0
                assert r_res["synced_assertions"] == 0

                # All 3 assertions must still be pending!
                sc_pending = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert all(a["status"] == "pending" for a in sc_pending["assertions"])

                order_id = r_res["context_vars"]["order_id"]

                # Case 3c: Replay with sync_scenarios=False evaluates but does not write back
                rp_no_sync = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "GET",
                        "url": f"{order_srv.base_url}/orders/{order_id}?expand=1",
                        "credential_id": cred_id,
                        "sync_scenarios": False,
                    },
                )
                assert rp_no_sync.status_code == 200
                rp_ns_data = rp_no_sync.json()
                assert len(rp_ns_data["assertion_results"]) == 1
                assert rp_ns_data["assertion_results"][0]["passed"] is True
                assert rp_ns_data["synced_scenarios"] == 0
                assert rp_ns_data["synced_assertions"] == 0

                sc_still_pending = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert all(a["status"] == "pending" for a in sc_still_pending["assertions"])

                # Case 4a: TryIt Replay with include_scenario_assertions=False -> no evaluation, no sync
                rp_no_inc = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "GET",
                        "url": f"{order_srv.base_url}/orders/{order_id}",
                        "credential_id": cred_id,
                        "include_scenario_assertions": False,
                    },
                )
                assert rp_no_inc.status_code == 200
                assert rp_no_inc.json()["assertion_results"] == []
                assert rp_no_inc.json()["synced_assertions"] == 0

                # Case 4b: TryIt Replay with explicit assertions=[] -> skips auto-binding
                rp_empty_list = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "GET",
                        "url": f"{order_srv.base_url}/orders/{order_id}",
                        "credential_id": cred_id,
                        "assertions": [],
                    },
                )
                assert rp_empty_list.status_code == 200
                assert rp_empty_list.json()["assertion_results"] == []
                assert rp_empty_list.json()["synced_assertions"] == 0

                # Case 4c: Bare TryIt Replay (assertions=None, defaults True) auto-binds & syncs path-param + query URL
                rp_auto = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "GET",
                        "url": f"{order_srv.base_url}/orders/{order_id}?expand=1",
                        "credential_id": cred_id,
                    },
                )
                assert rp_auto.status_code == 200, rp_auto.text
                rp_auto_data = rp_auto.json()
                assert len(rp_auto_data["assertion_results"]) == 1
                assert rp_auto_data["assertion_results"][0]["id"] == "rp-get"
                assert rp_auto_data["assertion_results"][0]["passed"] is True
                assert rp_auto_data["synced_scenarios"] == 1
                assert rp_auto_data["synced_assertions"] == 1

                # Case 4d: Network error on Replay (resp.error != None) does not touch scenario assertions
                rp_err = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "GET",
                        "url": f"http://127.0.0.1:1/orders/{order_id}",
                        "credential_id": cred_id,
                    },
                )
                assert rp_err.status_code == 200
                assert rp_err.json()["error"] is not None
                assert rp_err.json()["assertion_results"] == []
                assert rp_err.json()["synced_assertions"] == 0

                sc_after = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert {a["id"]: a["status"] for a in sc_after["assertions"]} == {
                    "rp-create": "pending",
                    "rp-pay": "pending",
                    "rp-get": "passed",
                }


# ---------------------------------------------------------------------------
# Case 5: Fallback Match Without ID, GraphQL #opName / :param, Failed-Wins, Idempotency & Archived
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_matching_graphql_colon_param_failed_wins_idempotency_and_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_fallback_unit.db")
    db_path = tmp_path / "orders_fb.db"
    required_token = "Bearer fb-token-1"

    # Helper path-matching assertions
    assert clean_endpoint_path("https://api.example.com/graphql?v=1#GetOrderList") == "/graphql#GetOrderList"
    assert clean_endpoint_path("/api/users/:id?expand=true") == "/api/users/{id}"
    assert canonical_path_shape("/graphql#GetOrderList") == "/graphql#GetOrderList"
    assert canonical_path_shape("/api/users/:userId") == "/api/users/{param}"
    assert endpoint_matches_target("POST", "https://api.example.com/graphql#GetOrderList", "POST /graphql#GetOrderList")
    assert not endpoint_matches_target("POST", "https://api.example.com/graphql#OtherOp", "POST /graphql#GetOrderList")
    assert endpoint_matches_target("GET", "/api/users/42?fields=name", "GET /api/users/:id")

    from reconqa.server.app import create_app

    with StatefulOrderServer(db_path=db_path, required_token=required_token) as order_srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, cred_id = await _seed_project_and_cred(client, order_srv.base_url, required_token)

                # Active scenario with :param and #opName targets + inline replay target
                sc_active = (
                    await client.post(
                        f"/api/v1/projects/{pid}/scenarios",
                        json={
                            "slug": "active_fb",
                            "name": "Active Fallback",
                            "priority": "P0",
                            "assertions": [
                                {
                                    "id": "fb-create",
                                    "title": "Status equals PENDING",
                                    "target_endpoint": "POST /orders",
                                    "jsonpath": "$.data.status",
                                    "operator": "equals",
                                    "expected": "PENDING",
                                },
                                {
                                    "id": "fb-gql",
                                    "title": "GraphQL orders non-empty",
                                    "target_endpoint": "POST /graphql#GetOrderList",
                                    "jsonpath": "$.data.orders",
                                    "operator": "not_empty",
                                    "expected": "",
                                },
                                {
                                    "id": "fb-colon",
                                    "title": "User lookup ok",
                                    "target_endpoint": "GET /api/users/:id",
                                    "jsonpath": "$.data.id",
                                    "operator": "not_empty",
                                    "expected": "",
                                },
                            ],
                        },
                    )
                ).json()

                # Archived scenario that must never be modified
                sc_archived = (
                    await client.post(
                        f"/api/v1/projects/{pid}/scenarios",
                        json={
                            "slug": "archived_fb",
                            "name": "Archived",
                            "status": "archived",
                            "assertions": [
                                {
                                    "id": "arch-create",
                                    "title": "Status equals PENDING",
                                    "target_endpoint": "POST /orders",
                                    "jsonpath": "$.data.status",
                                    "operator": "equals",
                                    "expected": "PENDING",
                                }
                            ],
                        },
                    )
                ).json()

                # Inline Replay omitting `id` -> matches fb-create via fallback (endpoint + title + operator)
                rp_inline = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "POST",
                        "url": f"{order_srv.base_url}/orders",
                        "body": {"amount": 30},
                        "credential_id": cred_id,
                        "assertions": [
                            {
                                "title": "status equals pending",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PENDING",
                            }
                        ],
                    },
                )
                assert rp_inline.status_code == 200
                assert rp_inline.json()["assertion_results"][0]["passed"] is True
                assert rp_inline.json()["synced_scenarios"] == 1
                assert rp_inline.json()["synced_assertions"] == 1

                # Direct helper verification: GraphQL #opName + :param + duplicate passed+failed ("failed" wins)
                sf = db_mod.get_session_factory()
                async with sf() as session:
                    # 1. Archived scenario untouched & fb-create already passed -> re-running passed returns (0, 0)
                    before_row = await session.get(ScenarioModel, sc_active["id"])
                    assert before_row is not None
                    before_updated_at = before_row.updated_at

                    sc_n, as_n = await sync_assertion_evals_to_scenarios(
                        session,
                        pid,
                        [
                            (
                                "POST",
                                "/orders",
                                [
                                    AssertionEvalResult(
                                        id="",
                                        title="Status equals PENDING",
                                        operator="equals",
                                        passed=True,
                                    )
                                ],
                            )
                        ],
                    )
                    assert (sc_n, as_n) == (0, 0)
                    await session.refresh(before_row)
                    assert before_row.updated_at == before_updated_at

                    # 2. Fallback match on #opName & :param, plus duplicate hit on fb-create (passed + failed -> failed wins!)
                    sc_n2, as_n2 = await sync_assertion_evals_to_scenarios(
                        session,
                        pid,
                        [
                            (
                                "POST",
                                "https://api.example.test/graphql?v=2#GetOrderList",
                                [
                                    AssertionEvalResult(
                                        id="ephemeral-gql-id",
                                        title="GraphQL orders non-empty",
                                        operator="not_empty",
                                        passed=True,
                                    )
                                ],
                            ),
                            (
                                "GET",
                                "/api/users/42?include=profile",
                                [
                                    AssertionEvalResult(
                                        id="",
                                        title="User lookup ok",
                                        operator="not_empty",
                                        passed=True,
                                    )
                                ],
                            ),
                            (
                                "POST",
                                "/orders",
                                [
                                    AssertionEvalResult(
                                        id="fb-create",
                                        title="Status equals PENDING",
                                        operator="equals",
                                        passed=True,
                                    ),
                                    AssertionEvalResult(
                                        id="fb-create",
                                        title="Status equals PENDING",
                                        operator="equals",
                                        passed=False,
                                    ),
                                ],
                            ),
                        ],
                    )
                    assert (sc_n2, as_n2) == (1, 3)

                    arch_row = await session.get(ScenarioModel, sc_archived["id"])
                    assert arch_row is not None
                    assert json.loads(arch_row.assertions_json)[0]["status"] == "pending"

                sc_final = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_active['id']}")).json()
                assert {a["id"]: a["status"] for a in sc_final["assertions"]} == {
                    "fb-create": "failed",
                    "fb-gql": "passed",
                    "fb-colon": "passed",
                }


# ---------------------------------------------------------------------------
# Case 6: MCP Tools Parity (Execute, Resume, Replay -> List Scenarios & Capability Map)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tools_sync_scenarios_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_mcp_sync.db")
    db_path = tmp_path / "orders_mcp.db"
    required_token = "Bearer mcp-sync-token"

    from reconqa.server.app import create_app

    with StatefulOrderServer(db_path=db_path, required_token=required_token) as order_srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, cred_id = await _seed_project_and_cred(client, order_srv.base_url, required_token)
                await _seed_completed_merge_run(client, pid, order_srv.base_url, "mcp_checkout")

            mcp = create_mcp_server("http://testserver", transport=transport)

            upserted = _extract_mcp_json(
                await mcp.call_tool(
                    "reconqa_upsert_scenario",
                    {
                        "project_id": pid,
                        "slug": "mcp_checkout",
                        "priority": "P0",
                        "assertions": [
                            {
                                "id": "mcp-create",
                                "title": "Create order status",
                                "target_endpoint": "POST /orders",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PENDING",
                            },
                            {
                                "id": "mcp-pay",
                                "title": "Pay order status",
                                "target_endpoint": "POST /orders/{id}/pay",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PAID",
                            },
                            {
                                "id": "mcp-get",
                                "title": "Get order verified",
                                "target_endpoint": "GET /orders/{id}",
                                "jsonpath": "$.data.tags",
                                "operator": "array_contains",
                                "expected": "verified",
                            },
                        ],
                    },
                )
            )
            assert upserted["assertion_count"] == 3

            # 1. reconqa_execute_chain fails at step 2 -> syncs mcp-create=passed, mcp-pay=failed
            mcp_exec = _extract_mcp_json(
                await mcp.call_tool(
                    "reconqa_execute_chain",
                    {
                        "project_id": pid,
                        "chain_name": "MCP Sync Chain",
                        "steps": _build_three_step_chain(order_srv.base_url, pay_code="BAD-CODE"),
                        "base_url": order_srv.base_url,
                        "credential_id": cred_id,
                    },
                )
            )
            assert mcp_exec["status"] == "failed"
            assert mcp_exec["synced_scenarios"] == 1
            assert mcp_exec["synced_assertions"] == 2

            # 2. reconqa_resume_chain recovers -> syncs mcp-pay & mcp-get to passed
            mcp_res = _extract_mcp_json(
                await mcp.call_tool(
                    "reconqa_resume_chain",
                    {
                        "project_id": pid,
                        "chain_run_id": mcp_exec["chain_run_id"],
                        "override_step_body": {"pay_code": "VALID-PAY"},
                        "credential_id": cred_id,
                    },
                )
            )
            assert mcp_res["status"] == "passed"
            assert mcp_res["synced_scenarios"] == 1
            assert mcp_res["synced_assertions"] == 2

            # 3. Verify reconqa_list_scenarios shows assertions_passed_count == 3
            sc_list = _extract_mcp_json(
                await mcp.call_tool("reconqa_list_scenarios", {"project_id": pid})
            )
            assert sc_list["scenarios"][0]["assertions_passed_count"] == 3

            # 4. reconqa_replay_endpoint with assertions + sync_scenarios=False, then sync_scenarios=True (with failing assertion -> passed->failed)
            order_id = mcp_res["context_vars"]["order_id"]
            rp_no_sync = _extract_mcp_json(
                await mcp.call_tool(
                    "reconqa_replay_endpoint",
                    {
                        "project_id": pid,
                        "method": "GET",
                        "url": f"{order_srv.base_url}/orders/{order_id}",
                        "credential_id": cred_id,
                        "sync_scenarios": False,
                    },
                )
            )
            assert len(rp_no_sync["assertion_results"]) == 1
            assert rp_no_sync["synced_assertions"] == 0

            rp_fail_sync = _extract_mcp_json(
                await mcp.call_tool(
                    "reconqa_replay_endpoint",
                    {
                        "project_id": pid,
                        "method": "GET",
                        "url": f"{order_srv.base_url}/orders/ORD-NONEXISTENT",
                        "credential_id": cred_id,
                        "assertions": [
                            {
                                "id": "mcp-get",
                                "title": "Get order verified",
                                "jsonpath": "$.data.tags",
                                "operator": "array_contains",
                                "expected": "verified",
                            }
                        ],
                        "sync_scenarios": True,
                    },
                )
            )
            assert rp_fail_sync["assertion_results"][0]["passed"] is False
            assert rp_fail_sync["synced_scenarios"] == 1
            assert rp_fail_sync["synced_assertions"] == 1

            cap_map = _extract_mcp_json(
                await mcp.call_tool("reconqa_get_capability_map", {"project_id": pid})
            )
            assert _slot_map(cap_map["capabilities"][0])["happy_path"] == "failed"


# ---------------------------------------------------------------------------
# Case 7: Full Zero-JDK L0 -> L1 -> L2 -> L3 -> L4 Acceptance Loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_jdk_full_l0_to_l4_acceptance_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_zero_jdk_l0_l4.db")
    db_path = tmp_path / "orders_l0_l4.db"
    required_token = "Bearer zero-jdk-token"

    from reconqa.server.app import create_app

    # Confirm resolve_bin is disabled (no mvn / JDK available)
    assert settings.resolve_bin("mvn") is None

    with StatefulOrderServer(db_path=db_path, required_token=required_token) as order_srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, cred_id = await _seed_project_and_cred(
                    client, order_srv.base_url, required_token, name="Zero-JDK Shop"
                )

                # L0: Create P0 scenario with no endpoints yet
                sc_l0 = (
                    await client.post(
                        f"/api/v1/projects/{pid}/scenarios",
                        json={
                            "slug": "01_order_checkout",
                            "name": "01 Order Checkout",
                            "priority": "P0",
                        },
                    )
                ).json()
                sc_id = sc_l0["id"]
                assert sc_l0["effectiveness_level"] == "L0"

                # L1: Add completed merge-only run (zero java_gen / mvn) -> L1
                await _seed_completed_merge_run(client, pid, order_srv.base_url, "01_order_checkout")
                sc_l1 = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert sc_l1["endpoint_count"] == 3
                assert sc_l1["effectiveness_level"] == "L1"

                # L2: Mark scenario verified -> L2 without java_gen
                sc_l2 = (
                    await client.patch(
                        f"/api/v1/projects/{pid}/scenarios/{sc_id}",
                        json={"status": "verified"},
                    )
                ).json()
                assert sc_l2["effectiveness_level"] == "L2"

                # L3 (pending): Attach 2 happy-path assertions + 1 pending negative case
                sc_l3 = (
                    await client.patch(
                        f"/api/v1/projects/{pid}/scenarios/{sc_id}",
                        json={
                            "assertions": [
                                {
                                    "id": "z-create",
                                    "title": "Order created PENDING",
                                    "target_endpoint": "POST /orders",
                                    "jsonpath": "$.data.status",
                                    "operator": "equals",
                                    "expected": "PENDING",
                                },
                                {
                                    "id": "z-get",
                                    "title": "Order retrieved PAID",
                                    "target_endpoint": "GET /orders/{id}",
                                    "jsonpath": "$.data.status",
                                    "operator": "equals",
                                    "expected": "PAID",
                                },
                            ],
                            "negative_cases": [
                                {
                                    "id": "z-neg-invalid-pay",
                                    "title": "Reject invalid pay_code with 400",
                                    "category": "validation",
                                    "target_endpoint": "POST /orders/{id}/pay",
                                    "expected_behavior": "Returns HTTP 400 when pay_code is invalid",
                                    "status": "pending",
                                }
                            ],
                        },
                    )
                ).json()
                assert sc_l3["effectiveness_level"] == "L3"
                assert sc_l3["assertions_passed_count"] == 0

                cap_pending = (await client.get(f"/api/v1/projects/{pid}/capabilities")).json()
                assert _slot_map(cap_pending["capabilities"][0])["happy_path"] == "pending"
                assert cap_pending["metrics"]["invariant_pass_rate_pct"] == 0.0

                # Online chain execution syncs both assertions to "passed" -> L3 verified online
                chain_out = (
                    await client.post(
                        f"/api/v1/projects/{pid}/chains/execute",
                        json={
                            "chain_name": "Zero-JDK Happy Path",
                            "steps": _build_three_step_chain(order_srv.base_url, pay_code="VALID-PAY"),
                            "base_url": order_srv.base_url,
                            "credential_id": cred_id,
                        },
                    )
                ).json()
                assert chain_out["status"] == "passed"
                assert chain_out["synced_scenarios"] == 1
                assert chain_out["synced_assertions"] == 2

                sc_l3_passed = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert sc_l3_passed["assertions_passed_count"] == 2
                assert sc_l3_passed["effectiveness_level"] == "L3"

                cap_happy = (await client.get(f"/api/v1/projects/{pid}/capabilities")).json()
                assert _slot_map(cap_happy["capabilities"][0])["happy_path"] == "covered"
                assert cap_happy["metrics"]["invariant_pass_rate_pct"] == 100.0

                # Negative Replay verifies 400 rejection on invalid pay_code, then mark negative_case covered -> L4
                order_id = chain_out["context_vars"]["order_id"]
                neg_replay = (
                    await client.post(
                        f"/api/v1/projects/{pid}/replay",
                        json={
                            "method": "POST",
                            "url": f"{order_srv.base_url}/orders/{order_id}/pay",
                            "body": {"pay_code": "INVALID-CODE"},
                            "credential_id": cred_id,
                            "assertions": [
                                {
                                    "title": "Negative invalid_params 400",
                                    "operator": "status_code",
                                    "expected": "400",
                                }
                            ],
                        },
                    )
                ).json()
                assert neg_replay["status_code"] == 400
                assert neg_replay["assertion_results"][0]["passed"] is True

                neg_case_payload = sc_l3_passed["negative_cases"][0]
                neg_case_payload["status"] = "covered"
                sc_l4 = (
                    await client.patch(
                        f"/api/v1/projects/{pid}/scenarios/{sc_id}",
                        json={"negative_cases": [neg_case_payload]},
                    )
                ).json()
                assert sc_l4["effectiveness_level"] == "L4"
                assert sc_l4["assertions_passed_count"] == 2
                assert sc_l4["negative_cases_covered_count"] == 1

                list_res = (await client.get(f"/api/v1/projects/{pid}/scenarios")).json()
                assert list_res["p0_p1_total"] == 1
                assert list_res["p0_p1_effective_count"] == 1
                assert list_res["p0_p1_effective_pct"] == 100.0

                cap_l4 = (await client.get(f"/api/v1/projects/{pid}/capabilities")).json()
                cap_item = cap_l4["capabilities"][0]
                slots_l4 = _slot_map(cap_item)
                assert slots_l4["happy_path"] == "covered"
                assert slots_l4["invalid_params"] == "covered"
                assert cap_item["case_coverage_pct"] > 20.0
                assert cap_l4["metrics"]["invariant_pass_rate_pct"] == 100.0


# ---------------------------------------------------------------------------
# Case 7: Regression Suite — Replay Transport Error Guard, Non-ID Path Params,
#         Static Collision Guard, Fragment/Query Normalization, Mixed Batch,
#         Correlation (:param / #opName) & ENV:RECONQA_* SQL Writeback
# ---------------------------------------------------------------------------


class _UserAndGraphQLTestServer:
    """Lightweight local HTTP server for non-ID path param & GraphQL regression checks."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> _UserAndGraphQLTestServer:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/api/users/alice":
                    payload = {"code": 0, "data": {"username": "alice", "role": "member"}}
                    raw = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                raw = json.dumps({"error": "not found"}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body_raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                path = self.path.split("?", 1)[0]
                if path == "/graphql":
                    try:
                        parsed = json.loads(body_raw)
                    except Exception:
                        parsed = {}
                    op = parsed.get("operationName") or "GetOrderList"
                    payload = {"data": {"op": op, "orders": [{"id": "ORD-GQL-1"}]}}
                    raw = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if path == "/api/orders/custom-op":
                    raw = json.dumps({"ok": True, "op": "custom-op"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                raw = json.dumps({"error": "bad request"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


@pytest.mark.asyncio
async def test_replay_unreachable_with_explicit_assertions_preserves_db_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_replay_transport_guard.db")
    db_path = tmp_path / "orders_guard.db"
    required_token = "Bearer guard-token-1"

    from reconqa.server.app import create_app

    with StatefulOrderServer(db_path=db_path, required_token=required_token) as order_srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, cred_id = await _seed_project_and_cred(client, order_srv.base_url, required_token)

                sc = (
                    await client.post(
                        f"/api/v1/projects/{pid}/scenarios",
                        json={
                            "slug": "transport_guard",
                            "name": "Transport Guard",
                            "priority": "P0",
                            "assertions": [
                                {
                                    "id": "exp-err-1",
                                    "title": "Expect 201",
                                    "target_endpoint": "POST /orders",
                                    "operator": "status_code",
                                    "expected": "201",
                                    "status": "passed",
                                }
                            ],
                        },
                    )
                ).json()
                sc_id = sc["id"]

                # Unreachable URL WITH explicit body.assertions must NOT evaluate or overwrite DB to "failed"
                rp_err = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "POST",
                        "url": "http://127.0.0.1:1/orders",
                        "credential_id": cred_id,
                        "assertions": [
                            {
                                "id": "exp-err-1",
                                "title": "Expect 201",
                                "operator": "status_code",
                                "expected": "201",
                            }
                        ],
                    },
                )
                assert rp_err.status_code == 200
                err_data = rp_err.json()
                assert err_data["error"] is not None
                assert err_data["status_code"] == 0
                assert err_data["assertion_results"] == []
                assert err_data["synced_scenarios"] == 0
                assert err_data["synced_assertions"] == 0

                sc_unchanged = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert sc_unchanged["assertions"][0]["status"] == "passed"

                # Control check: reachable HTTP 401 on the SAME endpoint POST /orders (resp.error is None)
                # DOES evaluate and flip DB to "failed"
                rp_401 = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "POST",
                        "url": f"{order_srv.base_url}/orders",
                        "body": {"amount": 10},
                        "assertions": [
                            {
                                "id": "exp-err-1",
                                "title": "Expect 201",
                                "operator": "status_code",
                                "expected": "201",
                            }
                        ],
                    },
                )
                assert rp_401.status_code == 200
                fail_data = rp_401.json()
                assert fail_data["error"] is None
                assert fail_data["status_code"] == 401
                assert fail_data["assertion_results"][0]["passed"] is False
                assert fail_data["synced_scenarios"] == 1
                assert fail_data["synced_assertions"] == 1

                sc_failed = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert sc_failed["assertions"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_non_id_concrete_path_params_and_static_collision_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_non_id_params.db")

    # Unit helper checks for non-ID concrete path params ({username}, :username, {{username}})
    assert endpoint_matches_target("GET", "http://127.0.0.1:9999/api/users/alice", "GET /api/users/{username}")
    assert endpoint_matches_target("GET", "/api/users/alice", "GET /api/users/:username")
    assert endpoint_matches_target("GET", "/api/users/alice", "GET /api/users/{{username}}")

    # Static REST collision guards & directionality
    assert not endpoint_matches_target("POST", "http://127.0.0.1/api/orders/submit", "POST /api/orders/{id}")
    assert not endpoint_matches_target("POST", "/api/orders/{id}", "POST /api/orders/submit")
    assert not endpoint_matches_target("GET", "/api/v1/items/search", "GET /api/v1/items/{id}")
    assert not endpoint_matches_target("GET", "/api/users/alice", "GET /api/orders/{id}")
    assert not endpoint_matches_target("GET", "/api/users/alice", "GET /api/users/{username}/profile")
    assert not endpoint_matches_target("GET", "/api/admins/alice", "GET /api/users/{username}")

    # Specificity >= 2 precedence: custom static sub-path prefers exact assertion over /{id}
    raw_assertions = [
        {
            "id": "a-custom",
            "title": "Custom op ok",
            "target_endpoint": "POST /api/orders/custom-op",
            "operator": "status_code",
            "expected": "200",
        },
        {
            "id": "a-param",
            "title": "Order by id ok",
            "target_endpoint": "POST /api/orders/{id}",
            "operator": "status_code",
            "expected": "200",
        },
    ]
    matched_custom = match_endpoint_scenario_assertions(
        "POST", "http://127.0.0.1/api/orders/custom-op", raw_assertions
    )
    assert [m.id for m in matched_custom] == ["a-custom"]

    from reconqa.server.app import create_app

    with _UserAndGraphQLTestServer() as srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                proj = (await client.post("/api/v1/projects", json={"name": "Non-ID Params"})).json()
                pid = proj["id"]
                await client.post(
                    f"/api/v1/projects/{pid}/targets",
                    json={"root_domain": "127.0.0.1", "base_url": srv.base_url},
                )

                sc = (
                    await client.post(
                        f"/api/v1/projects/{pid}/scenarios",
                        json={
                            "slug": "user_non_id",
                            "name": "User Non-ID",
                            "priority": "P1",
                            "assertions": [
                                {
                                    "id": "u-brace",
                                    "title": "Username matches alice",
                                    "target_endpoint": "GET /api/users/{username}",
                                    "jsonpath": "$.data.username",
                                    "operator": "equals",
                                    "expected": "alice",
                                },
                                {
                                    "id": "u-colon",
                                    "title": "Role matches member",
                                    "target_endpoint": "GET /api/users/:username",
                                    "jsonpath": "$.data.role",
                                    "operator": "equals",
                                    "expected": "member",
                                },
                                {
                                    "id": "u-mustache",
                                    "title": "Code equals 0",
                                    "target_endpoint": "GET /api/users/{{username}}",
                                    "jsonpath": "$.code",
                                    "operator": "equals",
                                    "expected": "0",
                                },
                            ],
                        },
                    )
                ).json()
                sc_id = sc["id"]

                # Replay auto-bind + sync against /api/users/alice
                rp = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={"method": "GET", "url": f"{srv.base_url}/api/users/alice"},
                )
                assert rp.status_code == 200, rp.text
                rp_data = rp.json()
                assert len(rp_data["assertion_results"]) == 3
                assert all(r["passed"] is True for r in rp_data["assertion_results"])
                assert rp_data["synced_scenarios"] == 1
                assert rp_data["synced_assertions"] == 3

                sc_after_rp = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert all(a["status"] == "passed" for a in sc_after_rp["assertions"])

                # Reset to pending and verify Chain Execute auto-bind + sync
                reset_payload = [{**a, "status": "pending"} for a in sc_after_rp["assertions"]]
                await client.patch(
                    f"/api/v1/projects/{pid}/scenarios/{sc_id}",
                    json={"assertions": reset_payload},
                )

                ch = await client.post(
                    f"/api/v1/projects/{pid}/chains/execute",
                    json={
                        "chain_name": "Non-ID User Chain",
                        "steps": [
                            {
                                "step": 1,
                                "method": "GET",
                                "path": "/api/users/alice",
                                "raw_url": f"{srv.base_url}/api/users/alice",
                                "expected_status": 200,
                            }
                        ],
                        "base_url": srv.base_url,
                    },
                )
                assert ch.status_code == 200, ch.text
                ch_data = ch.json()
                assert ch_data["status"] == "passed"
                assert ch_data["synced_scenarios"] == 1
                assert ch_data["synced_assertions"] == 3

                sc_after_ch = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert all(a["status"] == "passed" for a in sc_after_ch["assertions"])


@pytest.mark.asyncio
async def test_mixed_id_and_fallback_same_batch_failed_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_mixed_id_fallback.db")

    from reconqa.server.app import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            proj = (await client.post("/api/v1/projects", json={"name": "Mixed Batch"})).json()
            pid = proj["id"]
            sc = (
                await client.post(
                    f"/api/v1/projects/{pid}/scenarios",
                    json={
                        "slug": "mixed_batch",
                        "name": "Mixed Batch",
                        "priority": "P0",
                        "assertions": [
                            {
                                "id": "mix-id-1",
                                "title": "Mixed Batch Rule",
                                "target_endpoint": "GET /api/flaky",
                                "operator": "status_code",
                                "expected": "200",
                            },
                            {
                                "id": "custom-op-rule",
                                "title": "Shared Rule",
                                "target_endpoint": "POST /api/orders/custom-op",
                                "operator": "status_code",
                                "expected": "200",
                            },
                            {
                                "id": "order-id-rule",
                                "title": "Shared Rule",
                                "target_endpoint": "POST /api/orders/{id}",
                                "operator": "status_code",
                                "expected": "200",
                            },
                        ],
                    },
                )
            ).json()
            sc_id = sc["id"]

            # Subcase A: ID PASS first, fallback FAIL second
            sf = db_mod.get_session_factory()
            async with sf() as session:
                synced_sc, synced_as = await sync_assertion_evals_to_scenarios(
                    session,
                    pid,
                    [
                        (
                            "GET",
                            "/api/flaky",
                            [
                                AssertionEvalResult(
                                    id="mix-id-1",
                                    title="Mixed Batch Rule",
                                    operator="status_code",
                                    passed=True,
                                ),
                                AssertionEvalResult(
                                    id="ephemeral-inline-999",
                                    title="Mixed Batch Rule",
                                    operator="status_code",
                                    passed=False,
                                ),
                            ],
                        )
                    ],
                )
                assert (synced_sc, synced_as) == (1, 1)

            sc_a = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
            assert {a["id"]: a["status"] for a in sc_a["assertions"]}["mix-id-1"] == "failed"

            # Reset to pending for Subcase B
            await client.patch(
                f"/api/v1/projects/{pid}/scenarios/{sc_id}",
                json={"assertions": [{**a, "status": "pending"} for a in sc_a["assertions"]]},
            )

            # Subcase B: Reverse order (fallback FAIL first, ID PASS second) + fallback specificity precedence
            async with sf() as session:
                synced_sc, synced_as = await sync_assertion_evals_to_scenarios(
                    session,
                    pid,
                    [
                        (
                            "GET",
                            "/api/flaky",
                            [
                                AssertionEvalResult(
                                    id="",
                                    title="Mixed Batch Rule",
                                    operator="status_code",
                                    passed=False,
                                )
                            ],
                        ),
                        (
                            "GET",
                            "https://api.example.test/api/flaky?attempt=2",
                            [
                                AssertionEvalResult(
                                    id="mix-id-1",
                                    title="Mixed Batch Rule",
                                    operator="status_code",
                                    passed=True,
                                )
                            ],
                        ),
                        (
                            "POST",
                            "http://127.0.0.1/api/orders/custom-op",
                            [
                                AssertionEvalResult(
                                    id="ephemeral-custom",
                                    title="Shared Rule",
                                    operator="status_code",
                                    passed=True,
                                )
                            ],
                        ),
                    ],
                )
                assert (synced_sc, synced_as) == (1, 2)

            sc_b = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
            statuses_b = {a["id"]: a["status"] for a in sc_b["assertions"]}
            assert statuses_b["mix-id-1"] == "failed"
            assert statuses_b["custom-op-rule"] == "passed"
            assert statuses_b["order-id-rule"] == "pending"


def test_fragment_query_and_duplicate_slash_normalization_and_step_op_preservation() -> None:
    assert clean_endpoint_path("http://127.0.0.1/graphql#GetOrderList?debug=1") == "/graphql#GetOrderList"
    assert clean_endpoint_path("https://api.example.com/graphql?v=2#GetOrderList?debug=1") == "/graphql#GetOrderList"
    assert clean_endpoint_path("/graphql#GetOrderList?debug=1") == "/graphql#GetOrderList"
    assert clean_endpoint_path("http://127.0.0.1//api///users//:id//") == "/api/users/{id}"
    assert canonical_path_shape("http://127.0.0.1/graphql#GetOrderList?debug=1") == "/graphql#GetOrderList"

    assert endpoint_matches_target(
        "POST",
        "http://127.0.0.1/graphql#GetOrderList?debug=1",
        "POST /graphql#GetOrderList",
    )
    assert not endpoint_matches_target(
        "POST",
        "https://api.example.com/graphql#CreateOrder?debug=1",
        "POST /graphql#GetOrderList",
    )
    assert not endpoint_matches_target(
        "POST",
        "https://api.example.com/graphql#GetOrderList?debug=1",
        "POST /graphql",
    )
    assert not endpoint_matches_target(
        "POST",
        "https://api.example.com/graphql",
        "POST /graphql#GetOrderList",
    )

    # _apply_uses_to_step preserves #opName and binds :param / {{param}} uses
    st_gql = _apply_uses_to_step(
        {
            "step": 1,
            "method": "POST",
            "path": "/graphql#GetOrderList",
            "raw_url": "http://127.0.0.1:8000/graphql#GetOrderList?debug=1",
            "query_params": {"debug": "1"},
        }
    )
    assert st_gql.path == "/graphql#GetOrderList"
    assert endpoint_matches_target("POST", st_gql.path, "POST /graphql#GetOrderList")

    st_gql_wire_raw = _apply_uses_to_step(
        {
            "step": 1,
            "method": "POST",
            "path": "/graphql#GetOrderList",
            "raw_url": "http://127.0.0.1:8000/graphql",
        }
    )
    assert st_gql_wire_raw.path == "/graphql#GetOrderList"

    st_colon = _apply_uses_to_step(
        {
            "step": 2,
            "method": "POST",
            "path": "/orders/:id/pay",
            "raw_url": "http://127.0.0.1:8000/orders/STALE-99/pay",
            "uses": {"path:id": "{{order_id}}"},
        }
    )
    assert st_colon.path == "/orders/{{order_id}}/pay"

    st_mustache = _apply_uses_to_step(
        {
            "step": 3,
            "method": "GET",
            "path": "/orders/{{id}}",
            "raw_url": "http://127.0.0.1:8000/orders/STALE-99",
            "uses": {"path:id": "{{order_id}}"},
        }
    )
    assert st_mustache.path == "/orders/{{order_id}}"


@pytest.mark.asyncio
async def test_correlation_driven_chain_with_sql_assertions_aiosqlite_and_replay_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_corr_sql_writeback.db")
    db_path = tmp_path / "orders_corr_sql.db"
    required_token = "Bearer corr-sql-token-1"
    monkeypatch.setenv("RECONQA_TEST_CORR_SQL_DB", f"sqlite+aiosqlite:///{db_path}")

    from reconqa.server.app import create_app

    with StatefulOrderServer(db_path=db_path, required_token=required_token) as order_srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, cred_id = await _seed_project_and_cred(client, order_srv.base_url, required_token)

                run_resp = await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Corr Run", "target_url": order_srv.base_url, "modules": ["merge"]},
                )
                assert run_resp.status_code == 201
                run_data = run_resp.json()
                run_id = run_data["id"]
                hub_dir = Path(run_data["workspace_dir"]) / "hub"
                hub_dir.mkdir(parents=True, exist_ok=True)
                (hub_dir / "correlation.json").write_text(
                    json.dumps(
                        {
                            "variables": [],
                            "chains": [
                                {
                                    "name": "Order Pay SQL Correlation Chain",
                                    "scenario": "checkout",
                                    "steps": [
                                        {
                                            "step": 1,
                                            "method": "POST",
                                            "path": "/orders",
                                            "raw_url": f"{order_srv.base_url}/orders",
                                            "request_body": {"amount": 50},
                                            "expected_status": 201,
                                            "extracts": {"order_id": "$.data.order_id"},
                                            "uses": {},
                                        },
                                        {
                                            "step": 2,
                                            "method": "POST",
                                            "path": "/orders/:id/pay",
                                            "raw_url": f"{order_srv.base_url}/orders/STALE-ORD-0/pay",
                                            "request_body": {"pay_code": "VALID-PAY"},
                                            "expected_status": 200,
                                            "uses": {"path:id": "{{order_id}}"},
                                        },
                                        {
                                            "step": 3,
                                            "method": "GET",
                                            "path": "/orders/{id}",
                                            "raw_url": f"{order_srv.base_url}/orders/STALE-ORD-0?expand=1",
                                            "query_params": {"expand": "1"},
                                            "expected_status": 200,
                                            "uses": {"path:id": "{{order_id}}"},
                                        },
                                    ],
                                }
                            ],
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                sc = (
                    await client.post(
                        f"/api/v1/projects/{pid}/scenarios",
                        json={
                            "slug": "corr_sql_scenario",
                            "name": "Correlation SQL Scenario",
                            "priority": "P0",
                            "assertions": [
                                {
                                    "id": "corr-create",
                                    "title": "Order created PENDING",
                                    "target_endpoint": "POST /orders",
                                    "jsonpath": "$.data.status",
                                    "operator": "equals",
                                    "expected": "PENDING",
                                },
                                {
                                    "id": "corr-sql-eq",
                                    "title": "SQL DB status PAID",
                                    "target_endpoint": "GET /orders/{id}",
                                    "jsonpath": "SELECT status FROM orders WHERE id = {{order_id}}",
                                    "operator": "sql_equals",
                                    "expected": "PAID",
                                    "db_dsn_ref": "ENV:RECONQA_TEST_CORR_SQL_DB",
                                },
                                {
                                    "id": "corr-sql-exists",
                                    "title": "SQL DB balance deducted row exists",
                                    "target_endpoint": "GET /orders/{id}",
                                    "jsonpath": "SELECT 1 FROM orders WHERE id = {{order_id}} AND balance_after = 150",
                                    "operator": "sql_exists",
                                    "expected": "",
                                },
                            ],
                        },
                    )
                ).json()
                sc_id = sc["id"]

                # Execute via run_id (correlation.json) with db_dsn_ref fallback
                exec_resp = await client.post(
                    f"/api/v1/projects/{pid}/chains/execute",
                    json={
                        "run_id": run_id,
                        "credential_id": cred_id,
                        "db_dsn_ref": "ENV:RECONQA_TEST_CORR_SQL_DB",
                    },
                )
                assert exec_resp.status_code == 200, exec_resp.text
                out = exec_resp.json()
                assert out["status"] == "passed"
                assert out["run_id"] == run_id
                assert out["db_dsn_ref"] == "ENV:RECONQA_TEST_CORR_SQL_DB"
                assert out["synced_scenarios"] == 1
                assert out["synced_assertions"] == 3

                step3_evals = {a["id"]: a for a in out["step_results"][2]["assertions"]}
                assert step3_evals["corr-sql-eq"]["passed"] is True
                assert step3_evals["corr-sql-eq"]["actual"] == "PAID"
                assert step3_evals["corr-sql-exists"]["passed"] is True
                assert step3_evals["corr-sql-exists"]["actual"] == 1

                sc_passed = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert all(a["status"] == "passed" for a in sc_passed["assertions"])
                sql_eq_saved = next(a for a in sc_passed["assertions"] if a["id"] == "corr-sql-eq")
                assert sql_eq_saved["db_dsn_ref"] == "ENV:RECONQA_TEST_CORR_SQL_DB"

                # Negative SQL writeback via TryIt Replay (same assertion definition, missing order -> 0 rows)
                order_id = out["context_vars"]["order_id"]
                rp_fail = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "GET",
                        "url": f"{order_srv.base_url}/orders/{order_id}",
                        "credential_id": cred_id,
                        "context_vars": {"order_id": "ORD-MISSING-999"},
                        "assertions": [
                            {
                                "id": "corr-sql-eq",
                                "title": "SQL DB status PAID",
                                "jsonpath": "SELECT status FROM orders WHERE id = {{order_id}}",
                                "operator": "sql_equals",
                                "expected": "PAID",
                                "db_dsn_ref": "ENV:RECONQA_TEST_CORR_SQL_DB",
                            }
                        ],
                    },
                )
                assert rp_fail.status_code == 200, rp_fail.text
                rp_data = rp_fail.json()
                assert rp_data["assertion_results"][0]["passed"] is False
                assert rp_data["synced_scenarios"] == 1
                assert rp_data["synced_assertions"] == 1

                sc_flipped = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert {a["id"]: a["status"] for a in sc_flipped["assertions"]}["corr-sql-eq"] == "failed"


def test_merge_recorded_and_bound_path_and_apply_uses_multi_param_and_trailing_slash() -> None:
    # Multi-parameter partial bind must NOT let the earlier unbound param steal the later binding
    assert (
        _merge_recorded_and_bound_path(
            "/api/users/{user_id}/orders/{order_id}",
            "/api/users/42/orders/100",
            {"order_id": "{{oid}}"},
        )
        == "/api/users/42/orders/{{oid}}"
    )
    assert (
        _merge_recorded_and_bound_path(
            "/api/users/:user_id/orders/{{order_id}}",
            "/api/users/42/orders/999",
            {"order_id": "{{created_oid}}"},
        )
        == "/api/users/42/orders/{{created_oid}}"
    )
    assert (
        _merge_recorded_and_bound_path(
            "/api/users/{{uid}}/orders/{{order_id}}",
            "/api/users/42/orders/999",
            {"order_id": "{{created_oid}}"},
        )
        == "/api/users/{{uid}}/orders/{{created_oid}}"
    )

    # Trailing-slash mismatches preserve pre-bound {{var}} and concrete unbound segments
    assert (
        _merge_recorded_and_bound_path("/api/orders/{{order_id}}", "/api/orders/999/", {})
        == "/api/orders/{{order_id}}"
    )
    assert (
        _merge_recorded_and_bound_path(
            "/api/users/{user_id}/orders/{order_id}",
            "/api/users/42/orders/999/",
            {"order_id": "{{oid}}"},
        )
        == "/api/users/42/orders/{{oid}}"
    )
    assert (
        _merge_recorded_and_bound_path(
            "/api/users/{user_id}/orders/{order_id}/",
            "/api/users/42/orders/999",
            {"order_id": "{{oid}}"},
        )
        == "/api/users/42/orders/{{oid}}/"
    )

    # Single-token positional fallback still works when key does not match tpl_param_names
    assert (
        _merge_recorded_and_bound_path(
            "/api/orders/{order_id}",
            "/api/orders/999",
            {"id": "{{oid}}"},
        )
        == "/api/orders/{{oid}}"
    )

    # _replace_bound_tokens_in_path does not corrupt multi-param templates or double-replace
    assert (
        _replace_bound_tokens_in_path(
            "/api/users/{user_id}/orders/{order_id}",
            {"order_id": "{{oid}}"},
        )
        == "/api/users/{user_id}/orders/{{oid}}"
    )
    assert (
        _replace_bound_tokens_in_path(
            "/api/users/{user_id}/orders/{order_id}",
            {"order_id": "{{oid}}", "unrelated": "{{other}}"},
        )
        == "/api/users/{user_id}/orders/{{oid}}"
    )

    # _apply_uses_to_step with 2-param :param/{{param}}, trailing slash on raw_url, and query string
    st_two = _apply_uses_to_step(
        {
            "step": 2,
            "method": "GET",
            "path": "/api/users/:user_id/orders/{{order_id}}",
            "raw_url": "http://old.example:8080/api/users/42/orders/999/?old=1",
            "uses": {"path:order_id": "{{created_oid}}"},
        }
    )
    assert st_two.path == "/api/users/42/orders/{{created_oid}}"
    assert st_two.raw_url == "http://old.example:8080"


@pytest.mark.asyncio
async def test_correlation_two_param_partial_bind_e2e_and_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "r1_two_param_partial.db")
    recorded_wire_requests: list[tuple[str, str]] = []

    class TwoParamHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            recorded_wire_requests.append(("POST", self.path))
            if self.path == "/api/users/42/orders":
                self._send(201, {"data": {"order_id": "888"}})
                return
            self._send(404, {"error": "not found"})

        def do_GET(self) -> None:  # noqa: N802
            recorded_wire_requests.append(("GET", self.path))
            if self.path == "/api/users/42/orders/888":
                self._send(200, {"data": {"user_id": "42", "order_id": "888", "status": "PAID"}})
                return
            self._send(404, {"error": f"wrong path {self.path}"})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), TwoParamHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    srv_url = f"http://{host}:{port}"

    from reconqa.server.app import create_app

    try:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, _ = await _seed_project_and_cred(
                    client, srv_url, "Bearer unused", name="TwoParam Partial Bind"
                )
                run_resp = await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "TwoParam Run", "target_url": srv_url, "modules": ["merge"]},
                )
                assert run_resp.status_code == 201
                run_data = run_resp.json()
                run_id = run_data["id"]

                hub_dir = Path(run_data["workspace_dir"]) / "hub"
                hub_dir.mkdir(parents=True, exist_ok=True)
                (hub_dir / "correlation.json").write_text(
                    json.dumps(
                        {
                            "variables": [],
                            "chains": [
                                {
                                    "name": "TwoParamPartialBind",
                                    "scenario": "two_param",
                                    "steps": [
                                        {
                                            "step": 1,
                                            "method": "POST",
                                            "path": "/api/users/42/orders",
                                            "raw_url": f"{srv_url}/api/users/42/orders",
                                            "request_body": {"sku": "book"},
                                            "expected_status": 201,
                                            "extracts": {"oid": "$.data.order_id"},
                                            "uses": {},
                                        },
                                        {
                                            "step": 2,
                                            "method": "GET",
                                            "path": "/api/users/{user_id}/orders/{order_id}",
                                            "raw_url": f"{srv_url}/api/users/42/orders/100/",
                                            "expected_status": 200,
                                            "uses": {"path:order_id": "{{oid}}"},
                                        },
                                    ],
                                }
                            ],
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                sc = (
                    await client.post(
                        f"/api/v1/projects/{pid}/scenarios",
                        json={
                            "slug": "two_param_sc",
                            "name": "Two Param Scenario",
                            "priority": "P0",
                            "assertions": [
                                {
                                    "id": "two-param-paid",
                                    "title": "User order status PAID",
                                    "target_endpoint": "GET /api/users/{user_id}/orders/{order_id}",
                                    "jsonpath": "$.data.status",
                                    "operator": "equals",
                                    "expected": "PAID",
                                }
                            ],
                        },
                    )
                ).json()
                sc_id = sc["id"]

                exec_resp = await client.post(
                    f"/api/v1/projects/{pid}/chains/execute",
                    json={"run_id": run_id, "chain_name": "TwoParamPartialBind"},
                )
                assert exec_resp.status_code == 200, exec_resp.text
                out = exec_resp.json()
                assert out["status"] == "passed"
                assert out["step_results"][1]["status_code"] == 200
                assert out["synced_scenarios"] == 1
                assert out["synced_assertions"] == 1
                assert ("GET", "/api/users/42/orders/888") in recorded_wire_requests

                sc_after = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert {a["id"]: a["status"] for a in sc_after["assertions"]} == {
                    "two-param-paid": "passed"
                }
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.asyncio
async def test_custom_assertions_stay_pending_and_tryit_endpoint_mismatch_does_not_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 #6 & #7: custom rules never auto-pass, and TryIt edits to URL/method/definition never corrupt other assertions."""
    from reconqa.models.schemas import ScenarioAssertion
    from reconqa.modules.hub.assertion_engine import evaluate_assertion

    _setup_isolated_env(tmp_path, monkeypatch, "r1_p1_6_7_safety.db")
    db_path = tmp_path / "orders_p1_6_7.db"
    required_token = "Bearer p1-6-7-token"

    # Unit check: evaluate_assertion never auto-passes custom rules
    custom_ev = evaluate_assertion(
        ScenarioAssertion(
            id="cust-unit",
            title="Manual audit check",
            operator="custom",
            note="Verify audit table manually",
        ),
        status_code=200,
        duration_ms=10.0,
        response_headers={},
        body_json={"ok": True},
        body_text='{"ok": true}',
    )
    assert custom_ev.passed is False
    assert "Manual verification required" in custom_ev.message

    from reconqa.server.app import create_app

    with StatefulOrderServer(db_path=db_path, required_token=required_token) as order_srv:
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid, cred_id = await _seed_project_and_cred(
                    client, order_srv.base_url, required_token, name="Writeback Safety"
                )

                sc = (
                    await client.post(
                        f"/api/v1/projects/{pid}/scenarios",
                        json={
                            "slug": "writeback_safety",
                            "name": "Writeback Safety",
                            "priority": "P0",
                            "assertions": [
                                {
                                    "id": "safe-create",
                                    "title": "Order created PENDING",
                                    "target_endpoint": "POST /orders",
                                    "jsonpath": "$.data.status",
                                    "operator": "equals",
                                    "expected": "PENDING",
                                    "status": "pending",
                                },
                                {
                                    "id": "safe-custom",
                                    "title": "Manual cross-resource invariant",
                                    "target_endpoint": "POST /orders",
                                    "jsonpath": "",
                                    "operator": "custom",
                                    "note": "Check accounting ledger manually",
                                    "status": "pending",
                                },
                            ],
                        },
                    )
                ).json()
                sc_id = sc["id"]

                # 1. TryIt Replay on POST /orders -> safe-create passes, safe-custom MUST stay pending!
                rp_ok = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "POST",
                        "url": f"{order_srv.base_url}/orders",
                        "body": {"amount": 25},
                        "credential_id": cred_id,
                    },
                )
                assert rp_ok.status_code == 200
                rp_ok_data = rp_ok.json()
                assert rp_ok_data["synced_scenarios"] == 1
                assert rp_ok_data["synced_assertions"] == 1

                sc1 = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert {a["id"]: a["status"] for a in sc1["assertions"]} == {
                    "safe-create": "passed",
                    "safe-custom": "pending",
                }

                # Even direct sync_assertion_evals_to_scenarios with passed=True on custom MUST NOT mark it passed!
                sf = db_mod.get_session_factory()
                async with sf() as session:
                    sc_n, as_n = await sync_assertion_evals_to_scenarios(
                        session,
                        pid,
                        [
                            (
                                "POST",
                                "/orders",
                                [
                                    AssertionEvalResult(
                                        id="safe-custom",
                                        title="Manual cross-resource invariant",
                                        operator="custom",
                                        passed=True,
                                    )
                                ],
                            )
                        ],
                    )
                    assert (sc_n, as_n) == (0, 0)

                # 2. TryIt Replay with EDITED URL (different endpoint POST /orders/ORD-MISSING/pay)
                #    passing safe-create's id -> MUST NOT overwrite safe-create ("passed" -> "failed")!
                rp_wrong_url = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "POST",
                        "url": f"{order_srv.base_url}/orders/ORD-MISSING/pay",
                        "body": {"pay_code": "BAD"},
                        "credential_id": cred_id,
                        "assertions": [
                            {
                                "id": "safe-create",
                                "title": "Order created PENDING",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PENDING",
                            }
                        ],
                    },
                )
                assert rp_wrong_url.status_code == 200
                assert rp_wrong_url.json()["assertion_results"][0]["passed"] is False
                assert rp_wrong_url.json()["synced_assertions"] == 0

                # 3. TryIt Replay with EDITED METHOD (GET /orders instead of POST /orders)
                #    passing safe-create's id -> MUST NOT overwrite safe-create!
                rp_wrong_method = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "GET",
                        "url": f"{order_srv.base_url}/orders",
                        "credential_id": cred_id,
                        "assertions": [
                            {
                                "id": "safe-create",
                                "title": "Order created PENDING",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "PENDING",
                            }
                        ],
                    },
                )
                assert rp_wrong_method.status_code == 200
                assert rp_wrong_method.json()["assertion_results"][0]["passed"] is False
                assert rp_wrong_method.json()["synced_assertions"] == 0

                # 4. TryIt Replay on SAME endpoint POST /orders but EDITED expected value ("CANCELLED")
                #    passing safe-create's id -> MUST NOT overwrite safe-create!
                rp_wrong_def = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "POST",
                        "url": f"{order_srv.base_url}/orders",
                        "body": {"amount": 25},
                        "credential_id": cred_id,
                        "assertions": [
                            {
                                "id": "safe-create",
                                "title": "Order created PENDING",
                                "jsonpath": "$.data.status",
                                "operator": "equals",
                                "expected": "CANCELLED",
                            }
                        ],
                    },
                )
                assert rp_wrong_def.status_code == 200
                assert rp_wrong_def.json()["assertion_results"][0]["passed"] is False
                assert rp_wrong_def.json()["synced_assertions"] == 0

                sc_final = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_id}")).json()
                assert {a["id"]: a["status"] for a in sc_final["assertions"]} == {
                    "safe-create": "passed",
                    "safe-custom": "pending",
                }
