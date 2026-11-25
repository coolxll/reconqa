"""Tests for R4 Standalone Acceptance Report (HTML/MD/JSON) & Postman Collection v2.1 Export."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.models.tables import Finding, Run, ScenarioModel
from reconqa.modules.hub.acceptance_report import (
    build_acceptance_report_data,
    render_acceptance_report_html,
    render_acceptance_report_markdown,
)
from reconqa.modules.hub.postman_exporter import (
    POSTMAN_SCHEMA_V2_1,
    _compile_assertion_js,
    export_postman_collection,
)


def _setup_isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_name: str) -> Path:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / db_name
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    return ws_dir


def test_acceptance_report_builders_and_xss_safety() -> None:
    """Verify 5-pillar aggregation, Markdown/HTML rendering, inline CSS, and strict XSS escaping."""
    xss_script = "<script>alert(1)</script>"
    xss_img = '<img src=x onerror=alert(1)>'
    xss_attr = '" onclick="alert(2)'

    inventory = {
        "total_endpoints": 10,
        "har_live_count": 7,
        "js_static_count": 3,
        "coverage_pct": 70.0,
        "runs_merged_count": 2,
        "explore_sessions_count": 1,
        "by_prefix": [
            {"prefix": f"/api/v1/orders{xss_script}", "total": 6, "covered": 5, "coverage_pct": 83.33},
            {"prefix": "/api/v1/users", "total": 4, "covered": 2, "coverage_pct": 50.0},
        ],
    }
    capability_map = {
        "metrics": {
            "total_capabilities": 2,
            "core_capabilities": 1,
            "live_capabilities": 2,
            "capability_coverage_pct": 80.0,
            "state_coverage_pct": 50.0,
            "role_matrix_coverage_pct": 75.0,
            "negative_coverage_pct": 60.0,
            "slots_passed": 4,
            "slots_failed": 1,
            "slots_pending": 2,
            "slots_missing": 3,
        },
        "capabilities": [
            {
                "key": "orders.create",
                "display_name": f"Create Order {xss_img}",
                "recommended_priority": "P0",
                "is_core": True,
                "total_endpoints": 3,
                "live_endpoints": 3,
                "roles": ["buyer", f"admin{xss_script}"],
                "case_coverage_pct": 80.0,
                "case_matrix": [
                    {"slot": "happy_path", "status": "covered", "count": 2},
                    {"slot": "invalid_params", "status": "covered", "count": 1},
                    {"slot": "illegal_state", "status": "failed", "count": 1},
                    {"slot": "authz_role", "status": "pending", "count": 1},
                    {"slot": "consistency_idempotency", "status": "missing", "count": 0},
                ],
            }
        ],
    }
    scenario_list = {
        "total": 2,
        "by_priority": {"P0": 1, "P1": 1, "P2": 0, "P3": 0},
        "by_effectiveness": {"L0": 0, "L1": 0, "L2": 0, "L3": 1, "L4": 1},
        "p0_p1_total": 2,
        "p0_p1_effective_count": 2,
        "p0_p1_effective_pct": 100.0,
        "scenarios": [
            {
                "id": "sc1",
                "slug": "01_checkout",
                "name": f"Checkout {xss_script}",
                "priority": "P0",
                "status": "verified",
                "effectiveness_level": "L4",
                "endpoint_count": 3,
                "run_count": 2,
                "assertion_count": 2,
                "assertions_passed_count": 2,
                "negative_case_count": 1,
                "negative_cases_covered_count": 1,
                "assertions": [
                    {
                        "id": "a1",
                        "title": f"Order ID present {xss_img}",
                        "target_endpoint": "POST /api/v1/orders",
                        "operator": "not_empty",
                        "expected": "",
                        "status": "passed",
                    }
                ],
            },
            {
                "id": "sc2",
                "slug": "02_refund",
                "name": "Refund Flow",
                "priority": "P1",
                "status": "active",
                "effectiveness_level": "L3",
                "endpoint_count": 2,
                "run_count": 1,
                "assertion_count": 1,
                "assertions_passed_count": 0,
                "negative_case_count": 1,
                "negative_cases_covered_count": 0,
                "assertions": [
                    {
                        "id": "a2",
                        "title": "Refund status",
                        "target_endpoint": "POST /api/v1/refunds",
                        "operator": "equals",
                        "expected": "refunded",
                        "status": "failed",
                    }
                ],
            },
        ],
    }
    chain_runs = [
        {
            "chain_run_id": "cr_1",
            "chain_name": f"Order Lifecycle {xss_attr}",
            "run_id": "run_1",
            "status": "failed",
            "total_steps": 2,
            "completed_steps": 1,
            "step_results": [
                {
                    "step_index": 0,
                    "step": 1,
                    "name": "Create Order",
                    "method": "POST",
                    "url": f"https://api.example.test/orders?q={xss_script}",
                    "status": "passed",
                    "status_code": 201,
                    "duration_ms": 42.5,
                    "assertions": [
                        {
                            "id": "a1",
                            "title": f"Verify ID {xss_script}",
                            "operator": "not_empty",
                            "passed": True,
                            "actual": f"ord_123{xss_img}",
                            "expected": "",
                            "message": f"ok {xss_attr}",
                        }
                    ],
                },
                {
                    "step_index": 1,
                    "step": 2,
                    "name": "Get Order",
                    "method": "GET",
                    "url": "https://api.example.test/orders/ord_123",
                    "status": "failed",
                    "status_code": 500,
                    "duration_ms": 120.0,
                    "error": f"Server error {xss_script}",
                    "assertions": [
                        {
                            "id": "a2",
                            "title": "Status 200",
                            "operator": "status_code",
                            "passed": False,
                            "actual": 500,
                            "expected": f"200 {xss_img}",
                            "message": "Expected 200",
                        }
                    ],
                },
            ],
        }
    ]
    findings = [
        {
            "id": "f_sec_crit",
            "category": "idor",
            "severity": "critical",
            "endpoint": f"/api/v1/orders/{{id}}{xss_script}",
            "title": f"Cross-tenant IDOR {xss_img}",
            "evidence": {"detail": f"leaked {xss_script}"},
        },
        {
            "id": "f_sec_low",
            "category": "security_header",
            "severity": "low",
            "endpoint": "/api/v1/ping",
            "title": "Missing X-Frame-Options",
            "evidence": {},
        },
        {
            "id": "f_perf_high",
            "category": "perf_slow_ttfb",
            "severity": "high",
            "endpoint": "/api/v1/orders/search",
            "title": f"Slow TTFB 2100ms {xss_script}",
            "evidence": {"ttfb_ms": 2100},
        },
        {
            "id": "f_perf_med",
            "category": "perf_large_body",
            "severity": "medium",
            "endpoint": "/api/v1/catalog",
            "title": "Medium payload",
            "evidence": {},
        },
    ]
    har_perf = {
        "summary": {
            "total_requests": 42,
            "avg_ttfb_ms": 180.5,
            "p95_ttfb_ms": 1950.0,
            "p95_total_ms": 2100.0,
            "large_body_count": 1,
        },
        "slowest_ttfb": [{"method": "GET", "path": "/api/v1/orders/search", "ttfb_ms": 2100.0}],
    }
    k6_analysis = {
        "status": "threshold_breached",
        "metrics": {"rps": 115.2, "http_req_failed_rate": 0.02, "latency_ms": {"p95": 840.0}},
        "diagnosis": {"notes": [f"Latency spike {xss_script}"]},
    }

    report = build_acceptance_report_data(
        project_id="proj_1",
        project_name=f"Acme Shop {xss_script}",
        project_slug="acme-shop",
        run_id="run_1",
        inventory=inventory,
        capability_map=capability_map,
        scenario_list=scenario_list,
        chain_runs=chain_runs,
        findings=findings,
        har_perf_report=har_perf,
        k6_analysis=k6_analysis,
        generated_at="2026-11-25T05:00:00Z",
    )

    assert report.summary.overall_status == "failed"
    assert report.api_coverage.coverage_pct == 70.0
    assert report.scenarios.p0_p1_total == 2
    assert report.scenarios.p0_p1_effective_pct == 100.0
    assert report.scenarios.total_assertions == 3
    assert report.scenarios.assertions_passed == 2
    assert report.scenarios.assertions_failed == 1
    assert report.chain_evidence.total_chain_runs == 1
    assert report.chain_evidence.failed_chains == 1
    assert report.chain_evidence.total_assertions_evaluated == 2
    assert report.chain_evidence.assertions_passed == 1
    assert report.chain_evidence.assertions_failed == 1
    assert report.security_findings.total_count == 2
    assert report.security_findings.high_critical_count == 1
    assert report.security_findings.high_critical_items[0].id == "f_sec_crit"
    assert report.performance.total_count == 2
    assert report.performance.high_critical_count == 1
    assert report.performance.high_critical_items[0].id == "f_perf_high"

    md_out = render_acceptance_report_markdown(report)
    assert "# Acceptance Report:" in md_out
    assert "## 1. API Coverage" in md_out
    assert "## 2. Capability Map & 5-Defense Matrix" in md_out
    assert "## 3. P0/P1 L0->L4 Scenario Statistics" in md_out
    assert "## 4. Chain Assertion Execution Evidence" in md_out
    assert "## 5. High & Critical Security and Performance Findings" in md_out
    assert "70.00%" in md_out
    assert "Happy Path" in md_out and "Consistency / Idempotency" in md_out
    assert "Cross-tenant IDOR" in md_out
    assert "Missing X-Frame-Options" not in md_out
    assert "Slow TTFB 2100ms" in md_out
    assert "Medium payload" not in md_out

    # Also verify dict input works identically
    assert render_acceptance_report_markdown(report.model_dump(mode="json")) == md_out

    html_out = render_acceptance_report_html(report)
    assert html_out.startswith("<!DOCTYPE html>")
    assert "<style>" in html_out and "</style>" in html_out
    # Zero external CDN tags or @import
    assert re.search(r"<script\b", html_out, re.IGNORECASE) is None
    assert re.search(r"<link\b", html_out, re.IGNORECASE) is None
    assert "@import" not in html_out.lower()
    # Zero raw XSS payloads anywhere in HTML
    assert xss_script not in html_out
    assert xss_img not in html_out
    assert xss_attr not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_out
    assert "1. API Coverage" in html_out
    assert "2. Capability Map" in html_out
    assert "3. P0/P1 L0-&gt;L4 Scenario Statistics" in html_out
    assert "4. Chain Assertion Execution Evidence" in html_out
    assert "5. High &amp; Critical Security and Performance Findings" in html_out


@pytest.mark.asyncio
async def test_acceptance_report_http_endpoints_and_run_scoping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify GET /projects/{id}/acceptance-report HTML/MD/JSON downloads, run_id scoping, and 400/404s."""
    ws_dir = _setup_isolated_env(tmp_path, monkeypatch, "accept_report_http.db")

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            proj = (
                await client.post("/api/v1/projects", json={"name": "Report Demo <script>alert(1)</script>"})
            ).json()
            other_proj = (await client.post("/api/v1/projects", json={"name": "Other Proj"})).json()
            pid = proj["id"]

            r1 = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Run 1", "modules": ["merge"]},
                )
            ).json()
            r2 = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Run 2", "modules": ["merge"]},
                )
            ).json()
            r_other = (
                await client.post(
                    f"/api/v1/projects/{other_proj['id']}/runs?auto_start=false",
                    json={"name": "Other Run", "modules": ["merge"]},
                )
            ).json()

            # Write OpenAPI + coverage + perf artifacts into Run 1
            r1_ws = Path(r1["workspace_dir"])
            (r1_ws / "hub").mkdir(parents=True, exist_ok=True)
            (r1_ws / "hub" / "openapi.json").write_text(
                json.dumps(
                    {
                        "openapi": "3.0.3",
                        "info": {"title": "Demo", "version": "1.0"},
                        "servers": [{"url": "https://api.demo.test"}],
                        "paths": {
                            "/api/v1/orders": {
                                "post": {
                                    "summary": "Create order",
                                    "x-discovery-source": "har-live",
                                    "x-scenarios": ["01_checkout"],
                                    "responses": {"201": {"description": "Created"}},
                                }
                            },
                            "/api/v1/orders/{id}": {
                                "get": {
                                    "summary": "Get order",
                                    "x-discovery-source": "js-static",
                                    "responses": {"200": {"description": "OK"}},
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (r1_ws / "performance" / "k6").mkdir(parents=True, exist_ok=True)
            (r1_ws / "performance" / "har_perf_report.json").write_text(
                json.dumps(
                    {
                        "summary": {
                            "total_requests": 15,
                            "avg_ttfb_ms": 110.0,
                            "p95_ttfb_ms": 450.0,
                            "p95_total_ms": 510.0,
                            "large_body_count": 0,
                        },
                        "slowest_ttfb": [],
                    }
                ),
                encoding="utf-8",
            )
            (r1_ws / "performance" / "k6" / "k6_analysis.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "metrics": {"rps": 250.0, "http_req_failed_rate": 0.0, "latency_ms": {"p95": 120.0}},
                        "diagnosis": {"notes": ["All thresholds passed"]},
                    }
                ),
                encoding="utf-8",
            )

            # Seed checkpoints for r1 and r2
            cr_dir = ws_dir / "chain_runs"
            cr_dir.mkdir(parents=True, exist_ok=True)
            (cr_dir / "cr_run1.json").write_text(
                json.dumps(
                    {
                        "chain_run_id": "cr_run1",
                        "project_id": pid,
                        "run_id": r1["id"],
                        "chain_name": "Checkout Chain",
                        "status": "passed",
                        "total_steps": 1,
                        "completed_steps": 1,
                        "step_results": [
                            {
                                "step_index": 0,
                                "step": 1,
                                "name": "Create Order",
                                "method": "POST",
                                "url": "https://api.demo.test/api/v1/orders",
                                "status": "passed",
                                "status_code": 201,
                                "duration_ms": 33.2,
                                "assertions": [
                                    {
                                        "id": "a_chk",
                                        "title": "Order created",
                                        "operator": "status_code",
                                        "passed": True,
                                        "actual": 201,
                                        "expected": "201",
                                        "message": "ok",
                                    }
                                ],
                            }
                        ],
                        "created_at": "2026-11-25T04:00:00+00:00",
                        "updated_at": "2026-11-25T04:00:01+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (cr_dir / "cr_run2.json").write_text(
                json.dumps(
                    {
                        "chain_run_id": "cr_run2",
                        "project_id": pid,
                        "run_id": r2["id"],
                        "chain_name": "Run2 Only Chain",
                        "status": "failed",
                        "total_steps": 1,
                        "completed_steps": 0,
                        "step_results": [],
                        "created_at": "2026-11-25T04:05:00+00:00",
                        "updated_at": "2026-11-25T04:05:01+00:00",
                    }
                ),
                encoding="utf-8",
            )

            # Mark runs completed & seed Findings + ScenarioModel in DB
            session_factory = db_mod.get_session_factory()
            async with session_factory() as session:
                db_r1 = await session.get(Run, r1["id"])
                db_r2 = await session.get(Run, r2["id"])
                assert db_r1 is not None and db_r2 is not None
                db_r1.status = "completed"
                db_r1.started_at = datetime(2026, 11, 25, 4, 0, tzinfo=UTC)
                db_r1.finished_at = datetime(2026, 11, 25, 4, 1, tzinfo=UTC)
                db_r2.status = "completed"
                db_r2.started_at = datetime(2026, 11, 25, 4, 5, tzinfo=UTC)
                db_r2.finished_at = datetime(2026, 11, 25, 4, 6, tzinfo=UTC)

                session.add_all(
                    [
                        Finding(
                            run_id=r1["id"],
                            category="auth_bypass",
                            severity="high",
                            endpoint="/api/v1/orders",
                            title="Run1 Auth Bypass",
                            evidence={"status": 200},
                        ),
                        Finding(
                            run_id=r1["id"],
                            category="perf_slow_ttfb",
                            severity="high",
                            endpoint="/api/v1/orders",
                            title="Run1 Slow Endpoint",
                            evidence={"ttfb_ms": 1800},
                        ),
                        Finding(
                            run_id=r2["id"],
                            category="idor",
                            severity="critical",
                            endpoint="/api/v1/users",
                            title="Run2 Critical IDOR",
                            evidence={},
                        ),
                        ScenarioModel(
                            project_id=pid,
                            slug="01_checkout",
                            name="01 Checkout",
                            priority="P0",
                            status="verified",
                            assertions_json=json.dumps(
                                [
                                    {
                                        "id": "a_chk",
                                        "title": "Order created",
                                        "target_endpoint": "POST /api/v1/orders",
                                        "operator": "status_code",
                                        "expected": "201",
                                        "status": "passed",
                                    }
                                ]
                            ),
                            negative_cases_json=json.dumps(
                                [
                                    {
                                        "id": "nc_1",
                                        "title": "Unauth order rejected",
                                        "category": "auth",
                                        "status": "covered",
                                    }
                                ]
                            ),
                        ),
                    ]
                )
                await session.commit()

            # 1. Default HTML download
            res_html = await client.get(f"/api/v1/projects/{pid}/acceptance-report")
            assert res_html.status_code == 200
            assert "text/html" in res_html.headers["content-type"]
            assert "attachment" in res_html.headers["content-disposition"]
            assert ".html" in res_html.headers["content-disposition"]
            assert "<script>alert(1)</script>" not in res_html.text
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in res_html.text
            assert "1. API Coverage" in res_html.text

            # 2. Markdown download (format=markdown and format=md)
            res_md = await client.get(f"/api/v1/projects/{pid}/acceptance-report?format=markdown")
            assert res_md.status_code == 200
            assert "text/markdown" in res_md.headers["content-type"]
            assert "attachment" in res_md.headers["content-disposition"]
            assert ".md" in res_md.headers["content-disposition"]
            assert "# Acceptance Report:" in res_md.text
            assert "01 Checkout" in res_md.text

            res_md_alias = await client.get(f"/api/v1/projects/{pid}/acceptance-report?format=md")
            assert res_md_alias.status_code == 200
            assert "text/markdown" in res_md_alias.headers["content-type"]
            assert "# Acceptance Report:" in res_md_alias.text
            assert "01 Checkout" in res_md_alias.text

            # 3. JSON with run_id scoping (run_1 vs latest run_2)
            res_r1 = await client.get(
                f"/api/v1/projects/{pid}/acceptance-report?format=json&run_id={r1['id']}"
            )
            assert res_r1.status_code == 200
            j1 = res_r1.json()
            assert j1["run_id"] == r1["id"]
            assert j1["api_coverage"]["total_endpoints"] == 2
            assert j1["api_coverage"]["har_live_count"] == 1
            assert j1["api_coverage"]["coverage_pct"] == 50.0
            assert j1["scenarios"]["p0_p1_total"] == 1
            assert j1["scenarios"]["p0_p1_effective_count"] == 1
            assert j1["scenarios"]["p0_p1_effective_pct"] == 100.0
            assert len(j1["chain_evidence"]["chains"]) == 1
            assert j1["chain_evidence"]["chains"][0]["chain_run_id"] == "cr_run1"
            assert j1["security_findings"]["high_critical_items"][0]["title"] == "Run1 Auth Bypass"
            assert j1["performance"]["high_critical_items"][0]["title"] == "Run1 Slow Endpoint"
            assert j1["performance"]["har_perf_summary"]["total_requests"] == 15
            assert j1["performance"]["k6_analysis"]["metrics"]["rps"] == 250.0

            # Without run_id, latest completed run is r2, while all project chains are included
            res_all = await client.get(f"/api/v1/projects/{pid}/acceptance-report?format=json")
            assert res_all.status_code == 200
            j_all = res_all.json()
            assert j_all["run_id"] == r2["id"]
            assert j_all["chain_evidence"]["total_chain_runs"] == 2
            assert j_all["security_findings"]["high_critical_items"][0]["title"] == "Run2 Critical IDOR"

            # 4. Error handling: 400 invalid format, 404 unknown project/run/cross-project run
            bad_fmt = await client.get(f"/api/v1/projects/{pid}/acceptance-report?format=pdf")
            assert bad_fmt.status_code == 400

            bad_proj = await client.get("/api/v1/projects/nonexistent/acceptance-report")
            assert bad_proj.status_code == 404

            bad_run = await client.get(
                f"/api/v1/projects/{pid}/acceptance-report?run_id=does-not-exist"
            )
            assert bad_run.status_code == 404

            cross_run = await client.get(
                f"/api/v1/projects/{pid}/acceptance-report?run_id={r_other['id']}"
            )
            assert cross_run.status_code == 404

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


def test_postman_exporter_structure_chains_scripts_and_secret_redaction() -> None:
    """Verify Postman v2.1 schema, GraphQL #op wire stripping, chain scripts, and secret redaction."""
    inventory: dict[str, Any] = {
        "project_id": "proj_pm",
        "project_name": "Shop API",
        "endpoints": [
            {
                "method": "GET",
                "path": "/api/v1/orders/{id}",
                "summary": "Get order by ID",
                "tags": ["Orders"],
                "custom_headers": {
                    ":method": "GET",
                    "Authorization": "Bearer raw-jwt-secret-123",
                    "Cookie": "sid=raw-cookie-val",
                    "X-API-Key": "sk-live-raw",
                    "X-Client": "web",
                },
                "parameters": [
                    {"name": "token", "in": "query", "required": True, "example": "raw-q-tok"},
                    {"name": "expand", "in": "query", "required": False, "example": "items"},
                ],
                "status_codes": [200],
                "response_example": {
                    "id": "ord_1",
                    "api_secret": "raw-resp-secret",
                },
            },
            {
                "method": "POST",
                "path": "/graphql#GetOrderList",
                "summary": "GraphQL GetOrderList",
                "tags": [],
                "custom_headers": {},
                "parameters": [],
                "status_codes": [200],
                "request_example": {
                    "operationName": "GetOrderList",
                    "query": "query GetOrderList { orders { id } }",
                },
            },
        ],
    }
    openapi_doc = {"servers": [{"url": "https://admin:rawpass@api.example.test"}]}
    chains = [
        {
            "name": "Order Create & Cleanup",
            "scenario": "01_orders",
            "steps": [
                {
                    "step": 1,
                    "method": "POST",
                    "path": "/api/v1/orders",
                    "raw_url": "https://admin:rawpass@api.example.test/api/v1/orders?token=raw-q-tok",
                    "request_headers": {
                        "Authorization": "Bearer raw-jwt-secret-123",
                        "X-Custom": "ok",
                    },
                    "query_params": {},
                    "request_body": {
                        "password": "raw-password-val",
                        "item": "sku_1",
                    },
                    "expected_status": 201,
                    "extracts": {"order_id": "$.data.order_id"},
                    "uses": {},
                },
                {
                    "step": 2,
                    "method": "GET",
                    "path": "/api/v1/orders/{id}",
                    "raw_url": "https://api.example.test/api/v1/orders/999",
                    "request_headers": {},
                    "query_params": {"page": "1"},
                    "request_body": None,
                    "expected_status": 200,
                    "extracts": {},
                    "uses": {
                        "path:id": "{{order_id}}",
                        "header:X-Trace": "{{trace_id}}",
                    },
                },
            ],
            "cleanup_step": {
                "step": 3,
                "method": "DELETE",
                "path": "/api/v1/orders/{id}",
                "raw_url": "https://api.example.test/api/v1/orders/999",
                "request_headers": {},
                "query_params": {},
                "request_body": {"order_id": "{{order_id}}", "meta": {"token": "{{step_token}}"}},
                "expected_status": 204,
                "extracts": {},
                "uses": {"path:id": "{{order_id}}"},
            },
        }
    ]
    scenario_assertions = [
        {
            "id": "sa_1",
            "title": 'Check "id" > 0\nand valid',
            "target_endpoint": "POST /api/v1/orders",
            "jsonpath": "$.data.count",
            "operator": "gt",
            "expected": "0",
        },
        {
            "id": "sa_2",
            "title": "Latency under 500ms",
            "target_endpoint": "POST /api/v1/orders",
            "jsonpath": "",
            "operator": "latency_lt",
            "expected": "500",
        },
        {
            "id": "sa_3",
            "title": "SQL row exists",
            "target_endpoint": "POST /api/v1/orders",
            "jsonpath": "",
            "operator": "sql_exists",
            "expected": "SELECT 1",
        },
        {
            "id": "sa_4",
            "title": "Order path matches",
            "target_endpoint": "GET /api/v1/orders/{id}",
            "jsonpath": "$.status",
            "operator": "equals",
            "expected": "paid",
        },
    ]

    collection = export_postman_collection(
        project_id="proj_pm",
        project_name="Shop API",
        inventory=inventory,
        openapi_doc=openapi_doc,
        chains=chains,
        scenario_assertions=scenario_assertions,
    )

    assert collection["info"]["schema"] == POSTMAN_SCHEMA_V2_1
    assert collection["info"]["_postman_id"]
    var_map = {v["key"]: v["value"] for v in collection["variable"]}
    assert var_map["baseUrl"] == "https://api.example.test"
    assert "authToken" in var_map
    assert "authCookie" in var_map
    assert "apiKey" in var_map
    assert "order_id" in var_map
    assert "trace_id" in var_map
    assert "step_token" in var_map

    top_folders = {f["name"]: f["item"] for f in collection["item"]}
    assert "Asset Library Endpoints" in top_folders
    assert "Correlation Chains" in top_folders

    # Verify Asset Library Endpoints
    asset_groups = {g["name"]: g["item"] for g in top_folders["Asset Library Endpoints"]}
    assert "Orders" in asset_groups
    orders_item = asset_groups["Orders"][0]
    assert orders_item["request"]["url"]["path"] == ["api", "v1", "orders", ":id"]
    assert orders_item["request"]["url"]["variable"] == [{"key": "id", "value": ""}]
    hdrs = {h["key"]: h["value"] for h in orders_item["request"]["header"]}
    assert ":method" not in hdrs
    assert hdrs["Authorization"] == "Bearer {{authToken}}"
    assert hdrs["Cookie"] == "{{authCookie}}"
    assert hdrs["X-API-Key"] == "{{apiKey}}"
    assert hdrs["X-Client"] == "web"

    # GraphQL virtual endpoint: keeps #GetOrderList in item name, strips # on wire URL
    gql_group = asset_groups["/graphql"]
    gql_item = gql_group[0]
    assert "#GetOrderList" in gql_item["name"]
    assert gql_item["request"]["url"]["raw"] == "{{baseUrl}}/graphql"
    assert gql_item["request"]["url"]["path"] == ["graphql"]

    # Verify Correlation Chains
    chain_folder = top_folders["Correlation Chains"][0]
    assert chain_folder["name"] == "Order Create & Cleanup"
    assert len(chain_folder["item"]) == 3  # 2 steps + cleanup_step

    step1, step2, cleanup = chain_folder["item"]
    step1_js = "\n".join(step1["event"][0]["script"]["exec"])
    assert 'pm.collectionVariables.set("order_id"' in step1_js
    assert "pm.response.to.have.status(201)" in step1_js
    assert "pm.test(" in step1_js
    assert "// Skipped server-only assertion (sql_exists):" in step1_js

    assert "{{order_id}}" in step2["request"]["url"]["raw"]
    step2_hdrs = {h["key"]: h["value"] for h in step2["request"]["header"]}
    assert step2_hdrs["X-Trace"] == "{{trace_id}}"
    step2_js = "\n".join(step2["event"][0]["script"]["exec"])
    assert 'pm.expect(String(_val)).to.eql("paid")' in step2_js

    assert "Cleanup" in cleanup["name"]
    cleanup_body = json.loads(cleanup["request"]["body"]["raw"])
    assert cleanup_body["order_id"] == "{{order_id}}"
    assert cleanup_body["meta"]["token"] == "{{step_token}}"

    # Zero literal secret leakage anywhere in serialized collection
    serialized = json.dumps(collection, ensure_ascii=False)
    for leaked in (
        "raw-jwt-secret-123",
        "raw-cookie-val",
        "sk-live-raw",
        "raw-q-tok",
        "rawpass",
        "raw-password-val",
        "raw-resp-secret",
    ):
        assert leaked not in serialized


@pytest.mark.asyncio
async def test_postman_export_http_endpoint_and_edge_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify GET /projects/{id}/export/postman for empty/populated projects, run_id, and 404s."""
    _setup_isolated_env(tmp_path, monkeypatch, "postman_http.db")

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            proj = (await client.post("/api/v1/projects", json={"name": "Postman Demo"})).json()
            other_proj = (await client.post("/api/v1/projects", json={"name": "Other"})).json()
            pid = proj["id"]

            # 1. Empty project export still returns valid Postman v2.1 JSON
            empty_res = await client.get(f"/api/v1/projects/{pid}/export/postman")
            assert empty_res.status_code == 200
            assert "application/json" in empty_res.headers["content-type"]
            assert "attachment" in empty_res.headers["content-disposition"]
            assert "-postman-collection.json" in empty_res.headers["content-disposition"]
            empty_col = empty_res.json()
            assert empty_col["info"]["schema"] == POSTMAN_SCHEMA_V2_1
            assert len(empty_col["item"]) == 2

            # 2. Seed Target + completed Run with openapi.json & correlation.json + ScenarioModel
            await client.post(
                f"/api/v1/projects/{pid}/targets",
                json={"root_domain": "shop.local", "base_url": "https://api.shop.local"},
            )
            r1 = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"name": "Run 1", "modules": ["merge"]},
                )
            ).json()
            r_other = (
                await client.post(
                    f"/api/v1/projects/{other_proj['id']}/runs?auto_start=false",
                    json={"name": "Other Run", "modules": ["merge"]},
                )
            ).json()

            hub_dir = Path(r1["workspace_dir"]) / "hub"
            hub_dir.mkdir(parents=True, exist_ok=True)
            (hub_dir / "openapi.json").write_text(
                json.dumps(
                    {
                        "openapi": "3.0.3",
                        "info": {"title": "Shop", "version": "1.0"},
                        "servers": [{"url": "https://api.shop.local"}],
                        "paths": {
                            "/api/v1/carts": {
                                "post": {
                                    "summary": "Create cart",
                                    "x-reconqa-source": "har-live",
                                    "responses": {"201": {"description": "Created"}},
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (hub_dir / "correlation.json").write_text(
                json.dumps(
                    {
                        "chains": [
                            {
                                "name": "Cart Flow",
                                "scenario": "cart",
                                "steps": [
                                    {
                                        "step": 1,
                                        "method": "POST",
                                        "path": "/api/v1/carts",
                                        "raw_url": "https://api.shop.local/api/v1/carts",
                                        "request_headers": {"Authorization": "Bearer secret-tok-999"},
                                        "query_params": {},
                                        "request_body": {"sku": "abc"},
                                        "expected_status": 201,
                                        "extracts": {"cart_id": "$.id"},
                                        "uses": {},
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            session_factory = db_mod.get_session_factory()
            async with session_factory() as session:
                db_r1 = await session.get(Run, r1["id"])
                assert db_r1 is not None
                db_r1.status = "completed"
                db_r1.started_at = datetime(2026, 11, 25, 4, 0, tzinfo=UTC)
                db_r1.finished_at = datetime(2026, 11, 25, 4, 1, tzinfo=UTC)
                session.add(
                    ScenarioModel(
                        project_id=pid,
                        slug="cart",
                        name="Cart Scenario",
                        priority="P0",
                        status="active",
                        assertions_json=json.dumps(
                            [
                                {
                                    "id": "ca1",
                                    "title": "Cart id returned",
                                    "target_endpoint": "POST /api/v1/carts",
                                    "jsonpath": "$.id",
                                    "operator": "not_empty",
                                    "expected": "",
                                }
                            ]
                        ),
                    )
                )
                await session.commit()

            pop_res = await client.get(
                f"/api/v1/projects/{pid}/export/postman?run_id={r1['id']}"
            )
            assert pop_res.status_code == 200
            pop_col = pop_res.json()
            assert pop_col["info"]["schema"] == POSTMAN_SCHEMA_V2_1
            folders = {f["name"]: f["item"] for f in pop_col["item"]}
            assert len(folders["Asset Library Endpoints"]) == 1
            assert len(folders["Correlation Chains"]) == 1
            chain_step = folders["Correlation Chains"][0]["item"][0]
            js_code = "\n".join(chain_step["event"][0]["script"]["exec"])
            assert 'pm.collectionVariables.set("cart_id"' in js_code
            assert 'pm.test("Cart id returned"' in js_code
            assert "secret-tok-999" not in pop_res.text

            # 404 cases
            assert (await client.get("/api/v1/projects/missing/export/postman")).status_code == 404
            assert (
                await client.get(f"/api/v1/projects/{pid}/export/postman?run_id=missing")
            ).status_code == 404
            assert (
                await client.get(f"/api/v1/projects/{pid}/export/postman?run_id={r_other['id']}")
            ).status_code == 404

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


def test_postman_short_secret_scoping_and_embedded_credential_urls() -> None:
    """Short/common secrets like 'test' must not corrupt pm.test() JS or /test/login URLs."""
    col = export_postman_collection(
        project_id="test",
        project_name="test",
        base_url="https://alice:test@api.example.com",
        inventory={
            "endpoints": [
                {
                    "method": "POST",
                    "path": "/test/login",
                    "custom_headers": {
                        "X-Test-Header": "test-header-val",
                        "X-Secret-Leak": "super-secret-value-99",
                    },
                    "parameters": [
                        {"name": "note", "in": "query", "required": True, "example": "super-secret-value-99"}
                    ],
                    "status_codes": [200],
                    "request_example": {
                        "password": "test",
                        "token": "super-secret-value-99",
                        "callback": "Call https://admin:embeddedPass99@api.example.com/cb",
                    },
                    "response_example": {
                        "msg": "Callback sent to https://svc:otherPass42@hooks.example.com/ok",
                        "leaked": "super-secret-value-99",
                    },
                }
            ]
        },
    )

    var_map = {v["key"]: v["value"] for v in col["variable"]}
    assert var_map["baseUrl"] == "https://api.example.com"

    item = col["item"][0]["item"][0]["item"][0]
    assert item["name"] == "POST /test/login"
    assert item["request"]["url"]["path"] == ["test", "login"]
    assert item["request"]["url"]["raw"] == "{{baseUrl}}/test/login?note=[REDACTED]"

    body = json.loads(item["request"]["body"]["raw"])
    assert body["password"] == "[REDACTED]"
    assert body["token"] == "[REDACTED]"
    assert body["callback"] == "Call https://[REDACTED]:[REDACTED]@api.example.com/cb"
    assert "embeddedPass99" not in json.dumps(col)

    resp_body = json.loads(item["response"][0]["body"])
    assert resp_body["msg"] == "Callback sent to https://[REDACTED]:[REDACTED]@hooks.example.com/ok"
    assert resp_body["leaked"] == "[REDACTED]"
    assert "otherPass42" not in json.dumps(col)
    assert "super-secret-value-99" not in json.dumps(col)

    js_lines = item["event"][0]["script"]["exec"]
    js_code = "\n".join(js_lines)
    assert 'pm.test("Status code is 200"' in js_code
    assert "pm.[REDACTED]" not in js_code

    node_bin = shutil.which("node")
    if node_bin:
        proc = subprocess.run(
            [node_bin, "--check"],
            input=js_code,
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr


def test_postman_nonfinite_numeric_expectations_and_regex_inline_flags() -> None:
    """inf/nan numeric expectations and Python (?ims) regex flags compile to valid Postman JS."""
    operators_nonfinite = [
        ("status_code", "inf"),
        ("status_code", "nan"),
        ("length_eq", "inf"),
        ("length_gt", "-inf"),
        ("gt", "inf"),
        ("gte", "-inf"),
        ("lt", "nan"),
        ("lte", "inf"),
        ("latency_lt", "inf"),
        ("latency_lt", "nan"),
    ]
    all_js_lines: list[str] = []
    for idx, (op, exp) in enumerate(operators_nonfinite):
        lines, _ = _compile_assertion_js(
            {"title": f"Edge {op}\nline2", "operator": op, "jsonpath": "$.x", "expected": exp},
            idx,
        )
        assert len(lines) == 1
        assert lines[0].startswith("// Skipped non-finite numeric expectation")
        assert "\n" not in lines[0]
        all_js_lines.extend(lines)

    regex_lines, needs_helper = _compile_assertion_js(
        {"title": "Case-insensitive ok", "operator": "matches", "jsonpath": "$.status", "expected": "(?i)^ok$"},
        100,
    )
    assert needs_helper is True
    regex_js = "\n".join(regex_lines)
    assert 'new RegExp("^ok$", "i")' in regex_js
    assert "try {" in regex_js
    all_js_lines.extend(regex_lines)

    col = export_postman_collection(
        project_id="p_inf",
        project_name="InfTest",
        inventory={"endpoints": [{"method": "GET", "path": "/items", "status_codes": [200]}]},
        scenario_assertions=[
            {
                "id": "a_inf",
                "title": "Bad gt",
                "target_endpoint": "GET /items",
                "jsonpath": "$.count",
                "operator": "gt",
                "expected": "inf",
            },
            {
                "id": "a_re",
                "title": "CI regex",
                "target_endpoint": "GET /items",
                "jsonpath": "$.state",
                "operator": "matches",
                "expected": "(?im)^ok$",
            },
        ],
    )
    item_js = "\n".join(col["item"][0]["item"][0]["item"][0]["event"][0]["script"]["exec"])
    assert "// Skipped non-finite numeric expectation (gt): Bad gt" in item_js
    assert 'new RegExp("^ok$", "im")' in item_js
    assert "above(inf)" not in item_js

    node_bin = shutil.which("node")
    if node_bin:
        proc = subprocess.run(
            [node_bin, "--check"],
            input=item_js,
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr


def test_acceptance_report_malformed_metrics_and_k6_diagnosis_notes() -> None:
    """Malformed/null metrics & inventory counts do not crash, and string k6 notes are not split by char."""
    report = build_acceptance_report_data(
        project_id="p_bad",
        project_name="Malformed Metrics",
        inventory={
            "total_endpoints": "bad",
            "har_live_count": None,
            "coverage_pct": "nan",
            "by_prefix": [{"prefix": "/a", "total": "x", "covered": None, "coverage_pct": "inf"}],
        },
        capability_map={
            "metrics": {
                "live_capabilities": "N/A",
                "total_capabilities": None,
                "capability_coverage_pct": "nan",
                "state_coverage_pct": "bad",
                "slots_passed": None,
            },
            "capabilities": [],
        },
        k6_analysis={
            "status": "threshold_breached",
            "metrics": {"rps": 50.0, "http_req_failed_rate": 0.1, "latency_ms": {"p95": 650.0}},
            "diagnosis": {
                "notes": "single-string-note",
                "breached_thresholds": ["p95>500ms", "err<0.01 <script>"],
            },
        },
    )

    assert report.api_coverage.total_endpoints == 0
    assert report.api_coverage.coverage_pct == 0.0
    assert report.api_coverage.by_prefix[0].total == 0
    assert report.summary.capability_coverage_pct == 0.0

    html_out = render_acceptance_report_html(report)
    md_out = render_acceptance_report_markdown(report)

    assert "<li>single-string-note</li>" in html_out
    assert "<li>s</li>" not in html_out
    assert "<code>p95&gt;500ms</code>" in html_out
    assert "<script>" not in html_out
    assert "  - Note: single-string-note" in md_out
    assert "  - Note: s\n" not in md_out
    assert "Breached Thresholds:" in md_out
    assert "`p95>500ms`" in md_out


def test_postman_chain_step_preserves_graphql_operation_fragment() -> None:
    """Chain steps keep #OperationName for item name & ScenarioAssertion matching while stripping wire URL."""
    col = export_postman_collection(
        project_id="p_gql",
        project_name="GQL Chain",
        inventory={"endpoints": []},
        chains=[
            {
                "name": "GQL Flow",
                "steps": [
                    {
                        "step": 1,
                        "method": "POST",
                        "path": "/graphql#GetOrderList",
                        "raw_url": "https://api.example.com/graphql",
                        "expected_status": 200,
                    },
                    {
                        "step": 2,
                        "method": "GET",
                        "path": "/app#/dashboard",
                        "raw_url": "https://api.example.com/app",
                        "expected_status": 200,
                    },
                ],
            }
        ],
        scenario_assertions=[
            {
                "id": "a_gql",
                "title": "has orders",
                "target_endpoint": "POST /graphql#GetOrderList",
                "jsonpath": "$.data.orders",
                "operator": "not_empty",
                "expected": "",
            },
            {
                "id": "a_other_gql",
                "title": "wrong op",
                "target_endpoint": "POST /graphql#CreateOrder",
                "jsonpath": "$.data.createOrder",
                "operator": "not_empty",
                "expected": "",
            },
        ],
    )

    chain_items = col["item"][1]["item"][0]["item"]
    gql_step, spa_step = chain_items
    assert gql_step["name"] == "POST /graphql#GetOrderList"
    assert gql_step["request"]["url"]["raw"] == "{{baseUrl}}/graphql"
    assert gql_step["request"]["url"]["path"] == ["graphql"]

    gql_js = "\n".join(gql_step["event"][0]["script"]["exec"])
    assert 'pm.test("has orders"' in gql_js
    assert "wrong op" not in gql_js

    # SPA #/ route fragment is ignored
    assert spa_step["name"] == "GET /app"
    assert spa_step["request"]["url"]["raw"] == "{{baseUrl}}/app"
