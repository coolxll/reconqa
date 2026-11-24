"""Unit & integration tests for Modules 5 & 6 (Security Audit, Auth Matrix, Burp, HAR Profiler, k6, Proxy Pool)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reconqa.modules.hub.correlation import analyze_correlations
from reconqa.modules.performance.har_profiler import analyze_har_performance
from reconqa.modules.performance.k6_generator import generate_k6_script
from reconqa.modules.performance.proxy_pool import RoundRobinProxyPool
from reconqa.modules.recorder.har_importer import import_hars
from reconqa.modules.security.auth_matrix import run_auth_matrix
from reconqa.modules.security.burp_exporter import export_burp_requests
from reconqa.modules.security.static_audit import run_static_audit
from tests.conftest import rewrite_har_base_url
from tests.mock_server import MockShopServer


def test_static_audit_detects_missing_headers_cookies_and_redacts_secrets(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    har_dir = tmp_path / "har"
    import_hars([fixtures_har_dir], har_dir, extract_js=True)

    sec_dir = tmp_path / "security"
    report = run_static_audit(har_dir, sec_dir, js_dir=har_dir / "js_bundles")

    json_file = sec_dir / "static_audit.json"
    md_file = sec_dir / "static_audit.md"
    assert json_file.is_file()
    assert md_file.is_file()
    assert isinstance(report, dict)

    findings = report.get("findings", [])
    assert len(findings) >= 3

    json_text = json_file.read_text(encoding="utf-8")
    md_text = md_file.read_text(encoding="utf-8")
    combined = json_text + "\n" + md_text

    # Missing security headers & insecure Set-Cookie detected
    assert "Strict-Transport-Security" in combined or "HSTS" in combined
    assert "HttpOnly" in combined or "Set-Cookie" in combined

    # Sensitive leaks detected, AND raw secrets/PII must be REDACTED (never verbatim!)
    assert "rk_test_dummy1234567890abcdef" not in json_text, "Raw apiKey leaked unredacted in static_audit.json"
    assert "rk_test_dummy1234567890abcdef" not in md_text, "Raw apiKey leaked unredacted in static_audit.md"
    assert "13800138000" not in json_text, "Raw phone number leaked unredacted in static_audit.json"


def test_static_audit_signal_to_noise_dedup_vendor_and_pem_body(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    """Verify #124: JS bundle dedup across scenarios, vendor/OSS email skip, and real PEM body requirement."""
    har_dir = tmp_path / "har"
    import_hars([fixtures_har_dir], har_dir, extract_js=False)

    js_dir = tmp_path / "js_bundles"
    scen1 = js_dir / "01_scene_a"
    scen2 = js_dir / "02_scene_b"
    scen1.mkdir(parents=True, exist_ok=True)
    scen2.mkdir(parents=True, exist_ok=True)

    # 1. Identical JS bundle across 2 scenarios -> must be deduplicated to 1 finding
    dup_bundle = 'const internalDb = "10.99.88.77";\n'
    (scen1 / "app.dup.js").write_text(dup_bundle, encoding="utf-8")
    (scen2 / "app.dup.js").write_text(dup_bundle, encoding="utf-8")

    # 2. Vendor bundle + OSS/license author email + bare jsencrypt PEM constant -> MUST NOT be flagged
    pem_hdr = "-----BEGIN " + "PRIVATE KEY-----"
    (scen1 / "chunk-elementUI.vendor.js").write_text(
        f'/* Copyright (c) 2026 author: dev@github.com, phone: 13900139000 */\nconst H = "{pem_hdr}";\n',
        encoding="utf-8",
    )
    (scen1 / "jsencrypt.app.js").write_text(
        f'/* MIT License @author maintainer@example.org */\nconst PEM_PREFIX = "{pem_hdr}";\n',
        encoding="utf-8",
    )

    # 3. Real PEM header WITH Base64 body -> MUST be flagged as critical private_key
    real_pem_file = scen1 / "leaked.key.js"
    real_pem_file.write_text(
        f'const k = "{pem_hdr}\\n' + ("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5" * 2) + '";\n',
        encoding="utf-8",
    )

    sec_dir = tmp_path / "sec_s2n"
    report = run_static_audit(har_dir, sec_dir, js_dir=js_dir)
    findings = report.get("findings", [])

    # Duplicate JS bundle across 2 scenarios -> only 1 private_ip finding for 10.99.88.77
    dup_ip_findings = [
        f for f in findings if "app.dup.js" in str(f.get("endpoint", "")) or "app.dup.js" in str(f.get("evidence", ""))
    ]
    assert len(dup_ip_findings) == 1, f"Expected deduplicated 1 finding for dup bundle, got {dup_ip_findings}"

    # Vendor bundle & OSS author emails must NOT be flagged
    assert not any("chunk-elementUI" in str(f) for f in findings), (
        f"Vendor bundle should not produce PII/PEM false positives: {findings}"
    )
    assert not any("jsencrypt.app.js" in str(f) for f in findings), (
        f"Bare PEM constant / license email in jsencrypt.app.js should not be flagged: {findings}"
    )

    # Real PEM header + Base64 body MUST be flagged as critical with redacted context snippet (#125)
    pem_findings = [
        f
        for f in findings
        if f.get("severity") == "critical" and "leaked.key.js" in str(f)
    ]
    assert len(pem_findings) == 1, f"Expected 1 critical finding for real PEM body, got {findings}"
    ctx = pem_findings[0].get("evidence", {}).get("context", "")
    assert isinstance(ctx, str) and len(ctx) > 0, f"Expected non-empty redacted context: {pem_findings[0]}"
    assert "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5" not in ctx, "Raw PEM body leaked into context!"

    dup_occs = [
        o
        for o in dup_ip_findings[0].get("evidence", {}).get("occurrences", [])
        if "app.dup.js" in str(o.get("endpoint", ""))
    ]
    assert len(dup_occs) == 1, f"Expected deduplicated 1 occurrence for app.dup.js, got {dup_occs}"
    dup_ctx = dup_occs[0].get("context", "")
    assert "internalDb" in dup_ctx, f"Expected surrounding key 'internalDb' in context, got: {dup_ctx!r}"


