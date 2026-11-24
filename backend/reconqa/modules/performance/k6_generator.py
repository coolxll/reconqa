"""k6 load-test script generator (with data-flow correlation) and execution analyzer."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from reconqa.config import settings
from reconqa.utils.io import ensure_dir, write_json
from reconqa.utils.proc import run_cmd_sync

DEFAULT_STAGES: list[dict[str, Any]] = [
    {"duration": "15s", "target": 10},
    {"duration": "30s", "target": 30},
    {"duration": "30s", "target": 60},
    {"duration": "15s", "target": 0},
]

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "x-auth-token",
    "x-api-key",
    "proxy-authorization",
}

SKIP_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "accept-encoding",
    "keep-alive",
}

STATIC_EXTS = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
)

MUTATION_HINTS = (
    "create",
    "update",
    "delete",
    "remove",
    "pay",
    "cancel",
    "submit",
    "add",
    "save",
    "modify",
    "checkout",
    "claim",
)

ARRAY_INDEX_RE = re.compile(r"\[(\d+)\]")


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


def _jsonpath_to_k6_gjson(jsonpath: str) -> str:
    """Convert JSONPath like `$.data.list[0].id` to k6 gjson path `data.list.0.id`."""
    p = jsonpath.strip()
    if p.startswith("$."):
        p = p[2:]
    elif p.startswith("$"):
        p = p[1:]
    p = ARRAY_INDEX_RE.sub(r".\1", p)
    return p.lstrip(".")


def _sanitize_headers(raw_headers: dict[str, str] | list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    items: list[tuple[str, str]] = []
    if isinstance(raw_headers, dict):
        items = [(str(k), str(v)) for k, v in raw_headers.items()]
    elif isinstance(raw_headers, list):
        for h in raw_headers:
            if isinstance(h, dict):
                items.append((str(h.get("name") or ""), str(h.get("value") or "")))

    for name, val in items:
        n = name.strip()
        if not n or n.startswith(":"):
            continue
        low = n.lower()
        if low in SKIP_HEADERS or low in SENSITIVE_HEADERS:
            continue
        out[n] = val
    return out


def _inject_body_var(body: Any, param: str, placeholder: str) -> Any:
    if isinstance(body, dict):
        updated = dict(body)
        if param in updated:
            updated[param] = placeholder
            return updated
        # Support dotted nested path like data.skuId
        parts = [p for p in param.split(".") if p and p != "body"]
        if parts and parts[0] in updated:
            if len(parts) == 1:
                updated[parts[0]] = placeholder
            else:
                updated[parts[0]] = _inject_body_var(
                    updated[parts[0]], ".".join(parts[1:]), placeholder
                )
            return updated
        return {k: _inject_body_var(v, param, placeholder) for k, v in updated.items()}
    if isinstance(body, list):
        return [_inject_body_var(item, param, placeholder) for item in body]
    return body


def _build_chain_step_spec(step: dict[str, Any]) -> dict[str, Any]:
    method = str(step.get("method") or "GET").upper()
    raw_url = str(step.get("raw_url") or "")
    norm_path = str(step.get("path") or "/")
    parsed = urlparse(raw_url) if raw_url else urlparse(f"https://api.example.com{norm_path}")
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or "api.example.com"
    url_path = parsed.path or norm_path
    query_pairs = dict(parse_qsl(parsed.query))
    for k, v in (step.get("query_params") or {}).items():
        query_pairs[str(k)] = str(v)

    headers = _sanitize_headers(step.get("request_headers") or {})
    body = step.get("request_body")
    uses = step.get("uses") or {}

    # Parameterize URL path, query, headers, and body based on `uses`
    for loc_param, placeholder in uses.items():
        if ":" in loc_param:
            loc, param = loc_param.split(":", 1)
        elif "." in loc_param:
            loc, param = loc_param.split(".", 1)
        else:
            loc, param = "body", loc_param

        if loc == "path":
            token = "{" + param + "}"
            if token in norm_path:
                url_path = norm_path.replace(token, placeholder)
            elif placeholder in norm_path:
                url_path = norm_path
        elif loc == "query":
            query_pairs[param] = placeholder
        elif loc == "header":
            if param.lower() not in SENSITIVE_HEADERS:
                headers[param] = placeholder
        elif loc == "body" and body is not None:
            body = _inject_body_var(body, param, placeholder)

    query_str = urlencode(query_pairs, safe="{}") if query_pairs else ""
    url_template = urlunparse((scheme, netloc, url_path, "", query_str, ""))

    extracts_raw = step.get("extracts") or {}
    extracts = {
        str(var_name): _jsonpath_to_k6_gjson(str(jp))
        for var_name, jp in extracts_raw.items()
        if var_name and jp
    }

    return {
        "scenario": str(step.get("scenario") or "default"),
        "method": method,
        "path_tag": norm_path,
        "url_template": url_template,
        "fallback_url": raw_url or url_template,
        "headers": headers,
        "body": body,
        "expected_status": int(step.get("expected_status") or 200),
        "extracts": extracts,
    }


def _extract_fallback_har_steps(
    har_dir: Path,
    target_domain: str | None,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for hp in _iter_har_files(har_dir):
        scenario = hp.stem
        try:
            har_data = json.loads(hp.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for entry in har_data.get("log", {}).get("entries", []):
            req = entry.get("request") or {}
            resp = entry.get("response") or {}
            method = str(req.get("method") or "GET").upper()
            if method == "OPTIONS":
                continue
            url = str(req.get("url") or "")
            parsed = urlparse(url)
            host = parsed.hostname or ""
            path = parsed.path or "/"
            if not host or not _domain_matches(host, target_domain):
                continue
            if path.lower().endswith(STATIC_EXTS):
                continue

            status = int(resp.get("status") or 0)
            if not (200 <= status < 300):
                continue

            # Prefer read-only endpoints for standalone fallback load
            if method not in ("GET", "HEAD"):
                if method != "POST" or any(h in path.lower() for h in MUTATION_HINTS):
                    continue

            key = (method, f"{host}{path}")
            if key in seen:
                continue
            seen.add(key)

            post_text = str((req.get("postData") or {}).get("text") or "")
            body = None
            if post_text:
                try:
                    body = json.loads(post_text)
                except Exception:
                    body = post_text

            steps.append(
                {
                    "scenario": scenario,
                    "method": method,
                    "path_tag": path,
                    "url_template": url,
                    "fallback_url": url,
                    "headers": _sanitize_headers(req.get("headers") or []),
                    "body": body,
                    "expected_status": status,
                    "extracts": {},
                }
            )
    return steps


def generate_k6_script(
    har_dir: Path,
    out_dir: Path,
    *,
    correlation_path: Path | None = None,
    stages: list[dict[str, Any]] | None = None,
    proxy_url: str | None = None,
    target_domain: str | None = None,
) -> dict[str, Any]:
    """Generate a runnable ES-module k6 load-test script at `out_dir/k6/scenario_ramp.js`."""
    k6_dir = ensure_dir(out_dir / "k6")
    script_path = k6_dir / "scenario_ramp.js"
    resolved_stages = stages if stages else DEFAULT_STAGES

    chains_spec: list[dict[str, Any]] = []
    if correlation_path and correlation_path.exists():
        try:
            corr = json.loads(correlation_path.read_text(encoding="utf-8"))
        except Exception:
            corr = {}
        for ch in corr.get("chains") or []:
            steps = [_build_chain_step_spec(s) for s in (ch.get("steps") or [])]
            cleanup = ch.get("cleanup_step")
            if cleanup:
                steps.append(_build_chain_step_spec(cleanup))
            if steps:
                chains_spec.append(
                    {
                        "name": str(ch.get("name") or ch.get("scenario") or "chain"),
                        "scenario": str(ch.get("scenario") or "default"),
                        "steps": steps,
                    }
                )

    fallback_steps = _extract_fallback_har_steps(har_dir, target_domain)
    if not chains_spec and fallback_steps:
        chains_spec.append(
            {
                "name": "har_read_smoke",
                "scenario": "default",
                "steps": fallback_steps[:25],
            }
        )

    proxy_hint = proxy_url or "http://127.0.0.1:8888"
    script_text = _render_k6_js(chains_spec, resolved_stages, proxy_hint)
    script_path.write_text(script_text, encoding="utf-8")

    return {
        "script_path": str(script_path),
        "chain_count": len(chains_spec),
        "step_count": sum(len(c["steps"]) for c in chains_spec),
        "stages": resolved_stages,
        "proxy_url": proxy_url,
    }


def _render_k6_js(
    chains_spec: list[dict[str, Any]],
    stages: list[dict[str, Any]],
    proxy_hint: str,
) -> str:
    stages_json = json.dumps(stages, ensure_ascii=False, indent=2)
    chains_json = json.dumps(chains_spec, ensure_ascii=False, indent=2)
    return f"""/**
 * ReconQA Auto-Generated k6 Load-Test Script
 *
 * Environment Variables:
 *   BASE_URL       - Override target origin (e.g. http://127.0.0.1:8000)
 *   AUTH_TOKEN     - Bearer token injected into Authorization header
 *   AUTH_COOKIE    - Cookie header value for authenticated scenarios
 *   NO_CONN_REUSE  - Set to "true" to disable HTTP keep-alive connection reuse
 *   THINK_TIME     - Pause seconds between iterations (default: 0.3)
 *
 * Proxy / Egress Pool Usage:
 *   HTTP_PROXY={proxy_hint} HTTPS_PROXY={proxy_hint} \\
 *     k6 run -e BASE_URL=https://api.example.com -e NO_CONN_REUSE=false scenario_ramp.js
 */
