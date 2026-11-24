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
from reconqa.modules.hub.har_parser import normalize_path, parse_har_dir, parse_har_file
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
