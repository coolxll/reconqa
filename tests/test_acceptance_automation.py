"""Tests for Autonomous External-System Acceptance Glue APIs (#154)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.models.tables import Run
from reconqa.server.routers import acceptance as acceptance_mod


def _make_har_doc(entries: list[dict]) -> dict:
    return {"log": {"version": "1.2", "creator": {"name": "test", "version": "1.0"}, "entries": entries}}


@pytest.mark.asyncio
async def test_extract_credential_from_har_and_400_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify POST /projects/{id}/auth-profiles/extract-from-har extracts auth headers and 400s when absent."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "accept_extract.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            proj = (await client.post("/api/v1/projects", json={"name": "Portal App"})).json()
            pid = proj["id"]

            await client.post(
                f"/api/v1/projects/{pid}/targets",
                json={"root_domain": "portal.example.test", "base_url": "https://api.portal.example.test"},
            )

            # Run 1: HAR with NO auth headers -> must return 400
            r_empty = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Empty Auth Run", "modules": ["merge"]},
                )
            ).json()
            har_dir_empty = Path(r_empty["workspace_dir"]) / "har"
            har_dir_empty.mkdir(parents=True, exist_ok=True)
            (har_dir_empty / "01_public.har").write_text(
                json.dumps(
                    _make_har_doc(
                        [
                            {
                                "request": {
                                    "method": "GET",
                                    "url": "https://api.portal.example.test/api/v1/public/ping",
                                    "headers": [{"name": "Accept", "value": "application/json"}],
                                },
                                "response": {
                                    "status": 200,
                                    "content": {"mimeType": "application/json", "text": '{"ok":true}'},
                                },
                            }
                        ]
                    )
                ),
                encoding="utf-8",
            )

            bad_res = await client.post(
                f"/api/v1/projects/{pid}/auth-profiles/extract-from-har",
                json={"run_id": r_empty["id"]},
            )
            assert bad_res.status_code == 400
            assert "No authentication headers found" in bad_res.text

            # Run 2: Completed HAR with live 200 API request carrying Authorization + accessToken
            r_auth = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Auth Captured Run", "modules": ["merge"]},
                )
            ).json()
            har_dir_auth = Path(r_auth["workspace_dir"]) / "har"
            har_dir_auth.mkdir(parents=True, exist_ok=True)
            (har_dir_auth / "01_orders.har").write_text(
                json.dumps(
                    _make_har_doc(
                        [
                            {
                                "request": {
                                    "method": "GET",
                                    "url": "https://cdn.example.test/app.js",
                                    "headers": [{"name": "Cookie", "value": "cdn=ignore"}],
                                },
                                "response": {
                                    "status": 200,
                                    "content": {"mimeType": "application/javascript", "text": ""},
                                },
                            },
                            {
                                "request": {
                                    "method": "GET",
                                    "url": "https://api.portal.example.test/api/v1/orders",
                                    "headers": [
                                        {"name": "Authorization", "value": "Bearer synth-jwt-token-999"},
                                        {"name": "accessToken", "value": "synth-access-xyz"},
                                        {"name": "Cookie", "value": "sid=synth-session-1"},
                                    ],
                                },
                                "response": {
                                    "status": 200,
                                    "content": {"mimeType": "application/json", "text": '{"code":0}'},
                                },
                            },
                        ]
                    )
                ),
                encoding="utf-8",
            )

            sf = db_mod.get_session_factory()
            async with sf() as session:
                run_obj = await session.get(Run, r_auth["id"])
                assert run_obj is not None
                run_obj.status = "completed"
                run_obj.finished_at = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
                await session.commit()

            # Omit run_id -> automatically picks latest completed run with HARs!
            ok_res = await client.post(
                f"/api/v1/projects/{pid}/auth-profiles/extract-from-har",
                json={"role_tag": "account_a"},
            )
            assert ok_res.status_code == 200, ok_res.text
            body = ok_res.json()
            assert body["source_run_id"] == r_auth["id"]
            assert body["source_host"] == "api.portal.example.test"
            assert set(body["extracted_header_names"]) == {"Authorization", "Cookie", "accessToken"}
            assert body["profile"]["role_tag"] == "account_a"
            assert "api.portal.example.test" in body["profile"]["name"]


@pytest.mark.asyncio
async def test_execute_external_tool_sync_back_and_tool_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify java_test + schemathesis execution, Scenario sync-back, and clean tool_missing response."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "accept_tools.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            proj = (await client.post("/api/v1/projects", json={"name": "Billing API"})).json()
            pid = proj["id"]

            run_data = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Billing Run", "target_url": "https://billing.example.test"},
                )
            ).json()
            rid = run_data["id"]
            run_dir = Path(run_data["workspace_dir"])
            hub_dir = run_dir / "hub"
            hub_dir.mkdir(parents=True, exist_ok=True)
            (hub_dir / "openapi.json").write_text(
                json.dumps(
                    {
                        "openapi": "3.0.3",
                        "servers": [{"url": "https://billing.example.test"}],
                        "paths": {
                            "/api/v1/invoices": {
                                "get": {
                                    "x-discovery-source": "har-live",
                                    "x-scenarios": ["01_invoices"],
                                    "responses": {"200": {"description": "OK"}},
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            # Mark run completed so scenario endpoints/effectiveness resolve
            sf = db_mod.get_session_factory()
            async with sf() as session:
                run_obj = await session.get(Run, rid)
                assert run_obj is not None
                run_obj.status = "completed"
                await session.commit()

            # Create a scenario with pending assertion + pending validation negative case
            sc = (
                await client.post(
                    f"/api/v1/projects/{pid}/scenarios",
                    json={
                        "slug": "01_invoices",
                        "name": "Invoice Listing",
                        "priority": "P0",
                        "assertions": [
                            {
                                "title": "Invoice total must be non-empty",
                                "target_endpoint": "GET /api/v1/invoices",
                                "jsonpath": "$.data.total",
                                "operator": "not_empty",
                                "status": "pending",
                            }
                        ],
                        "negative_cases": [
                            {
                                "title": "Invalid page parameter rejected cleanly",
                                "category": "validation",
                                "expected_behavior": "HTTP 400",
                                "status": "pending",
                            }
                        ],
                    },
                )
            ).json()
            assert sc["effectiveness_level"] == "L3"

            # 1. Missing binary -> 200 OK with status="tool_missing" (never 500)
            monkeypatch.setattr(acceptance_mod, "_resolve_tool_bin", lambda name: None)
            miss_res = await client.post(
                f"/api/v1/projects/{pid}/runs/{rid}/execute-tool",
                json={"tool": "k6"},
            )
            assert miss_res.status_code == 200, miss_res.text
            miss_body = miss_res.json()
            assert miss_body["executed"] is False
            assert miss_body["status"] == "tool_missing"
            assert "/system/doctor" in (miss_body["error"] or "")

            # 2. Mocked java_test -> writes reconqa-execution-summary.json -> syncs assertion to "passed"
            monkeypatch.setattr(
                acceptance_mod,
                "_resolve_tool_bin",
                lambda name: f"/usr/bin/{name}" if name in {"mvn", "schemathesis"} else None,
            )

            async def _fake_exec(cmd, *, cwd=None, env=None, timeout=120):
                exe = Path(cmd[0]).name
                if exe == "mvn":
                    assert cwd is not None
                    target_dir = Path(cwd) / "target"
                    target_dir.mkdir(parents=True, exist_ok=True)
                    (target_dir / "reconqa-execution-summary.json").write_text(
                        json.dumps(
                            {
                                "total": 1,
                                "passed": 1,
                                "failed": 0,
                                "skipped": 0,
                                "durationMs": 420,
                                "tests": [
                                    {
                                        "displayName": "verifyEndpointContract[L1_001, 01_invoices, GET, /api/v1/invoices]",
                                        "status": "PASSED",
                                        "story": "01_invoices",
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return 0, "Tests run: 1, Failures: 0", "", False
                if exe == "schemathesis":
                    return 0, "10 passed, 0 failed in 1.2s", "", False
                return 0, "", "", False

            monkeypatch.setattr(acceptance_mod, "_run_exec", _fake_exec)

            java_res = await client.post(
                f"/api/v1/projects/{pid}/runs/{rid}/execute-tool",
                json={"tool": "java_test", "sync_scenarios": True},
            )
            assert java_res.status_code == 200, java_res.text
            jb = java_res.json()
            assert jb["executed"] is True
            assert jb["status"] == "passed"
            assert jb["summary"]["passed"] == 1
            assert jb["synced_scenarios"] == 1
            assert jb["synced_assertions"] == 1

            # 3. Mocked schemathesis (0 server errors) -> syncs validation negative case -> "covered" -> L4!
            st_res = await client.post(
                f"/api/v1/projects/{pid}/runs/{rid}/execute-tool",
                json={"tool": "schemathesis", "sync_scenarios": True},
            )
            assert st_res.status_code == 200, st_res.text
            sb = st_res.json()
            assert sb["executed"] is True
            assert sb["status"] == "passed"
            assert sb["summary"]["server_errors"] == 0
            assert sb["synced_scenarios"] == 1
            assert sb["synced_negative_cases"] == 1

            # Verify scenario upgraded to L4 with assertion=passed and negative_case=covered
            sc_after = (await client.get(f"/api/v1/projects/{pid}/scenarios/{sc['id']}")).json()
            assert sc_after["assertions"][0]["status"] == "passed"
            assert sc_after["negative_cases"][0]["status"] == "covered"
            assert sc_after["effectiveness_level"] == "L4"


@pytest.mark.asyncio
async def test_acceptance_prompt_zh_en_and_zero_secret_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify GET /projects/{id}/acceptance-prompt in zh & en includes real context and leaks ZERO secrets."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "accept_prompt.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            proj = (await client.post("/api/v1/projects", json={"name": "Checkout Engine"})).json()
            pid = proj["id"]

            await client.post(
                f"/api/v1/projects/{pid}/targets",
                json={"root_domain": "checkout.example.test", "base_url": "https://api.checkout.example.test"},
            )
            secret_token = "SUPER-SECRET-SYNTHETIC-BEARER-VALUE-998877"
            await client.post(
                f"/api/v1/projects/{pid}/auth-profiles",
                json={
                    "name": "QA Buyer Account",
                    "role_tag": "account_a",
                    "extra_headers_ref": {"Authorization": f"Bearer {secret_token}"},
                },
            )
            await client.post(
                f"/api/v1/projects/{pid}/knowledge",
                json={
                    "title": "Checkout Order PRD",
                    "doc_type": "prd",
                    "content": "Orders over 100 require coupon validation.",
                },
            )
            await client.post(
                f"/api/v1/projects/{pid}/scenarios",
                json={
                    "slug": "01_checkout_pay",
                    "name": "Checkout Payment Flow",
                    "priority": "P0",
                },
            )

            # Chinese prompt
            res_zh = await client.get(
                f"/api/v1/projects/{pid}/acceptance-prompt",
                params={"lang": "zh", "agent": "antigravity"},
            )
            assert res_zh.status_code == 200, res_zh.text
            zh_body = res_zh.json()
            assert zh_body["lang"] == "zh"
            assert zh_body["agent"] == "antigravity"
            md_zh = zh_body["prompt_markdown"]
            assert "Checkout Engine" in md_zh
            assert "https://api.checkout.example.test" in md_zh
            assert "QA Buyer Account" in md_zh
            assert "Checkout Order PRD" in md_zh
            assert "01_checkout_pay" in md_zh
            assert "reconqa_run_external_tool" in md_zh
            assert secret_token not in md_zh  # ZERO secret leakage!
            assert zh_body["context_summary"]["targets_count"] == 1
            assert zh_body["context_summary"]["credentials_count"] == 1
            assert zh_body["context_summary"]["kb_docs_count"] == 1
            assert zh_body["context_summary"]["scenarios_needing_attention"] == 1

            # English prompt
            res_en = await client.get(
                f"/api/v1/projects/{pid}/acceptance-prompt",
                params={"lang": "en", "agent": "claude", "mcp_url": "http://127.0.0.1:8000/mcp"},
            )
            assert res_en.status_code == 200, res_en.text
            en_body = res_en.json()
            assert en_body["lang"] == "en"
            assert en_body["agent"] == "claude"
            md_en = en_body["prompt_markdown"]
            assert "Claude Code" in md_en
            assert "http://127.0.0.1:8000/mcp" in md_en
            assert "4-Stage Autonomous Acceptance Loop" in md_en
            assert "reconqa_extract_credential" in md_en
            assert secret_token not in md_en  # ZERO secret leakage!
            assert zh_body["mode"] == "full"
            assert en_body["mode"] == "full"


@pytest.mark.asyncio
async def test_acceptance_prompt_har_explore_mode_and_target_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify mode=har_explore (zh & en), target_ids filtering, static-only capabilities, and zero secret leak."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "accept_har_explore.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            proj = (await client.post("/api/v1/projects", json={"name": "Commerce Suite"})).json()
            pid = proj["id"]

            t_admin = (
                await client.post(
                    f"/api/v1/projects/{pid}/targets",
                    json={
                        "root_domain": "shop.example.test",
                        "base_url": "https://admin.shop.example.test",
                        "scope_rules": {"portal_label": "Admin Console"},
                    },
                )
            ).json()
            t_buyer = (
                await client.post(
                    f"/api/v1/projects/{pid}/targets",
                    json={
                        "root_domain": "shop.example.test",
                        "base_url": "https://portal.shop.example.test",
                        "scope_rules": {"portal_label": "Buyer Portal"},
                    },
                )
            ).json()

            secret_token = "SYNTH-HAR-EXPLORE-SECRET-TOKEN-12345"
            await client.post(
                f"/api/v1/projects/{pid}/auth-profiles",
                json={
                    "name": "Admin Session",
                    "role_tag": "account_a",
                    "extra_headers_ref": {"Authorization": f"Bearer {secret_token}"},
                },
            )

            # Seed a completed run with 1 live endpoint + 2 static-only endpoints
            run_data = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Initial Explore Run", "target_url": "https://admin.shop.example.test"},
                )
            ).json()
            rid = run_data["id"]
            hub_dir = Path(run_data["workspace_dir"]) / "hub"
            hub_dir.mkdir(parents=True, exist_ok=True)
            (hub_dir / "openapi.json").write_text(
                json.dumps(
                    {
                        "openapi": "3.0.3",
                        "servers": [{"url": "https://admin.shop.example.test"}],
                        "paths": {
                            "/orders/list": {
                                "get": {
                                    "x-discovery-source": "har-live",
                                    "x-scenarios": ["01_orders"],
                                    "responses": {"200": {"description": "OK"}},
                                }
                            },
                            "/billing/refund/apply": {
                                "post": {
                                    "x-discovery-source": "js-static",
                                    "responses": {"200": {"description": "OK"}},
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            sf = db_mod.get_session_factory()
            async with sf() as session:
                run_obj = await session.get(Run, rid)
                assert run_obj is not None
                run_obj.status = "completed"
                await session.commit()

            # 1. zh har_explore filtered strictly to Admin Console target_id
            res_zh = await client.get(
                f"/api/v1/projects/{pid}/acceptance-prompt",
                params=[
                    ("lang", "zh"),
                    ("agent", "antigravity"),
                    ("mode", "har_explore"),
                    ("target_ids", t_admin["id"]),
                ],
            )
            assert res_zh.status_code == 200, res_zh.text
            zh_body = res_zh.json()
            assert zh_body["mode"] == "har_explore"
            assert zh_body["lang"] == "zh"
            assert zh_body["context_summary"]["targets_count"] == 1
            md_zh = zh_body["prompt_markdown"]
            assert "Admin Console" in md_zh
            assert "https://admin.shop.example.test" in md_zh
            assert "Buyer Portal" not in md_zh
            assert "https://portal.shop.example.test" not in md_zh
            assert "record_har_path" in md_zh
            assert "reconqa_get_project_inventory" in md_zh
            assert "reconqa_get_capability_map" in md_zh
            assert "reconqa_upload_har_files" in md_zh
            assert "reconqa_extract_credential" in md_zh
            assert "billing/refund" in md_zh
            assert "CAPTCHA" in md_zh
            assert "Admin Session" in md_zh
            assert secret_token not in md_zh

            # 2. en har_explore with both target_ids + mcp_url
            res_en = await client.get(
                f"/api/v1/projects/{pid}/acceptance-prompt",
                params=[
                    ("lang", "en"),
                    ("agent", "claude"),
                    ("mode", "har_explore"),
                    ("target_ids", t_admin["id"]),
                    ("target_ids", t_buyer["id"]),
                    ("mcp_url", "http://127.0.0.1:8001/mcp"),
                ],
            )
            assert res_en.status_code == 200, res_en.text
            en_body = res_en.json()
            assert en_body["mode"] == "har_explore"
            assert en_body["context_summary"]["targets_count"] == 2
            md_en = en_body["prompt_markdown"]
            assert "Playwright HAR Exploration" in md_en
            assert "Admin Console" in md_en
            assert "Buyer Portal" in md_en
            assert "http://127.0.0.1:8001/mcp" in md_en
            assert "record_har_path" in md_en
            assert "reconqa_upload_har_files" in md_en
            assert "billing/refund" in md_en
            assert "CAPTCHA" in md_en
            assert secret_token not in md_en

            # 3. Unknown target_ids falls back cleanly to all project targets
            res_fallback = await client.get(
                f"/api/v1/projects/{pid}/acceptance-prompt",
                params=[
                    ("lang", "en"),
                    ("mode", "har_explore"),
                    ("target_ids", "nonexistent-target-id"),
                ],
            )
            assert res_fallback.status_code == 200
            fb_body = res_fallback.json()
            assert fb_body["context_summary"]["targets_count"] == 2
            assert "Admin Console" in fb_body["prompt_markdown"]
            assert "Buyer Portal" in fb_body["prompt_markdown"]

            # 4. mode=full default backward compatibility
            res_full = await client.get(
                f"/api/v1/projects/{pid}/acceptance-prompt",
                params={"lang": "en"},
            )
            assert res_full.status_code == 200
            assert res_full.json()["mode"] == "full"
            assert "4-Stage Autonomous Acceptance Loop" in res_full.json()["prompt_markdown"]