import http from 'k6/http';
import {{ check, group, sleep }} from 'k6';
import {{ Counter, Rate }} from 'k6/metrics';

export const http_429_rate = new Rate('http_429_rate');
export const http_5xx_rate = new Rate('http_5xx_rate');
export const http_429_count = new Counter('http_429_count');
export const http_5xx_count = new Counter('http_5xx_count');

export const options = {{
  noConnectionReuse: __ENV.NO_CONN_REUSE === 'true',
  stages: {stages_json},
  thresholds: {{
    http_req_duration: ['p(95)<2000'],
    http_429_rate: ['rate<0.05'],
    http_5xx_rate: ['rate<0.02'],
  }},
}};

const CHAINS = {chains_json};

function resolveUrl(rawUrl) {{
  const base = (__ENV.BASE_URL || '').replace(/\\/+$/, '');
  if (!base) {{
    return rawUrl;
  }}
  const match = rawUrl.match(/^https?:\\/\\/[^/]+(.*)$/i);
  if (match) {{
    return base + (match[1] || '/');
  }}
  return base + (rawUrl.startsWith('/') ? rawUrl : '/' + rawUrl);
}}

function renderString(value, ctx) {{
  if (typeof value !== 'string') {{
    return value;
  }}
  return value.replace(/\\{{\\{{([a-zA-Z0-9_]+)\\}}\\}}/g, (full, key) => {{
    return Object.prototype.hasOwnProperty.call(ctx, key) ? String(ctx[key]) : full;
  }});
}}

