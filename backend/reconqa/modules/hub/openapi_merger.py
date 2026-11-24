"""Merge multi-scenario HAR files and static JS endpoints into OpenAPI 3.0.3."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from reconqa.config import ProjectConfig
from reconqa.modules.hub.har_parser import HarEntry, normalize_path, parse_har_dir
from reconqa.utils.io import ensure_dir, read_json, redact_header_value, write_json, write_yaml

SOURCE_PRIORITY = {
    "js-static": 1,
    "js-static-inferred": 2,
    "har-live": 3,
}

STANDARD_HEADERS = {
    "accept",
    "accept-charset",
    "accept-encoding",
    "accept-language",
    "authorization",
    "cache-control",
    "connection",
    "content-length",
    "content-type",
    "cookie",
    "dnt",
    "host",
    "if-match",
    "if-modified-since",
    "if-none-match",
    "origin",
    "pragma",
    "priority",
    "referer",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "te",
    "upgrade-insecure-requests",
    "user-agent",
    "x-requested-with",
}

EPHEMERAL_TRACE_HEADERS = {
    "b3",
    "traceparent",
    "tracestate",
    "x-b3-traceid",
    "x-b3-spanid",
    "x-b3-parentspanid",
    "x-b3-sampled",
    "x-b3-flags",
    "sentry-trace",
    "baggage",
}

SENSITIVE_EXAMPLE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "privatekey",
    "private_key",
}

BIZ_CODE_CANDIDATE_KEYS = (
    "code",
    "errcode",
    "err_code",
    "errorCode",
    "error_code",
    "statusCode",
    "status_code",
    "ret",
    "retCode",
    "ret_code",
    "errno",
    "success",
    "ok",
    "status",
)

HTTP_METHODS_WITH_BODY = {"POST", "PUT", "PATCH", "DELETE"}

_AUTH_ERROR_MSG_PHRASES = (
    "未登录",
    "请登录",
    "请先登录",
    "重新登录",
    "登录已过期",
    "登录信息已失效",
    "登录失效",
    "登录超时",
    "令牌无效",
    "令牌过期",
    "无权限",
    "unauthorized",
    "not logged in",
    "login required",
    "please login",
    "please sign in",
    "session expired",
    "token expired",
    "invalid token",
    "authentication required",
    "unauthenticated",
)

_AUTH_ERROR_MSG_KEYS = ("msg", "message", "errmsg", "error", "detail", "desc")


def _response_indicates_auth_required(entry: HarEntry) -> bool:
    """Return True if a HAR response clearly indicates authentication/session is required."""
    if entry.status in (401, 403):
        return True
    if not isinstance(entry.response_json, dict):
        return False

    lower_map = {str(k).lower(): v for k, v in entry.response_json.items()}
    for key in _AUTH_ERROR_MSG_KEYS:
        val = lower_map.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        msg = val.lower()
        if any(phrase in msg for phrase in _AUTH_ERROR_MSG_PHRASES):
            return True
        if "登录" in msg and "解锁" in msg:
            return True

    if entry.status == 200 and lower_map.get("success") is not True:
        raw_code = lower_map.get("code")
        if isinstance(raw_code, int) and not isinstance(raw_code, bool) and raw_code in (401, 403):
            return True
        if isinstance(raw_code, str) and raw_code.strip() in {"401", "403"}:
            return True

    return False


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(ordered[int(rank)], 2)
    weight = rank - low
    return round(ordered[low] * (1.0 - weight) + ordered[high] * weight, 2)


def _infer_schema(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "string", "nullable": True}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        if not value:
            return {"type": "array", "items": {}}
        merged_items: dict[str, Any] | None = None
        for item in value[:20]:
            item_schema = _infer_schema(item)
            merged_items = (
                item_schema
                if merged_items is None
                else _merge_schemas(merged_items, item_schema)
            )
        return {"type": "array", "items": merged_items or {}}
    if isinstance(value, dict):
        props: dict[str, Any] = {}
        req: list[str] = []
        for k, v in value.items():
            key_str = str(k)
            props[key_str] = _infer_schema(v)
            if v is not None:
                req.append(key_str)
        schema: dict[str, Any] = {"type": "object", "properties": props}
        if req:
            schema["required"] = sorted(req)
        return schema
    return {"type": "string"}


def _merge_schemas(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    if not a:
        return dict(b)
    if not b:
        return dict(a)

    nullable = bool(a.get("nullable")) or bool(b.get("nullable"))
    type_a = a.get("type")
    type_b = b.get("type")

    # Handle nullable placeholder ("type": "string", "nullable": True) paired with real type
    if a.get("nullable") and type_a == "string" and "properties" not in a and type_b != "string":
        res = dict(b)
        res["nullable"] = True
        return res
    if b.get("nullable") and type_b == "string" and "properties" not in b and type_a != "string":
        res = dict(a)
        res["nullable"] = True
        return res

    if type_a == "object" and type_b == "object":
        props_a = a.get("properties") or {}
        props_b = b.get("properties") or {}
        merged_props: dict[str, Any] = {}
        all_keys = list(dict.fromkeys([*props_a.keys(), *props_b.keys()]))
        for key in all_keys:
            if key in props_a and key in props_b:
                merged_props[key] = _merge_schemas(props_a[key], props_b[key])
            elif key in props_a:
                merged_props[key] = dict(props_a[key])
            else:
                merged_props[key] = dict(props_b[key])

        req_a = set(a.get("required") or [])
        req_b = set(b.get("required") or [])
        common_req = sorted(req_a & req_b)
        merged: dict[str, Any] = {"type": "object", "properties": merged_props}
        if common_req:
            merged["required"] = common_req
        if nullable:
            merged["nullable"] = True
        return merged

    if type_a == "array" and type_b == "array":
        items_a = a.get("items") or {}
        items_b = b.get("items") or {}
        merged_arr: dict[str, Any] = {
            "type": "array",
            "items": _merge_schemas(items_a, items_b),
        }
        if nullable:
            merged_arr["nullable"] = True
        return merged_arr

    if {type_a, type_b} == {"integer", "number"}:
        res_num: dict[str, Any] = {"type": "number"}
        if nullable:
            res_num["nullable"] = True
        return res_num

    if type_a == type_b and type_a is not None:
        res_same = dict(a)
        if nullable:
            res_same["nullable"] = True
        return res_same

    # Mixed primitive/structural fallback -> string or untyped nullable
    fallback: dict[str, Any] = {"type": "string"}
    if nullable:
        fallback["nullable"] = True
    return fallback


def _redact_example(value: Any, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(k): _redact_example(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_example(item, parent_key) for item in value[:5]]
    if isinstance(value, str) and parent_key.lower() in SENSITIVE_EXAMPLE_KEYS:
        return "***REDACTED***"
    return value


def _derive_tag(normalized_path: str) -> str:
    segments = [s for s in normalized_path.strip("/").split("/") if s and not s.startswith("{")]
    for seg in segments:
        if seg.lower() in {"api", "rest", "rpc", "gateway", "gw"}:
            continue
        if re.match(r"^v\d+(?:\.\d+)?$", seg, re.IGNORECASE):
            continue
        return seg
    return "default"


def _make_operation_id(method: str, normalized_path: str, used: set[str]) -> str:
    parts = [method.lower()]
    for seg in normalized_path.strip("/").split("/"):
        if not seg:
            continue
        if seg.startswith("{") and seg.endswith("}"):
            parts.append("by_" + seg[1:-1])
        else:
            cleaned = re.sub(r"[^A-Za-z0-9]+", "_", seg).strip("_")
            if cleaned:
                parts.append(cleaned)
    raw_id = "_".join(parts)
    # Convert to camelCase identifier
    tokens = [t for t in raw_id.split("_") if t]
    op_id = tokens[0].lower() + "".join(t[0].upper() + t[1:] for t in tokens[1:]) if tokens else "operation"
    candidate = op_id
    suffix = 2
    while candidate in used:
        candidate = f"{op_id}{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _extract_custom_headers(entries: list[HarEntry]) -> dict[str, str]:
    custom: dict[str, str] = {}
    for entry in entries:
        for name, value in entry.request_headers.items():
            lower = name.lower()
            if (
                lower in STANDARD_HEADERS
                or lower in EPHEMERAL_TRACE_HEADERS
                or lower.startswith(("sec-", "x-b3-"))
            ):
                continue
            custom[name] = redact_header_value(name, value)
    return custom


def _infer_biz_success(
    entries: list[HarEntry],
    path: str,
    project_config: ProjectConfig | None,
    global_fallback: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if project_config is not None:
        override = project_config.match_biz_success_rule(path)
        if override is not None:
            return override

    dict_responses = [
        e.response_json
        for e in entries
        if 200 <= e.status < 300
        and isinstance(e.response_json, dict)
        and not _response_indicates_auth_required(e)
    ]
    if not dict_responses:
        return global_fallback

    for key in BIZ_CODE_CANDIDATE_KEYS:
        observed: list[Any] = []
        for payload in dict_responses:
            if key in payload:
                val = payload[key]
                if isinstance(val, (int, float, str, bool)):
                    observed.append(val)
        if not observed:
            continue

        if key in {"success", "ok"}:
            if True in observed:
                return {"field": key, "value": True}
            continue

        # Count occurrences (distinguish bool from int in Python dicts)
        counts: Counter[tuple[type, Any]] = Counter((type(v), v) for v in observed)
        (_, best_val), _ = counts.most_common(1)[0]
        if (
            key == "status"
            and isinstance(best_val, str)
            and best_val.lower() not in {"ok", "success", "succeeded", "200", "0"}
        ):
            continue
        return {"field": key, "value": best_val}

    return global_fallback


def _infer_global_biz_success(entries: list[HarEntry]) -> dict[str, Any] | None:
    return _infer_biz_success(entries, "", None, global_fallback=None)


def _load_js_endpoints(js_endpoints_path: Path | None) -> list[dict[str, Any]]:
    if not js_endpoints_path:
        return []
    path = Path(js_endpoints_path)
    if not path.is_file():
        return []
    data = read_json(path)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        endpoints = data.get("endpoints")
        if isinstance(endpoints, list):
            return [item for item in endpoints if isinstance(item, dict)]
    return []


def _schema_from_js_keys(keys: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {str(k): {"type": "string"} for k in keys if str(k).strip()},
    }


def merge_to_openapi(
    har_dir: Path,
    out_dir: Path,
    *,
    js_endpoints_path: Path | None = None,
    target_domain: str | None = None,
    project_config: ProjectConfig | None = None,
) -> dict[str, Any]:
    """Merge scenario HAR files and optional static JS endpoints into OpenAPI 3.0.3."""
    eff_domain = target_domain or (project_config.target.domain if project_config else None) or None
    ignore_prefixes = project_config.ignore_path_prefixes if project_config else None

    entries = parse_har_dir(
        Path(har_dir),
        target_domain=eff_domain,
        ignore_path_prefixes=ignore_prefixes,
    )
    js_items = _load_js_endpoints(js_endpoints_path)

    # Group HAR entries by (normalized_path, method)
    grouped_har: dict[tuple[str, str], list[HarEntry]] = defaultdict(list)
    host_counts: Counter[str] = Counter()
    all_scenarios: set[str] = set()

    for entry in entries:
        grouped_har[(entry.normalized_path, entry.method)].append(entry)
        host_counts[f"{entry.scheme}://{entry.host}"] += 1
        all_scenarios.add(entry.scenario)

    # Group JS endpoints by (normalized_path, method)
    grouped_js: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in js_items:
        raw_path = str(item.get("path") or item.get("url") or "").strip()
        if not raw_path:
            continue
        if "://" in raw_path:
            from urllib.parse import urlsplit

            raw_path = urlsplit(raw_path).path or "/"
        norm_path, _ = normalize_path(raw_path)
        method = str(item.get("method") or "GET").upper().strip()
        if method == "OPTIONS":
            continue
        grouped_js[(norm_path, method)].append(item)

    global_biz_success = _infer_global_biz_success(entries)
    if project_config is not None:
        cfg_global = project_config.match_biz_success_rule("")
        if cfg_global is not None:
            global_biz_success = cfg_global

    all_keys = sorted(set(grouped_har.keys()) | set(grouped_js.keys()))
    paths_obj: dict[str, dict[str, Any]] = {}
    used_op_ids: set[str] = set()
    source_counts: Counter[str] = Counter()

    for norm_path, method in all_keys:
        har_group = grouped_har.get((norm_path, method), [])
        js_group = grouped_js.get((norm_path, method), [])

        # Determine discovery source by priority: har-live > js-static-inferred > js-static
        source = "js-static"
        if har_group:
            source = "har-live"
        else:
            best_prio = 0
            for j in js_group:
                candidate_src = str(
                    j.get("x-discovery-source")
                    or j.get("discovery_source")
                    or j.get("source")
                    or "js-static"
                )
                if candidate_src not in SOURCE_PRIORITY:
                    has_clues = bool(
                        j.get("params")
                        or j.get("query_params")
                        or j.get("request_params")
                        or j.get("request_keys")
                        or j.get("body_keys")
                        or j.get("response_keys")
                    )
                    candidate_src = "js-static-inferred" if has_clues else "js-static"
                prio = SOURCE_PRIORITY[candidate_src]
                if prio > best_prio:
                    best_prio = prio
                    source = candidate_src

        source_counts[source] += 1

        # Scenarios & JS snippet
        scenarios_set: set[str] = {e.scenario for e in har_group}
        js_snippet = ""
        js_req_keys: list[str] = []
        js_resp_keys: list[str] = []
        for j in js_group:
            if j.get("scenario"):
                scenarios_set.add(str(j["scenario"]))
            for sc in j.get("scenarios") or []:
                scenarios_set.add(str(sc))
            if not js_snippet:
                js_snippet = str(
                    j.get("x-js-snippet") or j.get("js_snippet") or j.get("snippet") or ""
                )[:400]
            for field_name in ("params", "query_params", "request_params", "request_keys", "body_keys"):
                vals = j.get(field_name)
                if isinstance(vals, list):
                    js_req_keys.extend(str(v) for v in vals if v)
                elif isinstance(vals, dict):
                    js_req_keys.extend(str(k) for k in vals)
            rkeys = j.get("response_keys")
            if isinstance(rkeys, list):
                js_resp_keys.extend(str(v) for v in rkeys if v)
        js_req_keys = list(dict.fromkeys(js_req_keys))
        js_resp_keys = list(dict.fromkeys(js_resp_keys))

        # Parameters (path + query)
        parameters: list[dict[str, Any]] = []
        path_param_names = re.findall(r"\{([^}]+)\}", norm_path)
        for p_name in path_param_names:
            sample_val = next(
                (e.path_params.get(p_name) for e in har_group if p_name in e.path_params),
                "sample-id",
            )
            param_obj: dict[str, Any] = {
                "name": p_name,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
            if sample_val is not None:
                param_obj["example"] = sample_val
            parameters.append(param_obj)

        query_samples: dict[str, list[str]] = defaultdict(list)
        for e in har_group:
            for qk, qv in e.query_params.items():
                query_samples[qk].append(qv)
        if not har_group and method == "GET":
            for rk in js_req_keys:
                query_samples.setdefault(rk, [])

        for qk, vals in sorted(query_samples.items()):
            q_param: dict[str, Any] = {
                "name": qk,
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            }
            if vals:
                q_param["example"] = _redact_example(vals[0], qk)
            parameters.append(q_param)

        # Request Body
        request_body: dict[str, Any] | None = None
        if method in HTTP_METHODS_WITH_BODY:
            merged_req_schema: dict[str, Any] | None = None
            req_example: Any = None
            for e in har_group:
                if e.request_json is not None:
                    s = _infer_schema(e.request_json)
                    merged_req_schema = (
                        s if merged_req_schema is None else _merge_schemas(merged_req_schema, s)
                    )
                    if req_example is None:
                        req_example = _redact_example(e.request_json)
                elif e.request_form:
                    s = _infer_schema(e.request_form)
                    merged_req_schema = (
                        s if merged_req_schema is None else _merge_schemas(merged_req_schema, s)
                    )
                    if req_example is None:
                        req_example = _redact_example(e.request_form)

            if merged_req_schema is None and js_req_keys:
                merged_req_schema = _schema_from_js_keys(js_req_keys)

            if merged_req_schema is not None:
                media: dict[str, Any] = {"schema": merged_req_schema}
                if req_example is not None:
                    media["example"] = req_example
                request_body = {
                    "required": True,
                    "content": {"application/json": media},
                }

        # Responses
        responses: dict[str, Any] = {}
        resp_by_status: dict[int, list[HarEntry]] = defaultdict(list)
        for e in har_group:
            resp_by_status[e.status or 200].append(e)

        for status_code, status_entries in sorted(resp_by_status.items()):
            merged_resp_schema: dict[str, Any] | None = None
            resp_example: Any = None
            for e in status_entries:
                if e.response_json is not None:
                    s = _infer_schema(e.response_json)
                    merged_resp_schema = (
                        s if merged_resp_schema is None else _merge_schemas(merged_resp_schema, s)
                    )
                    if resp_example is None:
                        resp_example = _redact_example(e.response_json)
            resp_obj: dict[str, Any] = {
                "description": f"HTTP {status_code} response",
            }
            if merged_resp_schema is not None:
                media_resp: dict[str, Any] = {"schema": merged_resp_schema}
                if resp_example is not None:
                    media_resp["example"] = resp_example
                resp_obj["content"] = {"application/json": media_resp}
            responses[str(status_code)] = resp_obj

        if not responses:
            default_resp: dict[str, Any] = {"description": "Inferred static endpoint response"}
            if js_resp_keys:
                default_resp["content"] = {
                    "application/json": {"schema": _schema_from_js_keys(js_resp_keys)}
                }
            responses["200"] = default_resp

        # Performance baseline
        times = [e.time_ms for e in har_group]
        sizes = [e.response_size for e in har_group]
        perf_baseline = {
            "count": len(har_group),
            "p50_ms": _percentile(times, 50.0),
            "p95_ms": _percentile(times, 95.0),
            "avg_bytes": round(sum(sizes) / len(sizes)) if sizes else 0,
        }

        custom_headers = _extract_custom_headers(har_group)
        requires_auth = any(
            e.has_auth or _response_indicates_auth_required(e) for e in har_group
        )
        biz_success = _infer_biz_success(
            har_group,
            norm_path,
            project_config,
            global_fallback=global_biz_success if har_group else None,
        )

        endpoint_host_counts = Counter(e.host for e in har_group if e.host)
        endpoint_origin_counts = Counter(
            f"{e.scheme}://{e.host}" for e in har_group if e.host
        )
        endpoint_host = (
            endpoint_host_counts.most_common(1)[0][0] if endpoint_host_counts else ""
        )
        endpoint_origin = (
            endpoint_origin_counts.most_common(1)[0][0] if endpoint_origin_counts else ""
        )

        op_id = _make_operation_id(method, norm_path, used_op_ids)
        operation: dict[str, Any] = {
            "operationId": op_id,
            "summary": f"{method} {norm_path}",
            "tags": [_derive_tag(norm_path)],
            "parameters": parameters,
            "responses": responses,
            "x-discovery-source": source,
            "x-scenarios": sorted(scenarios_set),
            "x-custom-headers": custom_headers,
            "x-js-snippet": js_snippet,
            "x-perf-baseline": perf_baseline,
            "x-biz-success": biz_success,
            "x-requires-auth": requires_auth,
        }
        if endpoint_host:
            operation["x-host"] = endpoint_host
        if endpoint_origin:
            operation["x-origin"] = endpoint_origin
        if request_body is not None:
            operation["requestBody"] = request_body
        if requires_auth:
            operation["security"] = [{"bearerAuth": []}, {"cookieAuth": []}]

        paths_obj.setdefault(norm_path, {})[method.lower()] = operation

    # Determine server base URLs (configured base_url first if present, then observed origins by frequency)
    server_urls: list[str] = []
    if project_config and project_config.target.base_url:
        cfg_base = project_config.target.base_url.rstrip("/")
        if cfg_base:
            server_urls.append(cfg_base)
    for origin_url, _ in host_counts.most_common():
        clean_origin = origin_url.rstrip("/")
        if clean_origin and clean_origin not in server_urls:
            server_urls.append(clean_origin)
    if not server_urls:
        if eff_domain:
            server_urls.append(f"https://{eff_domain}")
        else:
            server_urls.append("https://api.example.com")

    title = (
        project_config.project_name
        if project_config and project_config.project_name
        else "ReconQA Unified API Spec"
    )

    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": title,
            "version": "1.0.0",
            "description": "Auto-merged OpenAPI 3.0.3 specification generated by ReconQA Hub.",
        },
        "servers": [{"url": u} for u in server_urls],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                },
                "cookieAuth": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "session",
                },
            }
        },
        "paths": paths_obj,
        "x-reconqa-summary": {
            "total_operations": len(all_keys),
            "har_live_count": source_counts.get("har-live", 0),
            "js_static_inferred_count": source_counts.get("js-static-inferred", 0),
            "js_static_count": source_counts.get("js-static", 0),
            "scenarios": sorted(all_scenarios),
        },
    }

    ensure_dir(Path(out_dir))
    write_yaml(Path(out_dir) / "openapi.yaml", spec)
    write_json(Path(out_dir) / "openapi.json", spec)
    return spec
