"""Seed-driven API & asset explorer (Seed cURL / URL -> OpenAPI probe + SPA/Webpack JS + Sibling Hosts + PD)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from reconqa.config import settings
from reconqa.modules.discovery.runner import run_discovery
from reconqa.modules.discovery.spa_parser import extract_spa_routes
from reconqa.modules.recorder.js_extractor import extract_js_endpoints
from reconqa.modules.recorder.text_importer import import_traffic_text
from reconqa.utils.io import ensure_dir, read_json, read_jsonl, slugify, write_json

_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

_OPENAPI_CANDIDATE_PATHS = (
    "/openapi.json",
    "/swagger.json",
    "/v3/api-docs",
    "/v2/api-docs",
    "/api/openapi.json",
    "/api/v3/api-docs",
    "/swagger-resources",
    "/v3/api-docs/swagger-config",
)

_MAX_JS_BUNDLES = 35

_SOURCE_PRIORITY = {
    "seed_curl": 6,
    "openapi_probe": 5,
    "js_static_inferred": 4,
    "js_static": 3,
    "katana": 2,
    "urlfinder": 1,
}

_AUTH_HEADER_EXACT = {
    "authorization",
    "cookie",
    "x-api-key",
    "x-auth-token",
    "x-token",
    "x-csrf-token",
    "x-xsrf-token",
}

_SKIP_PROBE_HEADERS = {
    "host",
    "content-length",
    "content-type",
    "accept-encoding",
    "connection",
    "transfer-encoding",
}

_TWO_PART_TLDS = frozenset(
    {
        "com.cn",
        "org.cn",
        "net.cn",
        "gov.cn",
        "edu.cn",
        "co.uk",
        "org.uk",
        "ac.uk",
        "com.au",
        "co.jp",
        "ne.jp",
        "com.hk",
        "com.tw",
        "com.sg",
        "co.kr",
        "com.br",
    }
)

_VENDOR_JS_TOKENS = (
    "elementui",
    "element-ui",
    "element-plus",
    "chunk-libs",
    "chunk-vendors",
    "chunk-elementui",
    "vendor",
    "echarts",
    "tinymce",
    "quill",
    "xlsx",
    "pdfjs",
    "pdf.worker",
    "monaco",
    "lodash",
    "moment",
    "jquery",
    "codemirror",
    "highlight",
    "ant-design",
    "antd",
    "polyfill",
)

_WEBPACK_CHUNK_PAIR_RE = re.compile(
    r"""["']?([A-Za-z0-9_-]{1,48})["']?\s*:\s*["']([0-9a-f]{6,16})["']"""
)
_WEBPACK_CHUNK_PREFIX_RE = re.compile(
    r"""["']((?:/?)(?:static/js|assets|js|chunks)/)["']\s*\+"""
)
_VITE_CHUNK_LITERAL_RE = re.compile(
    r"""["'](/?(?:static/js|assets|js|chunks)/[A-Za-z0-9_.-]{2,100}\.m?js(?:\?[^"'\s]{0,60})?)["']"""
)
_FULL_URL_RE = re.compile(r"""https?://([A-Za-z0-9.-]+(?::\d{2,5})?)(/[^\s"'`<>)]*)?""")
_STATIC_ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".css", ".mp4")


def _is_loopback(host: str) -> bool:
    return host.lower() in {"127.0.0.1", "localhost", "::1"}


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _registrable_root_domain(host: str) -> str:
    """Derive registrable root domain (e.g. admin.example.com -> example.com, a.b.com.cn -> b.com.cn)."""
    h = (host or "").strip().lower().rstrip(".")
    if not h or _is_loopback(h) or _is_ip_literal(h):
        return h
    parts = [p for p in h.split(".") if p]
    if len(parts) <= 2:
        return h
    last_two = ".".join(parts[-2:])
    if last_two in _TWO_PART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last_two


def _is_vendor_bundle(url: str) -> bool:
    name = Path(urlparse(url).path).name.lower()
    return any(tok in name for tok in _VENDOR_JS_TOKENS)