function renderValue(value, ctx) {{
  if (typeof value === 'string') {{
    return renderString(value, ctx);
  }}
  if (Array.isArray(value)) {{
    return value.map((item) => renderValue(item, ctx));
  }}
  if (value && typeof value === 'object') {{
    const out = {{}};
    for (const [k, v] of Object.entries(value)) {{
      out[k] = renderValue(v, ctx);
    }}
    return out;
  }}
  return value;
}}

function buildHeaders(templateHeaders, ctx, hasJsonBody) {{
  const headers = Object.assign(
    {{ Accept: 'application/json, text/plain, */*' }},
    renderValue(templateHeaders || {{}}, ctx),
  );
  if (hasJsonBody && !headers['Content-Type'] && !headers['content-type']) {{
    headers['Content-Type'] = 'application/json';
  }}
  if (__ENV.AUTH_TOKEN) {{
    headers['Authorization'] = __ENV.AUTH_TOKEN.startsWith('Bearer ')
      ? __ENV.AUTH_TOKEN
      : `Bearer ${{__ENV.AUTH_TOKEN}}`;
  }}
  if (__ENV.AUTH_COOKIE) {{
    headers['Cookie'] = __ENV.AUTH_COOKIE;
  }}
  return headers;
}}

function executeStep(step, ctx) {{
  let renderedUrl = renderString(step.url_template, ctx);
  if (renderedUrl.includes('{{{{')) {{
    renderedUrl = step.fallback_url;
  }}
  const url = resolveUrl(renderedUrl);

  const hasBody = step.body !== null && step.body !== undefined && step.method !== 'GET' && step.method !== 'HEAD';
  const renderedBody = hasBody ? renderValue(step.body, ctx) : null;
  const isObjBody = renderedBody !== null && typeof renderedBody === 'object';
  const payload = isObjBody ? JSON.stringify(renderedBody) : renderedBody;
  const headers = buildHeaders(step.headers, ctx, isObjBody);

  const res = http.request(step.method, url, payload, {{
    headers,
    tags: {{
      name: `${{step.method}} ${{step.path_tag}}`,
      scenario: step.scenario,
    }},
  }});

  const is429 = res.status === 429;
  const is5xx = res.status >= 500;
  http_429_rate.add(is429);
  http_5xx_rate.add(is5xx);
  if (is429) http_429_count.add(1);
  if (is5xx) http_5xx_count.add(1);

  check(res, {{
    'status is 2xx': (r) => r.status >= 200 && r.status < 300,
    'not rate limited (429)': (r) => r.status !== 429,
    'no server error (5xx)': (r) => r.status < 500,
  }});

  const extracts = step.extracts || {{}};
  for (const [varName, gjsonPath] of Object.entries(extracts)) {{
    try {{
      const extracted = res.json(gjsonPath);
      if (extracted !== undefined && extracted !== null && extracted !== '') {{
        ctx[varName] = String(extracted);
      }}
    }} catch (_) {{
      // Non-JSON response or missing field; keep existing context
    }}
  }}
}}

