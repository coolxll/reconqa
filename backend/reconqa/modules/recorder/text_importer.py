"""Multi-format DevTools traffic text importer (cURL, fetch, Raw HTTP, pasted HAR JSON) -> HAR 1.2."""

from __future__ import annotations

import base64
import json
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

from reconqa.modules.recorder.har_importer import import_hars
from reconqa.utils.io import ensure_dir, slugify, write_json

HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

_SYNTHETIC_RESP_TEXT = json.dumps({"code": 0, "msg": "ok", "data": {}})


@dataclass
class ParsedRequest:
    method: str
    url: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str | None = None
    mime_type: str | None = None


def _is_pseudo_header(name: str) -> bool:
    return name.strip().startswith(":")


def _clean_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in headers:
        name = k.strip()
        if not name or _is_pseudo_header(name):
            continue
        out.append((name, v.strip()))
    return out


def _get_header(headers: list[tuple[str, str]], target: str) -> str | None:
    low = target.lower()
    for k, v in headers:
        if k.lower() == low:
            return v
    return None


def _parse_cookies(cookie_header: str | None) -> list[dict[str, str]]:
    if not cookie_header:
        return []
    cookies: list[dict[str, str]] = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            cookies.append({"name": k.strip(), "value": v.strip()})
        else:
            cookies.append({"name": part, "value": ""})
    return cookies


