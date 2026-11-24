"""Generic minified JavaScript static API endpoint, method, and payload analyzer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from reconqa.utils.io import write_json

MAX_JS_FILE_BYTES = 5 * 1024 * 1024

DEFAULT_API_PREFIXES: tuple[str, ...] = (
    "api",
    "v1",
    "v2",
    "v3",
    "v4",
    "graphql",
    "rest",
    "rpc",
    "gateway",
    "gw",
    "bff",
    "service",
    "services",
    "backend",
    "internal",
    "public",
    "open",
    "oauth",
    "auth",
    "account",
    "accounts",
    "user",
    "users",
    "order",
    "orders",
    "cart",
    "checkout",
    "payment",
    "payments",
    "pay",
    "product",
    "products",
    "item",
    "items",
    "catalog",
    "search",
    "coupon",
    "coupons",
    "promo",
    "promotion",
    "config",
    "settings",
    "admin",
    "cms",
    "notification",
    "notifications",
    "upload",
    "download",
    "report",
    "reports",
    "billing",
    "subscription",
    "webhook",
)

DEFAULT_SKIP_EXTENSIONS: tuple[str, ...] = (
    ".js",
    ".mjs",
    ".cjs",
    ".css",
    ".scss",
    ".less",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".mp4",
    ".mp3",
    ".gif",
    ".mp4",
    ".webm",
    ".pdf",
    ".webp",
    ".avif",
    ".map",
    ".wasm",
    ".html",
    ".htm",
)

DEFAULT_SKIP_PREFIXES: tuple[str, ...] = (
    "/_next",
    "/__webpack",
    "/static/",
    "/assets/",
    "/images/",
    "/img/",
    "/fonts/",
    "/icons/",
    "/css/",
    "/node_modules/",
)

JS_RESERVED_KEYS: set[str] = {
    "method",
    "url",
    "headers",
    "header",
    "timeout",
    "responseType",
    "withCredentials",
    "baseURL",
    "params",
    "data",
    "body",
    "credentials",
    "mode",
    "cache",
    "redirect",
    "signal",
    "cancelToken",
    "showLoading",
    "hideLoading",
    "noToast",
    "silent",
    "return",
    "function",
    "var",
    "let",
    "const",
    "true",
    "false",
    "null",
    "undefined",
    "if",
    "else",
    "case",
    "default",
    "break",
    "catch",
    "try",
    "finally",
    "new",
    "typeof",
    "async",
    "await",
}

HTTP_CLIENT_CALL_RE = re.compile(
    r"""(?:\.(?:get|post|put|delete|patch)|fetch|axios|request)\s*\(\s*"""
    r"""(?:\{\s*url\s*:\s*)?(['"`])(?P<path>/[A-Za-z0-9_./:${}-]{3,140}(?:\?[^'"`\s]{0,120})?)\1""",
    re.IGNORECASE,
)

# Context-aware generic HTTP client patterns (Axios config objects, XHR, chained method calls)
AXIOS_URL_PROP_RE = re.compile(
    r"""\burl\s*:\s*(['"`])(?P<path>/[A-Za-z0-9_./:${}-]{3,140}(?:\?[^'"`\s]{0,120})?)\1""",
)

XHR_OPEN_RE = re.compile(
    r"""\.open\s*\(\s*['"](?P<method>GET|POST|PUT|DELETE|PATCH)['"]\s*,\s*"""
    r"""(['"`])(?P<path>(?:https?://[^'"`\s]{4,180}|/[A-Za-z0-9_./:${}-]{2,140})(?:\?[^'"`\s]{0,120})?)\2""",
    re.IGNORECASE,
)

METHOD_CALL_RE = re.compile(
    r"""\.(?P<method>get|post|put|delete|patch)\s*\(\s*"""
    r"""(['"`])(?P<path>/[A-Za-z0-9_./:${}-]{3,140}(?:\?[^'"`\s]{0,120})?)\2""",
    re.IGNORECASE,
)

SECRET_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]{8,}", re.IGNORECASE), r"\1***REDACTED***"),
    (
        re.compile(
            r"""((?:api[_-]?key|secret|token|password|passwd|access[_-]?key)"""
            r"""\s*[:=]\s*['"])([^'"]{6,})(['"])""",
            re.IGNORECASE,
        ),
        r"\1***REDACTED***\3",
    ),
]


def _redact_snippet(snippet: str) -> str:
    cleaned = re.sub(r"\s+", " ", snippet).strip()
    for pat, repl in SECRET_REDACT_PATTERNS:
        cleaned = pat.sub(repl, cleaned)
    return cleaned[:240]


def _extract_obj_keys(obj_text: str) -> list[str]:
    """Extract plausible top-level JS object literal keys from a minified snippet."""
    keys: list[str] = []
    # Strip nested brace blocks so we focus on top-level keys
    depth = 0
    top_chars: list[str] = []
    for ch in obj_text:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth = max(0, depth - 1)
        elif depth == 0:
            top_chars.append(ch)
    flat = "".join(top_chars)

    key_pat = (
        r"""(?:^\s*|[,{]\s*)(?:['"]([A-Za-z_][A-Za-z0-9_]{1,40})['"]|"""
        r"""([A-Za-z_][A-Za-z0-9_]{1,40}))\s*(?::|(?=[,}]|$))"""
    )
    for m in re.finditer(key_pat, flat):
        key = m.group(1) or m.group(2)
        if key and key not in JS_RESERVED_KEYS and not key.startswith("__") and len(key) > 1:
            if key not in keys:
                keys.append(key)
    return keys[:20]


def _find_balanced_brace(text: str, start_idx: int, max_len: int = 600) -> str:
    """Return substring inside {...} starting at start_idx (which must point to '{')."""
    if start_idx < 0 or start_idx >= len(text) or text[start_idx] != "{":
        return ""
    depth = 0
    end = min(len(text), start_idx + max_len)
    for i in range(start_idx, end):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start_idx + 1 : i]
    return ""


