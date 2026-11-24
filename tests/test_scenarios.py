"""Tests for First-Class Scenario entity, auto-hydration, L0..L4 effectiveness, and Java assertions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.models.tables import Job, Run
from reconqa.modules.codegen_java.generator import JavaProjectGenerator, generate_java_project


def _write_run_openapi(workspace_dir: Path, spec: dict) -> None:
    hub_dir = workspace_dir / "hub"
    hub_dir.mkdir(parents=True, exist_ok=True)
    (hub_dir / "openapi.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_scenarios_auto_hydration_crud_effectiveness_and_inventory_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify auto-hydration, zero-overwrite preservation, L0->L4 maturity, KPIs, and scenario_meta."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "scenarios_test.db"
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

            # Seed two completed runs: Run 1 (merge only -> L1), Run 2 (merge + java_gen -> L2)
            r1 = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Run 1 Merge Only", "modules": ["merge"]},
                )
            ).json()
            r2 = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Run 2 Full QA", "modules": ["merge", "java_gen"]},
                )
            ).json()

            _write_run_openapi(
                Path(r1["workspace_dir"]),
                {
                    "openapi": "3.0.3",
                    "servers": [{"url": "https://api.example.test"}],
                    "paths": {
                        "/api/v1/dashboard/summary": {
                            "get": {
                                "summary": "Dashboard KPI summary",
                                "x-discovery-source": "har-live",
                                "x-scenarios": ["01_dashboard_overview"],
                                "responses": {"200": {"description": "OK"}},
                            }
                        }
                    },
                },
            )
            _write_run_openapi(
                Path(r2["workspace_dir"]),
                {
                    "openapi": "3.0.3",
                    "servers": [{"url": "https://api.example.test"}],
                    "paths": {
                        "/api/v1/orders/create": {
                            "post": {
                                "summary": "Create order",
                                "x-discovery-source": "har-live",
                                "x-scenarios": ["02_order_checkout"],
                                "responses": {"201": {"description": "Created"}},
                            }
                        },
                        "/api/v1/orders/{id}": {
                            "get": {
                                "summary": "Get order detail",
                                "x-discovery-source": "har-live",
                                "x-scenarios": ["02_order_checkout"],
                                "responses": {"200": {"description": "OK"}},
                            }
                        },
                    },
                },
            )

            sf = db_mod.get_session_factory()
            async with sf() as session:
                run1 = await session.get(Run, r1["id"])
                run2 = await session.get(Run, r2["id"])
                assert run1 is not None and run2 is not None
                run1.status = "completed"
                run1.started_at = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
                run1.finished_at = datetime(2026, 11, 20, 10, 5, tzinfo=UTC)
                run1.summary_metrics = {"modules_executed": ["merge"]}
                run2.status = "completed"
                run2.started_at = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
                run2.finished_at = datetime(2026, 11, 21, 10, 5, tzinfo=UTC)
                run2.summary_metrics = {"modules_executed": ["merge", "java_gen"]}
                session.add(
                    Job(
                        run_id=run2.id,
                        module="java_gen",
                        status="completed",
                        output_meta={"l1_count": 1},
                    )
                )
                await session.commit()

            # 1. Lazy auto-hydration on GET /scenarios
            list_res = await client.get(f"/api/v1/projects/{pid}/scenarios")
            assert list_res.status_code == 200, list_res.text
            data = list_res.json()
            assert data["total"] == 2
            by_slug = {s["slug"]: s for s in data["scenarios"]}
            assert set(by_slug.keys()) == {"01_dashboard_overview", "02_order_checkout"}

            sc_dash = by_slug["01_dashboard_overview"]
            sc_order = by_slug["02_order_checkout"]
            assert sc_dash["name"] == "01 Dashboard Overview"
            assert sc_dash["source"] == "har-auto"
            assert sc_dash["endpoint_count"] == 1
            assert sc_dash["effectiveness_level"] == "L1"  # merge-only run, no assertions

            assert sc_order["endpoint_count"] == 2
            assert sc_order["effectiveness_level"] == "L2"  # run2 completed java_gen!

            # 2. Create manual Planned scenario -> L0
            c_res = await client.post(
                f"/api/v1/projects/{pid}/scenarios",
                json={
                    "slug": "03_refund_flow",
                    "name": "Refund Approval Flow",
                    "description": "Manual planned scenario before HAR capture",
                    "preconditions": "Paid order exists",
                    "business_domain": "Payments",
                    "priority": "P1",
                    "owner": "qa-lead",
                    "tags": ["payments", "regression"],
                },
            )
            assert c_res.status_code == 201, c_res.text
            sc_refund = c_res.json()
            assert sc_refund["effectiveness_level"] == "L0"
            assert sc_refund["endpoint_count"] == 0

            # Duplicate slug without upsert returns 409; with upsert=true updates cleanly
            dup_res = await client.post(
                f"/api/v1/projects/{pid}/scenarios",
                json={"slug": "03_refund_flow", "name": "Duplicate"},
            )
            assert dup_res.status_code == 409
            up_res = await client.post(
                f"/api/v1/projects/{pid}/scenarios",
                params={"upsert": "true"},
                json={
                    "slug": "03_refund_flow",
                    "name": "Refund Approval Flow v2",
                    "business_domain": "Payments",
                    "priority": "P1",
                },
            )
            assert up_res.status_code == 201
            assert up_res.json()["name"] == "Refund Approval Flow v2"
            # Partial upsert omits description/tags -> preserves prior description & tags
            assert up_res.json()["description"] == "Manual planned scenario before HAR capture"
            assert up_res.json()["tags"] == ["payments", "regression"]

            # Partial upsert by slug only (MCP ai-agent style, omitting name)
            up_partial = await client.post(
                f"/api/v1/projects/{pid}/scenarios",
                params={"upsert": "true"},
                json={"slug": "03_refund_flow", "owner": "ai-bot", "source": "ai-agent"},
            )
            assert up_partial.status_code == 201
            assert up_partial.json()["name"] == "Refund Approval Flow v2"
            assert up_partial.json()["owner"] == "ai-bot"
            assert up_partial.json()["source"] == "ai-agent"

            # 3. PATCH 01_dashboard_overview -> verified (L2) and customize fields
            p_dash = await client.patch(
                f"/api/v1/projects/{pid}/scenarios/{sc_dash['id']}",
                json={
                    "name": "Executive Dashboard KPIs",
                    "description": "Verifies real-time sales totals",
                    "business_domain": "Analytics",
                    "priority": "P1",
                    "status": "verified",
                    "owner": "alice",
                },
            )
            assert p_dash.status_code == 200, p_dash.text
            assert p_dash.json()["effectiveness_level"] == "L2"

            # 4. PATCH 02_order_checkout -> P0 + business assertion -> L3
            p_order_l3 = await client.patch(
                f"/api/v1/projects/{pid}/scenarios/{sc_order['id']}",
                json={
                    "name": "Core Order Checkout",
                    "business_domain": "Orders",
                    "priority": "P0",
                    "assertions": [
                        {
                            "title": "Order ID is returned",
                            "target_endpoint": "POST /api/v1/orders/create",
                            "jsonpath": "$.data.orderId",
                            "operator": "not_empty",
                            "status": "passed",
                        }
                    ],
                    "negative_cases": [
                        {
                            "title": "Negative quantity rejected",
                            "category": "validation",
                            "expected_behavior": "HTTP 400 invalid quantity",
                            "status": "pending",
                        }
                    ],
                },
            )
            assert p_order_l3.status_code == 200, p_order_l3.text
            order_l3 = p_order_l3.json()
            assert order_l3["effectiveness_level"] == "L3"
            assert order_l3["assertion_count"] == 1
            assert order_l3["assertions_passed_count"] == 1
            assert order_l3["assertions"][0]["id"]  # auto-generated id

            # Mark negative case covered -> L4!
            neg_id = order_l3["negative_cases"][0]["id"]
            p_order_l4 = await client.patch(
                f"/api/v1/projects/{pid}/scenarios/{sc_order['id']}",
                json={
                    "negative_cases": [
                        {
                            "id": neg_id,
                            "title": "Negative quantity rejected",
                            "category": "validation",
                            "expected_behavior": "HTTP 400 invalid quantity",
                            "status": "covered",
                        }
                    ]
                },
            )
            assert p_order_l4.status_code == 200
            assert p_order_l4.json()["effectiveness_level"] == "L4"
            assert p_order_l4.json()["negative_cases_covered_count"] == 1

            # 5. Re-run explicit /sync -> MUST NOT overwrite customized name/priority/assertions
            sync_res = await client.post(f"/api/v1/projects/{pid}/scenarios/sync")
            assert sync_res.status_code == 200, sync_res.text
            sync_body = sync_res.json()
            assert sync_body["created_count"] == 0
            # P0+P1 total = 3 (order P0=L4, dash P1=L2, refund P1=L0) -> 1/3 effective (33.33%)
            assert sync_body["p0_p1_total"] == 3
            assert sync_body["p0_p1_effective_count"] == 1
            assert sync_body["p0_p1_effective_pct"] == 33.33
            # Sorted P0 first, then P1 natural slug order
            assert [s["slug"] for s in sync_body["scenarios"]] == [
                "02_order_checkout",
                "01_dashboard_overview",
                "03_refund_flow",
            ]
            assert sync_body["scenarios"][0]["name"] == "Core Order Checkout"
            assert sync_body["scenarios"][1]["name"] == "Executive Dashboard KPIs"

            # 6. Query filters (priority, effectiveness_level, business_domain, q)
            f_p0 = (
                await client.get(f"/api/v1/projects/{pid}/scenarios", params={"priority": "P0"})
            ).json()
            assert [s["slug"] for s in f_p0["scenarios"]] == ["02_order_checkout"]
            assert f_p0["total"] == 1

            f_l4 = (
                await client.get(
                    f"/api/v1/projects/{pid}/scenarios", params={"effectiveness_level": "L4"}
                )
            ).json()
            assert [s["slug"] for s in f_l4["scenarios"]] == ["02_order_checkout"]

            f_q = (
                await client.get(f"/api/v1/projects/{pid}/scenarios", params={"q": "Executive"})
            ).json()
            assert [s["slug"] for s in f_q["scenarios"]] == ["01_dashboard_overview"]

            # 7. Detail endpoint includes linked endpoints & runs
            det_res = await client.get(f"/api/v1/projects/{pid}/scenarios/{sc_order['id']}")
            assert det_res.status_code == 200, det_res.text
            det = det_res.json()
            assert len(det["endpoints"]) == 2
            assert len(det["runs"]) == 1
            assert det["runs"][0]["run_id"] == r2["id"]

            # 8. Inventory includes scenario_meta enrichment
            inv_res = await client.get(f"/api/v1/projects/{pid}/inventory")
            assert inv_res.status_code == 200, inv_res.text
            inv = inv_res.json()
            smeta = inv.get("scenario_meta") or {}
            assert "02_order_checkout" in smeta
            assert smeta["02_order_checkout"]["name"] == "Core Order Checkout"
            assert smeta["02_order_checkout"]["priority"] == "P0"
            assert smeta["02_order_checkout"]["business_domain"] == "Orders"
            assert smeta["02_order_checkout"]["effectiveness_level"] == "L4"

            # 9. Delete manual scenario
            del_res = await client.delete(f"/api/v1/projects/{pid}/scenarios/{sc_refund['id']}")
            assert del_res.status_code == 200
            assert del_res.json()["deleted"] is True


def test_java_generator_compiles_scenario_business_assertions(tmp_path: Path) -> None:
    """Verify L1 and L2 generated Java tests embed real RestAssured business assertions."""
    openapi_path = tmp_path / "openapi.yaml"
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Order Service API", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.test"}],
        "paths": {
            "/api/v1/orders": {
                "get": {
                    "summary": "List orders",
                    "x-discovery-source": "har-live",
                    "x-scenarios": ["01_orders"],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"},
                                    "example": {"code": 0, "data": {"total": 5, "items": []}},
                                }
                            },
                        }
                    },
                },
                "post": {
                    "summary": "Create order",
                    "x-discovery-source": "har-live",
                    "x-scenarios": ["01_orders"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "example": {"sku": "SKU-100", "qty": 2},
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "code": 0,
                                        "data": {"id": "ORD-99", "status": "CONFIRMED"},
                                    }
                                }
                            },
                        }
                    },
                },
            }
        },
    }
    openapi_path.write_text(yaml.safe_dump(spec), encoding="utf-8")

    corr_path = tmp_path / "correlation.json"
    corr_path.write_text(
        json.dumps(
            {
                "variables": [],
                "chains": [
                    {
                        "name": "order_lifecycle",
                        "scenario": "01_orders",
                        "steps": [
                            {
                                "step": 1,
                                "scenario": "01_orders",
                                "method": "POST",
                                "path": "/api/v1/orders",
                                "raw_url": "https://api.example.test/api/v1/orders",
                                "request_headers": {},
                                "query_params": {},
                                "request_body": {"sku": "SKU-100", "qty": 2},
                                "expected_status": 201,
                                "extracts": {"orderId": "$.data.id"},
                                "uses": {},
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assertions = [
        {
            "id": "a1",
            "priority": "P0",
            "title": "Orders list total must be positive",
            "target_endpoint": "GET /api/v1/orders",
            "jsonpath": "$.data.total",
            "operator": "gt",
            "expected": "0",
        },
        {
            "id": "a2",
            "priority": "P1",
            "title": "Orders array must not be null",
            "target_endpoint": "/orders",
            "jsonpath": "$.data.items",
            "operator": "not_empty",
        },
        {
            "id": "a3",
            "priority": "P0",
            "title": "Created order status must equal CONFIRMED",
            "target_endpoint": "POST /api/v1/orders",
            "jsonpath": "$.data.status",
            "operator": "equals",
            "expected": "CONFIRMED",
        },
        {
            "id": "a4",
            "priority": "P1",
            "title": "Order ID contains ORD prefix",
            "target_endpoint": "POST /api/v1/orders",
            "jsonpath": "$.data.id",
            "operator": "contains",
            "expected": "ORD-",
        },
        {
            "id": "a5",
            "priority": "P2",
            "title": "Inventory deduction reconciled asynchronously",
            "target_endpoint": "POST /api/v1/orders",
            "operator": "custom",
            "note": "Check warehouse stock event within 5s",
        },
    ]

    out_dir = tmp_path / "java-tests"
    res = generate_java_project(
        openapi_path,
        out_dir,
        correlation_path=corr_path,
        package_name="com.reconqa.generated",
        scenario_assertions=assertions,
    )
    assert res["business_assertions_compiled"] >= 5

    l1_java = (
        out_dir
        / "src"
        / "test"
        / "java"
        / "com"
        / "reconqa"
        / "generated"
        / "smoke"
        / "L1SmokeContractTest.java"
    ).read_text(encoding="utf-8")
    assert "// Business Assertion [P0]: Orders list total must be positive" in l1_java
    assert 'response.then().assertThat().body("data.total", Matchers.greaterThan(0));' in l1_java
    assert "// Business Assertion [P1]: Orders array must not be null" in l1_java
    assert 'response.then().assertThat().body("data.items", Matchers.notNullValue());' in l1_java

    l2_java = (
        out_dir
        / "src"
        / "test"
        / "java"
        / "com"
        / "reconqa"
        / "generated"
        / "chains"
        / "L2OrderLifecycleTest.java"
    ).read_text(encoding="utf-8")
    assert "import org.hamcrest.Matchers;" in l2_java
    assert "// Business Assertion [P0]: Created order status must equal CONFIRMED" in l2_java
    assert 'response.then().assertThat().body("data.status", Matchers.equalTo("CONFIRMED"));' in l2_java
    assert 'Matchers.containsString("ORD-")' in l2_java
    assert "// TODO Business Rule: Check warehouse stock event within 5s" in l2_java

    # Also verify JavaProjectGenerator facade works identically
    out_dir2 = tmp_path / "java-tests-2"
    gen = JavaProjectGenerator(
        package_name="com.reconqa.generated",
        scenario_assertions=assertions,
    )
    res2 = gen.generate(openapi_path, out_dir2, correlation_path=corr_path)
    assert res2["business_assertions_compiled"] == res["business_assertions_compiled"]