class _HtmlScriptCollector(HTMLParser):
    """Stdlib HTML parser handling quoted AND unquoted <script>/<link> attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.script_urls: list[str] = []
        self.inline_scripts: list[str] = []
        self._in_inline_script = False
        self._inline_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs}
        if t == "script":
            src = attr_map.get("src", "")
            if src:
                self.script_urls.append(src)
            else:
                self._in_inline_script = True
                self._inline_buf = []
        elif t == "link":
            href = attr_map.get("href", "")
            if not href:
                return
            rel_tokens = set(attr_map.get("rel", "").lower().split())
            as_val = attr_map.get("as", "").lower()
            if "modulepreload" in rel_tokens:
                self.script_urls.append(href)
            elif "preload" in rel_tokens and (
                as_val == "script" or re.search(r"\.m?js(?:\?|$)", href, re.IGNORECASE)
            ):
                self.script_urls.append(href)

    def handle_data(self, data: str) -> None:
        if self._in_inline_script:
            self._inline_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_inline_script:
            chunk = "".join(self._inline_buf).strip()
            if len(chunk) > 20:
                self.inline_scripts.append(chunk)
            self._in_inline_script = False
            self._inline_buf = []


def _parse_html_scripts(html_text: str) -> tuple[list[str], list[str]]:
    parser = _HtmlScriptCollector()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass
    return parser.script_urls, parser.inline_scripts


def _discover_lazy_chunk_urls(
    js_texts: list[str],
    base_url: str,
    seed_script_urls: list[str],
) -> list[str]:
    """Discover Webpack hash-mapped lazy chunks and Vite/Rollup JS chunk paths."""
    discovered: list[str] = []
    seen: set[str] = set()

    # Infer default chunk directory prefix from entry script URLs (e.g. /static/js/ or /assets/)
    inferred_prefix = "/static/js/"
    for u in seed_script_urls:
        p = urlparse(u).path
        for candidate_dir in ("/static/js/", "/assets/", "/js/", "/chunks/"):
            if candidate_dir in p:
                inferred_prefix = candidate_dir
                break

    for text in js_texts:
        # 1. Explicit Vite/Webpack chunk string literals
        for raw_lit in _VITE_CHUNK_LITERAL_RE.findall(text):
            resolved = urljoin(base_url.rstrip("/") + "/", raw_lit.lstrip("/") if not raw_lit.startswith("/") else raw_lit)
            if resolved not in seen and not _is_vendor_bundle(resolved):
                seen.add(resolved)
                discovered.append(resolved)

        # 2. Webpack chunk hash map `{ "chunk-1a2b": "8f9e0a1b", ... }`
        pfx_match = _WEBPACK_CHUNK_PREFIX_RE.search(text)
        chunk_prefix = pfx_match.group(1) if pfx_match else inferred_prefix
        if not chunk_prefix.startswith("/"):
            chunk_prefix = "/" + chunk_prefix

        for block_match in re.finditer(
            r"""\{[^{}]{12,12000}["']?[A-Za-z0-9_-]+["']?\s*:\s*["'][0-9a-f]{6,16}["'][^{}]{0,12000}\}""",
            text,
        ):
            block = block_match.group(0)
            pairs = _WEBPACK_CHUNK_PAIR_RE.findall(block)
            if len(pairs) < 2:
                continue
            for chunk_id, chunk_hash in pairs:
                fname = f"{chunk_id}.{chunk_hash}.js"
                resolved = urljoin(base_url.rstrip("/") + "/", f"{chunk_prefix.rstrip('/')}/{fname}")
                if resolved not in seen and not _is_vendor_bundle(resolved):
                    seen.add(resolved)
                    discovered.append(resolved)

    return discovered


def _extract_sibling_origins(
    texts: list[str],
    root_domain: str,
    current_origin: str,
) -> list[str]:
    """Find same-root-domain sibling API origins referenced in HTML/JS (e.g. admin.x.com -> api.x.com)."""
    if not root_domain or _is_loopback(root_domain) or _is_ip_literal(root_domain):
        return []
    cur_netloc = urlparse(current_origin).netloc.lower()
    siblings: list[str] = []
    seen: set[str] = set()

    for text in texts:
        for m in _FULL_URL_RE.finditer(text):
            netloc = m.group(1).lower()
            path_part = (m.group(2) or "").lower()
            if netloc == cur_netloc:
                continue
            host = netloc.split(":", 1)[0]
            if not host or _registrable_root_domain(host) != root_domain:
                continue
            # Ignore pure static CDN asset links (e.g. cdn.example.com/logo.png)
            if path_part and any(path_part.split("?", 1)[0].endswith(ext) for ext in _STATIC_ASSET_EXTS):
                continue
            scheme = m.group(0).split("://", 1)[0].lower()
            origin = f"{scheme}://{netloc}"
            if origin not in seen:
                seen.add(origin)
                siblings.append(origin)
    return sorted(siblings)


def _infer_api_prefix(path: str) -> str:
    segs = [s for s in path.strip("/").split("/") if s]
    if not segs:
        return ""
    if len(segs) >= 2 and segs[0].lower() in {"api", "rest", "rpc", "gw", "bff"}:
        if re.match(r"^v\d+(?:\.\d+)?$", segs[1], re.IGNORECASE):
            return "/" + "/".join(segs[:2])
        return "/" + segs[0]
    if re.match(r"^v\d+(?:\.\d+)?$", segs[0], re.IGNORECASE):
        return "/" + segs[0]
    return ""


def _extract_openapi_params(op: dict[str, Any]) -> list[str]:
    params: list[str] = []
    for p in op.get("parameters") or []:
        if isinstance(p, dict) and p.get("name"):
            params.append(str(p["name"]))
    req_body = op.get("requestBody") or {}
    if isinstance(req_body, dict):
        content = req_body.get("content") or {}
        if isinstance(content, dict):
            for media in content.values():
                if not isinstance(media, dict):
                    continue
                schema = media.get("schema") or {}
                props = schema.get("properties") if isinstance(schema, dict) else None
                if isinstance(props, dict):
                    params.extend(str(k) for k in props)
    return list(dict.fromkeys(params))[:20]


def _normalize_js_source(raw: str) -> str:
    low = (raw or "").strip().lower().replace("-", "_")
    if "inferred" in low:
        return "js_static_inferred"
    return "js_static"


def _add_endpoint(
    bucket: dict[tuple[str, str], dict[str, Any]],
    *,
    method: str,
    path: str,
    base_url: str,
    source: str,
    url: str | None = None,
    inferred_params: list[str] | None = None,
    snippet: str = "",
) -> None:
    m = (method or "GET").upper().strip()
    if m not in _HTTP_METHODS:
        m = "GET"
    clean_path = path.strip() if path else "/"
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    full_url = url or f"{base_url.rstrip('/')}{clean_path}"
    key = (m, clean_path)
    params = [str(p) for p in (inferred_params or []) if str(p).strip()]

    existing = bucket.get(key)
    if existing is None:
        bucket[key] = {
            "id": f"{m} {clean_path}",
            "method": m,
            "path": clean_path,
            "url": full_url,
            "source": source,
            "sources": [source],
            "inferred_params": list(dict.fromkeys(params)),
            "snippet": snippet[:300],
        }
        return

    ep_sources = list(existing.get("sources") or [existing["source"]])
    if source not in ep_sources:
        ep_sources.append(source)
    existing["sources"] = ep_sources

    if _SOURCE_PRIORITY.get(source, 0) > _SOURCE_PRIORITY.get(existing["source"], 0):
        existing["source"] = source
        if url:
            existing["url"] = full_url
    for p in params:
        if p not in existing["inferred_params"]:
            existing["inferred_params"].append(p)
    if snippet and len(snippet) > len(existing["snippet"]):
        existing["snippet"] = snippet[:300]


def extract_seed_root_domain(seed_curl: str | None = None, target_url: str | None = None) -> str:
    """Quickly resolve registrable root_domain from seed_curl or target_url for deterministic keying."""
    if seed_curl and seed_curl.strip():
        m = re.search(r"https?://([^\s/'\"`<>)]+)", seed_curl)
        if m:
            host = urlparse(f"http://{m.group(1)}").hostname or ""
            if host:
                return _registrable_root_domain(host)
        h_match = re.search(r"(?mi)^\s*Host\s*:\s*([^\s\r\n]+)", seed_curl)
        if h_match:
            host = h_match.group(1).split(":", 1)[0].strip()
            if host:
                return _registrable_root_domain(host)
    if target_url and target_url.strip():
        raw = target_url.strip()
        if "://" not in raw:
            raw = f"https://{raw}"
        host = urlparse(raw).hostname or ""
        if host:
            return _registrable_root_domain(host)
    return ""


def merge_explore_results(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    explore_id: str | None = None,
) -> dict[str, Any]:
    """Merge an incoming ExploreResult into an existing root-domain inventory cleanly."""
    merged_id = explore_id or str(existing.get("explore_id") or incoming.get("explore_id") or "")

    # 1. Endpoints deduplicated by (method.upper(), path)
    ep_bucket: dict[tuple[str, str], dict[str, Any]] = {}
    existing_keys: set[tuple[str, str]] = set()
    for ep in existing.get("discovered_endpoints") or []:
        if not isinstance(ep, dict):
            continue
        m = str(ep.get("method") or "GET").upper()
        p = str(ep.get("path") or "/")
        existing_keys.add((m, p))
        ep_sources = list(ep.get("sources") or ([ep.get("source")] if ep.get("source") else []))
        ep_bucket[(m, p)] = {
            "id": f"{m} {p}",
            "method": m,
            "path": p,
            "url": str(ep.get("url") or ""),
            "source": str(ep.get("source") or "js_static"),
            "sources": list(dict.fromkeys(str(s) for s in ep_sources if s)),
            "inferred_params": list(dict.fromkeys(str(x) for x in (ep.get("inferred_params") or []))),
            "snippet": str(ep.get("snippet") or "")[:300],
        }

    new_endpoints_count = 0
    for ep in incoming.get("discovered_endpoints") or []:
        if not isinstance(ep, dict):
            continue
        m = str(ep.get("method") or "GET").upper()
        p = str(ep.get("path") or "/")
        key = (m, p)
        if key not in existing_keys:
            new_endpoints_count += 1
            existing_keys.add(key)
        src = str(ep.get("source") or "js_static")
        ep_sources = list(ep.get("sources") or [src])
        inc_params = [str(x) for x in (ep.get("inferred_params") or []) if str(x).strip()]
        inc_snippet = str(ep.get("snippet") or "")[:300]
        inc_url = str(ep.get("url") or "")

        cur = ep_bucket.get(key)
        if cur is None:
            ep_bucket[key] = {
                "id": f"{m} {p}",
                "method": m,
                "path": p,
                "url": inc_url,
                "source": src,
                "sources": list(dict.fromkeys(str(s) for s in ep_sources if s)),
                "inferred_params": list(dict.fromkeys(inc_params)),
                "snippet": inc_snippet,
            }
        else:
            for s in ep_sources:
                if s and s not in cur["sources"]:
                    cur["sources"].append(str(s))
            if _SOURCE_PRIORITY.get(src, 0) > _SOURCE_PRIORITY.get(cur["source"], 0):
                cur["source"] = src
                if inc_url:
                    cur["url"] = inc_url
            elif not cur["url"] and inc_url:
                cur["url"] = inc_url
            for param in inc_params:
                if param not in cur["inferred_params"]:
                    cur["inferred_params"].append(param)
            if len(inc_snippet) > len(cur["snippet"]):
                cur["snippet"] = inc_snippet

    merged_endpoints = sorted(
        ep_bucket.values(),
        key=lambda e: (-_SOURCE_PRIORITY.get(e["source"], 0), e["path"], e["method"]),
    )

    # 2. Subdomains union & sorted (normalized list[str]; accepts legacy dict items too)
    existing_sub_hosts: set[str] = set()
    for item in existing.get("subdomains") or []:
        if isinstance(item, dict) and item.get("host"):
            existing_sub_hosts.add(str(item["host"]).strip().lower())
        elif isinstance(item, str) and item.strip():
            existing_sub_hosts.add(item.strip().lower())

    new_subdomains_count = 0
    all_sub_hosts = set(existing_sub_hosts)
    for item in incoming.get("subdomains") or []:
        h = ""
        if isinstance(item, dict) and item.get("host"):
            h = str(item["host"]).strip().lower()
        elif isinstance(item, str) and item.strip():
            h = item.strip().lower()
        if not h:
            continue
        if h not in existing_sub_hosts:
            new_subdomains_count += 1
            existing_sub_hosts.add(h)
        all_sub_hosts.add(h)
    merged_subdomains = sorted(all_sub_hosts)

    # 3. Live hosts deduplicated by normalized URL
    live_map: dict[str, dict[str, Any]] = {}
    for group in (existing.get("live_hosts") or [], incoming.get("live_hosts") or []):
        for h_item in group:
            if not isinstance(h_item, dict):
                continue
            raw_u = str(h_item.get("url") or "").strip()
            if not raw_u:
                continue
            pu = urlparse(raw_u)
            norm_key = f"{pu.scheme.lower()}://{pu.netloc.lower()}{pu.path.rstrip('/')}" if pu.netloc else raw_u.rstrip("/").lower()
            cur_h = live_map.get(norm_key)
            tech_list = [str(t) for t in (h_item.get("tech") or []) if str(t).strip()]
            if cur_h is None:
                live_map[norm_key] = {
                    "url": raw_u,
                    "status_code": h_item.get("status_code"),
                    "title": str(h_item.get("title") or ""),
                    "tech": list(dict.fromkeys(tech_list)),
                }
            else:
                if cur_h.get("status_code") is None and h_item.get("status_code") is not None:
                    cur_h["status_code"] = h_item.get("status_code")
                if not cur_h.get("title") and h_item.get("title"):
                    cur_h["title"] = str(h_item["title"])
                for t in tech_list:
                    if t not in cur_h["tech"]:
                        cur_h["tech"].append(t)
    merged_live_hosts = sorted(live_map.values(), key=lambda x: str(x.get("url", "")))

    # 4. SPA routes, micro-frontends, OpenAPI docs
    merged_spa_routes = sorted(
        {str(r) for r in (*(existing.get("spa_routes") or []), *(incoming.get("spa_routes") or [])) if str(r).strip()}
    )

    mfe_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for mfe in (*(existing.get("micro_frontends") or []), *(incoming.get("micro_frontends") or [])):
        if isinstance(mfe, dict):
            k = (str(mfe.get("framework") or ""), str(mfe.get("name") or ""), str(mfe.get("entry") or ""))
            mfe_map[k] = mfe
    merged_mfe = list(mfe_map.values())

    docs_map: dict[str, dict[str, Any]] = {}
    for doc in (*(existing.get("openapi_docs_found") or []), *(incoming.get("openapi_docs_found") or [])):
        if isinstance(doc, dict) and doc.get("url"):
            u = str(doc["url"])
            cur_d = docs_map.get(u)
            if cur_d is None or int(doc.get("endpoints_count", 0) or 0) >= int(cur_d.get("endpoints_count", 0) or 0):
                docs_map[u] = doc
    merged_docs = list(docs_map.values())

    # 5. Seed metadata merge
    ex_seed = dict(existing.get("seed") or {})
    in_seed = dict(incoming.get("seed") or {})
    merged_siblings = sorted(
        {
            str(s)
            for s in (*(ex_seed.get("sibling_api_hosts") or []), *(in_seed.get("sibling_api_hosts") or []))
            if str(s).strip()
        }
    )
    merged_auth_hdrs = sorted(
        {
            str(h)
            for h in (*(ex_seed.get("auth_headers_detected") or []), *(in_seed.get("auth_headers_detected") or []))
            if str(h).strip()
        }
    )
    merged_seed = {
        "type": in_seed.get("type") or ex_seed.get("type") or "url",
        "base_url": in_seed.get("base_url") or ex_seed.get("base_url") or "",
        "root_domain": ex_seed.get("root_domain") or in_seed.get("root_domain") or "",
        "seed_url": in_seed.get("seed_url") or ex_seed.get("seed_url") or "",
        "api_prefix": in_seed.get("api_prefix") or ex_seed.get("api_prefix") or "",
        "auth_headers_detected": merged_auth_hdrs,
        "sibling_api_hosts": merged_siblings,
    }

    # 6. Scan audit history
    ex_scans = list(existing.get("scans") or [])
    if not ex_scans and existing.get("created_at"):
        ex_scans = [
            {
                "scan_id": str(existing.get("explore_id") or "scan-1"),
                "scanned_at": str(existing.get("created_at")),
                "mode": str(existing.get("mode") or "passive"),
                "seed_type": str(ex_seed.get("type") or "url"),
                "seed_url": str(ex_seed.get("seed_url") or ""),
                "new_endpoints": len(existing.get("discovered_endpoints") or []),
                "new_subdomains": len(existing.get("subdomains") or []),
            }
        ]
    in_scans = list(incoming.get("scans") or [])
    if in_scans:
        latest_scan = dict(in_scans[-1])
        latest_scan["new_endpoints"] = new_endpoints_count
        latest_scan["new_subdomains"] = new_subdomains_count
        merged_scans = [*ex_scans, *in_scans[:-1], latest_scan]
    else:
        merged_scans = [
            *ex_scans,
            {
                "scan_id": str(incoming.get("explore_id") or f"scan-{len(ex_scans) + 1}"),
                "scanned_at": str(incoming.get("created_at") or datetime.now(UTC).isoformat()),
                "mode": str(incoming.get("mode") or "passive"),
                "seed_type": str(in_seed.get("type") or "url"),
                "seed_url": str(in_seed.get("seed_url") or ""),
                "new_endpoints": new_endpoints_count,
                "new_subdomains": new_subdomains_count,
            },
        ]

    merged_warnings = list(
        dict.fromkeys([*(existing.get("warnings") or []), *(incoming.get("warnings") or [])])
    )[-30:]

    created_at = str(existing.get("created_at") or incoming.get("created_at") or datetime.now(UTC).isoformat())
    updated_at = str(incoming.get("updated_at") or incoming.get("created_at") or datetime.now(UTC).isoformat())
    merged_project_id = incoming.get("project_id") or existing.get("project_id")
    merged_hosts = _collect_merged_hosts(existing, incoming)

    merged_portals_by_host: dict[str, dict[str, Any]] = {}
    for p in existing.get("discovered_portals") or []:
        if isinstance(p, dict) and p.get("host"):
            merged_portals_by_host[str(p["host"]).lower()] = dict(p)
    for p in incoming.get("discovered_portals") or []:
        if isinstance(p, dict) and p.get("host"):
            merged_portals_by_host[str(p["host"]).lower()] = dict(p)
    merged_portals = sorted(
        merged_portals_by_host.values(),
        key=lambda item: (
            1 if item.get("is_api_gateway") else 0,
            0 if item.get("status_code") == 200 else 1,
            str(item.get("host") or ""),
        ),
    )

    merged_out: dict[str, Any] = {
        "explore_id": merged_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "mode": str(incoming.get("mode") or existing.get("mode") or "passive"),
        "seed": merged_seed,
        "merged_hosts": merged_hosts,
        "seed_hosts": list(merged_hosts),
        "summary": {
            "scans_count": len(merged_scans),
            "endpoints_count": len(merged_endpoints),
            "openapi_docs_count": len(merged_docs),
            "spa_routes_count": len(merged_spa_routes),
            "subdomains_count": len(merged_subdomains),
            "live_hosts_count": len(merged_live_hosts),
        },
        "scans_count": len(merged_scans),
        "scans": merged_scans,
        "discovered_endpoints": merged_endpoints,
        "discovered_portals": merged_portals,
        "openapi_docs_found": merged_docs,
        "spa_routes": merged_spa_routes,
        "micro_frontends": merged_mfe,
        "subdomains": merged_subdomains,
        "live_hosts": merged_live_hosts,
        "warnings": merged_warnings,
    }
    if merged_project_id:
        merged_out["project_id"] = str(merged_project_id)
    return merged_out


def _collect_merged_hosts(*docs: dict[str, Any]) -> list[str]:
    """Collect deduplicated portal hostnames across explore result seed/scans/merged_hosts."""
    hosts: list[str] = []
    seen: set[str] = set()

    def _add_host(raw: str | None) -> None:
        val = (raw or "").strip()
        if not val:
            return
        if "://" in val:
            parsed = urlparse(val)
            host = (parsed.hostname or "").lower()
            if parsed.port and host in {"127.0.0.1", "localhost", "::1"}:
                host = f"{host}:{parsed.port}"
        else:
            host = val.lower().split("/", 1)[0]
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key in ("merged_hosts", "seed_hosts"):
            for h in doc.get(key) or []:
                _add_host(str(h))
        seed = doc.get("seed") or {}
        if isinstance(seed, dict):
            _add_host(str(seed.get("base_url") or ""))
            _add_host(str(seed.get("seed_url") or ""))
        for sc in doc.get("scans") or []:
            if isinstance(sc, dict):
                _add_host(str(sc.get("seed_url") or ""))
    return hosts


def _extract_host_for_result(base_url: str, seed_url: str) -> list[str]:
    return _collect_merged_hosts({"seed": {"base_url": base_url, "seed_url": seed_url}})


_ENV_SUFFIXES = ("-staging", "-stage", "-test", "-dev", "-uat", "-sit", "-pre", "-qa")

_PORTAL_PREFIXES = (
    "admin",
    "api",
    "app",
    "portal",
    "web",
    "m",
    "h5",
    "ops",
    "manage",
    "console",
    "merchant",
    "seller",
    "shop",
    "mall",
    "store",
    "vendor",
    "partner",
    "doctor",
    "patient",
    "medicine",
    "pharmacy",
    "hospital",
    "clinic",
    "yz",
    "kf",
    "crm",
    "erp",
    "sso",
)

_FRIENDLY_PREFIX_LABELS: dict[str, str] = {
    "admin": "管理后台",
    "api": "API 网关",
    "app": "App 端",
    "portal": "门户",
    "web": "Web 端",
    "m": "移动端",
    "h5": "H5 端",
    "ops": "运维后台",
    "manage": "管理端",
    "console": "控制台",
    "merchant": "商户端",
    "seller": "卖家端",
    "shop": "商城端",
    "mall": "商城端",
    "store": "门店端",
    "vendor": "供应商端",
    "partner": "合作伙伴端",
    "doctor": "医生端",
    "patient": "患者端",
    "medicine": "药店端",
    "pharmacy": "药店端",
    "hospital": "医院端",
    "clinic": "诊所端",
    "yz": "医助端",
    "kf": "客服端",
    "crm": "CRM",
    "erp": "ERP",
    "sso": "单点登录",
}

_GENERIC_TITLES = {"document", "index", "home", "loading", "loading...", "vite app", "vue app", "react app", "404", "403", "401"}
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _detect_env_suffix(seed_host: str, root_domain: str) -> str:
    if not seed_host or not root_domain or seed_host == root_domain:
        return ""
    if seed_host.endswith("." + root_domain):
        sub = seed_host[: -(len(root_domain) + 1)]
    else:
        sub = seed_host.split(".", 1)[0]
    first_label = sub.split(".", 1)[0].lower()
    for suf in _ENV_SUFFIXES:
        if first_label.endswith(suf) and len(first_label) > len(suf):
            return suf
    return ""


def _build_portal_label(host: str, title: str, env_suffix: str) -> str:
    clean_title = re.sub(r"\s+", " ", (title or "")).strip()
    if clean_title and clean_title.lower() not in _GENERIC_TITLES and not clean_title.lower().startswith(("404 ", "403 ", "502 ", "nginx")):
        return clean_title[:24]
    first_label = (host or "").split(".", 1)[0].lower()
    if env_suffix and first_label.endswith(env_suffix):
        prefix = first_label[: -len(env_suffix)]
    else:
        prefix = first_label
    return _FRIENDLY_PREFIX_LABELS.get(prefix, first_label or host)


def _resolve_httpx_cli() -> str | None:
    found = settings.resolve_bin("httpx")
    if found:
        return found
    home_bin = Path.home() / ".reconqa" / "bin" / "httpx"
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin.resolve())
    return shutil.which("httpx")


