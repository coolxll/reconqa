"""Regression tests for Round 1 Code Review P0 & P1 Security and Reliability Hardening (#197)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.modules.codegen_java.generator import generate_java_project
from reconqa.modules.discovery.seed_explorer import _is_unsafe_redirect_url, _safe_redirect_hook
from reconqa.modules.hub.correlation import _escape_mermaid_label
from reconqa.modules.recorder.har_importer import import_hars
from reconqa.server.routers.projects import _resolve_header_value


def test_env_allowlist_java_unicode_escape_and_mermaid_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify RECONQA_ env allowlist, Java \\uXXXX/jsonpath sanitization, Mermaid escaping, and redirect guard."""
    # 1. RECONQA_ env allowlist in _resolve_header_value
    monkeypatch.setenv("RECONQA_TEST_BEARER", "Bearer safe-reconqa-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-never-be-read")
    assert _resolve_header_value("ENV:RECONQA_TEST_BEARER") == "Bearer safe-reconqa-token"
    assert _resolve_header_value("${RECONQA_TEST_BEARER}") == "Bearer safe-reconqa-token"
    assert _resolve_header_value("ENV:AWS_SECRET_ACCESS_KEY") == ""
    assert _resolve_header_value("${AWS_SECRET_ACCESS_KEY}") == ""

    # 2. Java codegen lexical unicode escape & unsafe jsonpath hardening
    oa = tmp_path / "openapi.yaml"
    oa.write_text(
        yaml.safe_dump(
            {
                "openapi": "3.0.3",
                "info": {"title": "Hardening API", "version": "1.0.0"},
                "servers": [{"url": "https://api.example.test"}],
                "paths": {
                    "/api/v1/items": {
                        "get": {
                            "x-discovery-source": "har-live",
                            "responses": {"200": {"description": "OK"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "java"
    generate_java_project(
        oa,
        out_dir,
        scenario_assertions=[
            {
                "id": "a-evil-comment",
                "priority": "P0",
                "title": r"Break comment \u000a System.exit(1); */",
                "target_endpoint": "GET /api/v1/items",
                "jsonpath": "$.data.id",
                "operator": "not_empty",
            },
            {
                "id": "a-evil-jp",
                "priority": "P0",
                "title": "Unsafe jsonpath injection",
                "target_endpoint": "GET /api/v1/items",
                "jsonpath": '$.data"); System.exit(1); //',
                "operator": "not_empty",
            },
            {
                "id": "a-unbalanced-jp",
                "priority": "P1",
                "title": "Unbalanced bracket jsonpath",
                "target_endpoint": "GET /api/v1/items",
                "jsonpath": "$.data[0",
                "operator": "not_empty",
            },
        ],
    )
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
    assert r"\u000a" not in l1_java
    assert r"\x00" not in l1_java
    assert "// Skipped unsafe jsonpath:" in l1_java
    assert "// Skipped unsafe jsonpath: $.data[0" in l1_java
    assert 'body("$.data"); System.exit(1)' not in l1_java

    # Control characters dropped without emitting invalid \xNN Java escapes
    from reconqa.modules.codegen_java.generator import escape_java_string

    assert escape_java_string("ok\x00\x07\x7f\nline") == "ok\\nline"

    # 3. Mermaid label escaping
    escaped = _escape_mermaid_label('<img src=x onerror="alert(1)">\nline2')
    assert "<" not in escaped and ">" not in escaped and '"' not in escaped and "\n" not in escaped

    # 4. Outbound redirect metadata & scheme guard
    assert _is_unsafe_redirect_url("http://169.254.169.254/latest/meta-data/", "https://example.test")
    assert _is_unsafe_redirect_url("http://metadata.google.internal/", "https://example.test")
    assert _is_unsafe_redirect_url("file:///etc/passwd", "https://example.test")
    assert not _is_unsafe_redirect_url("https://portal.example.test/login", "https://example.test")


def test_hardlink_safe_har_reimport(tmp_path: Path) -> None:
    """Verify import_hars writes via atomic replace so overwriting a hardlink never mutates source."""
    src_dir = tmp_path / "run_src"
    dst_dir = tmp_path / "run_dst"
    src_dir.mkdir()
    dst_dir.mkdir()

    src_har = src_dir / "01_flow.har"
    orig_doc = {
        "log": {
            "version": "1.2",
            "entries": [
                {
                    "request": {"method": "GET", "url": "https://api.example.test/v1/original"},
                    "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"v":1}'}},
                }
            ],
        }
    }
    src_har.write_text(json.dumps(orig_doc), encoding="utf-8")

    # Simulate hardlinked reuse into dst_dir
    dst_har = dst_dir / "01_flow.har"
    os.link(src_har, dst_har)
    assert src_har.stat().st_ino == dst_har.stat().st_ino

    # Re-import new content into dst_dir under the same scenario name
    new_upload = tmp_path / "01_flow.har"
    new_doc = {
        "log": {
            "version": "1.2",
            "entries": [
                {
                    "request": {"method": "GET", "url": "https://api.example.test/v1/updated"},
                    "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"v":2}'}},
                }
            ],
        }
    }
    new_upload.write_text(json.dumps(new_doc), encoding="utf-8")
    import_hars([new_upload], dst_dir, extract_js=False)

    # Inodes must now be distinct and source run's HAR must remain completely untouched!
    assert src_har.stat().st_ino != dst_har.stat().st_ino
    assert "/v1/original" in src_har.read_text(encoding="utf-8")
    assert "/v1/updated" in dst_har.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_api_credential_masking_containment_artifacts_by_id_target_clear_and_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify server-side credential masking, path containment, /artifacts/by-id, scope clear, and failed-job->failed-run."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "hardening.db"
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
            pid = (await client.post("/api/v1/projects", json={"name": "Hardening Suite"})).json()["id"]

            # 1. AuthProfileOut masks raw tokens while preserving ENV: refs, and ignores caller storage_state_path
            outside_file = tmp_path / "do_not_delete.json"
            outside_file.write_text('{"keep": true}', encoding="utf-8")
            raw_token = "Bearer super-secret-jwt-token-987654"
            r_ap = await client.post(
                f"/api/v1/projects/{pid}/auth-profiles",
                json={
                    "name": "Masked Cred",
                    "role_tag": "admin",
                    "storage_state_path": str(outside_file),
                    "extra_headers_ref": {
                        "Authorization": raw_token,
                        "X-Env-Ref": "ENV:RECONQA_ADMIN_TOKEN",
                        "X-Short": "1234",
                    },
                },
            )
            assert r_ap.status_code == 201, r_ap.text
            ap_data = r_ap.json()
            assert ap_data["storage_state_path"] is None
            assert raw_token not in json.dumps(ap_data)
            assert ap_data["extra_headers_ref"]["Authorization"] == "Bear•••7654"
            assert ap_data["extra_headers_ref"]["X-Env-Ref"] == "ENV:RECONQA_ADMIN_TOKEN"
            assert ap_data["extra_headers_ref"]["X-Short"] == "***"

            # Delete must NOT unlink outside_file
            await client.delete(f"/api/v1/projects/{pid}/auth-profiles/{ap_data['id']}")
            assert outside_file.is_file()

            # 2. Target update clears removed/empty scope_rules keys
            tg = (
                await client.post(
                    f"/api/v1/projects/{pid}/targets",
                    json={
                        "root_domain": "example.test",
                        "base_url": "https://portal.example.test",
                        "scope_rules": {"portal_label": "HQ Portal"},
                    },
                )
            ).json()
            r_clear = await client.patch(
                f"/api/v1/projects/{pid}/targets/{tg['id']}",
                json={"scope_rules": {"portal_label": ""}},
            )
            assert r_clear.status_code == 200
            assert "portal_label" not in r_clear.json()["scope_rules"]

            # 3. har_sources outside data_dir rejected with 400
            r_bad_run = await client.post(
                f"/api/v1/projects/{pid}/runs?auto_start=false",
                json={"modules": ["merge"], "config": {"har_sources": ["/etc/passwd"]}},
            )
            assert r_bad_run.status_code == 400

            # 4. Multi-HAR download by exact artifact ID (/artifacts/by-id/{artifact_id})
            r_run = (
                await client.post(
                    f"/api/v1/projects/{pid}/runs?auto_start=false",
                    json={"modules": ["merge"]},
                )
            ).json()
            rid = r_run["id"]
            har_a = json.dumps(
                {
                    "log": {
                        "version": "1.2",
                        "entries": [
                            {
                                "request": {"method": "GET", "url": "https://api.example.test/a"},
                                "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"src":"A"}'}},
                            }
                        ],
                    }
                }
            ).encode("utf-8")
            har_b = json.dumps(
                {
                    "log": {
                        "version": "1.2",
                        "entries": [
                            {
                                "request": {"method": "GET", "url": "https://api.example.test/b"},
                                "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"src":"B"}'}},
                            }
                        ],
                    }
                }
            ).encode("utf-8")
            up = await client.post(
                f"/api/v1/runs/{rid}/hars",
                files=[
                    ("files", ("01_a.har", har_a, "application/json")),
                    ("files", ("02_b.har", har_b, "application/json")),
                ],
            )
            assert up.status_code == 200, up.text
            arts = (await client.get(f"/api/v1/runs/{rid}/artifacts")).json()
            har_arts = {Path(a["rel_path"]).name: a["id"] for a in arts if a["kind"] == "har"}
            assert "01_a.har" in har_arts and "02_b.har" in har_arts

            dl_b = await client.get(f"/api/v1/runs/{rid}/artifacts/by-id/{har_arts['02_b.har']}")
            assert dl_b.status_code == 200
            dl_b_entries = dl_b.json()["log"]["entries"]
            assert dl_b_entries[0]["request"]["url"] == "https://api.example.test/b"
            assert dl_b_entries[0]["response"]["content"]["text"] == '{"src":"B"}'

            # 5. Worker sets run.status = "failed" when a child module fails inside run_pipeline
            import reconqa.server.worker as worker_mod

            def _failing_pipeline(
                run_dir: Path, *, module_cb: Any = None, **kwargs: Any
            ) -> dict[str, Any]:
                if module_cb:
                    module_cb("merge", "running", {})
                    module_cb("merge", "failed", {"error": "simulated merge failure"})
                return {"summary_metrics": {}, "findings": [], "artifacts": []}

            monkeypatch.setattr(worker_mod, "run_pipeline", _failing_pipeline)
            r_start = await client.post(f"/api/v1/runs/{rid}/start")
            assert r_start.status_code == 200
            final_run: dict[str, Any] = {}
            for _ in range(100):
                final_run = (await client.get(f"/api/v1/runs/{rid}")).json()
                if final_run["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.05)
            assert final_run["status"] == "failed", final_run
            assert any(j["module"] == "merge" and j["status"] == "failed" for j in final_run["jobs"])

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


def test_seed_explorer_redirect_hook_blocks_metadata_pivot() -> None:
    """Verify live httpx client with _safe_redirect_hook blocks 302 redirect to 169.254.169.254."""
    class _RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/redirect"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        with pytest.raises(httpx.HTTPError, match="Blocked unsafe redirect"):
            with httpx.Client(
                follow_redirects=True,
                trust_env=False,
                event_hooks={"response": [_safe_redirect_hook]},
            ) as client:
                client.get(url)
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=3)
