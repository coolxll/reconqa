"""Tests for Jira Workspace contracts: project-scoped Explore, Master Inventory, and safe Replay."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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
