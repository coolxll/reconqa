"""Integration tests for ReconQA MCP Server (19 tools & 3 resources via FastAPI & mocks)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from reconqa.config import settings
from reconqa.mcp_server import create_mcp_server, normalize_api_url, should_bypass_env_proxy

EXPECTED_TOOLS = {
    "reconqa_list_projects",
    "reconqa_create_project",
    "reconqa_list_knowledge",
    "reconqa_add_knowledge_doc",
    "reconqa_explore_from_seed",
    "reconqa_promote_explore_to_run",
    "reconqa_import_traffic_and_analyze",
    "reconqa_upload_har_files",
    "reconqa_execute_run_modules",
    "reconqa_get_run_summary",
    "reconqa_list_findings",
    "reconqa_get_project_inventory",
    "reconqa_list_scenarios",
    "reconqa_upsert_scenario",
    "reconqa_extract_credential",
    "reconqa_replay_endpoint",
    "reconqa_run_external_tool",
    "reconqa_get_capability_map",
    "reconqa_promote_capability_scenario",
}

EXPECTED_RESOURCE_TEMPLATES = {
    "reconqa://runs/{run_id}/openapi",
    "reconqa://runs/{run_id}/correlation",
    "reconqa://projects/{project_id}/knowledge",
}


def _extract_json_result(raw: Any) -> Any:
    """Normalize MCP tool result across mcp 1.x and mcp 2.x CallToolResult."""
    structured = getattr(raw, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    if hasattr(raw, "content"):
        raw = raw.content
    if isinstance(raw, tuple) and len(raw) == 2:
        content, structured_part = raw
        if isinstance(structured_part, dict):
            return structured_part
        raw = content
    if isinstance(raw, list) and raw:
        first = raw[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return json.loads(text)
    if isinstance(raw, dict):
        return raw
    raise AssertionError(f"Unexpected MCP tool result shape: {type(raw)} {raw!r}")


def _extract_resource_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if hasattr(raw, "contents"):
        raw = raw.contents
    if isinstance(raw, list) and raw:
        item = raw[0]
        content = getattr(item, "content", None) or getattr(item, "text", None)
        if isinstance(content, str):
            return content
    raise AssertionError(f"Unexpected MCP resource result shape: {type(raw)} {raw!r}")


def test_mcp_url_normalization_and_proxy_bypass() -> None:
    assert normalize_api_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/api/v1"
    assert normalize_api_url("http://127.0.0.1:8000/api/v1/") == "http://127.0.0.1:8000/api/v1"
    assert should_bypass_env_proxy("http://127.0.0.1:8000/api/v1") is True
    assert should_bypass_env_proxy("http://localhost:8000/api/v1") is True
    assert should_bypass_env_proxy("http://100.64.0.10:8000/api/v1") is True
    assert should_bypass_env_proxy("https://api.example.com/api/v1") is False


@pytest.mark.asyncio
async def test_mcp_server_registers_all_tools_resources_and_executes_e2e(
    fixtures_har_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "mcp_test.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    from reconqa.models import db as db_mod

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        mcp = create_mcp_server("http://testserver", transport=transport)

        # 1. Verify all 10 tools & 3 resource templates are registered
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        assert EXPECTED_TOOLS.issubset(tool_names), f"Missing MCP tools: {EXPECTED_TOOLS - tool_names}"

        templates = await mcp.list_resource_templates()
        template_uris = {
            str(getattr(rt, "uri_template", None) or getattr(rt, "uriTemplate", ""))
            for rt in templates
        }
        assert EXPECTED_RESOURCE_TEMPLATES.issubset(
            template_uris
        ), f"Missing MCP resource templates: {EXPECTED_RESOURCE_TEMPLATES - template_uris}"

        # 2. Create project & list projects
        created_proj = _extract_json_result(
            await mcp.call_tool(
                "reconqa_create_project",
                {
                    "name": "MCP Demo Shop",
                    "description": "Created via ReconQA MCP Server",
                    "root_domain": "shop.example.test",
                    "base_url": "https://shop.example.test",
                },
            )
        )
        proj_obj = created_proj.get("project", created_proj)
        project_id = proj_obj["id"]
        assert proj_obj["name"] == "MCP Demo Shop"

        listed = _extract_json_result(await mcp.call_tool("reconqa_list_projects", {}))
        assert any(p["id"] == project_id for p in listed.get("projects", []))

        # 3. Add & list Knowledge Base doc + read knowledge resource
        kb_doc = _extract_json_result(
            await mcp.call_tool(
                "reconqa_add_knowledge_doc",
                {
                    "project_id": project_id,
                    "title": "MCP Checkout Rules",
                    "content": "# Rules\nOrders require valid Bearer token.",
                    "doc_type": "prd",
                    "tags": ["checkout"],
                    "related_endpoints": ["/api/v1/orders/submit"],
                },
            )
        )
        assert kb_doc["title"] == "MCP Checkout Rules"

        kb_list = _extract_json_result(
            await mcp.call_tool("reconqa_list_knowledge", {"project_id": project_id, "q": "Bearer"})
        )
        assert len(kb_list.get("docs", [])) == 1

        kb_md = _extract_resource_text(
            await mcp.read_resource(f"reconqa://projects/{project_id}/knowledge")
        )
        assert "MCP Checkout Rules" in kb_md

        # 4. Import traffic & analyze (cURL -> merge)
        run_res = _extract_json_result(
            await mcp.call_tool(
                "reconqa_import_traffic_and_analyze",
                {
                    "content": (
                        "curl 'https://shop.example.test/api/v1/items/search?keyword=laptop' "
                        "-H 'Accept: application/json'"
                    ),
                    "project_id": project_id,
                    "scenario": "01_mcp_curl",
                },
            )
        )
        run_id = run_res["run_id"]
        assert run_res["status"] in ("completed", "succeeded")

        # 5. Execute on-demand java_gen & security modules + get summary & findings
        exec_res = _extract_json_result(
            await mcp.call_tool(
                "reconqa_execute_run_modules",
                {"run_id": run_id, "modules": ["java_gen", "security"], "wait": True},
            )
        )
        assert exec_res["status"] in ("completed", "succeeded")

        summary = _extract_json_result(
            await mcp.call_tool("reconqa_get_run_summary", {"run_id": run_id})
        )
        assert summary["run_id"] == run_id
        assert summary["summary_metrics"].get("total_endpoints", 0) >= 1

        findings = _extract_json_result(
            await mcp.call_tool("reconqa_list_findings", {"run_id": run_id, "limit": 20})
        )
        assert isinstance(findings.get("findings", []), list)

        # 6. Read OpenAPI & Correlation MCP resources
        oa_yaml = _extract_resource_text(
            await mcp.read_resource(f"reconqa://runs/{run_id}/openapi")
        )
        assert "/api/v1/items/search" in oa_yaml

        corr_text = _extract_resource_text(
            await mcp.read_resource(f"reconqa://runs/{run_id}/correlation")
        )
        assert "chains" in corr_text

        # 7. Upload local HAR directory via reconqa_upload_har_files
        har_run = _extract_json_result(
            await mcp.call_tool(
                "reconqa_upload_har_files",
                {
                    "har_paths": [str(fixtures_har_dir)],
                    "project_id": project_id,
                    "wait": True,
                },
            )
        )
        assert har_run["status"] in ("completed", "succeeded")
        assert har_run["run_id"]
        assert har_run.get("summary_metrics", {}).get("total_endpoints", 0) >= 6

        # 8. Master API Inventory + First-Class Scenarios against live in-process FastAPI
        inv = _extract_json_result(
            await mcp.call_tool(
                "reconqa_get_project_inventory",
                {"project_id": project_id, "source": "har-live", "limit": 20},
            )
        )
        assert inv["project_id"] == project_id
        assert inv["total_endpoints"] >= 6
        assert inv["har_live_count"] >= 6
        assert all(ep["discovery_source"] == "har-live" for ep in inv["endpoints"])

        upserted = _extract_json_result(
            await mcp.call_tool(
                "reconqa_upsert_scenario",
                {
                    "project_id": project_id,
                    "slug": "01_mcp_curl",
                    "name": "Item Search Flow",
                    "description": "Verify catalog search returns matching items.",
                    "business_domain": "catalog",
                    "priority": "P0",
                    "assertions": [
                        {
                            "title": "Search returns items array",
                            "target_endpoint": "GET /api/v1/items/search",
                            "jsonpath": "$.data",
                            "operator": "not_empty",
                        }
                    ],
                    "negative_cases": [
                        {
                            "title": "Empty keyword returns 400",
                            "category": "validation",
                            "expected_behavior": "HTTP 400 validation error",
                        }
                    ],
                },
            )
        )
        assert upserted["slug"] == "01_mcp_curl"
        assert upserted["name"] == "Item Search Flow"
        assert upserted["source"] == "ai-agent"
        assert upserted["priority"] == "P0"
        assert upserted["assertion_count"] == 1
        assert upserted["negative_case_count"] == 1

        # Partial upsert without `name` must preserve the existing custom scenario name
        patched_without_name = _extract_json_result(
            await mcp.call_tool(
                "reconqa_upsert_scenario",
                {
                    "project_id": project_id,
                    "slug": "01_mcp_curl",
                    "assertions": [
                        {
                            "title": "Search returns items array",
                            "target_endpoint": "GET /api/v1/items/search",
                            "jsonpath": "$.data",
                            "operator": "not_empty",
                        },
                        {
                            "title": "Total count >= 0",
                            "target_endpoint": "GET /api/v1/items/search",
                            "jsonpath": "$.total",
                            "operator": "gte",
                            "expected": "0",
                        },
                    ],
                },
            )
        )
        assert patched_without_name["name"] == "Item Search Flow"
        assert patched_without_name["assertion_count"] == 2

        sc_list = _extract_json_result(
            await mcp.call_tool(
                "reconqa_list_scenarios",
                {"project_id": project_id, "priority": "P0"},
            )
        )
        assert sc_list["total"] >= 1
        assert any(s["slug"] == "01_mcp_curl" for s in sc_list["scenarios"])

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


@pytest.mark.asyncio
async def test_mcp_inventory_and_scenarios_tools_mocked() -> None:
    """Verify inventory and scenario MCP tools against mocked FastAPI HTTP responses."""
    captured_requests: list[tuple[str, str, dict[str, str], Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            body = json.loads(request.content.decode("utf-8"))
        captured_requests.append(
            (request.method, request.url.path, dict(request.url.params), body)
        )

        if request.method == "GET" and request.url.path == "/api/v1/projects/proj_1/inventory":
            return httpx.Response(
                200,
                json={
                    "project_id": "proj_1",
                    "project_name": "Demo Shop",
                    "runs_merged_count": 2,
                    "explore_sessions_count": 1,
                    "total_endpoints": 3,
                    "har_live_count": 2,
                    "js_static_count": 1,
                    "coverage_pct": 66.7,
                    "scenarios": ["01_checkout", "02_orders"],
                    "by_prefix": [
                        {"prefix": "/api/v1/orders", "total": 2, "covered": 2, "coverage_pct": 100.0},
                        {"prefix": "/api/v1/admin", "total": 1, "covered": 0, "coverage_pct": 0.0},
                    ],
                    "endpoints": [
                        {
                            "method": "POST",
                            "path": "/api/v1/orders/submit",
                            "summary": "Submit order",
                            "discovery_source": "har-live",
                            "host": "shop.example.com",
                            "scenarios": ["01_checkout"],
                            "status_codes": [200, 201],
                            "parameters": [{"name": "coupon", "in": "query"}],
                            "seen_in_runs": [{"run_id": "run_1", "run_name": "Checkout"}],
                            "response_example": {"huge": "omitted-by-mcp"},
                        },
                        {
                            "method": "GET",
                            "path": "/api/v1/orders/{id}",
                            "summary": "Get order",
                            "discovery_source": "har-live",
                            "host": "shop.example.com",
                            "scenarios": ["02_orders"],
                            "status_codes": [200],
                            "parameters": [{"name": "id", "in": "path"}],
                            "seen_in_runs": [{"run_id": "run_1", "run_name": "Checkout"}],
                        },
                        {
                            "method": "DELETE",
                            "path": "/api/v1/admin/users/{id}",
                            "summary": "Delete user",
                            "discovery_source": "js-static",
                            "host": "shop.example.com",
                            "scenarios": [],
                            "status_codes": [],
                            "parameters": [],
                            "seen_in_runs": [],
                        },
                    ],
                },
            )

        if request.method == "GET" and request.url.path == "/api/v1/projects/proj_1/scenarios":
            return httpx.Response(
                200,
                json={
                    "project_id": "proj_1",
                    "total": 2,
                    "by_priority": {"P0": 1, "P1": 1},
                    "by_effectiveness": {"L1": 1, "L4": 1},
                    "p0_p1_total": 2,
                    "p0_p1_effective_count": 1,
                    "p0_p1_effective_pct": 50.0,
                    "scenarios": [
                        {
                            "id": "sc_1",
                            "project_id": "proj_1",
                            "slug": "01_checkout",
                            "name": "Checkout Flow",
                            "priority": "P0",
                            "status": "active",
                            "business_domain": "commerce",
                            "owner": "qa-team",
                            "source": "har-auto",
                            "effectiveness_level": "L1",
                            "description": "Checkout submission flow",
                            "tags": ["checkout"],
                            "endpoint_count": 2,
                            "run_count": 1,
                            "last_run_id": "run_1",
                            "assertion_count": 0,
                            "assertions_passed_count": 0,
                            "negative_case_count": 0,
                            "negative_cases_covered_count": 0,
                        }
                    ],
                },
            )

        if request.method == "POST" and request.url.path == "/api/v1/projects/proj_1/scenarios":
            assert request.url.params.get("upsert") == "true"
            assert body["source"] == "ai-agent"
            return httpx.Response(
                201,
                json={
                    "id": "sc_1",
                    "project_id": "proj_1",
                    "slug": body["slug"],
                    "name": body["name"],
                    "priority": body.get("priority", "P2"),
                    "status": body.get("status", "active"),
                    "business_domain": body.get("business_domain", ""),
                    "owner": body.get("owner", ""),
                    "source": body["source"],
                    "tags": body.get("tags", []),
                    "endpoint_count": 2,
                    "assertion_count": len(body.get("assertions", [])),
                    "negative_case_count": len(body.get("negative_cases", [])),
                    "effectiveness_level": "L4",
                },
            )

        return httpx.Response(404, json={"detail": f"Unhandled mock route: {request.url}"})

    mcp = create_mcp_server("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))

    # 1. reconqa_get_project_inventory filtering + limit cap
    inv = _extract_json_result(
        await mcp.call_tool(
            "reconqa_get_project_inventory",
            {
                "project_id": "proj_1",
                "source": "har-live",
                "prefix": "/api/v1/orders",
                "limit": 1,
            },
        )
    )
    assert inv["total_endpoints"] == 3
    assert inv["har_live_count"] == 2
    assert inv["js_static_count"] == 1
    assert inv["coverage_pct"] == 66.7
    assert inv["matched_endpoints_count"] == 2
    assert inv["endpoints_truncated"] == 1
    assert len(inv["endpoints"]) == 1
    assert inv["endpoints"][0]["path"] == "/api/v1/orders/submit"
    assert "response_example" not in inv["endpoints"][0]

    # 2. reconqa_list_scenarios query forwarding + effectiveness summary
    sc_list = _extract_json_result(
        await mcp.call_tool(
            "reconqa_list_scenarios",
            {"project_id": "proj_1", "priority": "p0", "effectiveness_level": "l1"},
        )
    )
    assert sc_list["p0_p1_effective_pct"] == 50.0
    assert sc_list["by_priority"] == {"P0": 1, "P1": 1}
    assert sc_list["by_effectiveness"] == {"L1": 1, "L4": 1}
    assert len(sc_list["scenarios"]) == 1
    assert sc_list["scenarios"][0]["effectiveness_level"] == "L1"

    # 3. reconqa_upsert_scenario sends ?upsert=true and source='ai-agent'
    upserted = _extract_json_result(
        await mcp.call_tool(
            "reconqa_upsert_scenario",
            {
                "project_id": "proj_1",
                "slug": "01_checkout",
                "name": "Checkout Submission",
                "description": "Order total must match line items.",
                "preconditions": "Authenticated shopper with cart items",
                "business_domain": "commerce",
                "priority": "P0",
                "status": "verified",
                "owner": "qa-agent",
                "tags": ["checkout", "p0"],
                "assertions": [
                    {
                        "title": "Order ID is returned",
                        "target_endpoint": "POST /api/v1/orders/submit",
                        "jsonpath": "$.data.order_id",
                        "operator": "not_empty",
                    }
                ],
                "negative_cases": [
                    {
                        "title": "Reject expired coupon",
                        "category": "validation",
                        "expected_behavior": "HTTP 400 coupon_expired",
                    }
                ],
            },
        )
    )
    assert upserted["source"] == "ai-agent"
    assert upserted["priority"] == "P0"
    assert upserted["assertion_count"] == 1
    assert upserted["negative_case_count"] == 1
    assert upserted["effectiveness_level"] == "L4"

    # Verify exact backend URLs & query parameters sent
    assert captured_requests[0][0:2] == ("GET", "/api/v1/projects/proj_1/inventory")
    assert captured_requests[1][0:3] == (
        "GET",
        "/api/v1/projects/proj_1/scenarios",
        {"priority": "P0", "effectiveness_level": "L1"},
    )
    assert captured_requests[2][0:3] == (
        "POST",
        "/api/v1/projects/proj_1/scenarios",
        {"upsert": "true"},
    )


@pytest.mark.asyncio
async def test_mcp_external_acceptance_tools_mocked() -> None:
    """Verify extract_credential, replay_endpoint (4KB cap), and run_external_tool via mocks."""
    captured: list[tuple[str, str, Any]] = []
    secret_sentinel = "FAKE-TOKEN-VALUE-MUST-NOT-ECHO"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        captured.append((request.method, request.url.path, body))

        if (
            request.method == "POST"
            and request.url.path == "/api/v1/projects/proj_1/auth-profiles/extract-from-har"
        ):
            return httpx.Response(
                200,
                json={
                    "profile": {
                        "id": "cred_1",
                        "project_id": "proj_1",
                        "name": body.get("name") or "Auto-Extracted Session (shop.example.com)",
                        "role_tag": body.get("role_tag", "account_a"),
                        "storage_state_path": None,
                        "extra_headers_ref": {"Authorization": secret_sentinel},
                    },
                    "extracted_header_names": ["Authorization"],
                    "source_run_id": body.get("run_id") or "run_9",
                    "source_host": "shop.example.com",
                },
            )

        if request.method == "POST" and request.url.path == "/api/v1/projects/proj_1/replay":
            oversized = "X" * 8000
            return httpx.Response(
                200,
                json={
                    "status_code": 200,
                    "status_text": "OK",
                    "duration_ms": 42.5,
                    "response_headers": {"content-type": "application/json"},
                    "body_text": oversized,
                    "body_json": {"payload": oversized},
                    "content_type": "application/json",
                    "truncated": False,
                    "error": None,
                },
            )

        if (
            request.method == "POST"
            and request.url.path == "/api/v1/projects/proj_1/runs/run_9/execute-tool"
        ):
            return httpx.Response(
                200,
                json={
                    "executed": True,
                    "tool": body["tool"],
                    "status": "passed",
                    "duration_ms": 1820.0,
                    "summary": {"total": 5, "passed": 5, "failed": 0},
                    "stdout_tail": "Tests run: 5, Failures: 0",
                    "synced_scenarios": 2,
                    "synced_assertions": 4,
                    "synced_negative_cases": 1,
                    "error": None,
                },
            )

        return httpx.Response(404, json={"detail": f"Unhandled mock route: {request.url}"})

    mcp = create_mcp_server("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))

    # 1. reconqa_extract_credential returns metadata only (ZERO raw secret echo!)
    extracted = _extract_json_result(
        await mcp.call_tool(
            "reconqa_extract_credential",
            {
                "project_id": "proj_1",
                "run_id": "run_9",
                "name": "Shop Account A",
                "role_tag": "account_a",
            },
        )
    )
    assert extracted == {
        "profile_id": "cred_1",
        "name": "Shop Account A",
        "role_tag": "account_a",
        "extracted_header_names": ["Authorization"],
        "source_run_id": "run_9",
        "source_host": "shop.example.com",
    }
    assert secret_sentinel not in json.dumps(extracted)

    # 2. reconqa_replay_endpoint enforces 4KB cap for LLM token safety
    replayed = _extract_json_result(
        await mcp.call_tool(
            "reconqa_replay_endpoint",
            {
                "project_id": "proj_1",
                "method": "get",
                "url": "https://shop.example.com/api/v1/orders",
                "query_params": {"page": "1"},
                "credential_id": "cred_1",
                "timeout_sec": 10.0,
            },
        )
    )
    assert replayed["status_code"] == 200
    assert replayed["truncated"] is True
    assert replayed["body_json"] is None
    assert len(replayed["body_text"]) <= 4096
    assert replayed["body_text"].endswith("...")

    # 3. reconqa_run_external_tool forwards payload and returns sync-back counts
    tool_res = _extract_json_result(
        await mcp.call_tool(
            "reconqa_run_external_tool",
            {
                "project_id": "proj_1",
                "run_id": "run_9",
                "tool": "java_test",
                "credential_id": "cred_1",
                "base_url": "https://shop.example.com",
                "sync_scenarios": True,
                "timeout_sec": 90,
            },
        )
    )
    assert tool_res["executed"] is True
    assert tool_res["tool"] == "java_test"
    assert tool_res["status"] == "passed"
    assert tool_res["synced_scenarios"] == 2
    assert tool_res["synced_assertions"] == 4
    assert tool_res["synced_negative_cases"] == 1
    assert "Tests run: 5" in tool_res["stdout_tail"]

    assert captured[0] == (
        "POST",
        "/api/v1/projects/proj_1/auth-profiles/extract-from-har",
        {"role_tag": "account_a", "run_id": "run_9", "name": "Shop Account A"},
    )
    assert captured[1] == (
        "POST",
        "/api/v1/projects/proj_1/replay",
        {
            "method": "GET",
            "url": "https://shop.example.com/api/v1/orders",
            "headers": {},
            "query_params": {"page": "1"},
            "body": None,
            "credential_id": "cred_1",
            "timeout_sec": 10.0,
        },
    )
    assert captured[2] == (
        "POST",
        "/api/v1/projects/proj_1/runs/run_9/execute-tool",
        {
            "tool": "java_test",
            "credential_id": "cred_1",
            "base_url": "https://shop.example.com",
            "sync_scenarios": True,
            "timeout_sec": 90,
        },
    )


@pytest.mark.asyncio
async def test_mcp_upsert_scenario_omits_name_when_unspecified() -> None:
    """Regression: omitting name in reconqa_upsert_scenario omits 'name' and keeps custom name."""
    sent_bodies: list[dict[str, Any]] = []
    existing_custom_name = "核心结算主流程"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/projects/proj_1/scenarios"
        assert request.url.params.get("upsert") == "true"
        body = json.loads(request.content.decode("utf-8"))
        sent_bodies.append(body)
        return httpx.Response(
            201,
            json={
                "id": "sc_1",
                "project_id": "proj_1",
                "slug": body["slug"],
                "name": body.get("name", existing_custom_name),
                "priority": body.get("priority", "P0"),
                "status": "active",
                "business_domain": "commerce",
                "owner": "qa-team",
                "source": body["source"],
                "tags": [],
                "endpoint_count": 3,
                "assertion_count": len(body.get("assertions", [])),
                "negative_case_count": 0,
                "effectiveness_level": "L3",
            },
        )

    mcp = create_mcp_server("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))

    res = _extract_json_result(
        await mcp.call_tool(
            "reconqa_upsert_scenario",
            {
                "project_id": "proj_1",
                "slug": "01_checkout",
                "assertions": [
                    {
                        "title": "Order created",
                        "target_endpoint": "POST /api/v1/orders/submit",
                        "jsonpath": "$.data.order_id",
                        "operator": "not_empty",
                    }
                ],
            },
        )
    )
    assert "name" not in sent_bodies[0]
    assert sent_bodies[0]["slug"] == "01_checkout"
    assert sent_bodies[0]["source"] == "ai-agent"
    assert res["name"] == existing_custom_name
    assert res["assertion_count"] == 1

    # Blank/whitespace-only name is also omitted rather than overwriting custom name
    res_blank = _extract_json_result(
        await mcp.call_tool(
            "reconqa_upsert_scenario",
            {
                "project_id": "proj_1",
                "slug": "01_checkout",
                "name": "   ",
                "priority": "P0",
            },
        )
    )
    assert "name" not in sent_bodies[1]
    assert res_blank["name"] == existing_custom_name


@pytest.mark.asyncio
async def test_mcp_capability_map_and_promote_tools_mocked() -> None:
    """Verify reconqa_get_capability_map and reconqa_promote_capability_scenario via mocks."""
    captured: list[tuple[str, str, dict[str, str], Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        captured.append(
            (request.method, request.url.path, dict(request.url.params), body)
        )

        if request.method == "GET" and request.url.path == "/api/v1/projects/proj_1/capabilities":
            ten_endpoints = [
                {"method": "GET", "path": f"/api/v1/orders/item-{i}", "role": "admin", "action": "read"}
                for i in range(10)
            ]
            return httpx.Response(
                200,
                json={
                    "project_id": "proj_1",
                    "project_name": "Demo Commerce",
                    "metrics": {
                        "total_endpoints": 120,
                        "total_capabilities": 18,
                        "live_capabilities": 12,
                        "target_capabilities": 18,
                        "capability_coverage_pct": 66.7,
                        "stateful_capabilities": 6,
                        "state_transition_covered": 3,
                        "state_coverage_pct": 50.0,
                        "role_matrix_cells_total": 24,
                        "role_matrix_cells_covered": 12,
                        "role_matrix_coverage_pct": 50.0,
                        "negative_slots_total": 48,
                        "negative_slots_tested": 12,
                        "negative_coverage_pct": 25.0,
                        "invariant_assertions_total": 10,
                        "invariant_assertions_passed": 8,
                        "invariant_pass_rate_pct": 80.0,
                    },
                    "modules": [
                        {"module": "orders", "total_capabilities": 5, "live_capabilities": 4},
                        {"module": "billing", "total_capabilities": 3, "live_capabilities": 2},
                    ],
                    "capabilities": [
                        {
                            "key": "orders/order",
                            "module": "orders",
                            "resource": "order",
                            "display_name": "orders / order",
                            "recommended_priority": "P0",
                            "priority_score": 95,
                            "live_endpoints": 8,
                            "total_endpoints": 10,
                            "write_endpoints": 4,
                            "roles": ["admin", "buyer"],
                            "actions": ["create", "audit", "refund", "list"],
                            "observed_statuses": {"orderStatus": [0, 1, 2, 5]},
                            "case_matrix": {
                                "happy_path": "covered",
                                "invalid_params": "pending",
                                "illegal_state": "missing",
                                "authz_role": "missing",
                                "consistency_idempotency": "missing",
                            },
                            "case_coverage_pct": 20.0,
                            "linked_scenarios": [],
                            "endpoints": ten_endpoints,
                        },
                        {
                            "key": "billing/invoice",
                            "module": "billing",
                            "resource": "invoice",
                            "display_name": "billing / invoice",
                            "recommended_priority": "P1",
                            "priority_score": 70,
                            "live_endpoints": 4,
                            "total_endpoints": 5,
                            "write_endpoints": 2,
                            "roles": ["admin"],
                            "actions": ["create", "detail"],
                            "observed_statuses": {"status": ["draft", "paid"]},
                            "case_matrix": {
                                "happy_path": "pending",
                                "invalid_params": "missing",
                                "illegal_state": "missing",
                                "authz_role": "missing",
                                "consistency_idempotency": "missing",
                            },
                            "case_coverage_pct": 0.0,
                            "linked_scenarios": [],
                            "endpoints": [
                                {"method": "POST", "path": "/api/v1/billing/invoice"},
                            ],
                        },
                    ],
                },
            )

        if (
            request.method == "POST"
            and request.url.path == "/api/v1/projects/proj_1/capabilities/promote-scenario"
        ):
            return httpx.Response(
                201,
                json={
                    "id": "sc_cap_1",
                    "project_id": "proj_1",
                    "slug": "cap_orders_order",
                    "name": body.get("name") or "orders / order Core Capability",
                    "priority": body.get("priority") or "P0",
                    "status": "active",
                    "business_domain": "orders",
                    "source": "ai-agent",
                    "endpoint_count": 10,
                    "assertion_count": 2,
                    "negative_case_count": 4,
                    "effectiveness_level": "L3",
                },
            )

        return httpx.Response(404, json={"detail": f"Unhandled mock route: {request.url}"})

    mcp = create_mcp_server("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))

    # 1. reconqa_get_capability_map forwards filters and bounds both capabilities & sample endpoints
    cap_map = _extract_json_result(
        await mcp.call_tool(
            "reconqa_get_capability_map",
            {
                "project_id": "proj_1",
                "live_only": True,
                "module": "orders",
                "role": "Admin",
                "has_writes": True,
                "limit": 1,
            },
        )
    )
    assert cap_map["metrics"]["capability_coverage_pct"] == 66.7
    assert cap_map["metrics"]["state_coverage_pct"] == 50.0
    assert len(cap_map["modules"]) == 2
    assert cap_map["total_matching_capabilities"] == 2
    assert cap_map["capabilities_truncated"] == 1
    assert len(cap_map["capabilities"]) == 1

    top_cap = cap_map["capabilities"][0]
    assert top_cap["key"] == "orders/order"
    assert top_cap["recommended_priority"] == "P0"
    assert top_cap["observed_statuses"] == {"orderStatus": [0, 1, 2, 5]}
    assert top_cap["case_matrix"]["happy_path"] == "covered"
    assert len(top_cap["sample_endpoints"]) == 8
    assert top_cap["endpoints_truncated"] == 2
    assert top_cap["sample_endpoints"][0] == "GET /api/v1/orders/item-0"

    # 2. reconqa_promote_capability_scenario materializes 5-case effectiveness Scenario
    promoted = _extract_json_result(
        await mcp.call_tool(
            "reconqa_promote_capability_scenario",
            {
                "project_id": "proj_1",
                "capability_key": "orders/order",
                "priority": "p0",
                "name": "Order Lifecycle 5-Case Verification",
            },
        )
    )
    assert promoted["id"] == "sc_cap_1"
    assert promoted["slug"] == "cap_orders_order"
    assert promoted["name"] == "Order Lifecycle 5-Case Verification"
    assert promoted["priority"] == "P0"
    assert promoted["assertion_count"] == 2
    assert promoted["negative_case_count"] == 4
    assert promoted["effectiveness_level"] == "L3"

    assert captured[0] == (
        "GET",
        "/api/v1/projects/proj_1/capabilities",
        {
            "live_only": "true",
            "has_writes": "true",
            "module": "orders",
            "role": "admin",
        },
        None,
    )
    assert captured[1] == (
        "POST",
        "/api/v1/projects/proj_1/capabilities/promote-scenario",
        {},
        {
            "capability_key": "orders/order",
            "priority": "P0",
            "name": "Order Lifecycle 5-Case Verification",
        },
    )