def _probe_portals_with_cli(
    httpx_bin: str,
    candidate_urls: list[str],
    *,
    timeout_sec: int,
) -> list[dict[str, Any]]:
    cmd = [
        httpx_bin,
        "-silent",
        "-status-code",
        "-title",
        "-json",
        "-timeout",
        "4",
        "-threads",
        "20",
    ]
    proxy_val = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    ).strip()
    if proxy_val and proxy_val.startswith(("http://", "https://", "socks5://")):
        cmd.extend(["-http-proxy", proxy_val])

    stdin_payload = "\n".join(candidate_urls) + "\n"
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=max(5, int(timeout_sec)),
            check=False,
        )
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            rec = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _probe_portals_with_python(
    candidate_urls: list[str],
    *,
    timeout_sec: int,
) -> list[dict[str, Any]]:
    per_req_timeout = httpx.Timeout(min(4.0, float(timeout_sec)), connect=3.0)

    def _check_one(u: str) -> dict[str, Any] | None:
        try:
            with httpx.Client(timeout=per_req_timeout, follow_redirects=True, verify=False) as client:
                resp = client.get(u)
                title = ""
                ctype = (resp.headers.get("content-type") or "").lower()
                if "html" in ctype or "<title" in resp.text[:4096].lower():
                    m = _HTML_TITLE_RE.search(resp.text[:16384])
                    if m:
                        title = re.sub(r"\s+", " ", m.group(1)).strip()
                return {
                    "url": str(resp.url),
                    "input": u,
                    "status_code": resp.status_code,
                    "title": title,
                }
        except Exception:
            return None

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for res in pool.map(_check_one, candidate_urls):
            if res is not None:
                rows.append(res)
    return rows