export default function () {{
  for (const chain of CHAINS) {{
    group(chain.name, () => {{
      const ctx = {{}};
      for (const step of chain.steps) {{
        executeStep(step, ctx);
      }}
    }});
  }}
  sleep(Number(__ENV.THINK_TIME || '0.3'));
}}
"""


def _metric_val(metrics: dict[str, Any], name: str, field: str, default: float = 0.0) -> float:
    m = metrics.get(name)
    if not isinstance(m, dict):
        return default
    # k6 summary-export puts stats directly on metric dict or under "values"
    container = m.get("values") if isinstance(m.get("values"), dict) else m
    val = container.get(field)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _parse_k6_summary(summary_data: dict[str, Any]) -> dict[str, Any]:
    metrics = summary_data.get("metrics") or {}

    p95_ms = round(_metric_val(metrics, "http_req_duration", "p(95)"), 2)
    p90_ms = round(_metric_val(metrics, "http_req_duration", "p(90)"), 2)
    avg_ms = round(_metric_val(metrics, "http_req_duration", "avg"), 2)
    med_ms = round(_metric_val(metrics, "http_req_duration", "med"), 2)
    max_ms = round(_metric_val(metrics, "http_req_duration", "max"), 2)

    req_count = int(_metric_val(metrics, "http_reqs", "count"))
    rps = round(_metric_val(metrics, "http_reqs", "rate"), 2)
    fail_rate = round(_metric_val(metrics, "http_req_failed", "value"), 4)
    if fail_rate == 0.0:
        fail_rate = round(_metric_val(metrics, "http_req_failed", "rate"), 4)

    rate_429 = round(_metric_val(metrics, "http_429_rate", "value"), 4)
    if rate_429 == 0.0:
        rate_429 = round(_metric_val(metrics, "http_429_rate", "rate"), 4)
    rate_5xx = round(_metric_val(metrics, "http_5xx_rate", "value"), 4)
    if rate_5xx == 0.0:
        rate_5xx = round(_metric_val(metrics, "http_5xx_rate", "rate"), 4)
    count_429 = int(_metric_val(metrics, "http_429_count", "count"))
    count_5xx = int(_metric_val(metrics, "http_5xx_count", "count"))

    breached_thresholds: list[str] = []
    for metric_name, mdata in metrics.items():
        if not isinstance(mdata, dict):
            continue
        thresholds = mdata.get("thresholds")
        if isinstance(thresholds, dict):
            for expr, status_obj in thresholds.items():
                # k6 --summary-export uses bool (True=breached, False=passed);
                # handleSummary dict form uses {"ok": True/False}.
                breached = (
                    status_obj.get("ok") is False
                    if isinstance(status_obj, dict)
                    else bool(status_obj)
                )
                if breached:
                    breached_thresholds.append(f"{metric_name}: {expr}")

    rate_limited = count_429 > 0 or rate_429 >= 0.05
    server_errors = count_5xx > 0 or rate_5xx >= 0.02
    latency_breach = p95_ms >= 2000.0
    inflection_detected = bool(
        breached_thresholds or rate_limited or server_errors or latency_breach
    )

    diagnosis_notes: list[str] = []
    if rate_limited:
        diagnosis_notes.append(
            f"Rate limiting detected (429 count={count_429}, rate={rate_429:.2%})."
        )
    if server_errors:
        diagnosis_notes.append(
            f"Server 5xx errors detected (5xx count={count_5xx}, rate={rate_5xx:.2%})."
        )
    if latency_breach:
        diagnosis_notes.append(f"p95 latency ({p95_ms}ms) exceeded 2000ms threshold.")
    if not diagnosis_notes:
        diagnosis_notes.append("All latency, 429, and 5xx thresholds passed within limits.")

    return {
        "metrics": {
            "http_reqs_count": req_count,
            "rps": rps,
            "http_req_failed_rate": fail_rate,
            "latency_ms": {
                "avg": avg_ms,
                "med": med_ms,
                "p90": p90_ms,
                "p95": p95_ms,
                "max": max_ms,
            },
            "http_429_rate": rate_429,
            "http_429_count": count_429,
            "http_5xx_rate": rate_5xx,
            "http_5xx_count": count_5xx,
        },
        "diagnosis": {
            "inflection_detected": inflection_detected,
            "rate_limited": rate_limited,
            "server_errors": server_errors,
            "latency_breach": latency_breach,
            "breached_thresholds": breached_thresholds,
            "notes": diagnosis_notes,
        },
    }


def run_k6_test(
    script_path: Path,
    out_dir: Path,
    *,
    base_url: str | None = None,
    proxy_url: str | None = None,
    no_conn_reuse: bool = False,
    extra_args: list[str] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """Execute local `k6` with `--summary-export` and diagnose latency/429/5xx inflection."""
    k6_dir = ensure_dir(out_dir / "k6")
    summary_path = k6_dir / "k6_summary.json"
    analysis_path = k6_dir / "k6_analysis.json"
    k6_bin = settings.resolve_bin("k6")

    if not k6_bin:
        result = {
            "status": "skipped",
            "reason": "k6 binary not found in PATH or RECONQA_BIN_DIR",
            "script_path": str(script_path),
            "analysis_path": str(analysis_path),
        }
        write_json(analysis_path, result)
        return result

    env = dict(os.environ)
    if base_url:
        env["BASE_URL"] = base_url
    effective_proxy = proxy_url or settings.k6_proxy_url
    if effective_proxy:
        env["HTTP_PROXY"] = effective_proxy
        env["HTTPS_PROXY"] = effective_proxy
    if no_conn_reuse:
        env["NO_CONN_REUSE"] = "true"

    cmd = [k6_bin, "run", "--summary-export", str(summary_path)]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(script_path))

    proc_res = run_cmd_sync(cmd, timeout=timeout, env=env)

    parsed_summary: dict[str, Any] = {}
    if summary_path.exists():
        try:
            raw_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            parsed_summary = _parse_k6_summary(raw_summary)
        except Exception:
            parsed_summary = {}

    if proc_res.timed_out:
        status = "timed_out"
    elif proc_res.returncode == 0:
        status = "completed"
    elif proc_res.returncode == 99:
        # k6 exits 99 when thresholds are crossed (valid test execution with breach)
        status = "threshold_breached"
    else:
        status = "error"

    analysis: dict[str, Any] = {
        "status": status,
        "returncode": proc_res.returncode,
        "timed_out": proc_res.timed_out,
        "script_path": str(script_path),
        "summary_path": str(summary_path),
        "analysis_path": str(analysis_path),
        "base_url": base_url,
        "proxy_url": effective_proxy,
        "no_conn_reuse": no_conn_reuse,
        **parsed_summary,
        "stderr": proc_res.stderr[:1000] if proc_res.stderr else "",
    }
    write_json(analysis_path, analysis)
    return analysis
