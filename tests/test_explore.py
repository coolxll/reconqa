"""Integration tests for Explore / Seed-cURL Discovery & Promote-to-Run API."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from reconqa.config import settings


class _MockExploreServer:
    """In-process server exposing /, /static/app.js, /openapi.json, and /api/v1/items/search."""

    def __init__(self) -> None:
        openapi_doc = {
            "openapi": "3.0.3",
            "info": {"title": "Explore Demo API", "version": "1.0.0"},
            "paths": {
                "/api/v1/orders/{orderId}": {
                    "get": {"summary": "Get order details", "responses": {"200": {"description": "OK"}}}
                },
                "/api/v1/coupons/redeem": {
                    "post": {"summary": "Redeem coupon", "responses": {"200": {"description": "OK"}}}
                },
            },
        }
        js_bundle = """
const routes = [
  { path: "/dashboard", name: "Dashboard" },
  { path: "/billing", name: "Billing" }
];
function refundOrder(orderId) {
  return axios.post("/api/v1/billing/refund", { data: { orderId } });
}
function loadSettings() {
  return fetch("/api/v1/profile/settings");
}
"""
        chunk_js = """
function fetchAuditLog() {
  return axios.get("/api/v1/admin/audit-logs");
}
"""
        index_html = (
            "<!doctype html><html><head><title>Demo</title>"
            '<link rel="modulepreload" href="/static/chunk.js"></head>'
            '<body><div id="app"></div><script type="module" src="/static/app.js"></script></body></html>'
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(self, code: int, body: str, content_type: str) -> None:
                raw = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self._send(200, index_html, "text/html; charset=utf-8")
                elif path == "/static/app.js":
                    self._send(200, js_bundle, "application/javascript; charset=utf-8")
                elif path == "/static/chunk.js":
                    self._send(200, chunk_js, "application/javascript; charset=utf-8")
                elif path == "/openapi.json":
                    self._send(200, json.dumps(openapi_doc), "application/json")
                elif path == "/api/v1/items/search":
                    self._send(200, '{"code":0,"data":[]}', "application/json")
                else:
                    self._send(404, '{"detail":"not found"}', "application/json")

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> _MockExploreServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=3)


@pytest.mark.asyncio
async def test_explore_from_seed_curl_and_promote_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "explore_test.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    # Disable external PD tools during this deterministic unit/integration test
    from reconqa.config import Settings

    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)

    from reconqa.models import db as db_mod

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    with _MockExploreServer() as mock_srv:
        fastapi_app = create_app()
        async with fastapi_app.router.lifespan_context(fastapi_app):
            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                seed_curl = (
                    f"curl '{mock_srv.base_url}/api/v1/items/search?q=laptop' "
                    "-H 'Accept: application/json' "
                    "-H 'Authorization: Bearer secret-seed-token-999'"
                )
                r_exp = await client.post(
                    "/api/v1/explore",
                    json={
                        "seed_curl": seed_curl,
                        "mode": "passive",
                        "probe_openapi_docs": True,
                        "crawl_js": True,
                        "depth": 1,
                    },
                )
                assert r_exp.status_code in (200, 201), r_exp.text
                exp = r_exp.json()
                explore_id = exp["explore_id"]
                assert exp["seed"]["base_url"] == mock_srv.base_url
                assert any(
                    h.lower() == "authorization"
                    for h in exp["seed"].get("auth_headers_detected", [])
                )

                # Ensure raw secret is never persisted in explore_result.json or response
                exp_text = json.dumps(exp)
                assert "secret-seed-token-999" not in exp_text
                persisted = ws_dir / "explores" / explore_id / "explore_result.json"
                assert persisted.is_file()
                assert "secret-seed-token-999" not in persisted.read_text(encoding="utf-8")

                # Verify OpenAPI probe + JS bundle extraction discovered extra endpoints
                endpoints = exp["discovered_endpoints"]
                paths = {e["path"] for e in endpoints}
                sources = {e["source"] for e in endpoints}
                assert "/api/v1/coupons/redeem" in paths, f"OpenAPI probe missed endpoint: {paths}"
                assert "/api/v1/billing/refund" in paths, f"JS crawl missed endpoint: {paths}"
                assert "/api/v1/admin/audit-logs" in paths, f"modulepreload JS missed endpoint: {paths}"
                assert "openapi_probe" in sources
                assert sources & {"js_static_inferred", "js_static"}
                assert len(exp.get("openapi_docs_found", [])) >= 1

                # Verify list & get endpoints
                r_list = await client.get("/api/v1/explore")
                assert r_list.status_code == 200
                assert any(item["explore_id"] == explore_id for item in r_list.json())

                r_get = await client.get(f"/api/v1/explore/{explore_id}")
                assert r_get.status_code == 200
                assert r_get.json()["explore_id"] == explore_id

                # Promote selected discovered endpoints to a real Test Run
                selected_ids = [
                    e["id"]
                    for e in endpoints
                    if e["path"] in ("/api/v1/items/search", "/api/v1/coupons/redeem", "/api/v1/billing/refund")
                ]
                r_prom = await client.post(
                    f"/api/v1/explore/{explore_id}/promote-to-run",
                    json={
                        "project_name": "Promoted Explore Project",
                        "modules": ["merge", "java_gen"],
                        "selected_endpoints": selected_ids,
                    },
                )
                assert r_prom.status_code in (200, 201), r_prom.text
                run_id = r_prom.json()["id"]

                # Wait for promoted run to finish cleanly
                run_data = {}
                for _ in range(100):
                    r_r = await client.get(f"/api/v1/runs/{run_id}")
                    assert r_r.status_code == 200
                    run_data = r_r.json()
                    if run_data["status"] in ("succeeded", "completed", "failed"):
                        break
                    await asyncio.sleep(0.1)
                assert run_data["status"] in ("succeeded", "completed"), run_data

                # Verify OpenAPI in promoted run includes discovered endpoints
                r_oa = await client.get(f"/api/v1/runs/{run_id}/openapi?format=json")
                assert r_oa.status_code == 200
                oa_paths = r_oa.json().get("paths", {})
                assert "/api/v1/coupons/redeem" in oa_paths or "/api/v1/billing/refund" in oa_paths

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


def test_explore_unquoted_html_webpack_chunks_axios_and_sibling_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify #90: unquoted script src, Webpack lazy chunks, generic Axios url:, and sibling hosts."""
    from reconqa.config import Settings
    from reconqa.modules.discovery.seed_explorer import explore_from_seed
    from reconqa.modules.recorder.js_extractor import extract_js_endpoints

    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)

    # 1. Direct js_extractor test for generic Axios url:, .post(), and same-root sibling URL
    js_dir = tmp_path / "js_only"
    js_dir.mkdir(parents=True, exist_ok=True)
    (js_dir / "app.1234.js").write_text(
        """
export function listUsers(params) {
  return request({ url: '/system/users/list', method: 'get', params });
}
export function exportSettlement(data) {
  return http.post('/finance/settlement/export', data);
}
const API_BASE = "https://api.example.test/v2/orders/summary";
const STATIC_IMG = "/static/images/logo.png";
""",
        encoding="utf-8",
    )
    js_out = tmp_path / "js_endpoints.json"
    extract_js_endpoints(js_dir, js_out, target_domain="example.test")
    extracted = json.loads(js_out.read_text(encoding="utf-8"))
    ep_paths = {e["path"] for e in extracted.get("endpoints", [])}
    assert "/system/users/list" in ep_paths, f"Missing Axios url: path in {ep_paths}"
    assert "/finance/settlement/export" in ep_paths, f"Missing .post() path in {ep_paths}"
    assert any("/v2/orders/summary" in p for p in ep_paths), f"Missing sibling URL path in {ep_paths}"
    assert not any(p.endswith(".png") for p in ep_paths), f"Static asset leaked: {ep_paths}"

    # 2. Live mock server with unquoted html-webpack-plugin <script src=...> + Webpack chunk map
    unquoted_html = (
        "<!doctype html><html><head><title>Portal</title></head>"
        "<body><div id=app></div>"
        "<script src=/static/js/chunk-elementUI.99999999.js></script>"
        "<script src=/static/js/app.1234abcd.js></script>"
        "</body></html>"
    )
    app_entry_js = (
        'function chunkUrl(e){return "static/js/"+(e)+"."+({"chunk-orders":"abcd1234","chunk-libs":"deadbeef"}[e])+".js"}'
        'const SIBLING="https://api.example.test/v1/health";'
        'function getMenu(){return request({url:"/system/menu/tree",method:"get"});}'
    )
    orders_chunk_js = (
        'function approveOrder(id){return request({url:"/workflow/order/approve",method:"post",data:{orderId:id}});}'
    )

    class WebpackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: str, ctype: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            p = self.path.split("?", 1)[0]
            if p == "/":
                self._send(200, unquoted_html, "text/html")
            elif p == "/static/js/app.1234abcd.js":
                self._send(200, app_entry_js, "application/javascript")
            elif p == "/static/js/chunk-orders.abcd1234.js":
                self._send(200, orders_chunk_js, "application/javascript")
            elif p == "/static/js/chunk-elementUI.99999999.js":
                self._send(200, "/* huge vendor */", "application/javascript")
            else:
                self._send(404, "not found", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), WebpackHandler)
    base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        res = explore_from_seed(
            tmp_path / "explore_webpack",
            target_url=base_url,
            mode="passive",
            probe_openapi_docs=False,
            crawl_js=True,
            timeout=15,
        )
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=3)

    discovered_paths = {e["path"] for e in res.get("discovered_endpoints", [])}
    assert "/system/menu/tree" in discovered_paths, f"Unquoted app.js missed: {discovered_paths}"
    assert "/workflow/order/approve" in discovered_paths, f"Webpack lazy chunk missed: {discovered_paths}"