def discover_sibling_portals(seed_url: str, timeout_sec: int = 12) -> dict[str, Any]:
    """Probe ~28 high-signal sibling portal subdomains matching the seed host's environment suffix."""
    raw = (seed_url or "").strip()
    if raw and "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    seed_host = (parsed.hostname or "").lower()
    if not seed_host or _is_loopback(seed_host):
        return {"root_domain": seed_host, "env_suffix": "", "portals": []}
    try:
        ipaddress.ip_address(seed_host)
        return {"root_domain": seed_host, "env_suffix": "", "portals": []}
    except ValueError:
        pass

    root_domain = _registrable_root_domain(seed_host)
    if not root_domain or "." not in root_domain:
        return {"root_domain": root_domain or seed_host, "env_suffix": "", "portals": []}

    env_suffix = _detect_env_suffix(seed_host, root_domain)
    candidate_hosts: list[str] = []
    seen_hosts: set[str] = set()
    for prefix in _PORTAL_PREFIXES:
        h = f"{prefix}{env_suffix}.{root_domain}"
        if h not in seen_hosts:
            seen_hosts.add(h)
            candidate_hosts.append(h)
    if seed_host not in seen_hosts:
        candidate_hosts.insert(0, seed_host)

    candidate_urls = [f"https://{h}" for h in candidate_hosts]
    httpx_bin = _resolve_httpx_cli()
    if httpx_bin:
        raw_rows = _probe_portals_with_cli(httpx_bin, candidate_urls, timeout_sec=timeout_sec)
    else:
        raw_rows = _probe_portals_with_python(candidate_urls, timeout_sec=timeout_sec)

    portals_by_host: dict[str, dict[str, Any]] = {}
    for rec in raw_rows:
        raw_url = str(rec.get("input") or rec.get("url") or "").strip()
        if not raw_url:
            continue
        p = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
        host = (p.hostname or str(rec.get("host") or "")).strip().lower()
        if not host:
            continue
        status_raw = rec.get("status_code") if rec.get("status_code") is not None else rec.get("status-code")
        try:
            status_code = int(status_raw) if status_raw is not None else 0
        except (TypeError, ValueError):
            status_code = 0

        if status_code <= 0 or status_code >= 500:
            continue

        first_label = host.split(".", 1)[0].lower()
        is_api_gw = first_label == "api" or first_label.startswith("api-")
        if not (200 <= status_code <= 403 or (status_code == 404 and is_api_gw)):
            continue

        title = re.sub(r"\s+", " ", str(rec.get("title") or "")).strip()[:120]
        canonical_url = f"https://{host}"
        portals_by_host[host] = {
            "host": host,
            "url": canonical_url,
            "status_code": status_code,
            "title": title,
            "is_api_gateway": is_api_gw,
            "suggested_label": _build_portal_label(host, title, env_suffix),
        }

    portals = sorted(
        portals_by_host.values(),
        key=lambda item: (
            1 if item["is_api_gateway"] else 0,
            0 if item["status_code"] == 200 else 1,
            item["host"],
        ),
    )
    return {
        "root_domain": root_domain,
        "env_suffix": env_suffix,
        "portals": portals,
    }