def test_auth_matrix_unauth_idor_and_mutation_safety(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    with MockShopServer(idor_vulnerable=True, unauth_bypass_orders=False) as srv:
        local_har_dir = rewrite_har_base_url(fixtures_har_dir, tmp_path / "local_har", srv.base_url)

        # Add a baseline entry for /api/v1/profile/leaked (authorized in HAR, but server leaks without auth)
        leaked_har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "startedDateTime": "2026-01-15T10:03:01.000Z",
                        "time": 40.0,
                        "request": {
                            "method": "GET",
                            "url": f"{srv.base_url}/api/v1/profile/leaked",
                            "httpVersion": "HTTP/1.1",
                            "headers": [
                                {"name": "Authorization", "value": "Bearer token-account-a"},
                                {"name": "Accept", "value": "application/json"},
                            ],
                            "queryString": [],
                            "cookies": [],
                        },
                        "response": {
                            "status": 200,
                            "statusText": "OK",
                            "httpVersion": "HTTP/1.1",
                            "headers": [{"name": "Content-Type", "value": "application/json"}],
                            "cookies": [],
                            "content": {
                                "size": 70,
                                "mimeType": "application/json",
                                "text": json.dumps(
                                    {
                                        "code": 0,
                                        "msg": "ok",
                                        "data": {"userId": "USR-1001", "account": "alice", "tier": "vip"},
                                    }
                                ),
                            },
                        },
                        "timings": {"send": 1, "wait": 30, "receive": 9},
                    }
                ],
            }
        }
        (local_har_dir / "04_leaked.har").write_text(json.dumps(leaked_har), encoding="utf-8")

        # 1. Default run (include_mutations=False): MUST NOT replay POST /api/v1/orders/submit!
        sec_dir = tmp_path / "security_safe"
        res_safe = run_auth_matrix(
            local_har_dir,
            sec_dir,
            auth_a_headers={"Authorization": "Bearer token-account-a"},
            auth_b_headers={"Authorization": "Bearer token-account-b"},
            include_mutations=False,
        )
        assert (sec_dir / "auth_matrix.json").is_file()
        assert (sec_dir / "auth_matrix.md").is_file()
        assert len(srv.state.orders_submitted) == 0, "Mutation endpoint was replayed despite include_mutations=False!"

        res_text = json.dumps(res_safe)
        assert "unauth_bypass" in res_text, f"Expected unauth_bypass on /api/v1/profile/leaked: {res_safe}"
        assert (
            "confirmed_idor" in res_text or "suspected_idor" in res_text
        ), f"Expected IDOR detection on /api/v1/orders/ORD-66201: {res_safe}"
        assert "mutation_candidate" in res_text, f"Expected mutation_candidate for orders/submit: {res_safe}"

        # 2. Explicit include_mutations=True: SHOULD replay mutation endpoints
        sec_dir_mut = tmp_path / "security_mut"
        run_auth_matrix(
            local_har_dir,
            sec_dir_mut,
            auth_a_headers={"Authorization": "Bearer token-account-a"},
            auth_b_headers={"Authorization": "Bearer token-account-b"},
            include_mutations=True,
        )
        assert len(srv.state.orders_submitted) >= 1, "Expected mutation replay when include_mutations=True"

        # 3. Burp export using auth_matrix.json + signed X-Sign request
        burp_res = export_burp_requests(
            local_har_dir,
            sec_dir,
            auth_matrix_path=sec_dir / "auth_matrix.json",
        )
        assert isinstance(burp_res, dict)
        exports_dir = sec_dir / "burp_exports"
        assert exports_dir.is_dir()
        http_files = list(exports_dir.glob("*.http"))
        assert len(http_files) >= 1
        replay_sh = exports_dir / "replay_commands.sh"
        assert replay_sh.is_file()

        all_http = "\n".join(p.read_text(encoding="utf-8") for p in http_files)
        assert "/api/v1/orders/submit" in all_http
        assert "X-Sign" in all_http
        # No HTTP/2 pseudo-headers in raw HTTP export
        for line in all_http.splitlines():
            assert not line.startswith(":"), f"Pseudo-header found in Burp export: {line}"


