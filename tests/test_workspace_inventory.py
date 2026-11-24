"""Tests for Jira Workspace contracts: project-scoped Explore, Master Inventory, and safe Replay."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from reconqa.config import Settings, settings
from reconqa.models import db as db_mod


def _write_sample_openapi(run_dir: Path, spec: dict[str, Any]) -> None:
    hub_dir = run_dir / "hub"
    hub_dir.mkdir(parents=True, exist_ok=True)
    (hub_dir / "openapi.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_project_scoped_explore_and_master_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify project-scoped GET /explore, POST /explore/seed, and cumulative Project Inventory."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "workspace_inv.db"
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
            # Create Project A (shop.example.test) and Project B (unrelated.example.test)
            p_a = (await client.post("/api/v1/projects", json={"name": "Shop Workspace"})).json()
            p_b = (await client.post("/api/v1/projects", json={"name": "Other Workspace"})).json()
            pid_a = p_a["id"]
            pid_b = p_b["id"]

            await client.post(
                f"/api/v1/projects/{pid_a}/targets",
                json={"root_domain": "shop.example.test", "base_url": "https://api.shop.example.test"},
            )

            # 1a. Seed an explore session WITHOUT project_id matching Project A's target domain
            exp_domain_dir = ws_dir / "explores" / "shop-example-test"
            exp_domain_dir.mkdir(parents=True, exist_ok=True)
            (exp_domain_dir / "explore_result.json").write_text(
                json.dumps(
                    {
                        "explore_id": "shop-example-test",
                        "created_at": "2026-11-23T01:00:00+00:00",
                        "updated_at": "2026-11-23T01:00:00+00:00",
                        "mode": "passive",
                        "seed": {
                            "type": "url",
                            "base_url": "https://api.shop.example.test",
                            "root_domain": "shop.example.test",
                            "seed_url": "https://api.shop.example.test",
                            "api_prefix": "/api/v1",
                            "auth_headers_detected": [],
                        },
                        "summary": {"endpoints_count": 2, "scans_count": 1},
                        "discovered_endpoints": [
                            {
                                "id": "GET /api/v1/items/search",
                                "method": "GET",
                                "path": "/api/v1/items/search",
                                "url": "https://api.shop.example.test/api/v1/items/search",
                                "source": "js_static",
                                "inferred_params": ["keyword"],
                                "snippet": "",
                            },
                            {
                                "id": "GET /api/v1/coupons/hidden",
                                "method": "GET",
                                "path": "/api/v1/coupons/hidden",
                                "url": "https://api.shop.example.test/api/v1/coupons/hidden",
                                "source": "js_static",
                                "inferred_params": [],
                                "snippet": "",
                            },
                        ],
                        "openapi_docs_found": [],
                        "spa_routes": [],
                        "micro_frontends": [],
                        "subdomains": [],
                        "live_hosts": [],
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )

            # 1b. Explicitly link a second explore via POST /api/v1/explore/seed with project_id=pid_b
            r_seed = await client.post(
                "/api/v1/explore/seed",
                json={
                    "seed_curl": "curl 'https://beta.other.test/api/v1/health'",
                    "probe_openapi_docs": False,
                    "crawl_js": False,
                    "project_id": pid_b,
                },
            )
            assert r_seed.status_code == 201, r_seed.text
            assert r_seed.json().get("project_id") == pid_b

            # GET /explore?project_id=pid_a returns domain-matched explore, NOT pid_b's explore
            exp_a = (await client.get("/api/v1/explore", params={"project_id": pid_a})).json()
            assert [e["explore_id"] for e in exp_a] == ["shop-example-test"], exp_a

            exp_b = (await client.get("/api/v1/explore", params={"project_id": pid_b})).json()
            assert len(exp_b) == 1 and exp_b[0]["project_id"] == pid_b, exp_b

            # 2. Seed two completed Runs for Project A to test cumulative Master API Inventory
            r1 = (
                await client.post(
                    f"/api/v1/projects/{pid_a}/runs?auto_start=false",
                    json={"name": "Pass 1 Browse", "modules": ["merge"]},
                )
            ).json()
            r2 = (
                await client.post(
                    f"/api/v1/projects/{pid_a}/runs?auto_start=false",
                    json={"name": "Pass 2 Checkout", "modules": ["merge"]},
                )
            ).json()

            _write_sample_openapi(
                Path(r1["workspace_dir"]),
                {
                    "openapi": "3.0.3",
                    "servers": [{"url": "https://api.shop.example.test"}],
                    "paths": {
                        "/api/v1/items/search": {
                            "get": {
                                "summary": "Search catalog",
                                "x-discovery-source": "har-live",
                                "x-scenarios": ["01_browse"],
                                "responses": {
                                    "200": {
                                        "content": {
                                            "application/json": {
                                                "schema": {"type": "object"},
                                                "example": {"code": 0, "items": ["v1"]},
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    },
                },
            )
            _write_sample_openapi(
                Path(r2["workspace_dir"]),
                {
                    "openapi": "3.0.3",
                    "servers": [{"url": "https://api.shop.example.test"}],
                    "paths": {
                        "/api/v1/items/search": {
                            "get": {
                                "summary": "Search catalog v2",
                                "x-discovery-source": "har-live",
                                "x-scenarios": ["02_search_filter"],
                                "responses": {
                                    "200": {
                                        "content": {
                                            "application/json": {
                                                "schema": {"type": "object"},
                                                "example": {"code": 0, "items": ["v2"]},
                                            }
                                        }
                                    }
                                },
                            }
                        },
                        "/api/v1/orders/submit": {
                            "post": {
                                "summary": "Submit order",
                                "x-discovery-source": "har-live",
                                "x-scenarios": ["03_checkout"],
                                "requestBody": {
                                    "content": {
                                        "application/json": {
                                            "schema": {"type": "object"},
                                            "example": {"skuId": "SKU-1"},
                                        }
                                    }
                                },
                                "responses": {
                                    "201": {
                                        "content": {
                                            "application/json": {
                                                "schema": {"type": "object"},
                                                "example": {"orderId": "ORD-1"},
                                            }
                                        }
                                    }
                                },
                            }
                        },
                    },
                },
            )

            # Mark both runs completed chronologically in DB
            from datetime import UTC, datetime, timedelta

            from reconqa.models.tables import Run

            factory = db_mod.get_session_factory()
            async with factory() as session:
                t0 = datetime.now(UTC)
                run1_row = await session.get(Run, r1["id"])
                run2_row = await session.get(Run, r2["id"])
                assert run1_row is not None and run2_row is not None
                run1_row.status = "completed"
                run1_row.started_at = t0
                run1_row.finished_at = t0 + timedelta(seconds=5)
                run2_row.status = "completed"
                run2_row.started_at = t0 + timedelta(seconds=10)
                run2_row.finished_at = t0 + timedelta(seconds=15)
                await session.commit()

            inv_res = await client.get(f"/api/v1/projects/{pid_a}/inventory")
            assert inv_res.status_code == 200, inv_res.text
            inv = inv_res.json()
            assert inv["runs_merged_count"] == 2
            assert inv["explore_sessions_count"] == 1
            assert inv["total_endpoints"] == 3
            assert inv["har_live_count"] == 2
            assert inv["js_static_count"] == 1
            assert inv["coverage_pct"] == pytest.approx(66.67, abs=0.1)
            assert inv["scenarios"] == ["01_browse", "02_search_filter", "03_checkout"]

            by_key = {(e["method"], e["path"]): e for e in inv["endpoints"]}
            # har-live upgraded js-static /api/v1/items/search, newer example won, scenarios & runs unioned
            search_ep = by_key[("GET", "/api/v1/items/search")]
            assert search_ep["discovery_source"] == "har-live"
            assert search_ep["response_example"] == {"code": 0, "items": ["v2"]}
            assert search_ep["scenarios"] == ["01_browse", "02_search_filter"]
            assert [r["run_id"] for r in search_ep["seen_in_runs"]] == [r1["id"], r2["id"]]
            assert by_key[("GET", "/api/v1/coupons/hidden")]["discovery_source"] == "js-static"

            # OpenAPI JSON download
            dl = await client.get(f"/api/v1/projects/{pid_a}/inventory/openapi.json")
            assert dl.status_code == 200
            assert "attachment;" in dl.headers.get("content-disposition", "")
            oa = dl.json()
            assert oa["openapi"] == "3.0.3"
            assert "/api/v1/orders/submit" in oa["paths"]
            assert "/api/v1/coupons/hidden" in oa["paths"]


@pytest.mark.asyncio
async def test_project_inline_replay_credential_injection_and_ssrf_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify POST /projects/{id}/replay forwards params/body, injects credentials, and blocks metadata IPs."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "replay_test.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    class EchoHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            resp = {
                "path": self.path,
                "auth_received": bool(self.headers.get("Authorization") == "Bearer secret-cred-token"),
                "cookie_received": "sid=cookie-xyz" in (self.headers.get("Cookie") or ""),
                "custom_hdr": self.headers.get("X-Custom-Check", ""),
                "body": json.loads(raw_body) if raw_body else None,
            }
            payload = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = HTTPServer(("127.0.0.1", 0), EchoHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    try:
        from reconqa.server.app import create_app

        fastapi_app = create_app()
        async with fastapi_app.router.lifespan_context(fastapi_app):
            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid = (await client.post("/api/v1/projects", json={"name": "Replay Proj"})).json()["id"]

                # Create credential with header + uploaded storage_state cookie
                prof = (
                    await client.post(
                        f"/api/v1/projects/{pid}/auth-profiles",
                        json={
                            "name": "Account A",
                            "role_tag": "account_a",
                            "extra_headers_ref": {"Authorization": "Bearer secret-cred-token"},
                        },
                    )
                ).json()
                cred_id = prof["id"]
                storage_json = json.dumps(
                    {"cookies": [{"name": "sid", "value": "cookie-xyz", "domain": "127.0.0.1"}]}
                ).encode("utf-8")
                up = await client.post(
                    f"/api/v1/projects/{pid}/auth-profiles/{cred_id}/storage-state",
                    files={"file": ("state.json", storage_json, "application/json")},
                )
                assert up.status_code == 200

                # 1. Valid replay with credential_id + query_params + custom header + JSON body
                res = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={
                        "method": "POST",
                        "url": f"http://127.0.0.1:{port}/api/v1/echo",
                        "query_params": {"env": "test"},
                        "headers": {"X-Custom-Check": "yes"},
                        "body": {"itemId": "ITEM-1"},
                        "credential_id": cred_id,
                    },
                )
                assert res.status_code == 200, res.text
                data = res.json()
                assert data["status_code"] == 200
                assert data["error"] is None
                assert data["body_json"]["path"] == "/api/v1/echo?env=test"
                assert data["body_json"]["auth_received"] is True
                assert data["body_json"]["cookie_received"] is True
                assert data["body_json"]["custom_hdr"] == "yes"
                assert data["body_json"]["body"] == {"itemId": "ITEM-1"}
                assert "secret-cred-token" not in json.dumps(data)

                # 2. SSRF cloud metadata guardrails -> 400
                for blocked_url in (
                    "http://169.254.169.254/latest/meta-data/",
                    "http://metadata.google.internal/computeMetadata/v1/",
                ):
                    r_bad = await client.post(
                        f"/api/v1/projects/{pid}/replay",
                        json={"method": "GET", "url": blocked_url},
                    )
                    assert r_bad.status_code == 400, (blocked_url, r_bad.status_code)

                # 3. Invalid scheme / method -> 400
                r_scheme = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={"method": "GET", "url": "file:///etc/passwd"},
                )
                assert r_scheme.status_code == 400

                # 4. Unreachable port returns clean 200 with status_code=0 and error message
                r_err = await client.post(
                    f"/api/v1/projects/{pid}/replay",
                    json={"method": "GET", "url": "http://127.0.0.1:1/unreachable", "timeout_sec": 1.0},
                )
                assert r_err.status_code == 200
                assert r_err.json()["status_code"] == 0
                assert r_err.json()["error"]
    finally:
        srv.shutdown()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_custom_business_roles_and_replay_matrix_bfla_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify custom role_tag values and concurrent /replay-matrix BFLA/IDOR detection."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "replay_matrix.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    class _MatrixHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            auth = self.headers.get("Authorization", "")
            if self.path == "/api/v1/admin-only":
                if auth == "Bearer tok-admin":
                    code, payload = 200, {"code": 0, "data": {"ok": True}}
                elif auth == "Bearer tok-pharmacy":
                    # HTTP 200 with business denial code 40300 -> must classify as denied
                    code, payload = 200, {"code": 40300, "msg": "无权限访问"}
                else:
                    code, payload = 401, {"code": 401, "message": "Unauthorized"}
            elif self.path == "/api/v1/bfla-leaked":
                if auth in {"Bearer tok-admin", "Bearer tok-doctor"}:
                    code, payload = 200, {"code": 0, "data": {"records": [1, 2]}}
                else:
                    code, payload = 403, {"code": 403, "message": "Forbidden"}
            else:
                code, payload = 200, {"code": 0, "public": True}

            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MatrixHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    from reconqa.server.app import create_app

    try:
        fastapi_app = create_app()
        async with fastapi_app.router.lifespan_context(fastapi_app):
            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid = (
                    await client.post("/api/v1/projects", json={"name": "Multi-Portal Health"})
                ).json()["id"]

                # Verify custom business role_tags work cleanly
                created_roles = []
                for name, role, tok in (
                    ("HQ Admin", "admin", "tok-admin"),
                    ("Store Pharmacy", "pharmacy", "tok-pharmacy"),
                    ("Clinic Doctor", "doctor", "tok-doctor"),
                    ("Care Assistant", "assistant", "tok-assistant"),
                ):
                    r = await client.post(
                        f"/api/v1/projects/{pid}/auth-profiles",
                        json={
                            "name": name,
                            "role_tag": f" {role} ",
                            "extra_headers_ref": {"Authorization": f"Bearer {tok}"},
                        },
                    )
                    assert r.status_code == 201, r.text
                    created_roles.append(r.json()["role_tag"])
                assert created_roles == ["admin", "pharmacy", "doctor", "assistant"]

                # 1. Properly protected admin-only endpoint -> only admin allowed, NO BFLA warning
                m_safe = await client.post(
                    f"/api/v1/projects/{pid}/replay-matrix",
                    json={
                        "method": "GET",
                        "url": f"http://127.0.0.1:{port}/api/v1/admin-only",
                        "include_anonymous": True,
                    },
                )
                assert m_safe.status_code == 200, m_safe.text
                safe_data = m_safe.json()
                assert len(safe_data["results"]) == 5  # Anonymous + 4 roles
                by_role = {e["role_tag"]: e for e in safe_data["results"]}
                assert by_role["admin"]["verdict"] == "allowed"
                assert by_role["pharmacy"]["verdict"] == "denied"
                assert by_role["pharmacy"]["biz_code"] == 40300
                assert by_role["anonymous"]["verdict"] == "denied"
                assert safe_data["allowed_count"] == 1
                assert safe_data["denied_count"] == 4
                assert safe_data["potential_bfla_warning"] is False

                # 2. Multi-role allowed endpoint (admin + doctor) -> potential_bfla_warning=True!
                m_bfla = await client.post(
                    f"/api/v1/projects/{pid}/replay-matrix",
                    json={
                        "method": "GET",
                        "url": f"http://127.0.0.1:{port}/api/v1/bfla-leaked",
                        "include_anonymous": True,
                    },
                )
                assert m_bfla.status_code == 200, m_bfla.text
                bfla_data = m_bfla.json()
                assert bfla_data["allowed_count"] == 2
                assert bfla_data["denied_count"] == 3
                assert bfla_data["potential_bfla_warning"] is True
                assert "tok-admin" not in json.dumps(bfla_data)

                # 3. Anonymous-allowed endpoint -> potential_bfla_warning=True!
                m_anon = await client.post(
                    f"/api/v1/projects/{pid}/replay-matrix",
                    json={
                        "method": "GET",
                        "url": f"http://127.0.0.1:{port}/api/v1/public-open",
                        "include_anonymous": True,
                    },
                )
                assert m_anon.status_code == 200
                assert m_anon.json()["potential_bfla_warning"] is True

                # 4. PATCH /projects/{id} rename & description update + blank name rejection + 404
                r_patch = await client.patch(
                    f"/api/v1/projects/{pid}",
                    json={
                        "name": "  Unified Health Suite  ",
                        "description": "Updated multi-portal description",
                    },
                )
                assert r_patch.status_code == 200, r_patch.text
                assert r_patch.json()["name"] == "Unified Health Suite"
                assert r_patch.json()["description"] == "Updated multi-portal description"

                r_blank = await client.patch(
                    f"/api/v1/projects/{pid}",
                    json={"name": "   "},
                )
                assert r_blank.status_code in (400, 422)

                r_missing = await client.patch(
                    "/api/v1/projects/nonexistent-project-id",
                    json={"name": "Ghost"},
                )
                assert r_missing.status_code == 404
    finally:
        srv.shutdown()
        await asyncio.sleep(0)


def test_extract_auth_headers_skips_analytics_cookies_and_matrix_verdict_case_insensitive(
    tmp_path: Path,
) -> None:
    """Verify _extract_auth_headers_from_hars skips telemetry-only cookies and _classify_matrix_verdict handles Code/Msg."""
    from reconqa.server.routers.acceptance import _extract_auth_headers_from_hars
    from reconqa.server.routers.projects import _classify_matrix_verdict

    def _write_har(name: str, headers: list[dict[str, str]]) -> Path:
        p = tmp_path / name
        p.write_text(
            json.dumps(
                {
                    "log": {
                        "version": "1.2",
                        "entries": [
                            {
                                "request": {
                                    "method": "GET",
                                    "url": "https://api.example.com/api/v1/profile",
                                    "headers": headers,
                                },
                                "response": {
                                    "status": 200,
                                    "content": {"mimeType": "application/json", "text": '{"code":0}'},
                                },
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        return p

    # 1. Anonymous HAR with only _bl_uid / _ga cookies -> extracts nothing
    anon_har = _write_har(
        "anon.har",
        [{"name": "Cookie", "value": "_bl_uid=arms-123; _ga=GA1.2.3.4; acw_tc=waf-99"}],
    )
    extracted_anon, host_anon = _extract_auth_headers_from_hars([anon_har])
    assert extracted_anon == {}
    assert host_anon == ""

    # 2. Authenticated HAR with real session cookie + Bearer token -> extracts both
    auth_har = _write_har(
        "auth.har",
        [
            {"name": "Cookie", "value": "_bl_uid=arms-123; sid=real-session-abc"},
            {"name": "Authorization", "value": "Bearer live-jwt-token"},
        ],
    )
    extracted_auth, host_auth = _extract_auth_headers_from_hars([auth_har])
    assert host_auth == "api.example.com"
    assert extracted_auth["Authorization"] == "Bearer live-jwt-token"
    assert "sid=real-session-abc" in extracted_auth["Cookie"]

    # 3. _classify_matrix_verdict handles case-insensitive Code/Msg & auth-denial phrases
    assert _classify_matrix_verdict(200, {"Code": 2002, "Msg": "未登录"}) == (2002, "denied")
    assert _classify_matrix_verdict(200, {"code": 84101, "msg": "登录信息已失效"}) == (84101, "denied")
    assert _classify_matrix_verdict(200, {"Code": 84103, "Msg": "登录悠悠有品，解锁更多功能"}) == (
        84103,
        "denied",
    )
    assert _classify_matrix_verdict(200, {"Code": 0, "Msg": "OK", "Data": {"ok": True}}) == (0, "allowed")
