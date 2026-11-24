"""Unit & integration tests for Modules 1 & 2 (Discovery, HAR Importer, JS Extractor, SPA Parser, Mitm)."""

from __future__ import annotations

import json
from pathlib import Path

from reconqa.modules.discovery.runner import run_discovery
from reconqa.modules.discovery.spa_parser import extract_spa_routes
from reconqa.modules.recorder.har_importer import import_hars
from reconqa.modules.recorder.js_extractor import extract_js_endpoints
from reconqa.modules.recorder.mitm_addon import build_mitmdump_command


def test_import_hars_decodes_base64_strips_h2_cleans_timings_and_extracts_js(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "imported_har"
    result = import_hars(
        [fixtures_har_dir],
        out_dir,
        target_domain="shop.example.test",
        extract_js=True,
    )
    assert isinstance(result, dict)

    # Normalized HAR files created with scenario stems
    har_01 = out_dir / "01_browse_search.har"
    har_02 = out_dir / "02_detail_cart.har"
    har_03 = out_dir / "03_user_order.har"
    assert har_01.is_file()
    assert har_02.is_file()
    assert har_03.is_file()

    data_01 = json.loads(har_01.read_text(encoding="utf-8"))
    entries = data_01["log"]["entries"]
    assert len(entries) >= 2

    # Check HTTP/2 pseudo-headers stripped, negative timings sanitized, base64 decoded
    found_decoded_search = False
    for entry in entries:
        req_headers = [h["name"] for h in entry["request"].get("headers", [])]
        assert not any(h.startswith(":") for h in req_headers), f"Pseudo-header remained: {req_headers}"
        timings = entry.get("timings", {})
        for key, val in timings.items():
            if isinstance(val, (int, float)):
                assert val >= 0, f"Negative timing remained: {key}={val}"
        content = entry["response"].get("content", {})
        if (
            entry["request"]["url"].endswith("/api/v1/items/search?keyword=laptop")
            and content.get("encoding") != "base64"
            and "ITEM-88421" in content.get("text", "")
        ):
            found_decoded_search = True
    assert found_decoded_search, "Expected decoded JSON content with ITEM-88421"

    # Embedded JS bundle extracted
    js_bundles_dir = out_dir / "js_bundles"
    assert js_bundles_dir.is_dir()
    js_files = list(js_bundles_dir.rglob("*.js"))
    assert len(js_files) >= 1
    combined_js = "\n".join(p.read_text(encoding="utf-8") for p in js_files)
    assert "/api/v1/coupons/claim" in combined_js


def test_js_extractor_infers_methods_params_and_sources(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    out_har = tmp_path / "har"
    import_hars([fixtures_har_dir], out_har, extract_js=True)

    js_dir = out_har / "js_bundles"
    out_file = out_har / "js_endpoints.json"
    result = extract_js_endpoints(js_dir, out_file)

    assert out_file.is_file()
    assert isinstance(result, dict)
    raw = json.loads(out_file.read_text(encoding="utf-8"))
    endpoints = raw.get("endpoints", raw) if isinstance(raw, dict) else raw
    if isinstance(endpoints, dict):
        paths = list(endpoints.keys())
        items = list(endpoints.values())
    else:
        paths = [e.get("path", "") for e in endpoints]
        items = endpoints

    assert any("/api/v1/coupons/claim" in p for p in paths), f"Missing /api/v1/coupons/claim in {paths}"
    assert any("/api/v1/user/preferences" in p for p in paths), f"Missing /api/v1/user/preferences in {paths}"

    # Verify /api/v1/coupons/claim has inferred params or POST method and x-js-snippet
    claim_eps = [e for e in items if "/api/v1/coupons/claim" in str(e)]
    assert claim_eps
    claim = claim_eps[0]
    assert str(claim.get("method", "")).upper() == "POST"
    source = claim.get("source") or claim.get("x-discovery-source") or claim.get("discovery_source", "")
    assert source in ("js-static-inferred", "js-static")


def test_spa_parser_detects_vue_routes_and_microfrontends(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    out_har = tmp_path / "har"
    import_hars([fixtures_har_dir], out_har, extract_js=True)

    out_disc = tmp_path / "discovery"
    report = extract_spa_routes(
        out_disc,
        js_dir=out_har / "js_bundles",
        urls=[
            "https://shop.example.test/subapp-checkout/remoteEntry.js",
            "https://shop.example.test/#/profile",
        ],
    )
    spa_file = out_disc / "spa_routes.json"
    assert spa_file.is_file()
    assert isinstance(report, dict)

    text = spa_file.read_text(encoding="utf-8")
    assert "/items/:id" in text or "/cart" in text
    assert "qiankun" in text.lower() or "checkout-subapp" in text or "remoteEntry.js" in text


def test_mitm_addon_command_builder(tmp_path: Path) -> None:
    cmd = build_mitmdump_command(
        tmp_path / "har",
        port=8899,
        scenario="01_checkout",
        target_domain="shop.example.test",
    )
    assert isinstance(cmd, list)
    assert any("mitmdump" in str(part) for part in cmd)
    assert "8899" in [str(c) for c in cmd]
    assert any("mitm_addon.py" in str(part) for part in cmd)
    assert any("01_checkout" in str(part) for part in cmd)


def test_discovery_runner_handles_missing_binaries_gracefully(tmp_path: Path, monkeypatch) -> None:
    from reconqa.config import Settings

    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    out_dir = tmp_path / "discovery"
    res = run_discovery("shop.example.test", out_dir, mode="passive")
    assert isinstance(res, dict)
    assert (out_dir / "spa_routes.json").is_file()