def _infer_method_from_path(path: str) -> str:
    low = path.lower()
    if any(tok in low for tok in ("/delete", "/remove", "/destroy")):
        return "DELETE"
    if any(tok in low for tok in ("/update", "/edit", "/modify")):
        return "PUT"
    if any(
        tok in low
        for tok in (
            "/create",
            "/add",
            "/submit",
            "/claim",
            "/login",
            "/logout",
            "/register",
            "/checkout",
            "/pay",
            "/cancel",
            "/upload",
            "/save",
            "/bind",
        )
    ):
        return "POST"
    return "GET"


def analyze_js_call_context(
    text: str,
    match_start: int,
    match_end: int,
    path: str,
) -> dict[str, Any]:
    """Inspect JS around a matched API URL to recover method, request keys, and response keys."""
    before = text[max(0, match_start - 180) : match_start]
    after = text[match_end : min(len(text), match_end + 360)]
    before_stmt = before.rsplit(";", 1)[-1]
    after_stmt = after.split(";", 1)[0]
    snippet = _redact_snippet(before[-80:] + text[match_start:match_end] + after[:140])

    # 1. Infer HTTP method (prioritize immediately adjacent client call, then same-statement opts)
    method: str | None = None
    m_chain = re.search(
        r"""\.(get|delete|put|patch|post)\s*\(\s*$""",
        before_stmt,
        re.IGNORECASE,
    )
    m_after = re.search(
        r"""method\s*:\s*['"](GET|POST|PUT|DELETE|PATCH)['"]""",
        after_stmt[:160],
        re.IGNORECASE,
    )
    m_before = re.search(
        r"""method\s*:\s*['"](GET|POST|PUT|DELETE|PATCH)['"][^{}]{0,120}$""",
        before_stmt,
        re.IGNORECASE,
    )
    if m_chain:
        method = m_chain.group(1).upper()
    elif m_after:
        method = m_after.group(1).upper()
    elif m_before:
        method = m_before.group(1).upper()
    elif re.search(r"""\bparams\s*:\s*""", after_stmt[:120]) and not re.search(
        r"""\b(?:data|body)\s*:\s*""", after_stmt[:120]
    ):
        method = "GET"
    elif re.search(r"""\b(?:data|body)\s*:\s*""", after_stmt[:120]):
        method = "POST"

    req_keys: list[str] = []

    # 2a. Inline data:{...} / params:{...} / body:{...} / body:JSON.stringify({...})
    for m_obj in re.finditer(
        r"""(?:data|params|body)\s*:\s*(?:JSON\.stringify\s*\(\s*)?\{""",
        after_stmt[:220],
    ):
        inner = _find_balanced_brace(after_stmt, m_obj.end() - 1)
        for key in _extract_obj_keys(inner):
            if key not in req_keys:
                req_keys.append(key)

    # 2b. Direct second-argument object literal: post("/api/foo", { a: 1, b: 2 })
    m_arg2 = re.match(r"""\s*,\s*\{""", after_stmt[:40])
    if m_arg2 and not req_keys:
        inner = _find_balanced_brace(after_stmt, m_arg2.end() - 1)
        if not re.search(r"""\bmethod\s*:\s*['"]""", inner[:80]):
            for key in _extract_obj_keys(inner):
                if key not in req_keys:
                    req_keys.append(key)

    # 2c. Wrapper function call sites: fn({ k1: ..., k2: ... })
    if not req_keys:
        m_wrap = re.search(
            r"""([A-Za-z_$][A-Za-z0-9_$]{1,30})\s*=\s*(?:async\s+)?"""
            r"""(?:\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\)\s*=>|"""
            r"""function\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\))\s*\{?[^}]{0,120}$""",
            before_stmt,
        )
        if not m_wrap:
            m_wrap = re.search(
                r"""function\s+([A-Za-z_$][A-Za-z0-9_$]{1,30})\s*\(\s*"""
                r"""([A-Za-z_$][A-Za-z0-9_$]*)\s*\)\s*\{[^}]{0,120}$""",
                before_stmt,
            )
        if m_wrap:
            fn_name = m_wrap.group(1)
            if len(fn_name) >= 2:
                call_pat = re.compile(rf"""(?:^|[^\w$]){re.escape(fn_name)}\s*\(\s*\{{""")
                for cm in list(call_pat.finditer(text))[:3]:
                    inner = _find_balanced_brace(text, cm.end() - 1)
                    for key in _extract_obj_keys(inner):
                        if key not in req_keys:
                            req_keys.append(key)

    # 3. Response property hints from .then(r => ... r.xxx ...) or destructuring .then(({a}) => ...)
    resp_keys: list[str] = []
    m_then_destruct = re.search(
        r"""\.then\s*\(\s*\(\s*\{(?P<keys>[^}]{1,120})\}\s*\)\s*=>""",
        after_stmt[:240],
    )
    if m_then_destruct:
        for key in _extract_obj_keys(m_then_destruct.group("keys")):
            if key not in resp_keys:
                resp_keys.append(key)

    m_then = re.search(
        r"""\.then\s*\(\s*(?:\(?\s*function\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\)|"""
        r"""\(?\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\)?\s*=>)""",
        after_stmt[:240],
    )
    if m_then:
        var = m_then.group(1) or m_then.group(2)
        if var:
            tail = after_stmt[m_then.end() : m_then.end() + 240]
            for rm in re.finditer(
                rf"""\b{re.escape(var)}\.([A-Za-z_][A-Za-z0-9_]{{1,30}})""", tail
            ):
                rk = rm.group(1)
                if rk not in resp_keys and rk not in ("then", "catch", "finally"):
                    resp_keys.append(rk)

    if method is None:
        method = "POST" if (req_keys and m_arg2) else _infer_method_from_path(path)

    return {
        "method": method,
        "method_explicit": bool(m_after or m_before or m_chain),
        "req_keys": req_keys[:20],
        "resp_keys": resp_keys[:12],
        "snippet": snippet,
    }


