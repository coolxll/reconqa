"""Seed-driven API & asset explorer (Seed cURL / URL -> OpenAPI probe + SPA/Webpack JS + Sibling Hosts + PD)."""

from __future__ import annotations

import ipaddress
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

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
    "katana": 3,
    "js_static": 2,
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
            "inferred_params": list(dict.fromkeys(params)),
            "snippet": snippet[:300],
        }
        return

    if _SOURCE_PRIORITY.get(source, 0) > _SOURCE_PRIORITY.get(existing["source"], 0):
        existing["source"] = source
        if url:
            existing["url"] = full_url
    for p in params:
        if p not in existing["inferred_params"]:
            existing["inferred_params"].append(p)
    if snippet and len(snippet) > len(existing["snippet"]):
        existing["snippet"] = snippet[:300]


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
    subdomains: list[dict[str, Any]] = []
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
                    subdomains.append(
                        {"host": str(host), "source": str(rec.get("source") or "subfinder")}
                    )

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

    discovered_endpoints = sorted(
        endpoints_bucket.values(),
        key=lambda ep: (-_SOURCE_PRIORITY.get(ep["source"], 0), ep["path"], ep["method"]),
    )

    result: dict[str, Any] = {
        "explore_id": exp_id,
        "created_at": created_at,
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
        "summary": {
            "endpoints_count": len(discovered_endpoints),
            "openapi_docs_count": len(openapi_docs_found),
            "spa_routes_count": len(spa_routes),
            "subdomains_count": len(subdomains),
            "live_hosts_count": len(live_hosts),
        },
        "discovered_endpoints": discovered_endpoints,
        "openapi_docs_found": openapi_docs_found,
        "spa_routes": spa_routes,
        "micro_frontends": micro_frontends,
        "subdomains": subdomains,
        "live_hosts": live_hosts,
        "warnings": warnings,
    }

    write_json(out_dir / "explore_result.json", result)
    return result
