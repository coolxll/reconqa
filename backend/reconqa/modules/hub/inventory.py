"""Project-level cumulative Master API Inventory builder (Explore sessions + Completed Runs)."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.modules.hub.coverage_diff import _group_prefix
from reconqa.modules.hub.har_parser import normalize_path
from reconqa.utils.io import read_json, read_yaml

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

_SOURCE_PRIORITY = {
    "har-live": 4,
    "openapi-doc": 3,
    "js-static-inferred": 2,
    "js-static": 1,
}


def _natural_sort_key(value: str) -> list[Any]:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", value)]


def _normalize_source(raw: str | None) -> str:
    val = (raw or "").strip().lower().replace("_", "-")
    if val in {"har-live", "live"}:
        return "har-live"
    if val in {"openapi-doc", "openapi-probe", "swagger"}:
        return "openapi-doc"
    if val in {"js-static-inferred", "seed-curl"}:
        return "js-static-inferred"
    return "js-static"


def _extract_json_media(container: dict[str, Any] | None) -> tuple[dict[str, Any] | None, Any]:
    if not isinstance(container, dict):
        return None, None
    content = container.get("content")
    if not isinstance(content, dict) or not content:
        return None, None
    media = content.get("application/json")
    if not isinstance(media, dict):
        for k, v in content.items():
            if "json" in str(k).lower() and isinstance(v, dict):
                media = v
                break
    if not isinstance(media, dict):
        first = next(iter(content.values()), None)
        media = first if isinstance(first, dict) else None
    if not isinstance(media, dict):
        return None, None

    schema = media.get("schema") if isinstance(media.get("schema"), dict) else None
    example = media.get("example")
    if example is None and isinstance(media.get("examples"), dict):
        for ex_obj in media["examples"].values():
            if isinstance(ex_obj, dict) and "value" in ex_obj:
                example = ex_obj["value"]
                break
    return schema, example


def _extract_first_2xx_response(
    responses: dict[str, Any] | None,
) -> tuple[list[int], dict[str, Any] | None, Any]:
    if not isinstance(responses, dict):
        return [], None, None
    codes: list[int] = []
    for k in responses:
        try:
            codes.append(int(str(k)))
        except ValueError:
            pass
    codes = sorted(set(codes))

    preferred_keys = ["200", "201", "202", "204"] + [str(c) for c in codes if 200 <= c < 300]
    for rk in preferred_keys:
        if rk in responses and isinstance(responses[rk], dict):
            schema, example = _extract_json_media(responses[rk])
            if schema is not None or example is not None:
                return codes, schema, example
    for resp_obj in responses.values():
        if isinstance(resp_obj, dict):
            schema, example = _extract_json_media(resp_obj)
            if schema is not None or example is not None:
                return codes, schema, example
    return codes, None, None


def _derive_run_label(run: Any) -> str:
    cfg = getattr(run, "config_snapshot", None) or {}
    if isinstance(cfg, dict) and str(cfg.get("run_name") or "").strip():
        return str(cfg["run_name"]).strip()
    target_url = str(cfg.get("target_url") or "") if isinstance(cfg, dict) else ""
    host = (urlparse(target_url).hostname or "") if target_url else ""
    run_id = str(getattr(run, "id", ""))
    if host:
        return f"{host} · #{run_id[:8]}"
    return f"Run #{run_id[:8]}"


def _load_run_openapi(workspace_dir: Path) -> dict[str, Any] | None:
    hub_dir = workspace_dir / "hub"
    json_path = hub_dir / "openapi.json"
    if json_path.is_file():
        try:
            data = read_json(json_path)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    yaml_path = hub_dir / "openapi.yaml"
    if yaml_path.is_file():
        try:
            data = read_yaml(yaml_path)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def _merge_parameters(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    prefer_incoming: bool,
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for p in existing:
        if isinstance(p, dict) and p.get("name"):
            k = (str(p.get("in") or "query"), str(p["name"]))
            by_key[k] = p
            order.append(k)
    for p in incoming:
        if isinstance(p, dict) and p.get("name"):
            k = (str(p.get("in") or "query"), str(p["name"]))
            if k not in by_key:
                by_key[k] = p
                order.append(k)
            elif prefer_incoming:
                by_key[k] = p
    return [by_key[k] for k in order]


def build_project_inventory(
    *,
    project_id: str,
    project_name: str,
    runs: list[Any],
    explore_results: list[dict[str, Any]],
    project_slug: str = "",
    scenario_meta: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build cumulative Project Master API Inventory and merged OpenAPI 3.0.3 spec."""
    bucket: dict[tuple[str, str], dict[str, Any]] = {}
    servers_seen: list[str] = []

    def _upsert(
        *,
        method: str,
        raw_path: str,
        source: str,
        summary: str = "",
        host: str = "",
        tags: list[str] | None = None,
        scenarios: list[str] | None = None,
        run_ref: dict[str, str] | None = None,
        status_codes: list[int] | None = None,
        parameters: list[dict[str, Any]] | None = None,
        request_schema: dict[str, Any] | None = None,
        response_schema: dict[str, Any] | None = None,
        request_example: Any = None,
        response_example: Any = None,
    ) -> None:
        m = method.upper().strip()
        if m.lower() not in _HTTP_METHODS:
            return
        norm_path, _ = normalize_path(raw_path or "/")
        key = (m, norm_path)
        norm_src = _normalize_source(source)
        src_prio = _SOURCE_PRIORITY.get(norm_src, 1)

        cur = bucket.get(key)
        if cur is None:
            bucket[key] = {
                "method": m,
                "path": norm_path,
                "summary": summary,
                "discovery_source": norm_src,
                "host": host,
                "tags": list(dict.fromkeys(str(t) for t in (tags or []) if str(t).strip())),
                "scenarios": list(dict.fromkeys(str(s) for s in (scenarios or []) if str(s).strip())),
                "seen_in_runs": [run_ref] if run_ref else [],
                "status_codes": sorted(set(status_codes or [])),
                "parameters": list(parameters or []),
                "request_schema": request_schema,
                "response_schema": response_schema,
                "request_example": request_example,
                "response_example": response_example,
            }
            return

        cur_prio = _SOURCE_PRIORITY.get(cur["discovery_source"], 1)
        prefer_new = src_prio >= cur_prio

        if src_prio > cur_prio:
            cur["discovery_source"] = norm_src
        if summary and (prefer_new or not cur["summary"]):
            cur["summary"] = summary
        if host and (prefer_new or not cur["host"]):
            cur["host"] = host

        for t in tags or []:
            ts = str(t).strip()
            if ts and ts not in cur["tags"]:
                cur["tags"].append(ts)
        for s in scenarios or []:
            ss = str(s).strip()
            if ss and ss not in cur["scenarios"]:
                cur["scenarios"].append(ss)
        if run_ref:
            if not any(r["run_id"] == run_ref["run_id"] for r in cur["seen_in_runs"]):
                cur["seen_in_runs"].append(run_ref)
        if status_codes:
            cur["status_codes"] = sorted(set(cur["status_codes"]) | set(status_codes))
        if parameters:
            cur["parameters"] = _merge_parameters(
                cur["parameters"], parameters, prefer_incoming=prefer_new
            )
        if request_schema is not None and (prefer_new or cur["request_schema"] is None):
            cur["request_schema"] = request_schema
        if response_schema is not None and (prefer_new or cur["response_schema"] is None):
            cur["response_schema"] = response_schema
        if request_example is not None and (prefer_new or cur["request_example"] is None):
            cur["request_example"] = request_example
        if response_example is not None and (prefer_new or cur["response_example"] is None):
            cur["response_example"] = response_example

    # 1. Seed baseline from linked Explore sessions
    for exp in explore_results:
        seed = exp.get("seed") or {}
        base_url = str(seed.get("base_url") or "")
        if base_url and base_url not in servers_seen:
            servers_seen.append(base_url)
        default_host = (urlparse(base_url).netloc or str(seed.get("root_domain") or "")).lower()

        for ep in exp.get("discovered_endpoints") or []:
            if not isinstance(ep, dict):
                continue
            ep_url = str(ep.get("url") or "")
            ep_host = (urlparse(ep_url).netloc if ep_url else "") or default_host
            inferred_params = [
                {"name": str(p), "in": "query", "required": False, "schema": {"type": "string"}}
                for p in (ep.get("inferred_params") or [])
                if str(p).strip()
            ]
            _upsert(
                method=str(ep.get("method") or "GET"),
                raw_path=str(ep.get("path") or "/"),
                source=str(ep.get("source") or "js-static"),
                summary=str(ep.get("snippet") or "")[:140]
                if str(ep.get("source") or "") == "openapi_probe"
                else "",
                host=ep_host,
                parameters=inferred_params,
            )

    # 2. Layer completed Runs chronologically (oldest -> newest so newer har-live wins)
    runs_merged_count = 0
    for run in runs:
        ws_str = str(getattr(run, "workspace_dir", "") or "")
        if not ws_str:
            continue
        spec = _load_run_openapi(Path(ws_str))
        if not spec or not isinstance(spec.get("paths"), dict):
            continue
        runs_merged_count += 1

        cfg = getattr(run, "config_snapshot", None) or {}
        run_target = str(cfg.get("target_url") or "") if isinstance(cfg, dict) else ""
        spec_servers = [
            str(s.get("url"))
            for s in (spec.get("servers") or [])
            if isinstance(s, dict) and s.get("url")
        ]
        for srv in spec_servers:
            if srv and srv not in servers_seen:
                servers_seen.append(srv)
        primary_server = spec_servers[0] if spec_servers else run_target
        run_host = (urlparse(primary_server).netloc or "") if primary_server else ""
        run_ref = {"run_id": str(run.id), "run_name": _derive_run_label(run)}

        for raw_path, path_item in spec["paths"].items():
            if not isinstance(path_item, dict):
                continue
            for m_key, op in path_item.items():
                if str(m_key).lower() not in _HTTP_METHODS or not isinstance(op, dict):
                    continue
                req_schema, req_example = _extract_json_media(op.get("requestBody"))
                status_codes, resp_schema, resp_example = _extract_first_2xx_response(
                    op.get("responses")
                )
                op_scenarios = [str(s) for s in (op.get("x-scenarios") or []) if str(s).strip()]
                op_tags = [str(t) for t in (op.get("tags") or []) if str(t).strip()]
                op_params = [p for p in (op.get("parameters") or []) if isinstance(p, dict)]
                _upsert(
                    method=str(m_key).upper(),
                    raw_path=str(raw_path),
                    source=str(op.get("x-discovery-source") or "har-live"),
                    summary=str(op.get("summary") or ""),
                    host=run_host,
                    tags=op_tags,
                    scenarios=op_scenarios,
                    run_ref=run_ref,
                    status_codes=status_codes,
                    parameters=op_params,
                    request_schema=req_schema,
                    response_schema=resp_schema,
                    request_example=req_example,
                    response_example=resp_example,
                )

    # 3. Sort endpoints (`har-live` first, then method, path) & compute rollups
    endpoints = sorted(
        bucket.values(),
        key=lambda ep: (
            -_SOURCE_PRIORITY.get(ep["discovery_source"], 1),
            ep["method"],
            ep["path"],
        ),
    )
    for ep in endpoints:
        ep["scenarios"] = sorted(set(ep["scenarios"]), key=_natural_sort_key)

    total_endpoints = len(endpoints)
    har_live_count = sum(1 for ep in endpoints if ep["discovery_source"] == "har-live")
    js_static_count = total_endpoints - har_live_count
    coverage_pct = round((har_live_count / total_endpoints) * 100.0, 2) if total_endpoints else 0.0

    all_scenarios = sorted(
        {s for ep in endpoints for s in ep["scenarios"]},
        key=_natural_sort_key,
    )

    prefix_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "covered": 0})
    for ep in endpoints:
        pfx = _group_prefix(ep["path"])
        prefix_totals[pfx]["total"] += 1
        if ep["discovery_source"] == "har-live":
            prefix_totals[pfx]["covered"] += 1

    by_prefix = [
        {
            "prefix": pfx,
            "total": counts["total"],
            "covered": counts["covered"],
            "coverage_pct": round((counts["covered"] / counts["total"]) * 100.0, 2)
            if counts["total"]
            else 0.0,
        }
        for pfx, counts in sorted(
            prefix_totals.items(),
            key=lambda kv: (-kv[1]["total"], kv[0]),
        )
    ]

    inventory_response: dict[str, Any] = {
        "project_id": project_id,
        "project_name": project_name,
        "runs_merged_count": runs_merged_count,
        "explore_sessions_count": len(explore_results),
        "total_endpoints": total_endpoints,
        "har_live_count": har_live_count,
        "js_static_count": js_static_count,
        "coverage_pct": coverage_pct,
        "scenarios": all_scenarios,
        "scenario_meta": scenario_meta or {},
        "by_prefix": by_prefix,
        "endpoints": endpoints,
    }

    # 4. Assemble standard OpenAPI 3.0.3 JSON document for download
    openapi_paths: dict[str, dict[str, Any]] = {}
    for ep in endpoints:
        m_low = ep["method"].lower()
        op_obj: dict[str, Any] = {
            "summary": ep["summary"] or f"{ep['method']} {ep['path']}",
            "x-discovery-source": ep["discovery_source"],
            "x-scenarios": ep["scenarios"],
            "x-seen-in-runs": [r["run_id"] for r in ep["seen_in_runs"]],
        }
        if ep["tags"]:
            op_obj["tags"] = ep["tags"]
        if ep["parameters"]:
            op_obj["parameters"] = ep["parameters"]
        if ep["request_schema"] is not None or ep["request_example"] is not None:
            media_req: dict[str, Any] = {}
            if ep["request_schema"] is not None:
                media_req["schema"] = ep["request_schema"]
            if ep["request_example"] is not None:
                media_req["example"] = ep["request_example"]
            op_obj["requestBody"] = {"content": {"application/json": media_req}}

        responses_obj: dict[str, Any] = {}
        primary_code = str(ep["status_codes"][0]) if ep["status_codes"] else "200"
        resp_entry: dict[str, Any] = {"description": "Response"}
        if ep["response_schema"] is not None or ep["response_example"] is not None:
            media_resp: dict[str, Any] = {}
            if ep["response_schema"] is not None:
                media_resp["schema"] = ep["response_schema"]
            if ep["response_example"] is not None:
                media_resp["example"] = ep["response_example"]
            resp_entry["content"] = {"application/json": media_resp}
        responses_obj[primary_code] = resp_entry
        for code in ep["status_codes"]:
            code_str = str(code)
            if code_str not in responses_obj:
                responses_obj[code_str] = {"description": f"HTTP {code_str}"}
        op_obj["responses"] = responses_obj

        openapi_paths.setdefault(ep["path"], {})[m_low] = op_obj

    openapi_doc: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": f"{project_name} — Master API Inventory",
            "version": "1.0.0",
            "description": (
                f"Cumulative OpenAPI 3.0.3 inventory for project '{project_name}' "
                f"({runs_merged_count} runs, {len(explore_results)} explore sessions)."
            ),
        },
        "servers": [{"url": u} for u in servers_seen] if servers_seen else [{"url": "https://api.example.com"}],
        "paths": openapi_paths,
        "x-reconqa-project-id": project_id,
        "x-reconqa-project-slug": project_slug or project_id,
    }

    return inventory_response, openapi_doc
