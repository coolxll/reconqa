"""Unit & integration tests for Module 3 (Unified Data Hub + Config)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from reconqa.config import (
    ProjectConfig,
    doctor_check,
    load_project_config,
    write_default_project_config,
)
from reconqa.modules.hub.correlation import analyze_correlations
from reconqa.modules.hub.coverage_diff import compute_coverage_diff
from reconqa.modules.hub.har_parser import (
    _is_static_asset,
    normalize_path,
    parse_har_dir,
    parse_har_file,
)
from reconqa.modules.hub.openapi_merger import merge_to_openapi
from reconqa.modules.recorder.har_importer import import_hars
from reconqa.modules.recorder.js_extractor import extract_js_endpoints


def test_project_config_roundtrip_and_doctor(tmp_path: Path) -> None:
    cfg_path = tmp_path / "reconqa.yaml"
    written = write_default_project_config(cfg_path)
    assert written.is_file()
    cfg = load_project_config(written)
    assert isinstance(cfg, ProjectConfig)

    doc = doctor_check()
    assert isinstance(doc, dict)
    for required_tool in ("subfinder", "katana", "httpx", "nuclei", "k6", "mvn", "docker"):
        assert required_tool in doc


def test_har_parser_path_normalization_and_filtering(fixtures_har_dir: Path) -> None:
    # Numeric ID
    norm_num, params_num = normalize_path("/api/v1/items/9823410")
    assert "{" in norm_num and "}" in norm_num
    assert "9823410" in params_num.values()

    # UUID
    uuid_val = "550e8400-e29b-41d4-a716-446655440000"
    norm_uuid, params_uuid = normalize_path(f"/api/v1/orders/{uuid_val}")
    assert "{" in norm_uuid and uuid_val not in norm_uuid
    assert uuid_val in params_uuid.values()

    # 24-hex ObjectId
    oid = "507f1f77bcf86cd799439011"
    norm_oid, params_oid = normalize_path(f"/api/v1/users/{oid}/profile")
    assert "{" in norm_oid and oid not in norm_oid
    assert oid in params_oid.values()

    # Parse single HAR file & whole directory (filtering OPTIONS and static JS)
    entries_01 = parse_har_file(fixtures_har_dir / "01_browse_search.har")
    assert all(e.method != "OPTIONS" for e in entries_01)
    assert all(not e.url.endswith(".js") for e in entries_01)
    assert len(entries_01) == 2

    all_entries = parse_har_dir(fixtures_har_dir)
    assert len(all_entries) >= 7
    scenarios = {e.scenario for e in all_entries}
    assert {"01_browse_search", "02_detail_cart", "03_user_order"}.issubset(scenarios)


def test_openapi_merger_three_tier_discovery_and_biz_success(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    har_dir = tmp_path / "har"
    import_hars([fixtures_har_dir], har_dir, extract_js=True)
    js_ep_file = har_dir / "js_endpoints.json"
    extract_js_endpoints(har_dir / "js_bundles", js_ep_file)

    hub_dir = tmp_path / "hub"
    spec = merge_to_openapi(
        har_dir,
        hub_dir,
        js_endpoints_path=js_ep_file,
        target_domain="shop.example.test",
    )
    assert (hub_dir / "openapi.yaml").is_file()
    assert (hub_dir / "openapi.json").is_file()
    assert spec["openapi"].startswith("3.0")

    loaded_yaml = yaml.safe_load((hub_dir / "openapi.yaml").read_text(encoding="utf-8"))
    paths = loaded_yaml["paths"]
    assert "/api/v1/items/search" in paths

    search_op = paths["/api/v1/items/search"]["get"]
    assert search_op["x-discovery-source"] == "har-live"
    assert "01_browse_search" in search_op["x-scenarios"]
    assert "x-perf-baseline" in search_op
    assert search_op["x-perf-baseline"]["count"] >= 2
    assert search_op.get("x-biz-success", {}).get("field") == "code"
    assert search_op.get("x-biz-success", {}).get("value") == 0

    # Check JS-only uncovered endpoints merged with js-static or js-static-inferred
    js_sources = set()
    for methods in paths.values():
        for op in methods.values():
            if isinstance(op, dict) and "x-discovery-source" in op:
                js_sources.add(op["x-discovery-source"])
    assert "har-live" in js_sources
    assert js_sources & {"js-static", "js-static-inferred"}

    # Ensure no raw auth bearer secret leaked into openapi.yaml
    yaml_text = (hub_dir / "openapi.yaml").read_text(encoding="utf-8")
    assert "token-account-a" not in yaml_text


def test_correlation_filters_static_constants_detects_chain_and_cleanup(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    hub_dir = tmp_path / "hub"
    report = analyze_correlations(fixtures_har_dir, hub_dir)

    assert (hub_dir / "correlation.json").is_file()
    assert (hub_dir / "correlation.mmd").is_file()

    # TENANT_STATIC_999 must be filtered as a high-frequency static constant
    filtered = report.get("filtered_constants", [])
    assert "TENANT_STATIC_999" in filtered, f"Expected TENANT_STATIC_999 in filtered_constants, got {filtered}"

    # Dynamic IDs must be correlated
    variables = report.get("variables", [])
    samples = {
        v.get("producer", {}).get("sample")
        for v in variables
        if isinstance(v, dict)
    }
    correlated_text = json.dumps(variables)
    assert "TENANT_STATIC_999" not in samples, "TENANT_STATIC_999 should NOT be a correlated variable"
    assert "SKU-55102" in correlated_text, f"SKU-55102 missing from correlated variables: {variables}"
    assert "CART-77309" in correlated_text, f"CART-77309 missing from correlated variables: {variables}"
    assert "ITEM-88421" in correlated_text, f"ITEM-88421 missing from correlated variables: {variables}"

    # At least one chain should exist and detect DELETE /api/v1/cart/items/... as cleanup_step
    chains = report.get("chains", [])
    assert len(chains) >= 1
    cleanup_steps = [c.get("cleanup_step") for c in chains if c.get("cleanup_step")]
    assert len(cleanup_steps) >= 1, f"Expected cleanup_step on cart chain, chains={chains}"
    assert any(
        str(cs.get("method", "")).upper() == "DELETE"
        and "/cart/items/" in str(cs.get("path", ""))
        for cs in cleanup_steps
    )

    # Mermaid diagram validity
    mmd = (hub_dir / "correlation.mmd").read_text(encoding="utf-8")
    assert "flowchart" in mmd or "sequenceDiagram" in mmd
    assert "-->" in mmd or "->>" in mmd


def test_coverage_diff_identifies_uncovered_js_endpoints(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    har_dir = tmp_path / "har"
    import_hars([fixtures_har_dir], har_dir, extract_js=True)
    js_ep_file = har_dir / "js_endpoints.json"
    extract_js_endpoints(har_dir / "js_bundles", js_ep_file)

    hub_dir = tmp_path / "hub"
    merge_to_openapi(har_dir, hub_dir, js_endpoints_path=js_ep_file)

    diff = compute_coverage_diff(hub_dir / "openapi.yaml", hub_dir)
    assert (hub_dir / "coverage_diff.json").is_file()
    assert (hub_dir / "coverage_diff.md").is_file()
    assert isinstance(diff, dict)

    diff_text = (hub_dir / "coverage_diff.json").read_text(encoding="utf-8")
    assert "/api/v1/coupons/claim" in diff_text
    assert "/api/v1/user/preferences" in diff_text


def test_correlation_filters_preexisting_session_auth_token_but_keeps_real_login_and_ids(
    tmp_path: Path,
) -> None:
    """Verify #121: pre-existing session accessToken echoed by /my-info is filtered & redacted,
    while real newly-minted /login token and business IDs still correlate."""
    har_dir = tmp_path / "har_auth_noise"
    har_dir.mkdir(parents=True, exist_ok=True)
    preexisting_tok = "preexisting_session_jwt_9988776655"
    fresh_login_tok = "fresh_minted_login_token_1122334455"

    def _entry(ts: str, method: str, url: str, headers: list[dict], resp_obj: dict) -> dict:
        body = json.dumps(resp_obj)
        return {
            "startedDateTime": ts,
            "time": 40.0,
            "request": {
                "method": method,
                "url": url,
                "httpVersion": "HTTP/1.1",
                "headers": headers,
                "queryString": [],
                "cookies": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "httpVersion": "HTTP/1.1",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
                "content": {"size": len(body), "mimeType": "application/json", "text": body},
            },
            "timings": {"send": 1, "wait": 30, "receive": 9},
        }

    # Scenario 1: Pre-existing token already in Step 0 Authorization header, echoed by /system/p/my-info
    har_preexisting = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry(
                    "2026-01-15T11:00:01.000Z",
                    "GET",
                    "https://shop.example.test/api/v1/home",
                    [{"name": "Authorization", "value": f"Bearer {preexisting_tok}"}],
                    {"code": 0, "data": {"banner": "welcome"}},
                ),
                _entry(
                    "2026-01-15T11:00:02.000Z",
                    "GET",
                    "https://shop.example.test/system/p/my-info",
                    [{"name": "Authorization", "value": f"Bearer {preexisting_tok}"}],
                    {"code": 0, "data": {"accessToken": preexisting_tok, "userId": "USR-44556"}},
                ),
                _entry(
                    "2026-01-15T11:00:03.000Z",
                    "GET",
                    "https://shop.example.test/api/v1/orders/list?userId=USR-44556",
                    [{"name": "Authorization", "value": f"Bearer {preexisting_tok}"}],
                    {"code": 0, "data": {"list": []}},
                ),
            ],
        }
    }
    (har_dir / "01_preexisting.har").write_text(json.dumps(har_preexisting), encoding="utf-8")

    # Scenario 2: Real fresh login issuing a brand-new token used afterwards
    har_fresh = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry(
                    "2026-01-15T11:05:01.000Z",
                    "POST",
                    "https://shop.example.test/api/auth/login",
                    [{"name": "Content-Type", "value": "application/json"}],
                    {"code": 0, "data": {"accessToken": fresh_login_tok}},
                ),
                _entry(
                    "2026-01-15T11:05:02.000Z",
                    "GET",
                    "https://shop.example.test/api/v1/profile/me",
                    [{"name": "Authorization", "value": f"Bearer {fresh_login_tok}"}],
                    {"code": 0, "data": {"name": "Alice"}},
                ),
            ],
        }
    }
    (har_dir / "02_fresh_login.har").write_text(json.dumps(har_fresh), encoding="utf-8")

    out_dir = tmp_path / "hub_auth_noise"
    report = analyze_correlations(har_dir, out_dir)
    corr_json = (out_dir / "correlation.json").read_text(encoding="utf-8")

    # Pre-existing session token MUST NOT appear as a correlated variable AND raw token MUST NOT leak into filtered_constants
    var_samples = {
        v.get("producer", {}).get("sample")
        for v in report.get("variables", [])
        if isinstance(v, dict)
    }
    assert preexisting_tok not in var_samples, f"Pre-existing session token should be filtered: {report}"
    assert preexisting_tok not in json.dumps(report.get("filtered_constants", [])), (
        "Raw pre-existing session token must be REDACTED in filtered_constants!"
    )
    assert any("session-auth" in str(fc) or "accessToken" in str(fc) for fc in report.get("filtered_constants", []))

    # Business userId AND fresh /api/auth/login token MUST still correlate
    assert "USR-44556" in corr_json, f"Expected business userId USR-44556 to correlate: {report}"
    assert fresh_login_tok in var_samples or "fresh_minted_login_token" in corr_json, (
        f"Expected real newly-minted login token to correlate: {report}"
    )


def test_har_parser_filters_spa_html_routes_and_keeps_json_apis(tmp_path: Path) -> None:
    """Verify extensionless SPA text/html routes are filtered while real JSON APIs & non-nav errors survive."""
    # Backwards-compatible 4-arg call signature on _is_static_asset
    assert _is_static_asset("/merchant/orders", "text/html", None, []) is True
    assert _is_static_asset("/api/v1/orders", "text/html", {"code": 0}, []) is False

    def _har_entry(
        method: str,
        url: str,
        status: int,
        resp_mime: str,
        resp_text: str,
        req_headers: list[dict[str, str]] | None = None,
    ) -> dict:
        return {
            "startedDateTime": "2026-11-24T01:00:00.000Z",
            "time": 12.0,
            "request": {
                "method": method,
                "url": url,
                "httpVersion": "HTTP/2",
                "headers": req_headers or [],
                "queryString": [],
                "cookies": [],
            },
            "response": {
                "status": status,
                "statusText": "OK",
                "httpVersion": "HTTP/2",
                "headers": [{"name": "Content-Type", "value": resp_mime}],
                "cookies": [],
                "content": {"size": len(resp_text), "mimeType": resp_mime, "text": resp_text},
            },
            "timings": {"send": 1, "wait": 8, "receive": 3},
        }

    har_payload = {
        "log": {
            "version": "1.2",
            "entries": [
                # Extensionless SPA navigation routes -> MUST be dropped
                _har_entry(
                    "GET",
                    "https://app.example.com/merchant/orders",
                    200,
                    "text/html; charset=utf-8",
                    "<!doctype html><html><body>SPA</body></html>",
                    req_headers=[
                        {"name": "sec-fetch-dest", "value": "document"},
                        {"name": "sec-fetch-mode", "value": "navigate"},
                    ],
                ),
                _har_entry(
                    "GET",
                    "https://app.example.com/merchant/finance",
                    200,
                    "text/html",
                    "<html>Finance Shell</html>",
                ),
                _har_entry(
                    "GET",
                    "https://app.example.com/login",
                    302,
                    "text/html",
                    "<html>Redirect</html>",
                ),
                _har_entry(
                    "GET",
                    "https://app.example.com/market",
                    404,
                    "application/xhtml+xml",
                    "<html>Not Found</html>",
                    req_headers=[{"name": "Sec-Fetch-Mode", "value": "navigate"}],
                ),
                # Non-navigation 502 HTML gateway error on an API path -> KEPT
                _har_entry(
                    "GET",
                    "https://app.example.com/api/v1/broken",
                    502,
                    "text/html",
                    "<html>502 Bad Gateway</html>",
                    req_headers=[
                        {"name": "sec-fetch-dest", "value": "empty"},
                        {"name": "sec-fetch-mode", "value": "cors"},
                    ],
                ),
                # JSON payload mislabelled as text/html -> MUST be kept (response_json is not None)
                _har_entry(
                    "GET",
                    "https://app.example.com/api/v1/legacy-json",
                    200,
                    "text/html",
                    json.dumps({"code": 0, "data": {"ok": True}}),
                ),
                # Normal JSON API -> KEPT
                _har_entry(
                    "POST",
                    "https://app.example.com/api/v1/orders/list",
                    200,
                    "application/json",
                    json.dumps({"code": 0, "data": []}),
                ),
            ],
        }
    }
    har_file = tmp_path / "spa_routes.har"
    har_file.write_text(json.dumps(har_payload), encoding="utf-8")

    entries = parse_har_file(har_file)
    paths = [e.normalized_path for e in entries]
    assert "/merchant/orders" not in paths
    assert "/merchant/finance" not in paths
    assert "/login" not in paths
    assert "/market" not in paths
    assert set(paths) == {"/api/v1/broken", "/api/v1/legacy-json", "/api/v1/orders/list"}


def test_openapi_merger_multihost_origins_and_clean_custom_headers(tmp_path: Path) -> None:
    """Verify per-operation x-host/x-origin, frequency-ordered servers, and trace header filtering."""
    har_dir = tmp_path / "multihost_har"
    har_dir.mkdir(parents=True, exist_ok=True)

    def _entry(ts: str, method: str, url: str, headers: list[dict[str, str]]) -> dict:
        body = json.dumps({"code": 0, "data": {"ok": True}})
        return {
            "startedDateTime": ts,
            "time": 20.0,
            "request": {
                "method": method,
                "url": url,
                "httpVersion": "HTTP/2",
                "headers": headers,
                "queryString": [],
                "cookies": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "httpVersion": "HTTP/2",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
                "content": {"size": len(body), "mimeType": "application/json", "text": body},
            },
            "timings": {"send": 1, "wait": 15, "receive": 4},
        }

    har_payload = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry(
                    "2026-11-24T02:00:01.000Z",
                    "GET",
                    "https://pc-api.example.com/api/v1/orders",
                    [
                        {"name": "App-Version", "value": "3.2.1"},
                        {"name": "platform", "value": "web"},
                        {"name": "b3", "value": "80f198ee56343ba864fe8b2a57d3eff7-e457b5a2e4d86bd1-1"},
                        {"name": "traceparent", "value": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
                        {"name": "tracestate", "value": "rojo=00f067aa0ba902b7"},
                        {"name": "x-b3-traceid", "value": "deadbeef"},
                        {"name": "x-b3-spanid", "value": "cafebabe"},
                        {"name": "x-b3-sampled", "value": "1"},
                        {"name": "sentry-trace", "value": "1234-5678-1"},
                        {"name": "baggage", "value": "sentry-environment=prod"},
                    ],
                ),
                _entry(
                    "2026-11-24T02:00:02.000Z",
                    "GET",
                    "https://pc-api.example.com/api/v1/orders",
                    [
                        {"name": "App-Version", "value": "3.2.1"},
                        {"name": "platform", "value": "web"},
                    ],
                ),
                _entry(
                    "2026-11-24T02:00:03.000Z",
                    "POST",
                    "https://acctrade-api.example.com/api/v1/settlement/list",
                    [
                        {"name": "AppVersion", "value": "2.0.0"},
                        {"name": "appType", "value": "merchant"},
                        {"name": "secret-v", "value": "v2"},
                        {"name": "deviceId", "value": "dev-42"},
                        {"name": "x-b3-parentspanid", "value": "9999"},
                    ],
                ),
            ],
        }
    }
    (har_dir / "01_multihost.har").write_text(json.dumps(har_payload), encoding="utf-8")

    out_dir = tmp_path / "hub_out"
    spec = merge_to_openapi(har_dir, out_dir, target_domain="example.com")

    assert [s["url"] for s in spec["servers"]] == [
        "https://pc-api.example.com",
        "https://acctrade-api.example.com",
    ]

    orders_op = spec["paths"]["/api/v1/orders"]["get"]
    assert orders_op["x-host"] == "pc-api.example.com"
    assert orders_op["x-origin"] == "https://pc-api.example.com"
    assert orders_op["x-custom-headers"] == {"App-Version": "3.2.1", "platform": "web"}

    settle_op = spec["paths"]["/api/v1/settlement/list"]["post"]
    assert settle_op["x-host"] == "acctrade-api.example.com"
    assert settle_op["x-origin"] == "https://acctrade-api.example.com"
    assert settle_op["x-custom-headers"] == {
        "AppVersion": "2.0.0",
        "appType": "merchant",
        "secret-v": "v2",
        "deviceId": "dev-42",
    }

    # With configured project base_url, configured URL comes first without dropping observed origins
    cfg = ProjectConfig.model_validate(
        {"project_name": "MultiHost", "target": {"domain": "example.com", "base_url": "https://gw.example.com/"}}
    )
    spec_cfg = merge_to_openapi(har_dir, tmp_path / "hub_cfg", project_config=cfg)
    assert [s["url"] for s in spec_cfg["servers"]] == [
        "https://gw.example.com",
        "https://pc-api.example.com",
        "https://acctrade-api.example.com",
    ]


def test_har_parser_ignores_analytics_waf_cookies_for_has_auth(tmp_path: Path) -> None:
    """Verify telemetry/WAF cookies (_bl_uid, _ga, acw_tc) do not trigger has_auth while real session/Bearer do."""

    def _entry(url: str, headers: list[dict[str, str]], cookies: list[dict[str, str]] | None = None) -> dict:
        body = json.dumps({"code": 0, "data": {}})
        return {
            "startedDateTime": "2026-11-24T03:00:00.000Z",
            "time": 10.0,
            "request": {
                "method": "GET",
                "url": url,
                "httpVersion": "HTTP/2",
                "headers": headers,
                "queryString": [],
                "cookies": cookies or [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "httpVersion": "HTTP/2",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
                "content": {"size": len(body), "mimeType": "application/json", "text": body},
            },
            "timings": {"send": 1, "wait": 5, "receive": 4},
        }

    har_payload = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry(
                    "https://api.example.com/api/v1/public-telemetry",
                    [{"name": "Cookie", "value": "_bl_uid=abc123; _ga=GA1.1.12345; acw_tc=xyz; lang=zh"}],
                    cookies=[
                        {"name": "_bl_uid", "value": "abc123"},
                        {"name": "_ga", "value": "GA1.1.12345"},
                    ],
                ),
                _entry(
                    "https://api.example.com/api/v1/session-auth",
                    [{"name": "Cookie", "value": "_bl_uid=abc123; session_id=real-sess-999"}],
                    cookies=[
                        {"name": "_bl_uid", "value": "abc123"},
                        {"name": "session_id", "value": "real-sess-999"},
                    ],
                ),
                _entry(
                    "https://api.example.com/api/v1/bearer-auth",
                    [
                        {"name": "Cookie", "value": "_bl_uid=abc123"},
                        {"name": "Authorization", "value": "Bearer jwt-token-123"},
                    ],
                ),
            ],
        }
    }
    har_file = tmp_path / "cookies_auth.har"
    har_file.write_text(json.dumps(har_payload), encoding="utf-8")

    entries = {e.normalized_path: e for e in parse_har_file(har_file)}
    assert entries["/api/v1/public-telemetry"].has_auth is False
    assert entries["/api/v1/session-auth"].has_auth is True
    assert entries["/api/v1/bearer-auth"].has_auth is True


def test_openapi_merger_infers_requires_auth_from_unauthenticated_responses(tmp_path: Path) -> None:
    """Verify anonymous HTTP 200 auth-denial responses set x-requires-auth=True and do NOT pollute x-biz-success."""
    har_dir = tmp_path / "anon_auth_err"
    har_dir.mkdir(parents=True, exist_ok=True)

    def _entry(ts: str, url: str, resp_obj: dict, status: int = 200) -> dict:
        body = json.dumps(resp_obj, ensure_ascii=False)
        return {
            "startedDateTime": ts,
            "time": 15.0,
            "request": {
                "method": "GET",
                "url": url,
                "httpVersion": "HTTP/2",
                "headers": [{"name": "Cookie", "value": "_bl_uid=rum-only-id"}],
                "queryString": [],
                "cookies": [{"name": "_bl_uid", "value": "rum-only-id"}],
            },
            "response": {
                "status": status,
                "statusText": "OK",
                "httpVersion": "HTTP/2",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
                "content": {"size": len(body.encode("utf-8")), "mimeType": "application/json", "text": body},
            },
            "timings": {"send": 1, "wait": 10, "receive": 4},
        }

    har_payload = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry(
                    "2026-11-24T04:00:01.000Z",
                    "https://api.example.com/api/v1/public/banner",
                    {"code": 0, "data": {"items": []}},
                ),
                _entry(
                    "2026-11-24T04:00:02.000Z",
                    "https://api.example.com/api/v1/user/wallet",
                    {"code": 84101, "msg": "登录信息已失效"},
                ),
                _entry(
                    "2026-11-24T04:00:03.000Z",
                    "https://api.example.com/api/v1/user/orders",
                    {"Code": 2002, "Msg": "未登录"},
                ),
                _entry(
                    "2026-11-24T04:00:04.000Z",
                    "https://api.example.com/api/v1/user/favorites",
                    {"Code": 84103, "Msg": "登录悠悠有品，解锁更多功能"},
                ),
            ],
        }
    }
    (har_dir / "01_anon.har").write_text(json.dumps(har_payload, ensure_ascii=False), encoding="utf-8")

    spec = merge_to_openapi(har_dir, tmp_path / "hub_anon", target_domain="example.com")
    paths = spec["paths"]

    # Public endpoint stays unauthenticated with biz_success code=0
    banner_op = paths["/api/v1/public/banner"]["get"]
    assert banner_op["x-requires-auth"] is False
    assert banner_op["x-biz-success"] == {"field": "code", "value": 0}

    # All 3 auth-denied endpoints infer x-requires-auth=True and do NOT adopt 84101/2002/84103 as biz_success
    for ep_path, bad_code in (
        ("/api/v1/user/wallet", 84101),
        ("/api/v1/user/orders", 2002),
        ("/api/v1/user/favorites", 84103),
    ):
        op = paths[ep_path]["get"]
        assert op["x-requires-auth"] is True, f"Expected x-requires-auth=True on {ep_path}"
        assert op["x-biz-success"] == {"field": "code", "value": 0}
        assert op["x-biz-success"]["value"] != bad_code
