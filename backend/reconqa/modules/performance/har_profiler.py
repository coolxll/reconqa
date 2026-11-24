"""Static HAR performance profiler.

Computes:
  - Slowest Top N requests by TTFB (`timings.wait`) and total latency
  - Largest response bodies Top N
  - Uncompressed text/JSON/JS/CSS/HTML responses exceeding threshold
  - Duplicate redundant requests within the same scenario
  - CDN / HTTP cache hit-rate statistics
"""

from __future__ import annotations

import base64
import json
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.utils.io import ensure_dir, write_json

COMPRESSIBLE_MIMES = (
    "json",
    "javascript",
    "ecmascript",
    "html",
    "css",
    "xml",
    "text/",
    "svg",
)

COMPRESSED_ENCODINGS = ("gzip", "br", "zstd")


def _iter_har_files(har_dir: Path) -> list[Path]:
    if har_dir.is_file() and har_dir.suffix.lower() == ".har":
        return [har_dir]
    if har_dir.is_dir():
        return sorted(p for p in har_dir.glob("*.har") if p.is_file())
    return []


def _domain_matches(host: str, target_domain: str | None) -> bool:
    if not target_domain:
        return True
    h = host.lower().strip(".")
    td = target_domain.lower().strip(".")
    return h == td or h.endswith("." + td) or td in h


