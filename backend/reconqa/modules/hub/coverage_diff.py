"""Compute API coverage diff between live HAR traffic and statically discovered JS endpoints."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from reconqa.models.schemas import (
    CoverageDiffReport,
    CoveragePrefixStats,
    UncoveredEndpoint,
)
from reconqa.modules.hub.har_parser import normalize_path
from reconqa.utils.io import ensure_dir, read_json, read_yaml, write_json

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def _group_prefix(normalized_path: str) -> str:
    segments = [s for s in normalized_path.strip("/").split("/") if s]
    if not segments:
        return "/"
    non_param = [s for s in segments if not s.startswith("{")]
    if not non_param:
        return "/"
    if len(non_param) >= 3 and non_param[0].lower() in {"api", "rest", "rpc", "gw"} and re.match(
        r"^v\d+(?:\.\d+)?$", non_param[1], re.IGNORECASE
    ):
        return "/" + "/".join(non_param[:3])
    if len(non_param) >= 2 and non_param[0].lower() in {"api", "rest", "rpc", "gw"}:
        return "/" + "/".join(non_param[:2])
    return "/" + non_param[0]


def _extract_inferred_params(op: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for p in op.get("parameters") or []:
        if isinstance(p, dict) and p.get("name") and p.get("in") != "path":
            names.append(str(p["name"]))
    req_body = op.get("requestBody") or {}
    content = req_body.get("content") or {}
    json_media = content.get("application/json") or {}
    props = (json_media.get("schema") or {}).get("properties") or {}
    for k in props:
        names.append(str(k))
    return list(dict.fromkeys(names))


def _render_markdown(report: CoverageDiffReport) -> str:
    lines = [
        "# API Coverage Diff Report",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Total Endpoints | {report.total_endpoints} |",
        f"| Covered (`har-live`) | {report.covered_count} |",
        f"| Uncovered (JS Static) | {report.uncovered_count} |",
        f"| Uncovered (`js-static-inferred`, High Priority) | {report.js_static_inferred_uncovered_count} |",
        f"| Uncovered (`js-static`, Path Only) | {report.js_static_uncovered_count} |",
        f"| Overall Coverage | {report.coverage_pct:.2f}% |",
        "",
        "## Coverage by Prefix",
        "",
        "| Prefix | Total | Covered | Uncovered | Coverage % |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for grp in report.by_prefix:
        lines.append(
            f"| `{grp.prefix}` | {grp.total} | {grp.covered} | {grp.uncovered} | {grp.coverage_pct:.2f}% |"
        )
    if not report.by_prefix:
        lines.append("| _(none)_ | 0 | 0 | 0 | 0.00% |")

    lines.extend(
        [
            "",
            "## Uncovered Blind Spots (Prioritized)",
            "",
        ]
    )
    if not report.uncovered_endpoints:
        lines.append("All statically discovered endpoints are covered by live HAR traffic.")
    else:
        lines.extend(
            [
                "| Priority | Source | Method | Path | Inferred Params |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for ep in report.uncovered_endpoints:
            params_str = ", ".join(f"`{p}`" for p in ep.inferred_params) if ep.inferred_params else "-"
            lines.append(
                f"| {ep.priority} | `{ep.source}` | `{ep.method}` | `{ep.path}` | {params_str} |"
            )

    return "\n".join(lines) + "\n"


def compute_coverage_diff(openapi_path: Path, out_dir: Path) -> dict[str, Any]:
    """Compare live HAR endpoints against static JS endpoints in an OpenAPI 3.0.3 spec."""
    spec_path = Path(openapi_path)
    if spec_path.suffix.lower() == ".json":
        spec = read_json(spec_path)
    else:
        spec = read_yaml(spec_path)

    paths = (spec or {}).get("paths") or {}
    covered_endpoints: list[dict[str, Any]] = []
    uncovered_endpoints: list[UncoveredEndpoint] = []
    prefix_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "covered": 0})
    inferred_uncovered = 0
    static_uncovered = 0

    seen_keys: set[tuple[str, str]] = set()
    for raw_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        norm_path, _ = normalize_path(str(raw_path))
        prefix = _group_prefix(norm_path)
        for method_key, op in path_item.items():
            if method_key.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            method = method_key.upper()
            dedup_key = (method, norm_path)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            source = str(op.get("x-discovery-source") or "har-live")
            scenarios = [str(s) for s in (op.get("x-scenarios") or [])]
            prefix_totals[prefix]["total"] += 1

            if source == "har-live":
                prefix_totals[prefix]["covered"] += 1
                covered_endpoints.append(
                    {
                        "method": method,
                        "path": norm_path,
                        "scenarios": scenarios,
                    }
                )
            else:
                if source == "js-static-inferred":
                    inferred_uncovered += 1
                    priority = "high"
                else:
                    static_uncovered += 1
                    priority = "medium"
                uncovered_endpoints.append(
                    UncoveredEndpoint(
                        method=method,
                        path=norm_path,
                        source=source,
                        priority=priority,  # type: ignore[arg-type]
                        scenarios=scenarios,
                        js_snippet=str(op.get("x-js-snippet") or ""),
                        inferred_params=_extract_inferred_params(op),
                    )
                )

    # Sort uncovered endpoints: js-static-inferred first, then path & method
    source_rank = {"js-static-inferred": 0, "js-static": 1}
    uncovered_endpoints.sort(
        key=lambda e: (source_rank.get(e.source, 2), e.path, e.method)
    )
    covered_endpoints.sort(key=lambda e: (e["path"], e["method"]))

    by_prefix: list[CoveragePrefixStats] = []
    for prefix, counts in sorted(prefix_totals.items()):
        total_p = counts["total"]
        cov_p = counts["covered"]
        uncov_p = total_p - cov_p
        pct_p = round((cov_p / total_p) * 100.0, 2) if total_p > 0 else 0.0
        by_prefix.append(
            CoveragePrefixStats(
                prefix=prefix,
                total=total_p,
                covered=cov_p,
                uncovered=uncov_p,
                coverage_pct=pct_p,
            )
        )

    total = len(covered_endpoints) + len(uncovered_endpoints)
    covered_count = len(covered_endpoints)
    uncovered_count = len(uncovered_endpoints)
    coverage_pct = round((covered_count / total) * 100.0, 2) if total > 0 else 0.0

    report = CoverageDiffReport(
        total_endpoints=total,
        covered_count=covered_count,
        uncovered_count=uncovered_count,
        js_static_inferred_uncovered_count=inferred_uncovered,
        js_static_uncovered_count=static_uncovered,
        coverage_pct=coverage_pct,
        by_prefix=by_prefix,
        covered_endpoints=covered_endpoints,
        uncovered_endpoints=uncovered_endpoints,
    )
    payload = report.model_dump()

    out_path = ensure_dir(Path(out_dir))
    write_json(out_path / "coverage_diff.json", payload)
    (out_path / "coverage_diff.md").write_text(_render_markdown(report), encoding="utf-8")
    return payload