def _probe_openapi_on_origin(
    origin: str,
    *,
    api_prefix: str,
    probe_headers: dict[str, str],
    http_timeout: httpx.Timeout,
    endpoints_bucket: dict[tuple[str, str], dict[str, Any]],
    openapi_docs_found: list[dict[str, Any]],
) -> None:
    host = (urlparse(origin).hostname or "").lower()
    probe_queue = list(_OPENAPI_CANDIDATE_PATHS)
    if api_prefix:
        for suffix in ("/openapi.json", "/v3/api-docs"):
            prefixed = f"{api_prefix.rstrip('/')}{suffix}"
            if prefixed not in probe_queue:
                probe_queue.append(prefixed)

    seen_doc_urls: set[str] = set()
    q_idx = 0
    with httpx.Client(
        timeout=http_timeout,
        follow_redirects=True,
        trust_env=not _is_loopback(host),
        verify=False,
    ) as client:
        while q_idx < len(probe_queue) and len(seen_doc_urls) < 25:
            cand_path = probe_queue[q_idx]
            q_idx += 1
            probe_url = (
                cand_path
                if re.match(r"^https?://", cand_path, re.IGNORECASE)
                else f"{origin.rstrip('/')}/{cand_path.lstrip('/')}"
            )
            if probe_url in seen_doc_urls:
                continue
            seen_doc_urls.add(probe_url)
            try:
                resp = client.get(probe_url, headers=probe_headers)
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            try:
                doc = resp.json()
            except Exception:
                continue

            if isinstance(doc, list):
                for item in doc:
                    if isinstance(item, dict):
                        ref_url = item.get("url") or item.get("location")
                        if isinstance(ref_url, str) and ref_url.strip():
                            probe_queue.append(ref_url.strip())
                continue

            if not isinstance(doc, dict):
                continue

            if "paths" not in doc and ("urls" in doc or "url" in doc):
                if isinstance(doc.get("url"), str) and doc["url"].strip():
                    probe_queue.append(doc["url"].strip())
                if isinstance(doc.get("urls"), list):
                    for item in doc["urls"]:
                        if isinstance(item, dict) and isinstance(item.get("url"), str):
                            probe_queue.append(item["url"].strip())
                continue

            if not isinstance(doc.get("paths"), dict):
                continue
            if "openapi" not in doc and "swagger" not in doc:
                continue

            base_path = str(doc.get("basePath") or "").rstrip("/")
            harvested = 0
            for raw_path, path_item in doc["paths"].items():
                if not isinstance(path_item, dict):
                    continue
                full_path = (
                    f"{base_path}{raw_path}"
                    if base_path and not raw_path.startswith(base_path)
                    else str(raw_path)
                )
                for m_key, op in path_item.items():
                    m_upper = str(m_key).upper()
                    if m_upper not in _HTTP_METHODS or not isinstance(op, dict):
                        continue
                    _add_endpoint(
                        endpoints_bucket,
                        method=m_upper,
                        path=full_path,
                        base_url=origin,
                        source="openapi_probe",
                        inferred_params=_extract_openapi_params(op),
                        snippet=str(op.get("summary") or op.get("operationId") or ""),
                    )
                    harvested += 1

            info_obj = doc.get("info") if isinstance(doc.get("info"), dict) else {}
            openapi_docs_found.append(
                {
                    "url": probe_url,
                    "title": str(info_obj.get("title") or cand_path),
                    "endpoints_count": harvested,
                }
            )