def _headers_lower(headers: list[dict[str, Any]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in headers or []:
        name = str(h.get("name") or "").strip().lower()
        if not name or name.startswith(":"):
            continue
        val = str(h.get("value") or "")
        out[name] = f"{out[name]}, {val}" if name in out else val
    return out


def _safe_ms(val: Any) -> float:
    try:
        num = float(val)
        return num if num > 0.0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _compute_body_size(resp: dict[str, Any]) -> int:
    content = resp.get("content") or {}
    size = content.get("size")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    text = content.get("text")
    if isinstance(text, str) and text:
        if content.get("encoding") == "base64":
            try:
                return len(base64.b64decode(text, validate=False))
            except Exception:
                return len(text)
        return len(text.encode("utf-8"))
    body_size = resp.get("bodySize")
    if isinstance(body_size, (int, float)) and body_size > 0:
        return int(body_size)
    return 0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 2)


def analyze_har_performance(
    har_dir: Path,
    out_dir: Path,
    *,
    target_domain: str | None = None,
    top_n: int = 10,
    large_body_bytes: int = 100_000,
    uncompressed_threshold_bytes: int = 20_000,
) -> dict[str, Any]:
    """Profile HAR files for latency, payload size, compression, duplicates, and cache stats."""
    ensure_dir(out_dir)
    har_files = _iter_har_files(har_dir)

    rows: list[dict[str, Any]] = []
    uncompressed: list[dict[str, Any]] = []
    dup_counter: Counter[tuple[str, str, str]] = Counter()
    cache_counts: Counter[str] = Counter()
    total_bytes = 0

    for hp in har_files:
        scenario = hp.stem
        try:
            har_data = json.loads(hp.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue

        for entry in har_data.get("log", {}).get("entries", []):
            req = entry.get("request") or {}
            resp = entry.get("response") or {}
            url = str(req.get("url") or "")
            parsed = urlparse(url)
            host = parsed.hostname or ""
            path = parsed.path or "/"
            if not host or not _domain_matches(host, target_domain):
                continue

            method = str(req.get("method") or "GET").upper()
            status = int(resp.get("status") or 0)
            rh = _headers_lower(resp.get("headers"))
            content = resp.get("content") or {}
            mime = (
                str(content.get("mimeType") or rh.get("content-type", ""))
                .split(";")[0]
                .strip()
                .lower()
            )
            encoding = rh.get("content-encoding", "").strip().lower()
            size_bytes = _compute_body_size(resp)
            total_bytes += size_bytes

            timings = entry.get("timings") or {}
            ttfb_ms = _safe_ms(timings.get("wait"))
            total_ms = _safe_ms(entry.get("time"))
            if total_ms == 0.0:
                total_ms = round(
                    sum(
                        _safe_ms(timings.get(k))
                        for k in ("blocked", "dns", "connect", "send", "wait", "receive")
                    ),
                    2,
                )

            row = {
                "scenario": scenario,
                "method": method,
                "status": status,
                "host": host,
                "path": path,
                "url": url,
                "mime_type": mime,
                "content_encoding": encoding or "none",
                "size_bytes": size_bytes,
                "ttfb_ms": round(ttfb_ms, 2),
                "total_ms": round(total_ms, 2),
            }
            rows.append(row)
            dup_counter[(scenario, method, url)] += 1

            # Compression audit
            is_compressible = any(tok in mime for tok in COMPRESSIBLE_MIMES) or path.lower().endswith(
                (".js", ".css", ".html", ".json", ".svg")
            )
            is_compressed = any(enc in encoding for enc in COMPRESSED_ENCODINGS)
            if is_compressible and size_bytes > uncompressed_threshold_bytes and not is_compressed:
                uncompressed.append(
                    {
                        "scenario": scenario,
                        "method": method,
                        "url": url,
                        "endpoint": f"{host}{path}",
                        "mime_type": mime,
                        "size_bytes": size_bytes,
                        "content_encoding": encoding or "none",
                    }
                )

            # CDN / Cache header audit
            cdn_val = " ".join(
                filter(
                    None,
                    [
                        rh.get("cf-cache-status", ""),
                        rh.get("x-cache", ""),
                        rh.get("x-cache-lookup", ""),
                    ],
                )
            ).upper()
            age_val = rh.get("age", "").strip()
            cache_ctrl = rh.get("cache-control", "").strip()

            if "HIT" in cdn_val or (age_val.isdigit() and int(age_val) > 0):
                cache_counts["hit"] += 1
            elif "MISS" in cdn_val:
                cache_counts["miss"] += 1
            elif any(tok in cdn_val for tok in ("BYPASS", "DYNAMIC", "EXPIRED", "REVALIDATED")):
                cache_counts["bypass_or_dynamic"] += 1
            elif cache_ctrl:
                cache_counts["cache_control_only"] += 1
            else:
                cache_counts["no_cache_header"] += 1

    slowest_ttfb = sorted(rows, key=lambda r: -r["ttfb_ms"])[:top_n]
    slowest_total = sorted(rows, key=lambda r: -r["total_ms"])[:top_n]
    largest_bodies = sorted(rows, key=lambda r: -r["size_bytes"])[:top_n]
    large_body_alerts = [r for r in largest_bodies if r["size_bytes"] > large_body_bytes]
    uncompressed_sorted = sorted(uncompressed, key=lambda r: -r["size_bytes"])

    duplicates = [
        {
            "scenario": scenario,
            "method": method,
            "url": url,
            "count": count,
        }
        for (scenario, method, url), count in dup_counter.most_common()
        if count > 1
    ]

    hits = cache_counts.get("hit", 0)
    misses = cache_counts.get("miss", 0)
    cdn_hit_rate = round(hits / (hits + misses), 4) if (hits + misses) > 0 else None

    ttfb_vals = [r["ttfb_ms"] for r in rows]
    total_vals = [r["total_ms"] for r in rows]

    findings: list[dict[str, Any]] = []
    for r in slowest_ttfb:
        if r["ttfb_ms"] >= 500.0:
            sev = "high" if r["ttfb_ms"] >= 1500.0 else "medium"
            findings.append(
                {
                    "category": "perf_slow_ttfb",
                    "severity": sev,
                    "endpoint": f"{r['method']} {r['path']}",
                    "title": f"Slow TTFB ({r['ttfb_ms']:.0f}ms) on {r['method']} {r['path']}",
                    "evidence": {
                        "scenario": r["scenario"],
                        "url": r["url"],
                        "ttfb_ms": r["ttfb_ms"],
                        "total_ms": r["total_ms"],
                        "status": r["status"],
                    },
                }
            )

    large_by_ep: dict[tuple[str, str], dict[str, Any]] = {}
    for r in large_body_alerts:
        lkey = (r["method"], r["path"])
        agg = large_by_ep.get(lkey)
        if agg is None:
            large_by_ep[lkey] = {
                "method": r["method"],
                "path": r["path"],
                "url": r["url"],
                "max_size_bytes": r["size_bytes"],
                "mime_type": r["mime_type"],
                "scenarios": [r["scenario"]],
                "occurrence_count": 1,
            }
        else:
            agg["occurrence_count"] += 1
            if r["size_bytes"] > agg["max_size_bytes"]:
                agg["max_size_bytes"] = r["size_bytes"]
                agg["url"] = r["url"]
            if r["scenario"] not in agg["scenarios"]:
                agg["scenarios"].append(r["scenario"])

    for agg in large_by_ep.values():
        sc_list = agg["scenarios"]
        ep_label = f"{agg['method']} {agg['path']}"
        if len(sc_list) > 1:
            title_str = (
                f"Large response payload ({agg['max_size_bytes'] / 1024:.1f}KB across "
                f"{len(sc_list)} scenarios) on {ep_label}"
            )
        else:
            title_str = (
                f"Large response payload ({agg['max_size_bytes'] / 1024:.1f}KB) on {ep_label}"
            )
        findings.append(
            {
                "category": "perf_large_body",
                "severity": "medium",
                "endpoint": ep_label,
                "title": title_str,
                "evidence": {
                    "scenario": sc_list[0],
                    "scenarios": sc_list,
                    "affected_scenarios_count": len(sc_list),
                    "occurrence_count": agg["occurrence_count"],
                    "url": agg["url"],
                    "size_bytes": agg["max_size_bytes"],
                    "mime_type": agg["mime_type"],
                    "threshold_bytes": large_body_bytes,
                },
            }
        )

    for u in uncompressed_sorted:
        findings.append(
            {
                "category": "perf_uncompressed",
                "severity": "medium",
                "endpoint": u["endpoint"],
                "title": (
                    f"Uncompressed text/API payload ({u['size_bytes'] / 1024:.1f}KB) "
                    f"on {u['endpoint']}"
                ),
                "evidence": u,
            }
        )

    dup_by_ep: dict[tuple[str, str], dict[str, Any]] = {}
    for d in duplicates:
        parsed_u = urlparse(d["url"])
        ep_path = parsed_u.path or "/"
        dkey = (d["method"], ep_path)
        agg = dup_by_ep.get(dkey)
        if agg is None:
            dup_by_ep[dkey] = {
                "method": d["method"],
                "path": ep_path,
                "url": d["url"],
                "total_duplicate_calls": int(d["count"]),
                "max_count": int(d["count"]),
                "scenarios": [d["scenario"]],
            }
        else:
            agg["total_duplicate_calls"] += int(d["count"])
            agg["max_count"] = max(agg["max_count"], int(d["count"]))
            if d["scenario"] not in agg["scenarios"]:
                agg["scenarios"].append(d["scenario"])

    for agg in sorted(dup_by_ep.values(), key=lambda x: -x["total_duplicate_calls"]):
        sc_list = agg["scenarios"]
        total_calls = agg["total_duplicate_calls"]
        sev = "medium" if (agg["max_count"] >= 3 or total_calls >= 5) else "low"
        ep_label = f"{agg['method']} {agg['path']}"
        if len(sc_list) > 1:
            title_str = (
                f"Duplicate request ({total_calls}x across {len(sc_list)} scenarios): {ep_label}"
            )
        else:
            title_str = f"Duplicate request ({total_calls}x in {sc_list[0]}): {ep_label}"
        findings.append(
            {
                "category": "perf_duplicate_request",
                "severity": sev,
                "endpoint": ep_label,
                "title": title_str,
                "evidence": {
                    "method": agg["method"],
                    "path": agg["path"],
                    "url": agg["url"],
                    "count": total_calls,
                    "total_duplicate_calls": total_calls,
                    "affected_scenarios_count": len(sc_list),
                    "scenarios": sc_list,
                },
            }
        )

    cache_summary = {
        "hit": hits,
        "miss": misses,
        "bypass_or_dynamic": cache_counts.get("bypass_or_dynamic", 0),
        "cache_control_only": cache_counts.get("cache_control_only", 0),
        "no_cache_header": cache_counts.get("no_cache_header", 0),
        "cdn_hit_rate": cdn_hit_rate,
    }

    summary = {
        "har_files": len(har_files),
        "total_requests": len(rows),
        "total_bytes": total_bytes,
        "avg_ttfb_ms": round(sum(ttfb_vals) / len(ttfb_vals), 2) if ttfb_vals else 0.0,
        "p50_ttfb_ms": _percentile(ttfb_vals, 50),
        "p95_ttfb_ms": _percentile(ttfb_vals, 95),
        "p95_total_ms": _percentile(total_vals, 95),
        "large_body_count": len(large_body_alerts),
        "uncompressed_count": len(uncompressed_sorted),
        "duplicate_group_count": len(duplicates),
        "cdn_hit_rate": cdn_hit_rate,
        "findings_count": len(findings),
    }

    report: dict[str, Any] = {
        "summary": summary,
        "slowest_ttfb": slowest_ttfb,
        "slowest_total": slowest_total,
        "largest_bodies": largest_bodies,
        "uncompressed_responses": uncompressed_sorted,
        "duplicate_requests": duplicates,
        "cache_stats": cache_summary,
        "findings": findings,
    }

    json_path = out_dir / "har_perf_report.json"
    md_path = out_dir / "har_perf_report.md"
    write_json(json_path, report)
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    report["artifacts"] = {
        "json": str(json_path),
        "markdown": str(md_path),
    }
    return report


def _render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    cs = report["cache_stats"]
    hit_rate_s = f"{cs['cdn_hit_rate'] * 100:.1f}%" if cs["cdn_hit_rate"] is not None else "N/A"
    lines = [
        "# HAR Performance Profiling Report",
        "",
        f"- HAR files analyzed: `{s['har_files']}` | Total requests: `{s['total_requests']}` "
        f"| Total transferred: `{s['total_bytes'] / (1024 * 1024):.2f} MB`",
        f"- TTFB: avg `{s['avg_ttfb_ms']}ms`, p50 `{s['p50_ttfb_ms']}ms`, "
        f"p95 `{s['p95_ttfb_ms']}ms` | Total p95: `{s['p95_total_ms']}ms`",
        f"- CDN hit rate: `{hit_rate_s}` (HIT={cs['hit']}, MISS={cs['miss']}, "
        f"Cache-Control={cs['cache_control_only']}, None={cs['no_cache_header']})",
        f"- Uncompressed large payloads: `{s['uncompressed_count']}` "
        f"| Duplicate request groups: `{s['duplicate_group_count']}`",
        "",
        "## 1. Slowest Requests by TTFB",
        "",
    ]
    if report["slowest_ttfb"]:
        lines.append("| # | Scenario | Method | Status | TTFB (ms) | Total (ms) | Size (KB) | Path |")
        lines.append("| ---: | :--- | :--- | :---: | ---: | ---: | ---: | :--- |")
        for idx, r in enumerate(report["slowest_ttfb"], 1):
            lines.append(
                f"| {idx} | `{r['scenario']}` | `{r['method']}` | {r['status']} "
                f"| **{r['ttfb_ms']:.1f}** | {r['total_ms']:.1f} "
                f"| {r['size_bytes'] / 1024:.1f} | `{r['host']}{r['path'][:60]}` |"
            )
    else:
        lines.append("- No requests found.")

    lines.extend(["", "## 2. Largest Response Bodies", ""])
    if report["largest_bodies"]:
        lines.append("| # | Size (KB) | Encoding | MIME | Path |")
        lines.append("| ---: | ---: | :--- | :--- | :--- |")
        for idx, r in enumerate(report["largest_bodies"], 1):
            lines.append(
                f"| {idx} | **{r['size_bytes'] / 1024:.1f}** | `{r['content_encoding']}` "
                f"| `{r['mime_type'][:28]}` | `{r['host']}{r['path'][:60]}` |"
            )
    else:
        lines.append("- No responses found.")

    lines.extend(["", "## 3. Uncompressed Text/API Responses", ""])
    if report["uncompressed_responses"]:
        for u in report["uncompressed_responses"][:20]:
            lines.append(
                f"- **{u['size_bytes'] / 1024:.1f} KB** (`{u['mime_type']}`) `{u['endpoint']}`"
            )
    else:
        lines.append("- None detected.")

    lines.extend(["", "## 4. Duplicate Requests Within Same Scenario", ""])
    if report["duplicate_requests"]:
        lines.append("| Count | Scenario | Method | URL |")
        lines.append("| ---: | :--- | :--- | :--- |")
        for d in report["duplicate_requests"][:20]:
            lines.append(
                f"| {d['count']} | `{d['scenario']}` | `{d['method']}` | `{d['url'][:80]}` |"
            )
    else:
        lines.append("- None detected.")

    return "\n".join(lines).rstrip() + "\n"
