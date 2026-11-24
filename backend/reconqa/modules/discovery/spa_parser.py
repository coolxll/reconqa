"""SPA route and micro-frontend (MFE) sub-app baseline extractor."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.utils.io import ensure_dir, read_jsonl, write_json

MAX_FILE_BYTES = 5 * 1024 * 1024

STATIC_EXTENSIONS = (
    ".js",
    ".mjs",
    ".cjs",
    ".css",
    ".scss",
    ".less",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".avif",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".mp4",
    ".mp3",
    ".webm",
    ".pdf",
    ".map",
    ".wasm",
    ".json",
    ".xml",
)

API_LIKE_PREFIXES = (
    "/api/",
    "/v1/",
    "/v2/",
    "/v3/",
    "/graphql",
    "/rpc/",
    "/rest/",
    "/bff/",
    "/gw/",
    "/_next/",
    "/static/",
    "/assets/",
)

# Micro-frontend detection patterns
QIANKUN_REGISTER_RE = re.compile(
    r"""registerMicroApps\s*\(\s*\[(?P<body>[\s\S]{1,8000}?)\]""",
    re.MULTILINE,
)
QIANKUN_LOAD_RE = re.compile(
    r"""loadMicroApp\s*\(\s*\{(?P<body>[^}]{1,1500})\}""",
    re.MULTILINE,
)
SINGLE_SPA_OBJ_RE = re.compile(
    r"""registerApplication\s*\(\s*\{(?P<body>[^}]{1,1500})\}""",
    re.MULTILINE,
)
SINGLE_SPA_ARGS_RE = re.compile(
    r"""registerApplication\s*\(\s*['"](?P<name>[A-Za-z0-9_./@-]{1,80})['"]""",
)
WUJIE_CALL_RE = re.compile(
    r"""(?:setupApp|preloadApp|startApp)\s*\(\s*\{(?P<body>[^}]{1,1500})\}""",
    re.MULTILINE,
)
WUJIE_TAG_RE = re.compile(
    r"""<Wujie(?:Vue|React)[^>]{0,600}?"""
    r"""(?:name=['"](?P<name>[^'"]+)['"]|url=['"](?P<url>[^'"]+)['"])[^>]{0,600}?>""",
    re.IGNORECASE,
)
MF_REMOTE_ENTRY_RE = re.compile(
    r"""['"](?P<entry>(?:https?://[^'"\s]+|/[A-Za-z0-9_./-]+)/"""
    r"""(?:remoteEntry|mf-manifest|federation)[^'"\s]*\.(?:js|json))['"]""",
    re.IGNORECASE,
)
MF_REMOTES_MAP_RE = re.compile(
    r"""['"]?(?P<name>[A-Za-z0-9_-]{2,50})['"]?\s*:\s*"""
    r"""['"](?P<target>[A-Za-z0-9_$-]+@https?://[^'"]+)['"]""",
)
SUBAPP_PREFIX_RE = re.compile(
    r"""(?P<prefix>/(?:subapp|micro|mfe|microapp|micro-app|sub-app|__federation__)"""
    r"""[a-zA-Z0-9_-]*/?)""",
    re.IGNORECASE,
)

# SPA route detection patterns
NEXT_DATA_SCRIPT_RE = re.compile(
    r"""<script[^>]*id=['"]__NEXT_DATA__['"][^>]*>(?P<json>[\s\S]*?)</script>""",
    re.IGNORECASE,
)
NEXT_SORTED_PAGES_RE = re.compile(
    r"""sortedPages\s*:\s*\[(?P<items>[^\]]{1,8000})\]""",
)
NEXT_MANIFEST_ROUTE_RE = re.compile(
    r"""['"](/[A-Za-z0-9_.\[\]-]+(?:/[A-Za-z0-9_.\[\]-]+){0,5})['"]""",
)
VUE_ROUTER_BLOCK_RE = re.compile(
    r"""(?:createRouter|new\s+VueRouter)\s*\(\s*\{[\s\S]{0,400}?"""
    r"""routes\s*:\s*\[(?P<body>[\s\S]{1,12000}?)\]""",
    re.MULTILINE,
)
ROUTES_ARRAY_RE = re.compile(
    r"""\broutes\s*[:=]\s*\[(?P<body>[\s\S]{1,12000}?)\]""",
    re.MULTILINE,
)
ROUTE_OBJECT_PATH_RE = re.compile(
    r"""\{[^{}]{0,400}?\bpath\s*:\s*['"](?P<path>/[^'"\s]{0,160})['"]"""
    r"""[^{}]{0,400}?(?:component|components|name|redirect|children|element|lazy|meta)\s*:""",
    re.MULTILINE,
)
REACT_JSX_ROUTE_RE = re.compile(
    r"""<Route\b[^>]{0,300}?\bpath\s*=\s*['"](?P<path>/[^'"\s]{0,160})['"]""",
)
REACT_CREATE_ELEMENT_ROUTE_RE = re.compile(
    r"""(?:createElement|jsx|jsxs)\s*\(\s*[A-Za-z0-9_$.]*Route\b\s*,\s*"""
    r"""\{[^{}]{0,250}?\bpath\s*:\s*['"](?P<path>/[^'"\s]{0,160})['"]""",
)
REACT_ROUTER_FACTORY_RE = re.compile(
    r"""(?:createBrowserRouter|createHashRouter|createMemoryRouter|useRoutes)"""
    r"""\s*\(\s*\[(?P<body>[\s\S]{1,12000}?)\]""",
    re.MULTILINE,
)
GENERIC_PATH_PROP_RE = re.compile(
    r"""\bpath\s*:\s*['"](?P<path>/[^'"\s]{0,160})['"]""",
)
HASH_ROUTE_RE = re.compile(
    r"""#!?/(?P<route>[A-Za-z0-9_./:-]{1,120})""",
)


def _normalize_route(raw: str) -> str | None:
    route = raw.strip().split("?", 1)[0].split("#", 1)[0]
    if not route:
        return None
    if not route.startswith("/"):
        route = "/" + route
    route = re.sub(r"/{2,}", "/", route)
    if len(route) > 1:
        route = route.rstrip("/")
    lower = route.lower()
    if any(lower.endswith(ext) for ext in STATIC_EXTENSIONS):
        return None
    if any(lower.startswith(pfx) for pfx in ("/_next", "/__webpack", "/node_modules")):
        return None
    if len(route) > 160 or any(ch in route for ch in (" ", "\t", "\n", "\r", "\\", "<", ">")):
        return None
    return route


def _extract_field(block: str, *keys: str) -> str | None:
    for key in keys:
        m = re.search(rf"""\b{re.escape(key)}\s*:\s*['"]([^'"]+)['"]""", block)
        if m:
            return m.group(1).strip()
    return None


def _scan_micro_frontends(
    text: str,
    source_name: str,
    mfe_list: list[dict[str, Any]],
    seen_mfe: set[tuple[str, str, str]],
    sub_app_prefixes: set[str],
) -> None:
    def _add_mfe(framework: str, name: str, entry: str, active_rule: str = "") -> None:
        key = (framework, name, entry)
        if key in seen_mfe:
            return
        seen_mfe.add(key)
        mfe_list.append(
            {
                "framework": framework,
                "name": name,
                "entry": entry,
                "active_rule": active_rule,
                "source": source_name,
            }
        )
        if active_rule.startswith("/"):
            sub_app_prefixes.add(active_rule.rstrip("/") or "/")

    # 1. qiankun registerMicroApps([...])
    for match in QIANKUN_REGISTER_RE.finditer(text):
        body = match.group("body")
        for obj in re.finditer(r"\{([^{}]{1,1000})\}", body):
            chunk = obj.group(1)
            name = _extract_field(chunk, "name") or "unknown"
            entry = _extract_field(chunk, "entry") or ""
            active = _extract_field(chunk, "activeRule") or ""
            if entry or active:
                _add_mfe("qiankun", name, entry, active)

    for match in QIANKUN_LOAD_RE.finditer(text):
        chunk = match.group("body")
        name = _extract_field(chunk, "name") or "unknown"
        entry = _extract_field(chunk, "entry") or ""
        if entry:
            _add_mfe("qiankun", name, entry, "")

    # 2. single-spa registerApplication
    for match in SINGLE_SPA_OBJ_RE.finditer(text):
        chunk = match.group("body")
        name = _extract_field(chunk, "name") or "unknown"
        active = _extract_field(chunk, "activeWhen") or ""
        entry = _extract_field(chunk, "app", "entry", "url") or ""
        _add_mfe("single-spa", name, entry, active)

    for match in SINGLE_SPA_ARGS_RE.finditer(text):
        _add_mfe("single-spa", match.group("name"), "", "")

    # 3. wujie
    for match in WUJIE_CALL_RE.finditer(text):
        chunk = match.group("body")
        name = _extract_field(chunk, "name") or "unknown"
        entry = _extract_field(chunk, "url") or ""
        if entry or name != "unknown":
            _add_mfe("wujie", name, entry, "")

    for match in WUJIE_TAG_RE.finditer(text):
        tag_text = match.group(0)
        name_m = re.search(r"""name=['"]([^'"]+)['"]""", tag_text)
        url_m = re.search(r"""url=['"]([^'"]+)['"]""", tag_text)
        if name_m or url_m:
            _add_mfe(
                "wujie",
                name_m.group(1) if name_m else "wujie-app",
                url_m.group(1) if url_m else "",
                "",
            )

    # 4. Webpack Module Federation
    for match in MF_REMOTE_ENTRY_RE.finditer(text):
        entry = match.group("entry")
        name = Path(urlparse(entry).path).parent.name or "remote"
        _add_mfe("module-federation", name, entry, "")

    for match in MF_REMOTES_MAP_RE.finditer(text):
        _add_mfe("module-federation", match.group("name"), match.group("target"), "")

    # 5. Sub-app prefixes
    for match in SUBAPP_PREFIX_RE.finditer(text):
        pfx = match.group("prefix").rstrip("/")
        if pfx:
            sub_app_prefixes.add(pfx)


def _scan_js_routes(
    text: str,
    source_name: str,
    routes_by_fw: dict[str, set[str]],
) -> None:
    lower_name = source_name.lower()

    # Next.js buildManifest / _ssgManifest / sortedPages
    for match in NEXT_SORTED_PAGES_RE.finditer(text):
        for route_raw in NEXT_MANIFEST_ROUTE_RE.findall(match.group("items")):
            norm = _normalize_route(route_raw)
            if norm:
                routes_by_fw["nextjs"].add(norm)

    if "buildmanifest" in lower_name or "ssgmanifest" in lower_name:
        for route_raw in NEXT_MANIFEST_ROUTE_RE.findall(text):
            norm = _normalize_route(route_raw)
            if norm:
                routes_by_fw["nextjs"].add(norm)

    # Vue Router
    vue_matched = False
    for match in VUE_ROUTER_BLOCK_RE.finditer(text):
        vue_matched = True
        for pm in GENERIC_PATH_PROP_RE.finditer(match.group("body")):
            norm = _normalize_route(pm.group("path"))
            if norm:
                routes_by_fw["vue_router"].add(norm)

    if not vue_matched:
        for match in ROUTES_ARRAY_RE.finditer(text):
            body = match.group("body")
            if re.search(r"""\b(?:component|components|redirect|meta|name)\s*:""", body):
                for pm in GENERIC_PATH_PROP_RE.finditer(body):
                    norm = _normalize_route(pm.group("path"))
                    if norm:
                        routes_by_fw["vue_router"].add(norm)

    for match in ROUTE_OBJECT_PATH_RE.finditer(text):
        norm = _normalize_route(match.group("path"))
        if norm:
            # Determine whether React element/lazy or Vue component
            ctx_chunk = match.group(0)
            if "element" in ctx_chunk and "component" not in ctx_chunk:
                routes_by_fw["react_router"].add(norm)
            else:
                routes_by_fw["vue_router"].add(norm)

    # React Router
    for match in REACT_JSX_ROUTE_RE.finditer(text):
        norm = _normalize_route(match.group("path"))
        if norm:
            routes_by_fw["react_router"].add(norm)

    for match in REACT_CREATE_ELEMENT_ROUTE_RE.finditer(text):
        norm = _normalize_route(match.group("path"))
        if norm:
            routes_by_fw["react_router"].add(norm)

    for match in REACT_ROUTER_FACTORY_RE.finditer(text):
        for pm in GENERIC_PATH_PROP_RE.finditer(match.group("body")):
            norm = _normalize_route(pm.group("path"))
            if norm:
                routes_by_fw["react_router"].add(norm)

    # Hash routes in JS
    for match in HASH_ROUTE_RE.finditer(text):
        norm = _normalize_route("/" + match.group("route"))
        if norm:
            routes_by_fw["hash_routes"].add(norm)


def _scan_html_routes(
    text: str,
    source_name: str,
    routes_by_fw: dict[str, set[str]],
    mfe_list: list[dict[str, Any]],
    seen_mfe: set[tuple[str, str, str]],
    sub_app_prefixes: set[str],
) -> None:
    for match in NEXT_DATA_SCRIPT_RE.finditer(text):
        raw_json = match.group("json").strip()
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            page = payload.get("page")
            if isinstance(page, str):
                norm = _normalize_route(page)
                if norm:
                    routes_by_fw["nextjs"].add(norm)

    for match in HASH_ROUTE_RE.finditer(text):
        norm = _normalize_route("/" + match.group("route"))
        if norm:
            routes_by_fw["hash_routes"].add(norm)

    _scan_micro_frontends(text, source_name, mfe_list, seen_mfe, sub_app_prefixes)


def _collect_urls_from_out_dir(out_dir: Path) -> list[str]:
    urls: list[str] = []
    for fname in ("katana_urls.jsonl", "historical_urls.jsonl", "live_hosts.jsonl"):
        fpath = out_dir / fname
        if not fpath.is_file():
            continue
        for rec in read_jsonl(fpath):
            for key in ("url", "endpoint", "input"):
                val = rec.get(key)
                if isinstance(val, str) and val.startswith(("http://", "https://")):
                    urls.append(val)
            req = rec.get("request")
            if isinstance(req, dict):
                ep = req.get("endpoint")
                if isinstance(ep, str) and ep.startswith(("http://", "https://")):
                    urls.append(ep)
    return urls


def extract_spa_routes(
    out_dir: Path,
    js_dir: Path | None = None,
    html_dir: Path | None = None,
    urls: list[str] | None = None,
) -> dict[str, Any]:
    """Detect micro-frontend sub-apps and SPA routes, writing out_dir / 'spa_routes.json'."""
    ensure_dir(out_dir)

    routes_by_fw: dict[str, set[str]] = {
        "nextjs": set(),
        "vue_router": set(),
        "react_router": set(),
        "hash_routes": set(),
        "history_routes": set(),
    }
    micro_frontends: list[dict[str, Any]] = []
    seen_mfe: set[tuple[str, str, str]] = set()
    sub_app_prefixes: set[str] = set()

    js_files_scanned = 0
    html_files_scanned = 0

    # 1. Scan JS directory
    if js_dir is not None and js_dir.exists():
        js_paths = (
            [js_dir]
            if js_dir.is_file()
            else sorted(
                p
                for p in js_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in {".js", ".mjs", ".cjs"}
            )
        )
        for jp in js_paths:
            try:
                if jp.stat().st_size > MAX_FILE_BYTES:
                    continue
                content = jp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            js_files_scanned += 1
            _scan_js_routes(content, jp.name, routes_by_fw)
            _scan_micro_frontends(content, jp.name, micro_frontends, seen_mfe, sub_app_prefixes)

    # 2. Scan HTML directory
    if html_dir is not None and html_dir.exists():
        html_paths = (
            [html_dir]
            if html_dir.is_file()
            else sorted(
                p
                for p in html_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in {".html", ".htm"}
            )
        )
        for hp in html_paths:
            try:
                if hp.stat().st_size > MAX_FILE_BYTES:
                    continue
                content = hp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            html_files_scanned += 1
            _scan_html_routes(
                content, hp.name, routes_by_fw, micro_frontends, seen_mfe, sub_app_prefixes
            )

    # 3. Scan URL list (explicit or discovered JSONL files in out_dir)
    url_list = list(urls) if urls is not None else _collect_urls_from_out_dir(out_dir)
    for raw_url in url_list:
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue
        parsed = urlparse(raw_url.strip())
        path = parsed.path or ""

        # Check MFE remoteEntry or sub-app prefix in URL
        if re.search(r"/(?:remoteEntry|mf-manifest)[^/]*\.(?:js|json)$", path, re.IGNORECASE):
            name = Path(path).parent.name or "remote"
            key = ("module-federation", name, raw_url.strip())
            if key not in seen_mfe:
                seen_mfe.add(key)
                micro_frontends.append(
                    {
                        "framework": "module-federation",
                        "name": name,
                        "entry": raw_url.strip(),
                        "active_rule": "",
                        "source": "url_list",
                    }
                )
        for pm in SUBAPP_PREFIX_RE.finditer(path):
            pfx = pm.group("prefix").rstrip("/")
            if pfx:
                sub_app_prefixes.add(pfx)

        # Hash route in URL fragment
        if parsed.fragment and parsed.fragment.startswith(("/", "!/")):
            frag = parsed.fragment.lstrip("!")
            norm = _normalize_route(frag)
            if norm:
                routes_by_fw["hash_routes"].add(norm)

        # History route in URL path
        if path and not any(path.lower().startswith(p) for p in API_LIKE_PREFIXES):
            norm = _normalize_route(path)
            if norm:
                routes_by_fw["history_routes"].add(norm)

    all_routes = sorted({r for group in routes_by_fw.values() for r in group})
    out_file = out_dir / "spa_routes.json"
    result: dict[str, Any] = {
        "total_routes": len(all_routes),
        "routes": all_routes,
        "routes_by_framework": {k: sorted(v) for k, v in routes_by_fw.items()},
        "micro_frontends": micro_frontends,
        "sub_app_prefixes": sorted(sub_app_prefixes),
        "sources_scanned": {
            "js_files": js_files_scanned,
            "html_files": html_files_scanned,
            "urls": len(url_list),
        },
        "output_file": str(out_file),
    }
    write_json(out_file, result)
    return result
