"""Tests for Business Capability Map (Resource x Action x Role x State) & 5-Case Promotion (#212)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.models.schemas import (
    InventoryEndpoint,
    ProjectCapabilityMapResponse,
)
from reconqa.models.tables import Run
from reconqa.modules.hub.capability_map import build_capability_map
from reconqa.modules.hub.inventory import build_project_inventory


def test_capability_map_normalization_roles_actions_and_state_mining() -> None:
    """Verify infix stripping, noise filtering, bilingual titles, is_core, role/action, status mining & slot counts."""
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
            # Noise module endpoints that must be excluded by default
            {
                "method": "GET",
                "path": "/v2/ping",
                "discovery_source": "har-live",
                "host": "api.example.test",
                "scenarios": [],
                "response_example": {"code": 0},
            },
            {
                "method": "POST",
                "path": "/tuikit/im/callback",
                "discovery_source": "js-static",
                "host": "api.example.test",
                "scenarios": [],
                "response_example": None,
            },
            {
                "method": "GET",
                "path": "/",
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
    assert {m["module"] for m in cap_map["modules"]} == {"shopstore", "hospital"}

    # include_noise=True exposes noise modules when explicitly requested
    noisy_map = build_capability_map(
        inventory=inventory,
        scenarios=[],
        project_id="proj-1",
        project_name="Health Platform",
        include_noise=True,
    )
    assert {"v2/ping", "tuikit/im", "root/root"}.issubset(
        {c["key"] for c in noisy_map["capabilities"]}
    )

    order_cap = caps_by_key["shopstore/order"]
    assert order_cap["module"] == "shopstore"
    assert order_cap["resource"] == "order"
    assert order_cap["title_zh"] == "订单管理"
    assert order_cap["title_en"] == "Orders"
    assert order_cap["is_core"] is True
    assert order_cap["live_endpoints"] == 2
    assert order_cap["write_endpoints"] == 2
    assert set(order_cap["roles"]) == {"admin", "pharmacy"}
    assert set(order_cap["actions"]) == {"list", "refund"}
    assert order_cap["has_state_machine"] is True
    assert set(order_cap["observed_statuses"]["orderStatus"]) == {"0", "2", "5"}
    assert set(order_cap["observed_statuses"]["auditStatus"]) == {"APPROVED", "PENDING"}
    assert order_cap["recommended_priority"] == "P0"

    pres_cap = caps_by_key["hospital/pres"]
    assert pres_cap["title_zh"] == "处方管理"
    assert pres_cap["title_en"] == "Prescriptions"
    assert pres_cap["is_core"] is True
    assert set(pres_cap["roles"]) == {"doctor", "assistant"}
    assert set(pres_cap["actions"]) == {"audit", "detail"}
    assert pres_cap["live_endpoints"] == 1
    assert pres_cap["total_endpoints"] == 2
    assert pres_cap["observed_statuses"]["checkStatus"] == ["1"]

    metrics = cap_map["metrics"]
    assert metrics["total_endpoints"] == 7
    assert metrics["total_capabilities"] == 2
    assert metrics["core_capabilities"] == 2
    assert metrics["live_capabilities"] == 2
    assert metrics["stateful_capabilities"] == 2
    assert metrics["capability_coverage_pct"] == 100.0
    assert metrics["slots_passed"] == 0
    assert metrics["slots_failed"] == 0
    assert metrics["slots_pending"] == 2
    assert metrics["slots_missing"] == 8


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
                            "/v3/tool/health": {
                                "get": {
                                    "x-discovery-source": "har-live",
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

            # 1. GET /capabilities filters out noise module 'v3' & returns accurate metrics
            r_all = await client.get(f"/api/v1/projects/{pid}/capabilities")
            assert r_all.status_code == 200, r_all.text
            all_data = r_all.json()
            assert all_data["metrics"]["total_capabilities"] == 2
            assert all_data["metrics"]["core_capabilities"] == 1
            assert all_data["metrics"]["live_capabilities"] == 1
            assert {m["module"] for m in all_data["modules"]} == {"shopstore", "system"}

            caps_by_key = {c["key"]: c for c in all_data["capabilities"]}
            assert caps_by_key["shopstore/order"]["title_zh"] == "订单管理"
            assert caps_by_key["shopstore/order"]["title_en"] == "Orders"
            assert caps_by_key["shopstore/order"]["is_core"] is True
            assert caps_by_key["system/banner"]["title_zh"] == "system / banner"
            assert caps_by_key["system/banner"]["is_core"] is False

            # 2. Filters: core_only, live_only, role, module, has_writes, q (including Chinese title)
            r_core = (
                await client.get(f"/api/v1/projects/{pid}/capabilities", params={"core_only": "true"})
            ).json()
            assert [c["key"] for c in r_core["capabilities"]] == ["shopstore/order"]

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
                await client.get(f"/api/v1/projects/{pid}/capabilities", params={"q": "订单"})
            ).json()
            assert [c["key"] for c in r_q["capabilities"]] == ["shopstore/order"]

            # 3. Promote capability to 5-case Scenario (and 404 on unknown key)
            r_404 = await client.post(
                f"/api/v1/projects/{pid}/capabilities/promote-scenario",
                json={"capability_key": "missing/cap"},
            )
            assert r_404.status_code == 404

            # Default lang="zh" generates natural Chinese 5-case scenario + real endpoint_count & L3 level!
            r_prom = await client.post(
                f"/api/v1/projects/{pid}/capabilities/promote-scenario",
                json={"capability_key": "shopstore/order", "lang": "zh"},
            )
            assert r_prom.status_code == 201, r_prom.text
            sc = r_prom.json()
            assert sc["slug"] == "cap_shopstore-order"
            assert sc["name"] == "订单管理（核心能力验收）"
            assert sc["priority"] == "P0"
            assert sc["business_domain"] == "shopstore"
            assert sc["endpoint_count"] == 2
            assert sc["effectiveness_level"] == "L3"
            assert len(sc["assertions"]) == 2
            assert sc["assertions"][0]["title"].startswith("[正向流程]")
            assert sc["assertions"][1]["title"].startswith("[业务不变量]")
            assert {nc["category"] for nc in sc["negative_cases"]} == {
                "validation",
                "state_machine",
                "auth",
                "idempotency",
            }
            assert any(nc["title"].startswith("[缺参/非法参]") for nc in sc["negative_cases"])
            assert "4 类验证 Oracle" in sc["description"]

            # Verify GET /scenarios/{id} also links the 2 live endpoints cleanly
            r_sc_detail = await client.get(f"/api/v1/projects/{pid}/scenarios/{sc['id']}")
            assert r_sc_detail.status_code == 200
            sc_detail = r_sc_detail.json()
            assert sc_detail["endpoint_count"] == 2
            assert len(sc_detail["endpoints"]) == 2
            assert sc_detail["effectiveness_level"] == "L3"

            # Promote English scenario for system/banner to verify lang="en"
            r_prom_en = await client.post(
                f"/api/v1/projects/{pid}/capabilities/promote-scenario",
                json={"capability_key": "system/banner", "lang": "en"},
            )
            assert r_prom_en.status_code == 201, r_prom_en.text
            sc_en = r_prom_en.json()
            assert "Core Capability Verification" in sc_en["name"]
            assert "4-Class Verification Oracle" in sc_en["description"]
            assert sc_en["endpoint_count"] == 1
            assert sc_en["effectiveness_level"] == "L3"

            # Edit scenario: mark 1 assertion passed, 1 neg case covered, 1 neg case failed -> check slots_passed & slots_failed!
            assertions_list = sc["assertions"]
            assertions_list[0]["status"] = "passed"
            neg_list = sc["negative_cases"]
            neg_list[0]["status"] = "covered"
            neg_list[1]["status"] = "failed"
            r_edit = await client.patch(
                f"/api/v1/projects/{pid}/scenarios/{sc['id']}",
                json={
                    "name": "Custom Order Verification",
                    "assertions": assertions_list,
                    "negative_cases": neg_list,
                },
            )
            assert r_edit.status_code == 200
            assert r_edit.json()["effectiveness_level"] == "L4"

            r_prom2 = await client.post(
                f"/api/v1/projects/{pid}/capabilities/promote-scenario",
                json={"capability_key": "shopstore/order", "name": "Attempted Overwrite"},
            )
            assert r_prom2.status_code == 201
            assert r_prom2.json()["id"] == sc["id"]
            assert r_prom2.json()["name"] == "Custom Order Verification"

            # Verify capability map now reflects slots_passed vs slots_failed accurately (failed NOT lumped into passed!)
            r_after = (await client.get(f"/api/v1/projects/{pid}/capabilities")).json()
            order_after = next(c for c in r_after["capabilities"] if c["key"] == "shopstore/order")
            assert any(ls["slug"] == "cap_shopstore-order" for ls in order_after["linked_scenarios"])
            slot_map = {s["slot"]: s["status"] for s in order_after["case_matrix"]}
            assert slot_map["happy_path"] == "covered"
            assert slot_map["invalid_params"] == "covered"
            assert slot_map["illegal_state"] == "failed"
            assert r_after["metrics"]["core_capabilities"] == 1
            assert r_after["metrics"]["slots_passed"] == 2
            assert r_after["metrics"]["slots_failed"] == 1
            assert r_after["metrics"]["slots_pending"] == 2
            assert r_after["metrics"]["slots_missing"] == 0
            assert r_after["metrics"]["state_transition_covered"] == 1
            assert r_after["metrics"]["negative_slots_tested"] >= 2

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


def test_inventory_and_capability_map_preserve_per_endpoint_host_and_custom_headers(
    tmp_path: Path,
) -> None:
    """Verify per-endpoint x-host/x-origin and x-custom-headers survive inventory, OpenAPI, and capability map."""
    run1_ws = tmp_path / "run1"
    run2_ws = tmp_path / "run2"
    (run1_ws / "hub").mkdir(parents=True, exist_ok=True)
    (run2_ws / "hub").mkdir(parents=True, exist_ok=True)

    (run1_ws / "hub" / "openapi.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "servers": [{"url": "https://pc-api.example.com"}],
                "paths": {
                    "/shopstore/plf/order/list": {
                        "get": {
                            "summary": "List orders v1",
                            "x-discovery-source": "har-live",
                            "x-host": "pc-api.example.com",
                            "x-origin": "https://pc-api.example.com",
                            "x-custom-headers": {
                                "App-Version": "1.0.0",
                                "platform": "web",
                                "": "ignored-empty-key",
                            },
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                    "/shopstore/seller/order/settle": {
                        "post": {
                            "summary": "Settle order v1",
                            "x-discovery-source": "har-live",
                            "x-origin": "https://acctrade-api.example.com",
                            "x-custom-headers": ["appType"],
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    (run2_ws / "hub" / "openapi.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "servers": [{"url": "https://pc-api.example.com"}],
                "paths": {
                    "/shopstore/plf/order/list": {
                        "get": {
                            "summary": "List orders v2",
                            "x-discovery-source": "har-live",
                            "x-host": "pc-api.example.com",
                            "x-custom-headers": {
                                "App-Version": "2.0.0",
                                "deviceId": "dev-9",
                            },
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                    "/shopstore/seller/order/settle": {
                        "post": {
                            "summary": "Settle order v2",
                            "x-discovery-source": "har-live",
                            "servers": [{"url": "https://acctrade-api.example.com"}],
                            "x-custom-headers": {
                                "appType": "merchant",
                                "secret-v": "v3",
                            },
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    runs = [
        SimpleNamespace(id="run-001", workspace_dir=str(run1_ws), config_snapshot={"run_name": "Run 1"}),
        SimpleNamespace(id="run-002", workspace_dir=str(run2_ws), config_snapshot={"run_name": "Run 2"}),
    ]

    inventory, openapi_doc = build_project_inventory(
        project_id="proj-mh",
        project_name="MultiHost Platform",
        runs=runs,
        explore_results=[],
    )

    # Validate InventoryEndpoint schema and merged fields
    eps_by_key = {(ep["method"], ep["path"]): InventoryEndpoint.model_validate(ep) for ep in inventory["endpoints"]}
    list_ep = eps_by_key[("GET", "/shopstore/plf/order/list")]
    assert list_ep.host == "pc-api.example.com"
    assert list_ep.custom_headers == {
        "App-Version": "2.0.0",
        "platform": "web",
        "deviceId": "dev-9",
    }

    settle_ep = eps_by_key[("POST", "/shopstore/seller/order/settle")]
    assert settle_ep.host == "acctrade-api.example.com"
    assert settle_ep.custom_headers == {"appType": "merchant", "secret-v": "v3"}

    # Validate merged OpenAPI spec emits x-host and x-custom-headers per operation
    oa_settle = openapi_doc["paths"]["/shopstore/seller/order/settle"]["post"]
    assert oa_settle["x-host"] == "acctrade-api.example.com"
    assert oa_settle["x-custom-headers"] == {"appType": "merchant", "secret-v": "v3"}

    oa_list = openapi_doc["paths"]["/shopstore/plf/order/list"]["get"]
    assert oa_list["x-host"] == "pc-api.example.com"
    assert oa_list["x-custom-headers"] == {
        "App-Version": "2.0.0",
        "platform": "web",
        "deviceId": "dev-9",
    }

    # Validate CapabilityEndpointRef propagation through build_capability_map & Pydantic schema
    cap_map = build_capability_map(
        inventory=inventory,
        scenarios=[],
        project_id="proj-mh",
        project_name="MultiHost Platform",
    )
    validated_map = ProjectCapabilityMapResponse.model_validate(cap_map)
    order_cap = next(c for c in validated_map.capabilities if c.key == "shopstore/order")
    cap_eps = {(e.method, e.path): e for e in order_cap.endpoints}
    assert cap_eps[("GET", "/shopstore/plf/order/list")].host == "pc-api.example.com"
    assert cap_eps[("GET", "/shopstore/plf/order/list")].custom_headers == {
        "App-Version": "2.0.0",
        "platform": "web",
        "deviceId": "dev-9",
    }
    assert cap_eps[("POST", "/shopstore/seller/order/settle")].host == "acctrade-api.example.com"
    assert cap_eps[("POST", "/shopstore/seller/order/settle")].custom_headers == {
        "appType": "merchant",
        "secret-v": "v3",
    }


def test_inventory_filters_explore_spa_crawler_and_noise_but_keeps_apis_and_har_live(
    tmp_path: Path,
) -> None:
    """Verify inventory.py strips katana SPA routes, regex noise, and binary downloads from explore results."""
    run_ws = tmp_path / "run_live"
    (run_ws / "hub").mkdir(parents=True, exist_ok=True)
    (run_ws / "hub" / "openapi.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "servers": [{"url": "https://pc-api.example.test"}],
                "paths": {
                    "/merchant/live-rpc": {
                        "post": {
                            "summary": "Live HAR endpoint",
                            "x-discovery-source": "har-live",
                            "x-host": "pc-api.example.test",
                            "responses": {"200": {"description": "OK"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    explore_results = [
        {
            "seed": {
                "base_url": "https://www.example.test",
                "root_domain": "example.test",
            },
            "spa_routes": ["/merchant/finance", "/api/shop/health"],
            "discovered_endpoints": [
                # Katana / urlfinder SPA routes -> filtered out
                {
                    "method": "GET",
                    "path": "/merchant/orders",
                    "url": "https://www.example.test/merchant/orders",
                    "source": "katana",
                },
                {
                    "method": "GET",
                    "path": "/login",
                    "url": "https://www.example.test/login",
                    "source": "katana",
                },
                {
                    "method": "GET",
                    "path": "/agreement",
                    "url": "https://www.example.test/agreement",
                    "source": "urlfinder",
                },
                # JS regex / dot-property / single-char / root noise -> filtered out
                {"method": "GET", "path": "/.exec", "url": "https://www.example.test/.exec", "source": "katana"},
                {
                    "method": "GET",
                    "path": "/.exec.apply",
                    "url": "https://www.example.test/.exec.apply",
                    "source": "js_static",
                },
                {"method": "GET", "path": "/.test", "url": "https://www.example.test/.test", "source": "js_static"},
                {"method": "GET", "path": "/g", "url": "https://www.example.test/g", "source": "katana"},
                {"method": "GET", "path": "/", "url": "https://www.example.test/", "source": "katana"},
                # Binary installer download -> filtered out
                {
                    "method": "GET",
                    "path": "/v3/prod/Client-Setup.exe",
                    "url": "https://www.example.test/v3/prod/Client-Setup.exe",
                    "source": "js_static",
                },
                # js_static path that is in spa_routes and not API-like -> filtered out
                {
                    "method": "GET",
                    "path": "/merchant/finance",
                    "url": "https://www.example.test/merchant/finance",
                    "source": "js_static",
                },
                # Real API endpoints -> MUST be kept
                {
                    "method": "GET",
                    "path": "/api/shop/orders/list",
                    "url": "https://www.example.test/api/shop/orders/list",
                    "source": "katana",
                },
                {
                    "method": "GET",
                    "path": "/users",
                    "url": "https://api.example.com/users",
                    "source": "katana",
                },
                {
                    "method": "GET",
                    "path": "/api/shop/health",
                    "url": "https://www.example.test/api/shop/health",
                    "source": "js_static",
                },
                {
                    "method": "GET",
                    "path": "/.well-known/openid-configuration",
                    "url": "https://www.example.test/.well-known/openid-configuration",
                    "source": "js_static",
                },
            ],
        }
    ]

    runs = [
        SimpleNamespace(id="run-live-1", workspace_dir=str(run_ws), config_snapshot={"run_name": "Live Run"}),
    ]
    inventory, _ = build_project_inventory(
        project_id="proj-clean",
        project_name="Clean Inventory",
        runs=runs,
        explore_results=explore_results,
    )

    kept_paths = {ep["path"] for ep in inventory["endpoints"]}
    for dropped in (
        "/merchant/orders",
        "/login",
        "/agreement",
        "/.exec",
        "/.exec.apply",
        "/.test",
        "/g",
        "/",
        "/v3/prod/Client-Setup.exe",
        "/merchant/finance",
    ):
        assert dropped not in kept_paths, f"Expected noise/SPA path {dropped} to be filtered, got {kept_paths}"

    assert {
        "/merchant/live-rpc",
        "/api/shop/orders/list",
        "/users",
        "/api/shop/health",
        "/.well-known/openid-configuration",
    } == kept_paths


def test_inventory_filters_telemetry_endpoints_with_opt_out_and_keeps_business_apis(
    tmp_path: Path,
) -> None:
    """Verify inventory & classify_explore_endpoint filter telemetry by default and support include_telemetry=True."""
    from reconqa.modules.hub.inventory import classify_explore_endpoint

    assert (
        classify_explore_endpoint(
            "/api/shop/track/report", host="pc-api.example.test", source="js_static"
        )
        == "noise"
    )
    assert (
        classify_explore_endpoint(
            "/api/v1/order/track", host="pc-api.example.test", source="js_static"
        )
        == "api"
    )

    run_ws = tmp_path / "run_telemetry"
    (run_ws / "hub").mkdir(parents=True, exist_ok=True)
    (run_ws / "hub" / "openapi.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "servers": [{"url": "https://pc-api.example.test"}],
                "paths": {
                    "/api/shop/track/auth/newReport": {
                        "post": {
                            "x-discovery-source": "har-live",
                            "x-host": "pc-api.example.test",
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                    "/api/shop/track/auth/report": {
                        "post": {
                            "x-discovery-source": "har-live",
                            "x-host": "pc-api.example.test",
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                    "/api/shop/track/getConfig": {
                        "get": {
                            "x-discovery-source": "har-live",
                            "x-host": "pc-api.example.test",
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                    "/v1/ingest": {
                        "post": {
                            "x-discovery-source": "har-live",
                            "x-host": "bdapi.example.test",
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                    "/api/v1/order/track": {
                        "get": {
                            "x-discovery-source": "har-live",
                            "x-host": "pc-api.example.test",
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                    "/api/deviceW2": {
                        "post": {
                            "x-discovery-source": "har-live",
                            "x-host": "pc-api.example.test",
                            "responses": {"200": {"description": "OK"}},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    explore_results = [
        {
            "seed": {"base_url": "https://pc-api.example.test", "root_domain": "example.test"},
            "spa_routes": [],
            "discovered_endpoints": [
                {
                    "method": "POST",
                    "path": "/api/shop/track/report",
                    "url": "https://pc-api.example.test/api/shop/track/report",
                    "source": "js_static",
                },
                {
                    "method": "POST",
                    "path": "/rum/monitor",
                    "url": "https://api.example.test/rum/monitor",
                    "source": "katana",
                },
                {
                    "method": "GET",
                    "path": "/api/logistics/tracking/detail",
                    "url": "https://pc-api.example.test/api/logistics/tracking/detail",
                    "source": "js_static",
                },
                {
                    "method": "GET",
                    "path": "/api/v1/finance/report",
                    "url": "https://pc-api.example.test/api/v1/finance/report",
                    "source": "js_static",
                },
            ],
        }
    ]
    runs = [
        SimpleNamespace(id="run-tel-1", workspace_dir=str(run_ws), config_snapshot={"run_name": "Telemetry Run"}),
    ]

    # Default: telemetry filtered out across both Explore and historical Run OpenAPI
    inv_default, _ = build_project_inventory(
        project_id="proj-tel",
        project_name="Telemetry Filter",
        runs=runs,
        explore_results=explore_results,
    )
    default_paths = {ep["path"] for ep in inv_default["endpoints"]}
    assert default_paths == {
        "/api/v1/order/track",
        "/api/logistics/tracking/detail",
        "/api/v1/finance/report",
        "/api/deviceW2",
    }

    # Opt-out: include_telemetry=True preserves all telemetry endpoints
    inv_all, _ = build_project_inventory(
        project_id="proj-tel",
        project_name="Telemetry Filter",
        runs=runs,
        explore_results=explore_results,
        include_telemetry=True,
    )
    all_paths = {ep["path"] for ep in inv_all["endpoints"]}
    assert {
        "/api/shop/track/auth/newReport",
        "/api/shop/track/auth/report",
        "/api/shop/track/getConfig",
        "/api/shop/track/report",
        "/v1/ingest",
        "/rum/monitor",
        "/api/v1/order/track",
        "/api/logistics/tracking/detail",
        "/api/v1/finance/report",
        "/api/deviceW2",
    } == all_paths