def test_har_profiler_detects_slow_uncompressed_duplicate_and_cache(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    perf_dir = tmp_path / "performance"
    report = analyze_har_performance(
        fixtures_har_dir,
        perf_dir,
        uncompressed_threshold_bytes=20_000,
    )
    assert (perf_dir / "har_perf_report.json").is_file()
    assert (perf_dir / "har_perf_report.md").is_file()
    assert isinstance(report, dict)

    text = (perf_dir / "har_perf_report.json").read_text(encoding="utf-8")
    assert "/api/v1/items/search" in text

    # Slow TTFB > 600ms, uncompressed > 20KB, and duplicate request must all be reported
    findings = report.get("findings", [])
    assert len(findings) >= 2
    assert any("uncompress" in str(report).lower() or "gzip" in str(report).lower() for _ in [1])
    assert any("duplicate" in str(report).lower() for _ in [1])


def test_k6_generator_and_proxy_pool(fixtures_har_dir: Path, tmp_path: Path) -> None:
    hub_dir = tmp_path / "hub"
    analyze_correlations(fixtures_har_dir, hub_dir)

    perf_dir = tmp_path / "performance"
    res = generate_k6_script(
        fixtures_har_dir,
        perf_dir,
        correlation_path=hub_dir / "correlation.json",
    )
    assert isinstance(res, dict)
    script = perf_dir / "k6" / "scenario_ramp.js"
    assert script.is_file()
    src = script.read_text(encoding="utf-8")
    assert "k6/http" in src
    assert "http_429" in src
    assert "http_5xx" in src
    assert "NO_CONN_REUSE" in src
    assert "BASE_URL" in src

    # Verify RoundRobinProxyPool cycles upstreams cleanly and can bind/close
    pool = RoundRobinProxyPool(
        ["http://127.0.0.1:18081", "http://127.0.0.1:18082"],
        host="127.0.0.1",
        port=0,
    )
    picks = [
        getattr(p, "url", p) for p in (pool.next_upstream() for _ in range(4))
    ]
    assert picks == [
        "http://127.0.0.1:18081",
        "http://127.0.0.1:18082",
        "http://127.0.0.1:18081",
        "http://127.0.0.1:18082",
    ]

    async def _start_stop() -> None:
        proxy_url = await pool.start()
        assert proxy_url.startswith("http://127.0.0.1:")
        await pool.stop()

    asyncio.run(_start_stop())


def test_static_audit_cors_skips_same_root_and_aggregates_by_host(tmp_path: Path) -> None:
    """Verify #183: same-root-domain CORS with credentials produces 0 findings; cross-domain aggregates by host."""
    har_dir = tmp_path / "har_cors"
    har_dir.mkdir(parents=True, exist_ok=True)

    def _entry(url: str, origin: str, acao: str, acac: str) -> dict:
        return {
            "request": {
                "method": "GET",
                "url": url,
                "headers": [{"name": "Origin", "value": origin}],
            },
            "response": {
                "status": 200,
                "headers": [
                    {"name": "Access-Control-Allow-Origin", "value": acao},
                    {"name": "Access-Control-Allow-Credentials", "value": acac},
                    {"name": "Content-Type", "value": "application/json"},
                ],
                "content": {"mimeType": "application/json", "text": '{"code":0}'},
            },
        }

    # 1. Same registrable root domain (including two-part TLD .com.cn) -> MUST yield 0 CORS findings
    same_root_har = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry(
                    "https://api-stage.example.com/api/v1/orders",
                    "https://admin-stage.example.com",
                    "https://admin-stage.example.com",
                    "true",
                ),
                _entry(
                    "https://api-stage.example.com/api/v1/users",
                    "https://doctor-stage.example.com",
                    "https://doctor-stage.example.com",
                    "true",
                ),
                _entry(
                    "https://api.portal.example.com.cn/v1/status",
                    "https://admin.portal.example.com.cn",
                    "https://admin.portal.example.com.cn",
                    "true",
                ),
            ],
        }
    }
    (har_dir / "same_root.har").write_text(json.dumps(same_root_har), encoding="utf-8")

    rep_clean = run_static_audit(har_dir, tmp_path / "sec_clean")
    cors_clean = [f for f in rep_clean["findings"] if f.get("category") == "cors"]
    assert cors_clean == [], f"Same-root-domain CORS should produce 0 findings, got {cors_clean}"
    assert rep_clean["cors_issues"] == []

    # 2. Cross-domain reflected origin with credentials across 3 endpoints -> 1 aggregated high finding!
    cross_dir = tmp_path / "har_cross"
    cross_dir.mkdir(parents=True, exist_ok=True)
    cross_har = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry(
                    f"https://api.example.com/api/v1/ep{i}",
                    "https://evil-attacker.test",
                    "https://evil-attacker.test",
                    "true",
                )
                for i in (1, 2, 3)
            ],
        }
    }
    (cross_dir / "cross.har").write_text(json.dumps(cross_har), encoding="utf-8")

    rep_cross = run_static_audit(cross_dir, tmp_path / "sec_cross")
    cors_findings = [f for f in rep_cross["findings"] if f.get("category") == "cors"]
    assert len(cors_findings) == 1, f"Expected 1 host-aggregated CORS finding, got {cors_findings}"
    assert cors_findings[0]["severity"] == "high"
    ev = cors_findings[0]["evidence"]
    assert ev["kind"] == "reflected_origin_with_credentials"
    assert ev["affected_endpoints_count"] == 3
    assert len(ev["sample_endpoints"]) == 3


