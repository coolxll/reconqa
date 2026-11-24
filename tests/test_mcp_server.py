"""Integration tests for ReconQA MCP Server (10 tools & 3 resources via in-process FastAPI)."""

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

        # 7. Upload local HAR directory via 11th tool reconqa_upload_har_files
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

    if db_mod._engine is not None:
        await db_mod._engine.dispose()