def _normalize_js_path(
    raw: str,
    *,
    allow_single_segment: bool = False,
) -> tuple[str | None, list[str], str | None]:
    """Normalize a raw JS API path/URL into (openapi_path, inline_query_keys, full_url)."""
    s = raw.strip()
    if any(ch.isspace() for ch in s):
        return None, [], None
    full_url: str | None = None
    if s.startswith(("http://", "https://")):
        parsed = urlparse(s)
        path_part = parsed.path or ""
        query_part = parsed.query or ""
        if parsed.netloc:
            full_url = f"{parsed.scheme}://{parsed.netloc}{path_part}"
    else:
        if "?" in s:
            path_part, query_part = s.split("?", 1)
        else:
            path_part, query_part = s, ""

    query_keys = [k for k, _ in parse_qsl(query_part, keep_blank_values=True) if k]

    # Convert template expressions ${obj.itemId} -> {itemId}
    def _tpl_repl(m: re.Match[str]) -> str:
        expr = m.group(1).strip()
        ident = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr)
        name = ident[-1] if ident else "id"
        return "{" + name + "}"

    path_part = re.sub(r"\$\{([^}]+)\}", _tpl_repl, path_part)

    # Reject malformed colons that are not path params (e.g. "http:" or "data:")
    if ":" in path_part and not re.search(r"/:[A-Za-z_][A-Za-z0-9_]*", path_part):
        return None, [], None
    path_part = re.sub(r"/:([A-Za-z_][A-Za-z0-9_]*)", r"/{\1}", path_part)
    path_part = re.sub(r"/{2,}", "/", path_part)
    if len(path_part) > 1:
        path_part = path_part.rstrip("/")

    if not path_part.startswith("/") or len(path_part) <= 3:
        return None, [], None
    if not allow_single_segment and path_part.count("/") < 2:
        return None, [], None
    return path_part, query_keys, full_url