def explore_from_seed(
    out_dir: Path,
    *,
    seed_curl: str | None = None,
    target_url: str | None = None,
    mode: str = "passive",
    probe_openapi_docs: bool = True,
    crawl_js: bool = True,
    discover_portals: bool = True,
    depth: int = 2,
    timeout: int = 30,
    explore_id: str | None = None,
) -> dict[str, Any]:
    """Explore APIs, OpenAPI docs, JS/SPA routes, sibling API hosts, and assets from a seed cURL or URL."""
    out_dir = ensure_dir(Path(out_dir).resolve())
    exp_id = explore_id or out_dir.name
    created_at = datetime.now(UTC).isoformat()

    warnings: list[str] = []
    endpoints_bucket: dict[tuple[str, str], dict[str, Any]] = {}
    openapi_docs_found: list[dict[str, Any]] = []
    spa_routes: list[str] = []
    micro_frontends: list[dict[str, Any]] = []
    subdomains_set: set[str] = set()
    live_hosts: list[dict[str, Any]] = []
    sibling_api_hosts: list[str] = []

    seed_type = "url"
    seed_url = ""
    seed_method = "GET"
    base_url = ""
    seed_host = ""
    root_domain = ""
    api_prefix = ""
    probe_headers: dict[str, str] = {}
    auth_headers_detected: list[str] = []

    # --- Step A: Parse Seed cURL or Target URL ---
    if seed_curl and seed_curl.strip():
        seed_type = "curl"
        seed_har_dir = ensure_dir(out_dir / "seed_har")
        import_res = import_traffic_text(
            seed_curl,
            seed_har_dir,
            scenario_name="seed",
            format_hint="auto",
        )
        har_path_str = import_res.get("har_path") or ""
        if import_res.get("requests_imported", 0) == 0 or not har_path_str:
            raise ValueError("Could not parse any valid HTTP request from seed_curl")

        har_doc = read_json(Path(har_path_str))
        entries = har_doc.get("log", {}).get("entries", [])
        if not entries:
            raise ValueError("Seed HAR contains no entries")

        first_req = entries[0].get("request") or {}
        seed_url = str(first_req.get("url") or "")
        seed_method = str(first_req.get("method") or "GET").upper()
        parsed_first = urlparse(seed_url)
        if not parsed_first.scheme or not parsed_first.netloc:
            raise ValueError(f"Seed URL is missing scheme or host: '{seed_url}'")

        base_url = f"{parsed_first.scheme}://{parsed_first.netloc}"
        seed_host = (parsed_first.hostname or "").lower()
        root_domain = _registrable_root_domain(seed_host)
        api_prefix = _infer_api_prefix(parsed_first.path or "/")

        detected_auth_set: set[str] = set()
        for entry in entries:
            req = entry.get("request") or {}
            req_url = str(req.get("url") or "")
            req_method = str(req.get("method") or "GET").upper()
            p_url = urlparse(req_url)
            q_params = [
                str(q.get("name"))
                for q in (req.get("queryString") or [])
                if isinstance(q, dict) and q.get("name")
            ]
            post_data = req.get("postData") or {}
            if isinstance(post_data, dict) and isinstance(post_data.get("text"), str):
                try:
                    body_json = json.loads(post_data["text"])
                    if isinstance(body_json, dict):
                        q_params.extend(str(k) for k in body_json)
                except json.JSONDecodeError:
                    pass

            _add_endpoint(
                endpoints_bucket,
                method=req_method,
                path=p_url.path or "/",
                base_url=f"{p_url.scheme}://{p_url.netloc}" if p_url.netloc else base_url,
                url=req_url,
                source="seed_curl",
                inferred_params=q_params,
                snippet="Imported from seed cURL/traffic",
            )

            for hdr in req.get("headers") or []:
                if not isinstance(hdr, dict):
                    continue
                h_name = str(hdr.get("name") or "").strip()
                h_val = str(hdr.get("value") or "").strip()
                low = h_name.lower()
                if not h_name or low.startswith(":"):
                    continue
                if low in _AUTH_HEADER_EXACT or low.startswith("x-"):
                    detected_auth_set.add(h_name)
                if low not in _SKIP_PROBE_HEADERS and h_name not in probe_headers:
                    probe_headers[h_name] = h_val

        auth_headers_detected = sorted(detected_auth_set)

    elif target_url and target_url.strip():
        raw_target = target_url.strip()
        if "://" not in raw_target:
            raw_target = f"https://{raw_target}"
        parsed = urlparse(raw_target)
        if not parsed.netloc:
            raise ValueError(f"Invalid target_url: '{target_url}'")
        seed_type = "url"
        seed_url = raw_target
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        seed_host = (parsed.hostname or "").lower()
        root_domain = _registrable_root_domain(seed_host)
        api_prefix = _infer_api_prefix(parsed.path or "/")
    else:
        raise ValueError("Either seed_curl or target_url is required")

    loopback = _is_loopback(seed_host)
    http_timeout = httpx.Timeout(min(float(timeout), 10.0), connect=5.0)
    collected_urls: list[str] = [base_url]
    if seed_url and seed_url not in collected_urls:
        collected_urls.append(seed_url)

    # --- Step B: OpenAPI / Swagger / SpringBoot Auto-Probe on Primary Origin ---
    if probe_openapi_docs:
        _probe_openapi_on_origin(
            base_url,
            api_prefix=api_prefix,
            probe_headers=probe_headers,
            http_timeout=http_timeout,
            endpoints_bucket=endpoints_bucket,
            openapi_docs_found=openapi_docs_found,
        )

    # --- Step C: HTML + Same-Origin JS Bundle + Webpack/Vite Chunk Extraction ---
    if crawl_js:
        html_dir = ensure_dir(out_dir / "html_pages")
        js_dir = ensure_dir(out_dir / "js_bundles" / "explore")
        pages_to_fetch = [base_url]
        if seed_url and seed_url != base_url and seed_method == "GET":
            pages_to_fetch.append(seed_url)

        script_urls: list[str] = []
        seen_scripts: set[str] = set()
        inline_chunks: list[str] = []
        all_texts_for_siblings: list[str] = []
        base_netloc = urlparse(base_url).netloc.lower()

        def _is_same_site(candidate_url: str) -> bool:
            p_js = urlparse(candidate_url)
            if p_js.scheme not in {"http", "https"}:
                return False
            h = (p_js.hostname or "").lower()
            return (
                p_js.netloc.lower() == base_netloc
                or h == seed_host
                or (root_domain != "" and _registrable_root_domain(h) == root_domain)
            )

        with httpx.Client(
            timeout=http_timeout,
            follow_redirects=True,
            trust_env=not loopback,
            verify=False,
        ) as client:
            for page_idx, page_url in enumerate(pages_to_fetch):
                try:
                    resp = client.get(page_url, headers=probe_headers)
                except httpx.HTTPError as exc:
                    warnings.append(f"Failed to fetch page {page_url}: {exc}")
                    continue

                ctype = resp.headers.get("content-type", "").lower()
                text = resp.text
                if "html" in ctype or "<html" in text[:512].lower() or "<script" in text.lower():
                    (html_dir / f"page_{page_idx}.html").write_text(text, encoding="utf-8")
                    all_texts_for_siblings.append(text)
                    raw_srcs, page_inlines = _parse_html_scripts(text)
                    inline_chunks.extend(page_inlines)
                    all_texts_for_siblings.extend(page_inlines)

                    for raw_src in raw_srcs:
                        resolved = urljoin(str(resp.url), raw_src.strip())
                        if not _is_same_site(resolved) or _is_vendor_bundle(resolved):
                            continue
                        if resolved not in seen_scripts and len(script_urls) < _MAX_JS_BUNDLES:
                            seen_scripts.add(resolved)
                            script_urls.append(resolved)
                            collected_urls.append(resolved)

        if inline_chunks:
            (js_dir / "inline_scripts.js").write_text(
                "\n\n// --- inline script ---\n".join(inline_chunks),
                encoding="utf-8",
            )

        def _fetch_js_one(js_url: str) -> tuple[str, str | None]:
            h = (urlparse(js_url).hostname or "").lower()
            try:
                with httpx.Client(
                    timeout=http_timeout,
                    follow_redirects=True,
                    trust_env=not _is_loopback(h),
                    verify=False,
                ) as c:
                    r = c.get(js_url, headers=probe_headers)
                    if r.status_code == 200 and r.text.strip():
                        return js_url, r.text
            except httpx.HTTPError:
                pass
            return js_url, None

        # Round 1: fetch entry & preloaded scripts concurrently
        saved_js_count = 0
        entry_js_texts: list[str] = list(inline_chunks)
        if script_urls:
            with ThreadPoolExecutor(max_workers=6) as pool:
                for js_url, js_text in pool.map(_fetch_js_one, script_urls):
                    if not js_text:
                        continue
                    fname = Path(urlparse(js_url).path).name or f"bundle_{saved_js_count}.js"
                    safe_name = slugify(Path(fname).stem) or f"bundle_{saved_js_count}"
                    (js_dir / f"{saved_js_count:02d}_{safe_name}.js").write_text(
                        js_text, encoding="utf-8"
                    )
                    saved_js_count += 1
                    entry_js_texts.append(js_text)
                    all_texts_for_siblings.append(js_text)

        # Round 2: Webpack / Vite lazy chunk discovery from inline + entry scripts
        remaining_budget = max(0, _MAX_JS_BUNDLES - saved_js_count)
        if remaining_budget > 0 and entry_js_texts:
            lazy_urls = [
                u
                for u in _discover_lazy_chunk_urls(entry_js_texts, base_url, script_urls)
                if u not in seen_scripts and _is_same_site(u)
            ][:remaining_budget]
            if lazy_urls:
                for u in lazy_urls:
                    seen_scripts.add(u)
                    collected_urls.append(u)
                with ThreadPoolExecutor(max_workers=6) as pool:
                    for js_url, js_text in pool.map(_fetch_js_one, lazy_urls):
                        if not js_text:
                            continue
                        fname = Path(urlparse(js_url).path).name or f"chunk_{saved_js_count}.js"
                        safe_name = slugify(Path(fname).stem) or f"chunk_{saved_js_count}"
                        (js_dir / f"{saved_js_count:02d}_{safe_name}.js").write_text(
                            js_text, encoding="utf-8"
                        )
                        saved_js_count += 1
                        all_texts_for_siblings.append(js_text)

        # Sibling API host discovery (e.g. admin.example.com -> api.example.com)
        sibling_api_hosts = _extract_sibling_origins(
            all_texts_for_siblings, root_domain, base_url
        )
        for sib_origin in sibling_api_hosts:
            live_hosts.append(
                {
                    "url": sib_origin,
                    "status_code": None,
                    "title": "Sibling API host discovered in HTML/JS",
                    "tech": ["js_sibling_host"],
                }
            )

        # Probe OpenAPI/Swagger on discovered sibling API hosts too!
        if probe_openapi_docs and sibling_api_hosts:
            for sib_origin in sibling_api_hosts[:4]:
                _probe_openapi_on_origin(
                    sib_origin,
                    api_prefix=api_prefix,
                    probe_headers=probe_headers,
                    http_timeout=http_timeout,
                    endpoints_bucket=endpoints_bucket,
                    openapi_docs_found=openapi_docs_found,
                )

        if any(js_dir.glob("*.js")):
            js_res = extract_js_endpoints(
                out_dir / "js_bundles",
                out_dir / "js_endpoints.json",
                target_domain=root_domain,
            )
            for ep in js_res.get("endpoints") or []:
                if not isinstance(ep, dict) or not ep.get("path"):
                    continue
                ep_full_url = str(ep.get("url") or "") or None
                ep_origin = base_url
                if ep_full_url:
                    pu = urlparse(ep_full_url)
                    if pu.scheme and pu.netloc:
                        ep_origin = f"{pu.scheme}://{pu.netloc}"
                _add_endpoint(
                    endpoints_bucket,
                    method=str(ep.get("method") or "GET"),
                    path=str(ep["path"]),
                    base_url=ep_origin,
                    url=ep_full_url,
                    source=_normalize_js_source(str(ep.get("discovery_source") or "js_static")),
                    inferred_params=list(ep.get("req_keys") or []),
                    snippet=str(ep.get("snippet") or ""),
                )

        spa_res = extract_spa_routes(
            ensure_dir(out_dir / "discovery"),
            js_dir=out_dir / "js_bundles",
            html_dir=html_dir,
            urls=collected_urls,
        )
        spa_routes = list(spa_res.get("routes") or [])
        micro_frontends = list(spa_res.get("micro_frontends") or [])

    # --- Step D: Optional External ProjectDiscovery Tools ---
    disc_dir = ensure_dir(out_dir / "discovery")
    if loopback:
        warnings.append(
            "Skipped external ProjectDiscovery passive/active tools for loopback target; "
            "OpenAPI probe and JS/SPA extraction ran directly."
        )
    else:
        disc_target = base_url if mode == "active" else root_domain
        pd_timeout = min(timeout, 15) if mode == "passive" else min(timeout, 30)
        try:
            disc_res = run_discovery(
                disc_target,
                disc_dir,
                mode=mode,
                depth=depth,
                crawl_time="2m",
                timeout=pd_timeout,
            )
            for w in disc_res.get("warnings") or []:
                warnings.append(str(w))

            for rec in read_jsonl(disc_dir / "subdomains.jsonl"):
                host = rec.get("host") or rec.get("domain")
                if host:
                    subdomains_set.add(str(host).strip().lower())

            seen_live_urls = {str(h.get("url") or "") for h in live_hosts}
            for rec in read_jsonl(disc_dir / "live_hosts.jsonl"):
                u_str = str(rec.get("url") or rec.get("input") or "")
                if u_str and u_str in seen_live_urls:
                    continue
                seen_live_urls.add(u_str)
                live_hosts.append(
                    {
                        "url": u_str,
                        "status_code": rec.get("status_code") or rec.get("status-code"),
                        "title": str(rec.get("title") or ""),
                        "tech": list(rec.get("tech") or []),
                    }
                )

            for fname, src_label in (
                ("katana_urls.jsonl", "katana"),
                ("historical_urls.jsonl", "urlfinder"),
            ):
                for rec in read_jsonl(disc_dir / fname):
                    req_obj = rec.get("request") if isinstance(rec.get("request"), dict) else {}
                    raw_u = str(req_obj.get("endpoint") or rec.get("url") or "").strip()
                    if not raw_u or "://" not in raw_u:
                        continue
                    pu = urlparse(raw_u)
                    h = (pu.hostname or "").lower()
                    if _registrable_root_domain(h) != root_domain:
                        continue
                    if any(
                        (pu.path or "").lower().endswith(ext)
                        for ext in (".js", ".css", ".png", ".jpg", ".svg", ".ico", ".woff", ".woff2")
                    ):
                        continue
                    _add_endpoint(
                        endpoints_bucket,
                        method=str(req_obj.get("method") or "GET"),
                        path=pu.path or "/",
                        base_url=f"{pu.scheme}://{pu.netloc}",
                        url=raw_u,
                        source=src_label,
                    )
        except Exception as exc:
            warnings.append(f"Optional PD discovery step failed: {exc}")

    discovered_portals: list[dict[str, Any]] = []
    if discover_portals and not loopback and base_url:
        try:
            portal_res = discover_sibling_portals(base_url, timeout_sec=min(timeout, 12))
            discovered_portals = list(portal_res.get("portals") or [])
        except Exception as exc:
            warnings.append(f"Sibling portal discovery skipped: {exc}")

    subdomains = sorted(subdomains_set)
    discovered_endpoints = sorted(
        endpoints_bucket.values(),
        key=lambda ep: (-_SOURCE_PRIORITY.get(ep["source"], 0), ep["path"], ep["method"]),
    )

    initial_hosts = _extract_host_for_result(base_url, seed_url)
    result: dict[str, Any] = {
        "explore_id": exp_id,
        "created_at": created_at,
        "updated_at": created_at,
        "mode": mode,
        "seed": {
            "type": seed_type,
            "base_url": base_url,
            "root_domain": root_domain,
            "seed_url": seed_url,
            "api_prefix": api_prefix,
            "auth_headers_detected": auth_headers_detected,
            "sibling_api_hosts": sibling_api_hosts,
        },
        "merged_hosts": initial_hosts,
        "seed_hosts": list(initial_hosts),
        "summary": {
            "scans_count": 1,
            "endpoints_count": len(discovered_endpoints),
            "openapi_docs_count": len(openapi_docs_found),
            "spa_routes_count": len(spa_routes),
            "subdomains_count": len(subdomains),
            "live_hosts_count": len(live_hosts),
        },
        "scans_count": 1,
        "scans": [
            {
                "scan_id": exp_id,
                "scanned_at": created_at,
                "mode": mode,
                "seed_type": seed_type,
                "seed_url": seed_url,
                "new_endpoints": len(discovered_endpoints),
                "new_subdomains": len(subdomains),
            }
        ],
        "discovered_endpoints": discovered_endpoints,
        "discovered_portals": discovered_portals,
        "openapi_docs_found": openapi_docs_found,
        "spa_routes": spa_routes,
        "micro_frontends": micro_frontends,
        "subdomains": subdomains,
        "live_hosts": live_hosts,
        "warnings": warnings,
    }

    write_json(out_dir / "explore_result.json", result)
    return result