@pytest.mark.asyncio
async def test_explore_merges_multiple_scans_by_root_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify #111: Pass 1 (seed_curl) + Pass 2 (target_url) on same root_domain merge into 1 domain inventory."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "explore_merge_test.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    from reconqa.config import Settings
    from reconqa.models import db as db_mod
    from reconqa.modules.discovery import seed_explorer

    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    # Stub run_discovery to emit subdomains on pass 2 deterministically
    call_counter = {"n": 0}

    def fake_run_discovery(target: str, out_dir: Path, **kwargs: Any) -> dict[str, Any]:
        call_counter["n"] += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        if call_counter["n"] >= 2:
            (out_dir / "subdomains.jsonl").write_text(
                '{"host":"admin.shop.example.test"}\n{"host":"api.shop.example.test"}\n',
                encoding="utf-8",
            )
            (out_dir / "urlfinder_urls.jsonl").write_text(
                '{"url":"https://api.shop.example.test/api/v1/legacy/report"}\n',
                encoding="utf-8",
            )
        return {"warnings": []}

    monkeypatch.setattr(seed_explorer, "run_discovery", fake_run_discovery)

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Pass 1: seed_curl on admin.shop.example.test
            r1 = await client.post(
                "/api/v1/explore",
                json={
                    "seed_curl": (
                        "curl 'https://admin.shop.example.test/api/v1/orders/list' "
                        "-H 'Accept: application/json'"
                    ),
                    "mode": "passive",
                    "probe_openapi_docs": False,
                    "crawl_js": False,
                },
            )
            assert r1.status_code in (200, 201), r1.text
            d1 = r1.json()
            id1 = d1["explore_id"]
            assert d1["summary"]["scans_count"] == 1

            # Pass 2: target_url on api.shop.example.test (SAME registrable root_domain: example.test!)
            r2 = await client.post(
                "/api/v1/explore",
                json={
                    "seed_curl": (
                        "curl 'https://api.shop.example.test/api/v1/payments/status' "
                        "-H 'Accept: application/json'"
                    ),
                    "mode": "passive",
                    "probe_openapi_docs": False,
                    "crawl_js": False,
                },
            )
            assert r2.status_code in (200, 201), r2.text
            d2 = r2.json()
            id2 = d2["explore_id"]

            # Must merge into the SAME domain-keyed explore_id
            assert id2 == id1, f"Expected same domain explore_id, got {id1} vs {id2}"
            assert d2["summary"]["scans_count"] == 2
            assert len(d2.get("scans", [])) == 2

            merged_paths = {e["path"] for e in d2["discovered_endpoints"]}
            assert "/api/v1/orders/list" in merged_paths
            assert "/api/v1/payments/status" in merged_paths
            assert "admin.shop.example.test" in d2.get("subdomains", [])
            assert "api.shop.example.test" in d2.get("subdomains", [])

            # GET /api/v1/explore returns ONLY 1 consolidated domain entry with scans_count == 2
            r_list = await client.get("/api/v1/explore")
            assert r_list.status_code == 200
            items = r_list.json()
            assert len(items) == 1, f"Expected 1 merged domain entry, got {items}"
            assert items[0]["explore_id"] == id1
            assert items[0]["scans_count"] == 2

            # DELETE /api/v1/explore/{explore_id}: 200 first time, 404 second time
            r_del = await client.delete(f"/api/v1/explore/{id1}")
            assert r_del.status_code == 200, r_del.text
            assert r_del.json().get("deleted") is True
            assert (await client.get("/api/v1/explore")).json() == []

            r_del2 = await client.delete(f"/api/v1/explore/{id1}")
            assert r_del2.status_code == 404

    if db_mod._engine is not None:
        await db_mod._engine.dispose()