def _build_api_pattern(
    prefixes: list[str],
    target_domain: str | None,
) -> re.Pattern[str]:
    clean_prefixes = [re.escape(p.strip("/")) for p in prefixes if p.strip("/")]
    prefix_alt = "|".join(dict.fromkeys(clean_prefixes))
    rel_part = rf"(?:/(?:{prefix_alt})/[A-Za-z0-9_./:${{}}-]{{2,140}}(?:\?[^'\"`\s]{{0,120}})?)"
    if target_domain:
        dom = target_domain.strip().lower()
        if "://" in dom:
            dom = urlparse(dom).hostname or dom
        dom = dom.lstrip("*.")
        full_part = (
            rf"(?:https?://[A-Za-z0-9.-]*{re.escape(dom)}"
            rf"/[A-Za-z0-9_./:${{}}?=&-]{{2,160}})"
        )
        pattern = rf"""(['"`])(?P<raw>{rel_part}|{full_part})\1"""
    else:
        pattern = rf"""(['"`])(?P<raw>{rel_part})\1"""
    return re.compile(pattern)


def extract_js_endpoints(
    js_dir: Path,
    out_file: Path,
    *,
    target_domain: str | None = None,
    extra_prefixes: list[str] | None = None,
    skip_prefixes: list[str] | None = None,
) -> dict[str, Any]:
    """Statically extract API endpoints, HTTP methods, and request/response hints from JS files."""
    prefixes = list(DEFAULT_API_PREFIXES)
    if extra_prefixes:
        for pfx in extra_prefixes:
            cleaned = pfx.strip().strip("/")
            if cleaned and cleaned not in prefixes:
                prefixes.append(cleaned)

    effective_skip_prefixes = tuple(
        p.lower() for p in (list(DEFAULT_SKIP_PREFIXES) + list(skip_prefixes or [])) if p
    )
    api_pat = _build_api_pattern(prefixes, target_domain)

    js_files: list[Path] = []
    if js_dir.exists():
        if js_dir.is_file():
            js_files = [js_dir]
        else:
            js_files = sorted(
                p
                for p in js_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in {".js", ".mjs", ".cjs"}
            )

    endpoints_map: dict[str, dict[str, Any]] = {}
    files_scanned = 0

    for js_file in js_files:
        try:
            if js_file.stat().st_size > MAX_JS_FILE_BYTES:
                continue
            code = js_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files_scanned += 1

        # (raw_val, match_start, match_end, explicit_method_hint, allow_single_segment)
        matches: list[tuple[str, int, int, str | None, bool]] = []
        for m in api_pat.finditer(code):
            matches.append((m.group("raw"), m.start(), m.end(), None, False))
        for m in HTTP_CLIENT_CALL_RE.finditer(code):
            matches.append((m.group("path"), m.start("path") - 1, m.end("path") + 1, None, False))
        for m in AXIOS_URL_PROP_RE.finditer(code):
            matches.append((m.group("path"), m.start("path") - 1, m.end("path") + 1, None, False))
        for m in METHOD_CALL_RE.finditer(code):
            matches.append(
                (
                    m.group("path"),
                    m.start("path") - 1,
                    m.end("path") + 1,
                    m.group("method").upper(),
                    True,
                )
            )
        for m in XHR_OPEN_RE.finditer(code):
            matches.append(
                (
                    m.group("path"),
                    m.start("path") - 1,
                    m.end("path") + 1,
                    m.group("method").upper(),
                    True,
                )
            )

        for raw_val, m_start, m_end, explicit_method, allow_single in matches:
            oa_path, inline_qkeys, full_url = _normalize_js_path(
                raw_val, allow_single_segment=allow_single
            )
            if not oa_path:
                continue
            low = oa_path.lower()
            if any(low.endswith(ext) for ext in DEFAULT_SKIP_EXTENSIONS):
                continue
            if any(low.startswith(sp) for sp in effective_skip_prefixes):
                continue

            ctx = analyze_js_call_context(code, m_start, m_end, oa_path)
            eff_method = explicit_method or ctx["method"]
            eff_explicit = bool(explicit_method or ctx["method_explicit"])

            all_req_keys = list(ctx["req_keys"])
            for qk in inline_qkeys:
                if qk not in all_req_keys:
                    all_req_keys.append(qk)

            rec = endpoints_map.setdefault(
                oa_path,
                {
                    "path": oa_path,
                    "url": full_url,
                    "method": eff_method,
                    "method_explicit": eff_explicit,
                    "req_keys": [],
                    "resp_keys": [],
                    "sources": set(),
                    "snippet": ctx["snippet"],
                },
            )
            rec["sources"].add(js_file.name)
            if full_url and not rec.get("url"):
                rec["url"] = full_url
            if eff_explicit and not rec["method_explicit"]:
                rec["method"] = eff_method
                rec["method_explicit"] = True
            for k in all_req_keys:
                if k not in rec["req_keys"]:
                    rec["req_keys"].append(k)
            for k in ctx["resp_keys"]:
                if k not in rec["resp_keys"]:
                    rec["resp_keys"].append(k)
            if (all_req_keys or ctx["resp_keys"]) and len(ctx["snippet"]) > len(rec["snippet"]):
                rec["snippet"] = ctx["snippet"]

    endpoints_out: list[dict[str, Any]] = []
    inferred_count = 0
    path_only_count = 0

    for oa_path in sorted(endpoints_map):
        rec = endpoints_map[oa_path]
        has_inferred = bool(rec["req_keys"] or rec["resp_keys"])
        source_tier = "js-static-inferred" if has_inferred else "js-static"
        if has_inferred:
            inferred_count += 1
        else:
            path_only_count += 1

        path_params = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", oa_path)
        ep_item: dict[str, Any] = {
            "path": oa_path,
            "method": rec["method"],
            "discovery_source": source_tier,
            "x-discovery-source": source_tier,
            "req_keys": rec["req_keys"][:20],
            "resp_keys": rec["resp_keys"][:12],
            "path_params": path_params,
            "sources": sorted(rec["sources"]),
            "snippet": rec["snippet"],
            "x-js-snippet": rec["snippet"],
        }
        if rec.get("url"):
            ep_item["url"] = rec["url"]
        endpoints_out.append(ep_item)

    payload: dict[str, Any] = {
        "js_dir": str(js_dir),
        "files_scanned": files_scanned,
        "total_endpoints": len(endpoints_out),
        "inferred_count": inferred_count,
        "path_only_count": path_only_count,
        "endpoints": endpoints_out,
    }
    write_json(out_file, payload)
    return payload
