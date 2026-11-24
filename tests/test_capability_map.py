"""Tests for Business Capability Map (Resource x Action x Role x State) & 5-Case Promotion (#212)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.models.tables import Run
from reconqa.modules.hub.capability_map import build_capability_map


def test_capability_map_normalization_roles_actions_and_state_mining() -> None:
    """Verify infix stripping -> shopstore/order & hospital/pres, role/action classification, and status mining."""
    inventory = {
        "endpoints": [
            {
                "method": "POST",
                "path": "/shopstore/plf/order/list",
                "discovery_source": "har-live",
                "host": "api.example.test",
                "scenarios": ["01_admin_orders"],
                "response_example": {
                    "code": 0,
                    "data": {
                        "records": [
                            {"id": 1, "orderStatus": 0, "auditStatus": "PENDING"},
                            {"id": 2, "orderStatus": 2, "auditStatus": "APPROVED"},
                        ]
                    },
                },
            },
            {
                "method": "POST",
                "path": "/shopstore/seller/order/refund/{id}",
                "discovery_source": "har-live",
                "host": "api.example.test",
                "scenarios": ["02_pharmacy_refund"],
                "response_example": {"code": 0, "data": {"orderStatus": 5}},
            },
            {
                "method": "POST",
                "path": "/hospital/doctor/pres/audit",
                "discovery_source": "har-live",
                "host": "api.example.test",
                "scenarios": ["03_doctor_pres"],
                "response_example": {"code": 0, "data": {"checkStatus": 1}},
            },
            {
                "method": "GET",
                "path": "/hospital/yz/pres/detail/{id}",
                "discovery_source": "js-static",
                "host": "api.example.test",
                "scenarios": [],
                "response_example": None,
            },
        ]
    }

    cap_map = build_capability_map(
        inventory=inventory,
        scenarios=[],
        project_id="proj-1",
        project_name="Health Platform",
    )
    caps_by_key = {c["key"]: c for c in cap_map["capabilities"]}
    assert set(caps_by_key.keys()) == {"shopstore/order", "hospital/pres"}

    order_cap = caps_by_key["shopstore/order"]
    assert order_cap["module"] == "shopstore"
    assert order_cap["resource"] == "order"
    assert order_cap["live_endpoints"] == 2
    assert order_cap["write_endpoints"] == 2
    assert set(order_cap["roles"]) == {"admin", "pharmacy"}
    assert set(order_cap["actions"]) == {"list", "refund"}
    assert order_cap["has_state_machine"] is True
    assert set(order_cap["observed_statuses"]["orderStatus"]) == {"0", "2", "5"}
    assert set(order_cap["observed_statuses"]["auditStatus"]) == {"APPROVED", "PENDING"}
    assert order_cap["recommended_priority"] == "P0"

    pres_cap = caps_by_key["hospital/pres"]
    assert set(pres_cap["roles"]) == {"doctor", "assistant"}
    assert set(pres_cap["actions"]) == {"audit", "detail"}
    assert pres_cap["live_endpoints"] == 1
    assert pres_cap["total_endpoints"] == 2
    assert pres_cap["observed_statuses"]["checkStatus"] == ["1"]

    metrics = cap_map["metrics"]
    assert metrics["total_endpoints"] == 4
    assert metrics["total_capabilities"] == 2
    assert metrics["live_capabilities"] == 2
    assert metrics["stateful_capabilities"] == 2
    assert metrics["capability_coverage_pct"] == 100.0


@pytest.mark.asyncio
async def test_capabilities_api_filtering_and_idempotent_5case_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify GET /projects/{id}/capabilities filters + POST /capabilities/promote-scenario idempotency."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "cap_map_test.db"
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
            pid = (await client.post("/api/v1/projects", json={"name": "Care Ecosystem"})).json()["id"]

            run_res = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Multi-Portal Run", "modules": ["merge"]},
                )
            ).json()
            rid = run_res["id"]
            hub_dir = Path(run_res["workspace_dir"]) / "hub"
            hub_dir.mkdir(parents=True, exist_ok=True)
            (hub_dir / "openapi.json").write_text(
                json.dumps(
                    {
                        "openapi": "3.0.3",
                        "servers": [{"url": "https://api.example.test"}],
                        "paths": {
                            "/shopstore/plf/order/create": {
                                "post": {
                                    "x-discovery-source": "har-live",
                                    "x-scenarios": ["01_orders"],
                                    "responses": {
                                        "200": {
                                            "content": {
                                                "application/json": {
                                                    "example": {"code": 0, "data": {"orderStatus": 1}}
                                                }
                                            }
                                        }
                                    },
                                }
                            },
                            "/shopstore/seller/order/detail/{id}": {
                                "get": {
                                    "x-discovery-source": "har-live",
                                    "x-scenarios": ["01_orders"],
                                    "responses": {
                                        "200": {
                                            "content": {
                                                "application/json": {
                                                    "example": {"code": 0, "data": {"orderStatus": 2}}
                                                }
                                            }
                                        }
                                    },
                                }
                            },
                            "/system/p/banner/list": {
                                "get": {
                                    "x-discovery-source": "js-static",
                                    "x-scenarios": [],
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
                run_obj.started_at = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
                run_obj.finished_at = datetime(2026, 11, 24, 1, 2, tzinfo=UTC)
                await session.commit()

            # 1. GET /capabilities without filters returns both capabilities + accurate 5-dim metrics
            r_all = await client.get(f"/api/v1/projects/{pid}/capabilities")
            assert r_all.status_code == 200, r_all.text
            all_data = r_all.json()
            assert all_data["metrics"]["total_capabilities"] == 2
            assert all_data["metrics"]["live_capabilities"] == 1
            assert len(all_data["modules"]) == 2

            # 2. Filters: live_only, role, module, has_writes, q
            r_live = (
                await client.get(f"/api/v1/projects/{pid}/capabilities", params={"live_only": "true"})
            ).json()
            assert [c["key"] for c in r_live["capabilities"]] == ["shopstore/order"]
            assert r_live["metrics"]["total_capabilities"] == 2  # overall metrics stay accurate

            r_role = (
                await client.get(f"/api/v1/projects/{pid}/capabilities", params={"role": "pharmacy"})
            ).json()
            assert [c["key"] for c in r_role["capabilities"]] == ["shopstore/order"]

            r_writes = (
                await client.get(f"/api/v1/projects/{pid}/capabilities", params={"has_writes": "true"})
            ).json()
            assert [c["key"] for c in r_writes["capabilities"]] == ["shopstore/order"]

            r_q = (
                await client.get(f"/api/v1/projects/{pid}/capabilities", params={"q": "banner"})
            ).json()
            assert [c["key"] for c in r_q["capabilities"]] == ["system/banner"]

            # 3. Promote capability to 5-case Scenario (and 404 on unknown key)
            r_404 = await client.post(
                f"/api/v1/projects/{pid}/capabilities/promote-scenario",
                json={"capability_key": "missing/cap"},
            )
            assert r_404.status_code == 404

            r_prom = await client.post(
                f"/api/v1/projects/{pid}/capabilities/promote-scenario",
                json={"capability_key": "shopstore/order"},
            )
            assert r_prom.status_code == 201, r_prom.text
            sc = r_prom.json()
            assert sc["slug"] == "cap_shopstore-order"
            assert sc["priority"] == "P0"
            assert sc["business_domain"] == "shopstore"
            assert len(sc["assertions"]) == 2
            assert {nc["category"] for nc in sc["negative_cases"]} == {
                "validation",
                "state_machine",
                "auth",
                "idempotency",
            }
            assert "4-Class Verification Oracle" in sc["description"]

            # Edit scenario name & mark negative case covered, then re-promote -> MUST NOT overwrite!
            neg_list = sc["negative_cases"]
            neg_list[0]["status"] = "covered"
            neg_list[1]["status"] = "covered"
            r_edit = await client.patch(
                f"/api/v1/projects/{pid}/scenarios/{sc['id']}",
                json={"name": "Custom Order Verification", "negative_cases": neg_list},
            )
            assert r_edit.status_code == 200

            r_prom2 = await client.post(
                f"/api/v1/projects/{pid}/capabilities/promote-scenario",
                json={"capability_key": "shopstore/order", "name": "Attempted Overwrite"},
            )
            assert r_prom2.status_code == 201
            assert r_prom2.json()["id"] == sc["id"]
            assert r_prom2.json()["name"] == "Custom Order Verification"

            # Verify capability map now reflects linked scenario & covered state/negative metrics!
            r_after = (await client.get(f"/api/v1/projects/{pid}/capabilities")).json()
            order_after = next(c for c in r_after["capabilities"] if c["key"] == "shopstore/order")
            assert any(ls["slug"] == "cap_shopstore-order" for ls in order_after["linked_scenarios"])
            slot_map = {s["slot"]: s["status"] for s in order_after["case_matrix"]}
            assert slot_map["invalid_params"] == "covered"
            assert slot_map["illegal_state"] == "covered"
            assert r_after["metrics"]["state_transition_covered"] == 1
            assert r_after["metrics"]["negative_slots_tested"] >= 2

    if db_mod._engine is not None:
        await db_mod._engine.dispose()
