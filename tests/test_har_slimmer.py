"""Tests for Lossless-for-Testing HAR Archive Slimming and JS deduplication (#189)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from reconqa.modules.performance.har_profiler import analyze_har_performance
from reconqa.modules.recorder.har_slimmer import slim_har_file
from reconqa.modules.recorder.js_extractor import extract_js_endpoints
from reconqa.modules.security.static_audit import run_static_audit


def _build_bloated_har(first_party_js: str, vendor_js: str, img_b64: str, css_body: str) -> dict:
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "test", "version": "1.0"},
            "entries": [
                {
                    "startedDateTime": "2026-11-24T00:00:00Z",
                    "time": 42,
                    "request": {
                        "method": "GET",
                        "url": "https://api.example.test/api/v1/orders/list",
                        "headers": [{"name": "Accept", "value": "application/json"}],
                    },
                    "response": {
                        "status": 200,
                        "headers": [{"name": "Content-Type", "value": "application/json"}],
                        "content": {
                            "size": 46,
                            "mimeType": "application/json",
                            "text": '{"code":0,"data":{"items":[{"id":"ORD-1"}]}}',
                        },
                    },
                    "timings": {"send": 1, "wait": 38, "receive": 3},
                },
                {
                    "startedDateTime": "2026-11-24T00:00:01Z",
                    "time": 120,
                    "request": {
                        "method": "GET",
                        "url": "https://cdn.example.test/assets/banner.png",
                        "headers": [],
                    },
                    "response": {
                        "status": 200,
                        "headers": [{"name": "Content-Type", "value": "image/png"}],
                        "content": {
                            "size": 0,
                            "mimeType": "image/png",
                            "encoding": "base64",
                            "text": img_b64,
                        },
                    },
                    "timings": {"send": 1, "wait": 110, "receive": 9},
                },
                {
                    "startedDateTime": "2026-11-24T00:00:02Z",
                    "time": 30,
                    "request": {
                        "method": "GET",
                        "url": "https://cdn.example.test/assets/theme.css",
                        "headers": [],
                    },
                    "response": {
                        "status": 200,
                        "headers": [{"name": "Content-Type", "value": "text/css"}],
                        "content": {
                            "size": len(css_body.encode("utf-8")),
                            "mimeType": "text/css",
                            "text": css_body,
                        },
                    },
                    "timings": {"send": 1, "wait": 25, "receive": 4},
                },
                {
                    "startedDateTime": "2026-11-24T00:00:03Z",
                    "time": 80,
                    "request": {
                        "method": "GET",
                        "url": "https://app.example.test/static/js/chunk-elementUI.abc123.js",
                        "headers": [],
                    },
                    "response": {
                        "status": 200,
                        "headers": [{"name": "Content-Type", "value": "application/javascript"}],
                        "content": {
                            "size": len(vendor_js.encode("utf-8")),
                            "mimeType": "application/javascript",
                            "text": vendor_js,
                        },
                    },
                    "timings": {"send": 1, "wait": 70, "receive": 9},
                },
                {
                    "startedDateTime": "2026-11-24T00:00:04Z",
                    "time": 65,
                    "request": {
                        "method": "GET",
                        "url": "https://app.example.test/static/js/app.main.js",
                        "headers": [],
                    },
                    "response": {
                        "status": 200,
                        "headers": [{"name": "Content-Type", "value": "application/javascript"}],
                        "content": {
                            "size": len(first_party_js.encode("utf-8")),
                            "mimeType": "application/javascript",
                            "text": first_party_js,
                        },
                    },
                    "timings": {"send": 1, "wait": 60, "receive": 4},
                },
            ],
        }
    }


def test_slim_har_strips_static_dedups_js_and_preserves_testing_fidelity(tmp_path: Path) -> None:
    """Verify >70% size reduction, preserved sizes/timings/JSON, SHA-256 JS dedup, and pipeline fidelity."""
    har_dir = tmp_path / "har"
    har_dir.mkdir(parents=True, exist_ok=True)
    js_dir = har_dir / "js_bundles"

    first_party_js = (
        'export function fetchHidden() { return axios.post("/api/v1/coupons/secret-redeem", { code }); }\n'
        'const internalStagingIp = "10.88.77.66";\n'
        + ("// padding comment line\n" * 800)
    )
    vendor_js = "/* ElementUI vendor bundle */\n" + ("var _e = 1;\n" * 2000)
    raw_img_bytes = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 60_000)
    img_b64 = base64.b64encode(raw_img_bytes).decode("ascii")
    css_body = ".btn { color: #333; }\n" * 1500

    har1 = har_dir / "01_scene_a.har"
    har2 = har_dir / "02_scene_b.har"
    payload = json.dumps(_build_bloated_har(first_party_js, vendor_js, img_b64, css_body), indent=2)
    har1.write_text(payload, encoding="utf-8")
    har2.write_text(payload, encoding="utf-8")

    # Slim HAR 1
    st1 = slim_har_file(har1, js_bundles_dir=js_dir)
    assert st1["slimmed"] is True
    assert st1["saved_pct"] > 70.0, st1
    assert st1["stripped_binary_count"] == 2  # PNG + CSS
    assert st1["stripped_js_count"] == 2  # vendor + first-party
    assert st1["extracted_js_files"] == 1  # only first-party written, vendor skipped!

    # Slim HAR 2 with identical JS -> extracted_js_files == 0 (SHA-256 deduplicated!)
    st2 = slim_har_file(har2, js_bundles_dir=js_dir)
    assert st2["slimmed"] is True
    assert st2["extracted_js_files"] == 0
    js_files = list(js_dir.glob("*.js"))
    assert len(js_files) == 1, f"Expected 1 deduplicated first-party JS file, got {js_files}"
    assert "chunk-elementUI" not in js_files[0].name

    # Verify API JSON 100% intact and binary entry preserved exact decoded content.size & timings
    slimmed_doc = json.loads(har1.read_text(encoding="utf-8"))
    entries = slimmed_doc["log"]["entries"]
    assert entries[0]["response"]["content"]["text"] == '{"code":0,"data":{"items":[{"id":"ORD-1"}]}}'
    assert "text" not in entries[1]["response"]["content"]
    assert entries[1]["response"]["content"]["size"] == len(raw_img_bytes)
    assert entries[1]["response"]["content"]["_reconqa_stripped"] == "binary_or_static"
    assert entries[1]["timings"]["wait"] == 110
    assert entries[3]["response"]["content"]["_reconqa_stripped"] == "vendor_js"
    assert entries[4]["response"]["content"]["_reconqa_stripped"] == "js_extracted"

    # Verify downstream js_extractor, static_audit, and har_profiler work seamlessly on slimmed HARs
    js_ep_res = extract_js_endpoints(js_dir, har_dir / "js_endpoints.json")
    found_paths = {ep["path"] for ep in js_ep_res.get("endpoints") or []}
    assert "/api/v1/coupons/secret-redeem" in found_paths

    sec_rep = run_static_audit(har_dir, tmp_path / "sec", js_dir=js_dir)
    assert any(
        f.get("category") == "sensitive_leak" and "10.88.*.*" in str(f)
        for f in sec_rep["findings"]
    ), sec_rep["findings"]

    perf_rep = analyze_har_performance(har_dir, tmp_path / "perf")
    assert perf_rep["summary"]["total_requests"] == 10
    assert any(r["size_bytes"] == len(raw_img_bytes) for r in perf_rep["largest_bodies"])
