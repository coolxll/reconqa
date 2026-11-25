"""HAR 1.2 parser, cleaner, static-asset filter, and semantic path normalizer."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from reconqa.utils.io import read_json

STATIC_EXTENSIONS = {
    ".js",
    ".mjs",
    ".cjs",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".avif",
    ".bmp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".map",
    ".mp4",
    ".mp3",
    ".wav",
    ".webm",
    ".ogg",
    ".wasm",
    ".pdf",
    ".zip",
    ".gz",
}

STATIC_MIME_PREFIXES = (
    "image/",
    "font/",
    "audio/",
    "video/",
    "text/css",
    "application/font",
    "application/wasm",
    "application/octet-stream",
)

STATIC_MIME_SUBSTRINGS = (
    "javascript",
    "ecmascript",
)

AUTH_HEADER_NAMES = {
    "authorization",
    "cookie",
    "x-auth-token",
    "x-api-key",
    "x-access-token",
    "token",
    "access-token",
}

NON_AUTH_COOKIE_EXACT = frozenset(
    {
        "_bl_uid",
        "_gid",
        "_gcl_au",
        "_fbp",
        "_fbc",
        "_clck",
        "_clsk",
        "acw_tc",
        "acw_sc__v2",
        "aliyungf_tc",
        "__cf_bm",
        "cf_clearance",
        "ssuid",
        "gr_user_id",
        "distinct_id",
        "deviceid",
        "device_id",
        "lang",
        "locale",
        "theme",
        "timezone",
        "currency",
    }
)

NON_AUTH_COOKIE_PREFIXES = (
    "_ga",
    "_gat",
    "hm_lvt_",
    "hm_lpvt_",
    "_ym_",
    "sensorsdata",
    "sajssdk_",
    "sa_jssdk_",
    "um_distinctid",
    "cnzzdata",
    "_hj",
    "amplitude_",
    "mp_",
    "intercom-",
    "ajs_",
    "aliyungf_",
    "acw_",
)


def is_non_auth_cookie_name(name: str) -> bool:
    """Return True if a cookie name is a known analytics, RUM, WAF, or UI preference cookie."""
    low = str(name or "").strip().lower()
    if not low:
        return True
    if low in NON_AUTH_COOKIE_EXACT:
        return True
    return any(low.startswith(prefix) for prefix in NON_AUTH_COOKIE_PREFIXES)


def parse_cookie_header_names(cookie_header: str) -> list[str]:
    """Parse cookie names from an HTTP Cookie header string."""
    names: list[str] = []
    for part in str(cookie_header or "").split(";"):
        chunk = part.strip()
        if not chunk:
            continue
        name = chunk.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


def has_session_cookies(cookie_names: Iterable[str]) -> bool:
    """Return True if at least one cookie name looks like an auth/session cookie."""
    return any(not is_non_auth_cookie_name(name) for name in cookie_names)


TELEMETRY_HOST_LABELS = frozenset(
    {
        "bdapi",
        "rum",
        "telemetry",
        "analytics",
        "beacon",
        "buried",
        "spm",
        "log",
        "logs",
        "track",
        "tracking",
        "metrics",
        "monitor",
        "stat",
        "stats",
        "collect",
        "retcode",
        "arms-retcode",
    }
)

TELEMETRY_HOST_SUFFIXES = (
    "-rum",
    "-track",
    "-telemetry",
    "-analytics",
    "-beacon",
    "-log",
    "-logs",
    "-metrics",
)

TELEMETRY_PATH_SEGMENTS = frozenset(
    {
        "telemetry",
        "rum",
        "beacon",
        "analytics",
        "buried",
        "maidian",
        "spm",
        "sensors",
        "matomo",
        "piwik",
        "retcode",
        "sentry",
        "datadog",
    }
)

TELEMETRY_PAIR_PRIMARY = frozenset({"track", "tracking", "metric", "metrics", "monitor"})

TELEMETRY_PAIR_ACTIONS = frozenset(
    {
        "report",
        "newreport",
        "upload",
        "batch",
        "collect",
        "push",
        "send",
        "dot",
        "point",
        "config",
        "getconfig",
        "heartbeat",
        "ping",
        "log",
        "logs",
        "metric",
        "metrics",
        "event",
        "events",
    }
)

TELEMETRY_PAYLOAD_PATH_SEGMENTS = frozenset(
    {
        "track",
        "tracking",
        "report",
        "newreport",
        "log",
        "logs",
        "event",
        "events",
        "metric",
        "metrics",
    }
)

TELEMETRY_PAYLOAD_KEYS = frozenset({"logList", "metricList", "ev_type", "eventList", "bcn"})


def is_telemetry_endpoint(path: str, host: str = "", request_json: Any = None) -> bool:
    """Return True if an endpoint looks like client telemetry, RUM, analytics, or buried tracking."""
    # Rule 1: Telemetry / bigdata subdomain hosts
    raw_host = str(host or "").strip().lower()
    if raw_host:
        if "://" in raw_host:
            raw_host = (urlsplit(raw_host).netloc or "").lower()
        clean_host = raw_host.split("/", 1)[0].split(":", 1)[0]
        first_label = clean_host.split(".", 1)[0] if clean_host else ""
        if first_label in TELEMETRY_HOST_LABELS or first_label.endswith(TELEMETRY_HOST_SUFFIXES):
            return True

    clean_path = str(path or "").split("?", 1)[0].split("#", 1)[0].strip()
    segments = [seg.lower() for seg in clean_path.split("/") if seg]
    if not segments:
        return False
    seg_set = set(segments)

    # Rule 2: Unambiguous telemetry path segments
    if seg_set & TELEMETRY_PATH_SEGMENTS:
        return True

    # Rule 3: track/tracking/metric(s)/monitor paired with a distinct telemetry action segment
    if seg_set & TELEMETRY_PAIR_PRIMARY:
        for idx, seg in enumerate(segments):
            if seg not in TELEMETRY_PAIR_PRIMARY:
                continue
            for other_idx, other_seg in enumerate(segments):
                if other_idx != idx and other_seg in TELEMETRY_PAIR_ACTIONS:
                    return True

    # Rule 4: Telemetry JSON payload signature
    if (
        isinstance(request_json, dict)
        and (seg_set & TELEMETRY_PAYLOAD_PATH_SEGMENTS)
        and (set(request_json.keys()) & TELEMETRY_PAYLOAD_KEYS)
    ):
        return True

    return False

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_ID_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
_INT_RE = re.compile(r"^\d+$")
_PREFIXED_SEP_ID_RE = re.compile(r"^[A-Za-z]{2,12}[-_]([0-9A-Za-z_-]{3,})$")
_PREFIXED_COMPACT_ID_RE = re.compile(r"^[A-Za-z]{2,8}\d{4,}$")
_VERSION_SEG_RE = re.compile(r"^(?:v\d+(?:\.\d+)?|api[-_]?v\d+|v\d+[-_](?:alpha|beta|rc\d*))$", re.IGNORECASE)

_STATIC_TOKENS = {
    "api",
    "rest",
    "graphql",
    "oauth",
    "oauth2",
    "openid",
    "base64",
    "sha256",
    "sha512",
    "md5",
    "utf8",
    "utf-8",
    "html5",
    "http2",
    "web3",
    "i18n",
    "l10n",
    "k8s",
    "b2b",
    "b2c",
    "h5",
    "3d",
}

_GENERIC_PARENT_SEGMENTS = {
    "",
    "api",
    "rest",
    "rpc",
    "gateway",
    "gw",
    "public",
    "private",
    "internal",
    "external",
    "service",
    "services",
    "v1",
    "v2",
    "v3",
    "v4",
}

_IRREGULAR_SINGULARS = {
    "people": "person",
    "children": "child",
    "indices": "index",
    "statuses": "status",
    "addresses": "address",
    "businesses": "business",
    "batches": "batch",
    "matches": "match",
    "boxes": "box",
    "categories": "category",
    "entries": "entry",
    "companies": "company",
    "policies": "policy",
    "histories": "history",
    "inventories": "inventory",
    "deliveries": "delivery",
    "queries": "query",
    "skus": "sku",
    "menus": "menu",
}


@dataclass
class HarEntry:
    """Normalized representation of a single HAR 1.2 HTTP API transaction."""

    index: int
    scenario: str
    started_datetime: str
    timestamp_ms: float
    method: str
    url: str
    scheme: str
    host: str
    raw_path: str
    normalized_path: str
    path_params: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    request_cookies: dict[str, str] = field(default_factory=dict)
    request_mime_type: str = ""
    request_body_raw: str | None = None
    request_json: Any = None
    request_form: dict[str, str] = field(default_factory=dict)
    status: int = 0
    status_text: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    response_headers_list: list[tuple[str, str]] = field(default_factory=list)
    response_mime_type: str = ""
    response_body_raw: str | None = None
    response_json: Any = None
    response_size: int = 0
    time_ms: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)
    ttfb_ms: float = 0.0
    has_auth: bool = False
    graphql_operation_name: str | None = None
    graphql_operation_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "scenario": self.scenario,
            "started_datetime": self.started_datetime,
            "timestamp_ms": self.timestamp_ms,
            "method": self.method,
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "raw_path": self.raw_path,
            "normalized_path": self.normalized_path,
            "path_params": dict(self.path_params),
            "query_params": dict(self.query_params),
            "request_headers": dict(self.request_headers),
            "request_cookies": dict(self.request_cookies),
            "request_mime_type": self.request_mime_type,
            "request_body_raw": self.request_body_raw,
            "request_json": self.request_json,
            "request_form": dict(self.request_form),
            "status": self.status,
            "status_text": self.status_text,
            "response_headers": dict(self.response_headers),
            "response_mime_type": self.response_mime_type,
            "response_body_raw": self.response_body_raw,
            "response_json": self.response_json,
            "response_size": self.response_size,
            "time_ms": self.time_ms,
            "timings": dict(self.timings),
            "ttfb_ms": self.ttfb_ms,
            "has_auth": self.has_auth,
            "graphql_operation_name": self.graphql_operation_name,
            "graphql_operation_type": self.graphql_operation_type,
        }


def _to_camel_case(segment: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", segment) if p]
    if not parts:
        return ""
    first = parts[0].lower()
    rest = [p[0].upper() + p[1:].lower() if p else "" for p in parts[1:]]
    return first + "".join(rest)


def _singularize(word: str) -> str:
    lower = word.lower()
    if lower in _IRREGULAR_SINGULARS:
        return _IRREGULAR_SINGULARS[lower]
    if len(lower) > 3 and lower.endswith("ies"):
        return lower[:-3] + "y"
    if len(lower) > 3 and lower.endswith(("ses", "xes", "zes", "ches", "shes")):
        return lower[:-2]
    if len(lower) > 2 and lower.endswith("s") and not lower.endswith(("ss", "us", "is")):
        return lower[:-1]
    return lower


def _is_dynamic_segment(segment: str) -> bool:
    if not segment:
        return False
    lower = segment.lower()
    if lower in _STATIC_TOKENS or _VERSION_SEG_RE.match(segment):
        return False
    if _INT_RE.match(segment):
        return True
    if _UUID_RE.match(segment):
        return True
    if _HEX_ID_RE.match(segment):
        return True
    sep_match = _PREFIXED_SEP_ID_RE.match(segment)
    if sep_match and any(ch.isdigit() for ch in sep_match.group(1)):
        return True
    return bool(_PREFIXED_COMPACT_ID_RE.match(segment))


def _derive_param_name(prev_segment: str, used: set[str]) -> str:
    cleaned = prev_segment.strip()
    if (
        not cleaned
        or cleaned.startswith("{")
        or cleaned.lower() in _GENERIC_PARENT_SEGMENTS
        or _VERSION_SEG_RE.match(cleaned)
    ):
        base = "id"
    else:
        parts = [p for p in re.split(r"[^A-Za-z0-9]+", cleaned) if p]
        if parts:
            parts[-1] = _singularize(parts[-1])
            camel = _to_camel_case("_".join(parts))
            if camel.lower().endswith("id"):
                base = camel
            else:
                base = f"{camel}Id"
        else:
            base = "id"

    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def normalize_path(path: str) -> tuple[str, dict[str, str]]:
    """Normalize dynamic path segments into semantic OpenAPI placeholders.

    Preserves non-SPA ``#OperationName`` fragments (e.g. ``/graphql#GetOrderList``)
    while stripping query strings and SPA hash routes (``#/dashboard``, ``#!/home``).

    Returns ``(normalized_path, raw_params_map)``, e.g.:
    ``normalize_path("/api/v1/items/9823410")`` ->
    ``("/api/v1/items/{itemId}", {"itemId": "9823410"})``.
    """
    raw_in = (path or "").strip()
    if "#" in raw_in:
        before_frag, after_frag = raw_in.split("#", 1)
        raw = before_frag.split("?", 1)[0].strip()
        frag = after_frag.split("?", 1)[0].strip()
    else:
        raw = raw_in.split("?", 1)[0].strip()
        frag = ""

    if not frag or frag.startswith(("/", "!")):
        frag = ""

    if not raw:
        base_norm = "/"
        return (f"{base_norm}#{frag}" if frag else base_norm), {}
    if not raw.startswith("/"):
        raw = "/" + raw
    raw = re.sub(r"/{2,}", "/", raw)
    if len(raw) > 1 and raw.endswith("/"):
        raw = raw.rstrip("/")

    segments = raw.split("/")[1:]
    normalized_segments: list[str] = []
    raw_params: dict[str, str] = {}
    used_names: set[str] = set()

    for idx, seg in enumerate(segments):
        decoded = unquote(seg)
        if _is_dynamic_segment(decoded):
            prev_seg = ""
            for back in range(idx - 1, -1, -1):
                if not normalized_segments[back].startswith("{"):
                    prev_seg = segments[back]
                    break
            param_name = _derive_param_name(prev_seg, used_names)
            normalized_segments.append(f"{{{param_name}}}")
            raw_params[param_name] = decoded
        else:
            normalized_segments.append(seg)

    base_norm = "/" + "/".join(normalized_segments)
    return (f"{base_norm}#{frag}" if frag else base_norm), raw_params


def _normalize_target_domain(target_domain: str | None) -> str | None:
    if not target_domain:
        return None
    raw = target_domain.strip().lower()
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    else:
        raw = raw.split("/", 1)[0].split(":", 1)[0]
    return raw or None


def _host_matches(host: str, target_domain: str | None) -> bool:
    if not target_domain:
        return True
    norm_host = host.lower().split(":", 1)[0]
    return norm_host == target_domain or norm_host.endswith("." + target_domain)


def _clean_headers(
    raw_headers: list[dict[str, Any]],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Strip HTTP/2 pseudo-headers (:method, :path, :authority, :scheme) cleanly."""
    header_map: dict[str, str] = {}
    header_list: list[tuple[str, str]] = []
    for item in raw_headers or []:
        name = str(item.get("name", "")).strip()
        if not name or name.startswith(":"):
            continue
        value = str(item.get("value", ""))
        header_list.append((name, value))
        header_map[name] = value
    return header_map, header_list


def _clean_timings(raw_timings: dict[str, Any] | None, total_time: Any) -> tuple[dict[str, float], float]:
    keys = ("blocked", "dns", "connect", "ssl", "send", "wait", "receive")
    cleaned: dict[str, float] = {}
    for k in keys:
        val = (raw_timings or {}).get(k, 0)
        try:
            num = float(val)
        except (TypeError, ValueError):
            num = 0.0
        cleaned[k] = round(max(0.0, num), 3)

    try:
        total = float(total_time)
    except (TypeError, ValueError):
        total = 0.0
    if total < 0:
        total = sum(cleaned.values())
    return cleaned, round(max(0.0, total), 3)


def _decode_har_content(content: dict[str, Any]) -> tuple[str | None, int]:
    text = content.get("text")
    encoding = str(content.get("encoding") or "").lower()
    raw_size = content.get("size", 0)
    try:
        size = max(0, int(raw_size))
    except (TypeError, ValueError):
        size = 0

    if text is None:
        return None, size

    text_str = str(text)
    if encoding == "base64":
        try:
            decoded_bytes = base64.b64decode(text_str, validate=False)
            if size == 0:
                size = len(decoded_bytes)
            return decoded_bytes.decode("utf-8", errors="replace"), size
        except (ValueError, TypeError):
            return text_str, size

    if size == 0 and text_str:
        size = len(text_str.encode("utf-8"))
    return text_str, size


def _try_parse_json(text: str | None, mime_type: str = "") -> Any:
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    looks_json = "json" in mime_type.lower() or stripped.startswith(("{", "["))
    if not looks_json:
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_timestamp_ms(started_dt: str) -> float:
    if not started_dt:
        return 0.0
    try:
        dt = datetime.fromisoformat(started_dt)
        return dt.timestamp() * 1000.0
    except ValueError:
        return 0.0


def _is_static_asset(
    raw_path: str,
    response_mime: str,
    response_json: Any,
    ignore_prefixes: list[str],
    request_headers: dict[str, str] | None = None,
    status: int = 200,
) -> bool:
    if response_json is not None:
        return False

    path_lower = raw_path.lower()
    for prefix in ignore_prefixes:
        if prefix and path_lower.startswith(prefix.lower()):
            return True

    suffix = Path(path_lower).suffix
    if suffix in STATIC_EXTENSIONS:
        return True

    mime_lower = response_mime.lower().split(";", 1)[0].strip()
    if any(mime_lower.startswith(p) for p in STATIC_MIME_PREFIXES):
        return True
    if any(sub in mime_lower for sub in STATIC_MIME_SUBSTRINGS):
        return True

    if mime_lower in {"text/html", "application/xhtml+xml"}:
        if path_lower == "/" or suffix in {".html", ".htm", ".xhtml"}:
            return True
        if request_headers:
            lower_hdrs = {k.lower(): str(v).strip().lower() for k, v in request_headers.items()}
            if (
                lower_hdrs.get("sec-fetch-dest") == "document"
                or lower_hdrs.get("sec-fetch-mode") == "navigate"
            ):
                return True
        if 200 <= status < 400:
            return True

    return False


_GQL_COMMENT_RE = re.compile(r"#[^\r\n]*")
_GQL_NAMED_OP_RE = re.compile(
    r"(query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:\([^)]*\))?(?:\s*@[A-Za-z_][A-Za-z0-9_]*(?:\s*\([^)]*\))?)*\s*\{"
)
_GQL_ANON_TYPE_RE = re.compile(
    r"(query|mutation|subscription)\s*"
    r"(?:\([^)]*\))?(?:\s*@[A-Za-z_][A-Za-z0-9_]*(?:\s*\([^)]*\))?)*\s*\{"
)
_GQL_FRAGMENT_HEAD_RE = re.compile(r"fragment\b")


def _sanitize_graphql_op_name(name: str | None) -> str | None:
    raw = str(name or "").strip()
    if not raw:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_").lstrip("/!.")
    return cleaned or None


def _is_graphql_path_or_mime(raw_path: str, req_mime: str) -> bool:
    if "graphql" in (req_mime or "").lower():
        return True
    clean_path = (raw_path or "").split("#", 1)[0].split("?", 1)[0]
    segments = [s.strip().lower() for s in clean_path.split("/") if s.strip()]
    return any(s in {"graphql", "gql"} or s.endswith("graphql") for s in segments)


def _skip_gql_leading_fragments(cleaned: str) -> int:
    """Advance past any leading top-level `fragment ... { ... }` definitions."""
    n = len(cleaned)
    pos = 0
    while pos < n:
        while pos < n and cleaned[pos].isspace():
            pos += 1
        if not _GQL_FRAGMENT_HEAD_RE.match(cleaned, pos):
            break
        depth = 0
        saw_brace = False
        i = pos
        while i < n:
            if cleaned.startswith('"""', i):
                end_block = cleaned.find('"""', i + 3)
                i = n if end_block == -1 else end_block + 3
                continue
            ch = cleaned[i]
            if ch == '"':
                i += 1
                while i < n:
                    if cleaned[i] == "\\":
                        i += 2
                        continue
                    if cleaned[i] == '"':
                        i += 1
                        break
                    i += 1
                continue
            if ch == "{":
                depth += 1
                saw_brace = True
            elif ch == "}" and saw_brace:
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        if not saw_brace or depth != 0:
            break
        pos = i
    while pos < n and cleaned[pos].isspace():
        pos += 1
    return pos


def _extract_op_from_graphql_query(query_text: str) -> tuple[str | None, str | None]:
    cleaned = _GQL_COMMENT_RE.sub("", str(query_text or "")).strip()
    if not cleaned:
        return None, None
    pos = _skip_gql_leading_fragments(cleaned)
    if pos >= len(cleaned):
        return None, None
    named = _GQL_NAMED_OP_RE.match(cleaned, pos)
    if named:
        return _sanitize_graphql_op_name(named.group(2)), named.group(1).lower()
    anon_type = _GQL_ANON_TYPE_RE.match(cleaned, pos)
    if anon_type:
        return None, anon_type.group(1).lower()
    if cleaned.startswith("{", pos):
        return None, "query"
    return None, None


def _extract_graphql_ops_for_request(
    method: str,
    raw_path: str,
    req_mime: str,
    req_body_raw: str | None,
    req_json: Any,
    query_params: dict[str, str],
    resp_json: Any,
) -> list[tuple[str | None, str | None, Any, Any]]:
    on_gql_endpoint = _is_graphql_path_or_mime(raw_path, req_mime)

    # Case A: Batched GraphQL JSON array
    if isinstance(req_json, list) and req_json and all(isinstance(x, dict) for x in req_json):
        has_gql_keys = any(
            "query" in x
            or "operationName" in x
            or (isinstance(x.get("extensions"), dict) and "persistedQuery" in x["extensions"])
            for x in req_json
        )
        has_named_query = any(
            isinstance(x.get("query"), str) and _extract_op_from_graphql_query(x["query"])[0] is not None
            for x in req_json
        )
        if (on_gql_endpoint and has_gql_keys) or has_named_query:
            item_ops: list[tuple[str | None, str | None]] = []
            for item in req_json:
                raw_op = item.get("operationName")
                q_str = item.get("query") if isinstance(item.get("query"), str) else ""
                q_name, q_type = _extract_op_from_graphql_query(q_str) if q_str else (None, None)
                op_name = (
                    _sanitize_graphql_op_name(raw_op)
                    if isinstance(raw_op, str) and raw_op.strip()
                    else q_name
                )
                item_ops.append((op_name, q_type))

            if len(req_json) == 1:
                return [(item_ops[0][0], item_ops[0][1], req_json, resp_json)]

            if any(op_n is not None for op_n, _ in item_ops):
                resp_is_paired_list = isinstance(resp_json, list) and len(resp_json) == len(req_json)
                return [
                    (
                        item_ops[idx][0],
                        item_ops[idx][1],
                        item,
                        resp_json[idx] if resp_is_paired_list else resp_json,
                    )
                    for idx, item in enumerate(req_json)
                ]
            return [(None, item_ops[0][1], req_json, resp_json)]

    # Case B: Single GraphQL JSON object
    if isinstance(req_json, dict):
        q_str = req_json.get("query") if isinstance(req_json.get("query"), str) else ""
        q_name, q_type = _extract_op_from_graphql_query(q_str) if q_str else (None, None)
        has_persisted = isinstance(req_json.get("extensions"), dict) and "persistedQuery" in req_json["extensions"]
        has_gql_keys = "query" in req_json or "operationName" in req_json or has_persisted
        if (on_gql_endpoint and has_gql_keys) or q_name is not None or has_persisted:
            raw_op = req_json.get("operationName")
            op_name = (
                _sanitize_graphql_op_name(raw_op)
                if isinstance(raw_op, str) and raw_op.strip()
                else q_name
            )
            return [(op_name, q_type, req_json, resp_json)]

    # Case C: Raw application/graphql body
    if "graphql" in (req_mime or "").lower() and req_json is None and req_body_raw:
        op_name, op_type = _extract_op_from_graphql_query(req_body_raw)
        return [(op_name, op_type, req_json, resp_json)]

    # Case D: GET GraphQL query params
    if method == "GET" and on_gql_endpoint:
        raw_op = query_params.get("operationName")
        q_str = str(query_params.get("query") or "")
        q_name, q_type = _extract_op_from_graphql_query(q_str) if q_str else (None, None)
        op_name = (
            _sanitize_graphql_op_name(raw_op)
            if isinstance(raw_op, str) and raw_op.strip()
            else q_name
        )
        return [(op_name, q_type, req_json, resp_json)]

    return [(None, None, req_json, resp_json)]


def parse_har_file(
    path: Path,
    scenario: str | None = None,
    target_domain: str | None = None,
    *,
    ignore_path_prefixes: list[str] | None = None,
    include_static: bool = False,
    include_telemetry: bool = False,
) -> list[HarEntry]:
    """Parse a single HAR 1.2 file into cleaned, normalized HarEntry records."""
    har_path = Path(path)
    if not har_path.is_file():
        return []

    raw = read_json(har_path)
    raw_entries = raw.get("log", {}).get("entries", []) if isinstance(raw, dict) else []
    scenario_name = scenario or har_path.stem
    norm_domain = _normalize_target_domain(target_domain)
    ignore_prefixes = list(ignore_path_prefixes or [])

    parsed: list[tuple[float, int, HarEntry]] = []
    for raw_idx, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            continue
        req = item.get("request") or {}
        resp = item.get("response") or {}

        method = str(req.get("method", "GET")).upper().strip()
        if method == "OPTIONS":
            continue

        url = str(req.get("url", "")).strip()
        if not url or url.startswith(("data:", "blob:", "chrome-extension:", "about:")):
            continue

        split = urlsplit(url)
        scheme = split.scheme.lower() or "https"
        host = split.netloc.lower()
        if not host or not _host_matches(host, norm_domain):
            continue

        raw_path = split.path or "/"
        req_headers, _ = _clean_headers(req.get("headers") or [])
        resp_headers, resp_headers_list = _clean_headers(resp.get("headers") or [])

        resp_content = resp.get("content") or {}
        resp_mime = str(
            resp_content.get("mimeType")
            or next(
                (v for k, v in resp_headers.items() if k.lower() == "content-type"),
                "",
            )
        )
        resp_body_raw, resp_size = _decode_har_content(resp_content)
        resp_json = _try_parse_json(resp_body_raw, resp_mime)

        try:
            status_code = int(resp.get("status", 0))
        except (TypeError, ValueError):
            status_code = 0

        if not include_static and _is_static_asset(
            raw_path,
            resp_mime,
            resp_json,
            ignore_prefixes,
            request_headers=req_headers,
            status=status_code,
        ):
            continue

        # Query params (merge URL query string + HAR queryString array)
        query_params: dict[str, str] = {}
        for k, v in parse_qsl(split.query, keep_blank_values=True):
            query_params[k] = v
        for q_item in req.get("queryString") or []:
            if isinstance(q_item, dict) and q_item.get("name"):
                query_params[str(q_item["name"])] = str(q_item.get("value", ""))

        # Request cookies
        req_cookies: dict[str, str] = {}
        for c_item in req.get("cookies") or []:
            if isinstance(c_item, dict) and c_item.get("name"):
                req_cookies[str(c_item["name"])] = str(c_item.get("value", ""))

        # Request body
        post_data = req.get("postData") or {}
        req_mime = str(
            post_data.get("mimeType")
            or next(
                (v for k, v in req_headers.items() if k.lower() == "content-type"),
                "",
            )
        )
        req_body_raw = post_data.get("text")
        if req_body_raw is not None:
            req_body_raw = str(req_body_raw)
        req_json = _try_parse_json(req_body_raw, req_mime)
        if not include_telemetry and is_telemetry_endpoint(raw_path, host, req_json):
            continue
        req_form: dict[str, str] = {}
        if post_data.get("params"):
            for p_item in post_data["params"]:
                if isinstance(p_item, dict) and p_item.get("name"):
                    req_form[str(p_item["name"])] = str(p_item.get("value", ""))
        elif req_body_raw and "application/x-www-form-urlencoded" in req_mime.lower():
            for k, v in parse_qsl(req_body_raw, keep_blank_values=True):
                req_form[k] = v

        timings, total_time = _clean_timings(item.get("timings"), item.get("time", 0))
        started_dt = str(item.get("startedDateTime") or "")
        ts_ms = _parse_timestamp_ms(started_dt)
        base_normalized_path, path_params = normalize_path(raw_path)

        explicit_auth_headers = AUTH_HEADER_NAMES - {"cookie"}
        has_explicit_auth = any(
            k.lower() in explicit_auth_headers and str(v or "").strip() != ""
            for k, v in req_headers.items()
        )
        cookie_names: list[str] = list(req_cookies.keys())
        for k, v in req_headers.items():
            if k.lower() == "cookie":
                cookie_names.extend(parse_cookie_header_names(v))
        has_auth = has_explicit_auth or has_session_cookies(cookie_names)

        gql_ops = _extract_graphql_ops_for_request(
            method,
            raw_path,
            req_mime,
            req_body_raw,
            req_json,
            query_params,
            resp_json,
        )
        for op_name, op_type, entry_req_json, entry_resp_json in gql_ops:
            entry_norm_path = (
                f"{base_normalized_path}#{op_name}" if op_name else base_normalized_path
            )
            entry = HarEntry(
                index=raw_idx,
                scenario=scenario_name,
                started_datetime=started_dt,
                timestamp_ms=ts_ms,
                method=method,
                url=url,
                scheme=scheme,
                host=host,
                raw_path=raw_path,
                normalized_path=entry_norm_path,
                path_params=path_params,
                query_params=query_params,
                request_headers=req_headers,
                request_cookies=req_cookies,
                request_mime_type=req_mime,
                request_body_raw=req_body_raw,
                request_json=entry_req_json,
                request_form=req_form,
                status=status_code,
                status_text=str(resp.get("statusText", "")),
                response_headers=resp_headers,
                response_headers_list=resp_headers_list,
                response_mime_type=resp_mime,
                response_body_raw=resp_body_raw,
                response_json=entry_resp_json,
                response_size=resp_size,
                time_ms=total_time,
                timings=timings,
                ttfb_ms=timings.get("wait", 0.0),
                has_auth=has_auth,
                graphql_operation_name=op_name,
                graphql_operation_type=op_type,
            )
            parsed.append((ts_ms, raw_idx, entry))

    has_timestamps = all(ts > 0.0 for ts, _, _ in parsed) if parsed else False
    if has_timestamps:
        parsed.sort(key=lambda row: (row[0], row[1]))

    entries = [row[2] for row in parsed]
    for seq_idx, entry in enumerate(entries):
        entry.index = seq_idx
    return entries


def parse_har_dir(
    har_dir: Path,
    target_domain: str | None = None,
    *,
    ignore_path_prefixes: list[str] | None = None,
    include_static: bool = False,
    include_telemetry: bool = False,
) -> list[HarEntry]:
    """Parse all *.har files in a directory (or a single HAR file) in chronological order."""
    target = Path(har_dir)
    if not target.exists():
        return []
    if target.is_file():
        return parse_har_file(
            target,
            target_domain=target_domain,
            ignore_path_prefixes=ignore_path_prefixes,
            include_static=include_static,
            include_telemetry=include_telemetry,
        )

    har_files = sorted(p for p in target.glob("*.har") if p.is_file())
    combined: list[tuple[float, int, int, HarEntry]] = []
    for file_idx, har_file in enumerate(har_files):
        file_entries = parse_har_file(
            har_file,
            scenario=har_file.stem,
            target_domain=target_domain,
            ignore_path_prefixes=ignore_path_prefixes,
            include_static=include_static,
            include_telemetry=include_telemetry,
        )
        for entry_idx, entry in enumerate(file_entries):
            combined.append((entry.timestamp_ms, file_idx, entry_idx, entry))

    has_timestamps = all(row[0] > 0.0 for row in combined) if combined else False
    if has_timestamps:
        combined.sort(key=lambda row: (row[0], row[1], row[2]))
    else:
        combined.sort(key=lambda row: (row[1], row[2]))

    result = [row[3] for row in combined]
    for seq_idx, entry in enumerate(result):
        entry.index = seq_idx
    return result