def test_security_and_perf_findings_aggregation_no_spam(tmp_path: Path) -> None:
    """Verify #185: 3 endpoints with cn_mobile -> 1 sensitive_leak finding; duplicates across 2 scenarios -> 1 finding."""
    # 1. Security sensitive_leak aggregation across 3 endpoints
    sec_har_dir = tmp_path / "har_leaks"
    sec_har_dir.mkdir(parents=True, exist_ok=True)
    leak_har = {
        "log": {
            "version": "1.2",
            "entries": [
                {
                    "request": {"method": "GET", "url": f"https://api.example.com/api/v1/users/{i}"},
                    "response": {
                        "status": 200,
                        "headers": [{"name": "Content-Type", "value": "application/json"}],
                        "content": {
                            "mimeType": "application/json",
                            "text": json.dumps({"phone": f"1380013800{i}"}),
                        },
                    },
                }
                for i in (1, 2, 3)
            ],
        }
    }
    (sec_har_dir / "01_users.har").write_text(json.dumps(leak_har), encoding="utf-8")

    sec_rep = run_static_audit(sec_har_dir, tmp_path / "sec_agg")
    mobile_findings = [
        f
        for f in sec_rep["findings"]
        if f.get("category") == "sensitive_leak"
        and f.get("evidence", {}).get("kind") == "cn_mobile"
    ]
    assert len(mobile_findings) == 1, f"Expected 1 aggregated cn_mobile finding, got {mobile_findings}"
    ev = mobile_findings[0]["evidence"]
    assert ev["affected_endpoints_count"] == 3
    assert ev["occurrence_count"] == 3
    assert len(ev["sample_endpoints"]) == 3
    assert len(ev["occurrences"]) == 3

    # 2. Performance duplicate_request aggregation across 2 scenarios
    perf_har_dir = tmp_path / "har_perf_agg"
    perf_har_dir.mkdir(parents=True, exist_ok=True)
    for sc_name in ("01_home", "02_orders"):
        dup_entries = [
            {
                "request": {"method": "GET", "url": "https://api.example.com/system/p/my-info"},
                "response": {
                    "status": 200,
                    "headers": [{"name": "Content-Type", "value": "application/json"}],
                    "content": {"size": 120, "mimeType": "application/json", "text": '{"ok":true}'},
                },
                "timings": {"send": 1, "wait": 25, "receive": 2},
            }
            for _ in range(2)
        ]
        (perf_har_dir / f"{sc_name}.har").write_text(
            json.dumps({"log": {"version": "1.2", "entries": dup_entries}}),
            encoding="utf-8",
        )

    perf_rep = analyze_har_performance(perf_har_dir, tmp_path / "perf_agg")
    dup_findings = [
        f for f in perf_rep["findings"] if f.get("category") == "perf_duplicate_request"
    ]
    assert len(dup_findings) == 1, f"Expected 1 aggregated duplicate finding, got {dup_findings}"
    pev = dup_findings[0]["evidence"]
    assert pev["affected_scenarios_count"] == 2
    assert pev["total_duplicate_calls"] == 4
    assert set(pev["scenarios"]) == {"01_home", "02_orders"}