def _decode_ansi_c_quotes(text: str) -> str:
    """Convert POSIX $'...' ANSI-C quoted strings into standard single-quoted strings for shlex."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_single = False
    in_double = False

    while i < n:
        ch = text[i]
        if in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            out.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "'" and (i == 0 or text[i - 1] != "$"):
            in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue

        if ch == "$" and i + 1 < n and text[i + 1] == "'":
            i += 2
            buf: list[str] = []
            while i < n:
                c = text[i]
                if c == "'":
                    i += 1
                    break
                if c == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    if nxt == "n":
                        buf.append("\n")
                        i += 2
                    elif nxt == "r":
                        buf.append("\r")
                        i += 2
                    elif nxt == "t":
                        buf.append("\t")
                        i += 2
                    elif nxt in ("'", '"', "\\"):
                        buf.append(nxt)
                        i += 2
                    elif nxt == "x" and i + 3 < n:
                        hex_val = text[i + 2 : i + 4]
                        try:
                            buf.append(chr(int(hex_val, 16)))
                            i += 4
                            continue
                        except ValueError:
                            buf.append(nxt)
                            i += 2
                    elif nxt == "u" and i + 5 < n:
                        hex_val = text[i + 2 : i + 6]
                        try:
                            buf.append(chr(int(hex_val, 16)))
                            i += 6
                            continue
                        except ValueError:
                            buf.append(nxt)
                            i += 2
                    else:
                        buf.append(nxt)
                        i += 2
                else:
                    buf.append(c)
                    i += 1
            decoded = "".join(buf).replace("'", "'\"'\"'")
            out.append(f"'{decoded}'")
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _split_curl_commands(content: str) -> list[str]:
    """Normalize line continuations and split multi-curl pastes into individual commands."""
    # Join bash line continuations (`\` + newline) and Windows cmd (`^` + newline)
    joined = re.sub(r"\\\r?\n\s*", " ", content)
    joined = re.sub(r"\^\r?\n\s*", " ", joined)

    commands: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(joined)

    while i < n:
        ch = joined[i]
        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(joined[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "$" and i + 1 < n and joined[i + 1] == "'":
            buf.append(ch)
            buf.append("'")
            i += 2
            while i < n:
                c = joined[i]
                buf.append(c)
                if c == "\\" and i + 1 < n:
                    buf.append(joined[i + 1])
                    i += 2
                    continue
                if c == "'":
                    i += 1
                    break
                i += 1
            continue

        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue

        if ch in ("\n", "\r", ";"):
            chunk = "".join(buf).strip()
            if chunk:
                commands.append(chunk)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        commands.append(tail)

    return [c for c in commands if re.match(r"^curl(?:\.exe)?\b", c, re.IGNORECASE)]


def _parse_single_curl(cmd: str) -> ParsedRequest | None:
    normalized = _decode_ansi_c_quotes(cmd)
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return None
    if not tokens or not re.match(r"^curl(?:\.exe)?$", tokens[0], re.IGNORECASE):
        return None

    method: str | None = None
    url: str | None = None
    headers: list[tuple[str, str]] = []
    cookies: list[str] = []
    data_parts: list[str] = []
    force_get = False

    idx = 1
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in ("-X", "--request") and idx + 1 < len(tokens):
            method = tokens[idx + 1].upper()
            idx += 2
        elif tok.startswith("-X") and len(tok) > 2:
            method = tok[2:].upper()
            idx += 1
        elif tok in ("-H", "--header") and idx + 1 < len(tokens):
            hdr = tokens[idx + 1]
            if ":" in hdr:
                k, v = hdr.split(":", 1)
                headers.append((k.strip(), v.strip()))
            idx += 2
        elif tok in ("-b", "--cookie") and idx + 1 < len(tokens):
            cookies.append(tokens[idx + 1].strip())
            idx += 2
        elif tok in ("-A", "--user-agent") and idx + 1 < len(tokens):
            headers.append(("User-Agent", tokens[idx + 1]))
            idx += 2
        elif tok in ("-e", "--referer") and idx + 1 < len(tokens):
            headers.append(("Referer", tokens[idx + 1]))
            idx += 2
        elif tok in ("-u", "--user") and idx + 1 < len(tokens):
            encoded = base64.b64encode(tokens[idx + 1].encode("utf-8")).decode("ascii")
            headers.append(("Authorization", f"Basic {encoded}"))
            idx += 2
        elif tok in (
            "-d",
            "--data",
            "--data-raw",
            "--data-binary",
            "--data-ascii",
            "--data-urlencode",
        ) and idx + 1 < len(tokens):
            data_parts.append(tokens[idx + 1])
            idx += 2
        elif tok.startswith("--data-raw="):
            data_parts.append(tok.split("=", 1)[1])
            idx += 1
        elif tok.startswith("--data="):
            data_parts.append(tok.split("=", 1)[1])
            idx += 1
        elif tok in ("-G", "--get"):
            force_get = True
            idx += 1
        elif tok == "--url" and idx + 1 < len(tokens):
            url = tokens[idx + 1]
            idx += 2
        elif tok in (
            "--compressed",
            "-k",
            "--insecure",
            "-L",
            "--location",
            "-s",
            "--silent",
            "-S",
            "--show-error",
            "-i",
            "--include",
            "-v",
            "--verbose",
            "--http2",
            "--http1.1",
            "-g",
            "--globoff",
            "--tr-encoding",
        ):
            idx += 1
        elif tok.startswith("-"):
            # Skip unknown option and its potential argument if next token is not a flag/URL
            if (
                "=" not in tok
                and idx + 1 < len(tokens)
                and not tokens[idx + 1].startswith("-")
                and not re.match(r"^https?://", tokens[idx + 1], re.IGNORECASE)
            ):
                idx += 2
            else:
                idx += 1
        else:
            if url is None and re.match(r"^https?://", tok, re.IGNORECASE):
                url = tok
            idx += 1

    if not url:
        return None

    if cookies:
        existing_cookie = _get_header(headers, "cookie")
        merged_cookie = "; ".join(([existing_cookie] if existing_cookie else []) + cookies)
        headers = [(k, v) for k, v in headers if k.lower() != "cookie"]
        headers.append(("Cookie", merged_cookie))

    body: str | None = None
    if data_parts:
        raw_body = "&".join(data_parts) if len(data_parts) > 1 else data_parts[0]
        if force_get:
            parsed_url = urlparse(url)
            existing_q = parsed_url.query
            new_q = f"{existing_q}&{raw_body}" if existing_q else raw_body
            url = urlunparse(parsed_url._replace(query=new_q))
        else:
            body = raw_body

    final_method = method or ("GET" if force_get or body is None else "POST")
    cleaned = _clean_headers(headers)
    mime = _get_header(cleaned, "content-type")
    if body is not None and not mime:
        stripped = body.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            mime = "application/json"
            cleaned.append(("Content-Type", mime))
        else:
            mime = "application/x-www-form-urlencoded"
            cleaned.append(("Content-Type", mime))

    return ParsedRequest(method=final_method, url=url, headers=cleaned, body=body, mime_type=mime)


def _parse_curl_text(content: str) -> list[ParsedRequest]:
    requests: list[ParsedRequest] = []
    for cmd in _split_curl_commands(content):
        req = _parse_single_curl(cmd)
        if req is not None:
            requests.append(req)
    return requests


def _extract_balanced(text: str, start_idx: int, open_ch: str, close_ch: str) -> tuple[str, int] | None:
    depth = 0
    in_str: str | None = None
    escaped = False
    for idx in range(start_idx, len(text)):
        ch = text[idx]
        if in_str is not None:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start_idx : idx + 1], idx + 1
    return None


def _js_object_to_json(js_obj: str) -> str:
    """Convert a Chrome DevTools JS options object literal into valid JSON."""
    out: list[str] = []
    prev_non_ws = ""
    i = 0
    n = len(js_obj)
    while i < n:
        ch = js_obj[i]
        if ch in ('"', "'"):
            quote = ch
            i += 1
            buf: list[str] = []
            while i < n:
                c = js_obj[i]
                if c == "\\" and i + 1 < n:
                    nxt = js_obj[i + 1]
                    if quote == "'" and nxt == "'":
                        buf.append("'")
                    elif nxt == '"':
                        buf.append('\\"')
                    else:
                        buf.append("\\" + nxt)
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                if c == '"':
                    buf.append('\\"')
                elif c == "\n":
                    buf.append("\\n")
                elif c == "\r":
                    buf.append("\\r")
                else:
                    buf.append(c)
                i += 1
            out.append('"' + "".join(buf) + '"')
            prev_non_ws = '"'
            continue

        # Handle unquoted identifier keys like `method:` or `headers:`
        if prev_non_ws in ("", "{", ","):
            m = re.match(r"([A-Za-z_$][A-Za-z0-9_$-]*)\s*:", js_obj[i:])
            if m:
                out.append(f'"{m.group(1)}":')
                prev_non_ws = ":"
                i += m.end()
                continue

        if js_obj[i : i + 9] == "undefined" and (
            i + 9 >= n or not (js_obj[i + 9].isalnum() or js_obj[i + 9] == "_")
        ):
            out.append("null")
            prev_non_ws = "l"
            i += 9
            continue

        out.append(ch)
        if not ch.isspace():
            prev_non_ws = ch
        i += 1

    normalized = "".join(out)
    # Remove trailing commas before `}` or `]`
    normalized = re.sub(r",\s*([}\]])", r"\1", normalized)
    return normalized


def _parse_fetch_text(content: str) -> list[ParsedRequest]:
    requests: list[ParsedRequest] = []
    for match in re.finditer(r"\bfetch\s*\(", content):
        open_paren = content.find("(", match.start())
        balanced = _extract_balanced(content, open_paren, "(", ")")
        if balanced is None:
            continue
        inner = balanced[0][1:-1].strip()
        if not inner:
            continue

        # First argument: quoted URL
        url_match = re.match(r"""^(['"`])((?:\\.|(?!\1).)*)\1""", inner)
        if not url_match:
            continue
        url = url_match.group(2)
        rest = inner[url_match.end() :].strip()

        opts: dict[str, Any] = {}
        if rest.startswith(","):
            opts_str = rest[1:].strip()
            if opts_str.startswith("{"):
                obj_pair = _extract_balanced(opts_str, 0, "{", "}")
                if obj_pair is not None:
                    raw_obj = obj_pair[0]
                    try:
                        opts = json.loads(raw_obj)
                    except json.JSONDecodeError:
                        try:
                            opts = json.loads(_js_object_to_json(raw_obj))
                        except json.JSONDecodeError:
                            opts = {}

        raw_headers = opts.get("headers") or {}
        headers: list[tuple[str, str]] = []
        if isinstance(raw_headers, dict):
            headers = [(str(k), str(v)) for k, v in raw_headers.items()]
        elif isinstance(raw_headers, list):
            for pair in raw_headers:
                if isinstance(pair, list) and len(pair) == 2:
                    headers.append((str(pair[0]), str(pair[1])))

        if opts.get("referrer") and not _get_header(headers, "referer"):
            headers.append(("Referer", str(opts["referrer"])))

        raw_body = opts.get("body")
        body: str | None = None
        if raw_body is not None:
            body = raw_body if isinstance(raw_body, str) else json.dumps(raw_body, ensure_ascii=False)

        method = str(opts.get("method") or ("POST" if body is not None else "GET")).upper()
        cleaned = _clean_headers(headers)
        mime = _get_header(cleaned, "content-type")
        if body is not None and not mime:
            if body.lstrip().startswith(("{", "[")):
                mime = "application/json"
                cleaned.append(("Content-Type", mime))
            else:
                mime = "text/plain"

        requests.append(
            ParsedRequest(method=method, url=url, headers=cleaned, body=body, mime_type=mime)
        )
    return requests


def _parse_raw_http_text(content: str) -> list[ParsedRequest]:
    # Split blocks on explicit `###` separators first
    raw_blocks = re.split(r"(?m)^\s*###+.*$", content)
    req_line_re = re.compile(
        r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(\S+)(?:\s+HTTP/[\d.]+)?\s*$"
    )

    blocks: list[str] = []
    for raw_block in raw_blocks:
        stripped = raw_block.strip()
        if not stripped:
            continue
        # Also split when multiple raw HTTP requests are separated by blank lines + request line
        sub_parts = re.split(
            r"(?:\r?\n){2,}(?=(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+\S+\s+HTTP/[\d.]+)",
            stripped,
        )
        blocks.extend(p.strip() for p in sub_parts if p.strip())

    requests: list[ParsedRequest] = []
    for block in blocks:
        lines = block.splitlines()
        start_idx = 0
        while start_idx < len(lines) and (
            not lines[start_idx].strip() or lines[start_idx].strip().startswith("#")
        ):
            start_idx += 1
        if start_idx >= len(lines):
            continue

        first_line = lines[start_idx].strip()
        m = req_line_re.match(first_line)
        if not m:
            continue

        method = m.group(1).upper()
        target = m.group(2)

        headers: list[tuple[str, str]] = []
        body_lines: list[str] = []
        in_body = False
        for line in lines[start_idx + 1 :]:
            if not in_body:
                if line.strip() == "":
                    in_body = True
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers.append((k.strip(), v.strip()))
            else:
                body_lines.append(line)

        cleaned = _clean_headers(headers)
        if re.match(r"^https?://", target, re.IGNORECASE):
            url = target
        else:
            host = _get_header(cleaned, "host") or "example.com"
            scheme = "http" if host.startswith(("localhost", "127.0.0.1")) or host.endswith(":80") else "https"
            path = target if target.startswith("/") else f"/{target}"
            url = f"{scheme}://{host}{path}"

        body_str = "\n".join(body_lines).strip()
        body = body_str if body_str else None
        mime = _get_header(cleaned, "content-type")
        if body is not None and not mime and body.lstrip().startswith(("{", "[")):
            mime = "application/json"
            cleaned.append(("Content-Type", mime))

        requests.append(
            ParsedRequest(method=method, url=url, headers=cleaned, body=body, mime_type=mime)
        )

    return requests


def _matches_domain(url: str, target_domain: str | None) -> bool:
    if not target_domain:
        return True
    host = (urlparse(url).hostname or "").lower()
    want = target_domain.lower().lstrip(".")
    return host == want or host.endswith(f".{want}")


def _to_har_entry(req: ParsedRequest, started_at: datetime) -> dict[str, Any]:
    parsed_url = urlparse(req.url)
    query_pairs = parse_qsl(parsed_url.query, keep_blank_values=True)
    headers_list = [{"name": k, "value": v} for k, v in req.headers]
    cookies_list = _parse_cookies(_get_header(req.headers, "cookie"))
    body_bytes = len(req.body.encode("utf-8")) if req.body else 0

    har_request: dict[str, Any] = {
        "method": req.method,
        "url": req.url,
        "httpVersion": "HTTP/1.1",
        "cookies": cookies_list,
        "headers": headers_list,
        "queryString": [{"name": k, "value": v} for k, v in query_pairs],
        "headersSize": -1,
        "bodySize": body_bytes,
    }
    if req.body is not None:
        har_request["postData"] = {
            "mimeType": req.mime_type or "application/json",
            "text": req.body,
        }

    resp_bytes = len(_SYNTHETIC_RESP_TEXT.encode("utf-8"))
    return {
        "startedDateTime": started_at.isoformat(),
        "time": 52,
        "request": har_request,
        "response": {
            "status": 200,
            "statusText": "OK",
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": [{"name": "Content-Type", "value": "application/json"}],
            "content": {
                "size": resp_bytes,
                "mimeType": "application/json",
                "text": _SYNTHETIC_RESP_TEXT,
            },
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": resp_bytes,
        },
        "cache": {},
        "timings": {
            "blocked": 0,
            "dns": 0,
            "connect": 0,
            "send": 1,
            "wait": 50,
            "receive": 1,
            "ssl": 0,
        },
    }


def _detect_format(content: str, format_hint: str) -> str:
    hint = (format_hint or "auto").strip().lower()
    if hint in {"curl", "fetch", "raw_http", "har_json"}:
        return hint

    stripped = content.lstrip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and isinstance(parsed.get("log"), dict):
                return "har_json"
        except json.JSONDecodeError:
            pass

    if re.search(r"(?m)^\s*curl(?:\.exe)?\b", content, re.IGNORECASE):
        return "curl"
    if re.search(r"\bfetch\s*\(\s*['\"`]", content):
        return "fetch"
    if re.search(
        r"(?m)^\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+\S+(?:\s+HTTP/[\d.]+)?\s*$",
        content,
    ):
        return "raw_http"
    return "unknown"


def import_traffic_text(
    content: str,
    out_dir: Path,
    *,
    scenario_name: str = "01_devtools",
    format_hint: str = "auto",
    target_domain: str | None = None,
) -> dict[str, Any]:
    """Parse Chrome DevTools cURL / fetch / Raw HTTP / HAR JSON text into a HAR 1.2 file."""
    out_dir = ensure_dir(Path(out_dir))
    safe_scenario = slugify(scenario_name).replace("-", "_") or "01_devtools"
    detected = _detect_format(content, format_hint)

    if detected == "har_json":
        with tempfile.TemporaryDirectory(prefix="reconqa_har_text_") as tmp_dir:
            tmp_har = Path(tmp_dir) / f"{safe_scenario}.har"
            tmp_har.write_text(content, encoding="utf-8")
            res = import_hars(
                [tmp_har],
                out_dir,
                scenario_name=safe_scenario,
                target_domain=target_domain,
                extract_js=True,
            )
        har_path = out_dir / f"{safe_scenario}.har"
        imported_count = int(res.get("entries_kept", res.get("total_entries", 0)) or 0)
        if imported_count == 0 and har_path.is_file():
            try:
                har_data = json.loads(har_path.read_text(encoding="utf-8"))
                imported_count = len(har_data.get("log", {}).get("entries", []))
            except Exception:
                imported_count = 0
        return {
            "scenario": safe_scenario,
            "detected_format": "har_json",
            "requests_imported": imported_count,
            "har_path": str(har_path) if har_path.is_file() else "",
        }

    parsed_requests: list[ParsedRequest] = []
    if detected == "curl":
        parsed_requests = _parse_curl_text(content)
    elif detected == "fetch":
        parsed_requests = _parse_fetch_text(content)
    elif detected == "raw_http":
        parsed_requests = _parse_raw_http_text(content)

    # Fallback cascade when auto-detect misses mixed formatting
    if not parsed_requests and format_hint == "auto":
        for fmt, fn in (
            ("curl", _parse_curl_text),
            ("fetch", _parse_fetch_text),
            ("raw_http", _parse_raw_http_text),
        ):
            if fmt == detected:
                continue
            cand = fn(content)
            if cand:
                detected = fmt
                parsed_requests = cand
                break

    filtered = [r for r in parsed_requests if _matches_domain(r.url, target_domain)]
    if not filtered:
        return {
            "scenario": safe_scenario,
            "detected_format": detected,
            "requests_imported": 0,
            "har_path": "",
        }

    base_ts = datetime.now(UTC)
    entries = [
        _to_har_entry(req, base_ts + timedelta(milliseconds=idx * 100))
        for idx, req in enumerate(filtered)
    ]

    har_doc = {
        "log": {
            "version": "1.2",
            "creator": {"name": "reconqa-text-importer", "version": "0.1.0"},
            "pages": [],
            "entries": entries,
        }
    }
    har_path = out_dir / f"{safe_scenario}.har"
    write_json(har_path, har_doc)
    return {
        "scenario": safe_scenario,
        "detected_format": detected,
        "requests_imported": len(entries),
        "har_path": str(har_path),
    }
