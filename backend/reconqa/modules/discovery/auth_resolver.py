"""Security-hardened authentication resolution & login exchange for ReconQA exploration."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx

if TYPE_CHECKING:
    from reconqa.models.tables import AuthProfile

_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_BLOCKED_AUTH_HOSTS = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "metadata.google.internal",
        "metadata.goog",
    }
)
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")

_HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
)

_AUTO_TOKEN_PATHS: tuple[str, ...] = (
    "token",
    "access_token",
    "accessToken",
    "jwt",
    "id_token",
    "auth_token",
    "data.token",
    "data.access_token",
    "data.accessToken",
    "data.jwt",
    "result.token",
    "result.access_token",
)

_RESPONSE_TOKEN_HEADERS: tuple[str, ...] = (
    "authorization",
    "x-auth-token",
    "token",
    "accesstoken",
)

_STORAGE_TOKEN_KEYS: tuple[str, ...] = (
    "token",
    "access_token",
    "accessToken",
    "jwt",
    "id_token",
    "auth_token",
    "Authorization",
    "Admin-Token",
)

_SENSITIVE_FIELD_HINTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "api_key",
    "apikey",
    "authorization",
)

_SENSITIVE_AUTH_HEADER_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-auth-token",
        "x-token",
        "x-access-token",
        "accesstoken",
        "token",
        "x-api-key",
        "api-key",
        "apikey",
        "x-csrf-token",
        "x-xsrf-token",
    }
)

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


@dataclass
class ResolvedAuth:
    """Runtime-only resolved authentication material. Never persist raw headers or secrets."""

    headers: dict[str, str] = field(default_factory=dict)
    auth_header_names: list[str] = field(default_factory=list)
    auth_mode: str = "none"
    profile_id: str | None = None
    profile_name: str | None = None
    secret_values: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    login_status_code: int | None = None
    login_succeeded: bool | None = None
    storage_state_data: dict[str, Any] | None = None
    scope_domains: list[str] = field(default_factory=list)
    verify_url: str | None = None
    storage_state_path: str | None = None
    non_storage_headers: dict[str, str] = field(default_factory=dict)
    login_url: str | None = None


_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "accesstoken",
        "auth",
        "sig",
        "signature",
        "key",
        "apikey",
        "api_key",
        "api-key",
        "secret",
        "password",
        "passwd",
        "pwd",
        "code",
        "ticket",
        "session",
        "jwt",
    }
)


def _is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _registrable_root_domain(host: str) -> str:
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


def _is_sensitive_auth_header(header_name: str) -> bool:
    low = (header_name or "").strip().lower()
    if low in _SENSITIVE_AUTH_HEADER_KEYS:
        return True
    return any(hint in low for hint in ("token", "auth", "cookie", "secret", "api-key", "apikey"))


def _host_in_scope(
    host: str,
    allowed_root_domain: str,
    scope_domains: list[str] | None = None,
) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False

    cleaned_scope = [str(d).strip() for d in (scope_domains or ()) if str(d).strip()]
    if cleaned_scope:
        # Strict Burp Scope Isolation: when scope_domains is explicitly provided,
        # it is the SOLE allowlist (exact or subdomain match only; never OR with allowed_root_domain).
        for raw_dom in cleaned_scope:
            dom = raw_dom.lower()
            if "://" in dom:
                dom = (urlparse(dom).hostname or "").strip().lower()
            dom = dom.lstrip("*.").rstrip(".")
            if not dom:
                continue
            if _is_loopback(h) and _is_loopback(dom):
                return True
            if h == dom or h.endswith("." + dom):
                return True
        return False

    allowed = (allowed_root_domain or "").strip().lower().rstrip(".")
    if allowed:
        if "://" in allowed:
            allowed = (urlparse(allowed).hostname or "").strip().lower().rstrip(".")
        allowed = allowed.lstrip("*.")
        if _is_loopback(h) and _is_loopback(allowed):
            return True
        if h == allowed or h.endswith("." + allowed):
            return True
        if not _is_loopback(h) and not _is_ip_literal(h) and not _is_ip_literal(allowed):
            reg_h = _registrable_root_domain(h)
            reg_allowed = _registrable_root_domain(allowed)
            if reg_h and reg_allowed and reg_h == reg_allowed:
                return True

    return False


def headers_for_url(
    url: str,
    headers: dict[str, str],
    allowed_root_domain: str,
    scope_domains: list[str] | None = None,
) -> dict[str, str]:
    """Burp-style Scope Isolation: strip ALL credential headers for out-of-scope hosts."""
    if not headers:
        return {}
    raw = (url or "").strip()
    if not raw:
        return {}
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return {}
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if _host_in_scope(host, allowed_root_domain, scope_domains=scope_domains):
        return dict(headers)
    return {}


def make_scope_request_hook(
    allowed_root_domain: str,
    scope_domains: list[str] | None,
    auth_header_keys: Iterable[str],
) -> Any:
    """Create a sync httpx request event hook enforcing Burp scope isolation on every hop (incl. 3xx redirects)."""
    tracked_keys = {str(k).strip().lower() for k in (auth_header_keys or ()) if str(k).strip()}

    def _scope_hook(request: httpx.Request) -> None:
        if not tracked_keys:
            return
        host = (request.url.host or "").strip().lower().rstrip(".")
        if _host_in_scope(host, allowed_root_domain, scope_domains=scope_domains):
            return
        for hdr_name in list(request.headers.keys()):
            low = hdr_name.lower()
            if low in tracked_keys or _is_sensitive_auth_header(low):
                del request.headers[hdr_name]

    return _scope_hook


def make_async_scope_request_hook(
    allowed_root_domain: str,
    scope_domains: list[str] | None,
    auth_header_keys: Iterable[str],
) -> Any:
    """Create an async httpx request event hook enforcing Burp scope isolation on every hop."""
    sync_hook = make_scope_request_hook(allowed_root_domain, scope_domains, auth_header_keys)

    async def _async_scope_hook(request: httpx.Request) -> None:
        sync_hook(request)

    return _async_scope_hook


def sanitize_url_for_storage(url: str) -> str:
    """Strip user:pass@ userinfo and mask sensitive query parameter values before persisting."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    netloc = parts.netloc
    if "@" in netloc:
        hostport = netloc.rsplit("@", 1)[-1]
        netloc = hostport

    query = parts.query
    if query:
        sanitized_pairs: list[tuple[str, str]] = []
        changed = False
        for k, v in parse_qsl(query, keep_blank_values=True):
            k_low = k.strip().lower()
            is_sens = k_low in _SENSITIVE_QUERY_KEYS or any(
                hint in k_low for hint in ("token", "secret", "password", "passwd", "apikey", "api_key", "api-key")
            )
            if is_sens and v:
                sanitized_pairs.append((k, "[REDACTED]"))
                changed = True
            else:
                sanitized_pairs.append((k, v))
        if changed:
            query = urlencode(sanitized_pairs, doseq=True)

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def load_storage_state_cookies_for_host(
    storage_state_path: str | None,
    target_host: str,
) -> tuple[str, list[str]]:
    """Load Cookie header string and secret values from a Playwright storage_state file filtered by host."""
    if not storage_state_path:
        return "", []
    state_path = Path(storage_state_path)
    if not state_path.is_file():
        return "", []
    clean_host = (target_host or "").strip().lower()
    if clean_host.startswith(("http://", "https://")):
        clean_host = (urlparse(clean_host).hostname or "").lower()
    cookies: list[str] = []
    secrets: list[str] = []
    try:
        state_data = json.loads(state_path.read_text(encoding="utf-8"))
        for ck in state_data.get("cookies") or []:
            if not isinstance(ck, dict):
                continue
            c_name = str(ck.get("name") or "").strip()
            c_val = str(ck.get("value") or "")
            c_dom = str(ck.get("domain") or "").lstrip(".").lower()
            if not c_name:
                continue
            if (
                not clean_host
                or not c_dom
                or clean_host == c_dom
                or clean_host.endswith("." + c_dom)
            ):
                cookies.append(f"{c_name}={c_val}")
                if c_val:
                    secrets.append(c_val)
    except Exception:
        return "", []
    if not cookies:
        return "", []
    cookie_hdr = "; ".join(cookies)
    secrets.append(cookie_hdr)
    return cookie_hdr, _dedup_nonempty(secrets)


def _dedup_nonempty(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        s = str(item or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def resolve_env_ref(raw_val: str) -> str:
    """Resolve ENV:RECONQA_* / ${RECONQA_*} references strictly within the RECONQA_ allowlist."""
    val = str(raw_val or "").strip()
    if not val:
        return ""
    m = _ENV_REF_RE.match(val)
    if m:
        env_key = m.group(1)
        return os.environ.get(env_key, "") if env_key.startswith("RECONQA_") else ""
    if val.lower().startswith("env:"):
        env_key = val[4:].strip()
        return os.environ.get(env_key, "") if env_key.startswith("RECONQA_") else ""
    return val


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace any non-empty secret (len >= 4) and base64-encoded variants with [REDACTED]."""
    cleaned = str(text or "")
    if not cleaned:
        return cleaned

    candidates: set[str] = set()
    for raw in secrets or ():
        s = str(raw or "").strip()
        if len(s) < 4:
            continue
        candidates.add(s)
        # Also catch bare token after common prefixes like "Bearer <tok>" or "Basic <b64>"
        for pfx in ("bearer ", "basic ", "token "):
            if s.lower().startswith(pfx):
                inner = s[len(pfx) :].strip()
                if len(inner) >= 4:
                    candidates.add(inner)
        # Base64-encoded variant of the secret
        try:
            b64 = base64.b64encode(s.encode("utf-8")).decode("ascii")
            if len(b64) >= 4:
                candidates.add(b64)
        except Exception:
            pass

    if not candidates:
        return cleaned

    for secret in sorted(candidates, key=len, reverse=True):
        if secret in cleaned:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def is_unsafe_auth_url(url: str) -> bool:
    """Return True if URL uses non-http(s) scheme, cloud metadata host, or link-local IP."""
    raw = (url or "").strip()
    if not raw:
        return True
    try:
        parsed = urlparse(raw)
    except ValueError:
        return True
    if parsed.scheme.lower() not in {"http", "https"}:
        return True
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if _is_loopback(host):
        return False
    if host in _BLOCKED_AUTH_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv4Address) and ip in _LINK_LOCAL_V4:
            return True
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            if ip.ipv4_mapped in _LINK_LOCAL_V4 or str(ip.ipv4_mapped) in _BLOCKED_AUTH_HOSTS:
                return True
        if ip.is_link_local:
            return True
    except ValueError:
        pass
    return False


def _safe_auth_redirect_hook(response: httpx.Response) -> None:
    if 300 <= response.status_code < 400:
        location = response.headers.get("location")
        if location:
            resolved = urljoin(str(response.url), location.strip())
            if is_unsafe_auth_url(resolved):
                raise httpx.RequestError(
                    "Blocked unsafe redirect during authentication.",
                    request=response.request,
                )


def extract_json_path(data: Any, path: str) -> str | None:
    """Extract a scalar string value via dot path (e.g. 'data.token', '$.data.accessToken')."""
    if data is None or not path:
        return None
    cleaned = str(path).strip()
    if cleaned.startswith("$."):
        cleaned = cleaned[2:]
    elif cleaned.startswith("$"):
        cleaned = cleaned[1:]
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        return None

    cur: Any = data
    # Support optional bracket indices like items[0].token
    parts = [p for p in re.split(r"\.|\[(\d+)\]", cleaned) if p is not None and p != ""]
    for part in parts:
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return None
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            return None

    if isinstance(cur, bool) or cur is None:
        return None
    if isinstance(cur, (str, int, float)):
        val = str(cur).strip()
        return val if val else None
    return None


def auto_extract_token(data: Any) -> str | None:
    """Attempt to extract an auth token from common JSON response structures."""
    if not isinstance(data, dict):
        return None
    for path in _AUTO_TOKEN_PATHS:
        val = extract_json_path(data, path)
        if val:
            return val
    raw_data = data.get("data")
    if isinstance(raw_data, str) and raw_data.strip():
        return raw_data.strip()
    return None


def _apply_token_prefix(token: str, prefix: str) -> str:
    tok = token.strip()
    if not prefix:
        return tok
    if tok.lower().startswith(prefix.lower()):
        return tok
    return f"{prefix}{tok}"


def _format_basic_auth(username: str, password: str) -> tuple[str, list[str]]:
    raw = f"{username}:{password}".encode()
    b64 = base64.b64encode(raw).decode("ascii")
    header_val = f"Basic {b64}"
    secrets = _dedup_nonempty([password, b64, header_val])
    return header_val, secrets


def execute_login_api(config: dict[str, Any], *, timeout_sec: float = 15.0) -> ResolvedAuth:
    """Exchange username/password via an HTTP Login API for a runtime token and/or session cookies."""
    cfg = dict(config or {})
    login_url = str(cfg.get("login_url") or "").strip()
    if is_unsafe_auth_url(login_url):
        return ResolvedAuth(
            auth_mode="login_api",
            login_succeeded=False,
            warnings=["Login API URL is missing or blocked by security policy."],
        )

    username = resolve_env_ref(str(cfg.get("username") or ""))
    password = resolve_env_ref(str(cfg.get("password") or ""))
    username_field = str(cfg.get("username_field") or "username").strip() or "username"
    password_field = str(cfg.get("password_field") or "password").strip() or "password"
    method = str(cfg.get("login_method") or cfg.get("method") or "POST").strip().upper() or "POST"
    content_type = (
        str(cfg.get("login_content_type") or cfg.get("content_type") or "json").strip().lower()
    )
    token_header = str(cfg.get("token_header") or "Authorization").strip() or "Authorization"
    token_prefix = "Bearer " if cfg.get("token_prefix") is None else str(cfg.get("token_prefix"))
    token_jsonpath = str(cfg.get("token_jsonpath") or "").strip()

    secret_values: list[str] = []
    if password:
        secret_values.append(password)

    payload: dict[str, Any] = {
        username_field: username,
        password_field: password,
    }
    extra_fields = cfg.get("extra_fields")
    if isinstance(extra_fields, dict):
        for ek, ev in extra_fields.items():
            key_str = str(ek).strip()
            if not key_str:
                continue
            if isinstance(ev, str):
                resolved_ev = resolve_env_ref(ev)
                payload[key_str] = resolved_ev
                if any(h in key_str.lower() for h in _SENSITIVE_FIELD_HINTS) and resolved_ev:
                    secret_values.append(resolved_ev)
            else:
                payload[key_str] = ev

    host = (urlparse(login_url).hostname or "").lower()
    req_kwargs: dict[str, Any] = {}
    if content_type == "form":
        req_kwargs["data"] = payload
    else:
        req_kwargs["json"] = payload

    def _login_redirect_guard(response: httpx.Response) -> None:
        _safe_auth_redirect_hook(response)
        if response.status_code in {307, 308}:
            location = response.headers.get("location")
            if location:
                resolved = urljoin(str(response.url), location.strip())
                dest_host = (urlparse(resolved).hostname or "").lower()
                if dest_host and dest_host != host:
                    raise httpx.RequestError(
                        "Blocked cross-host redirect preserving login credentials.",
                        request=response.request,
                    )

    try:
        with httpx.Client(
            verify=False,
            follow_redirects=True,
            trust_env=not _is_loopback(host),
            timeout=timeout_sec,
            event_hooks={"response": [_login_redirect_guard]},
        ) as client:
            resp = client.request(method, login_url, **req_kwargs)
            all_cookies: dict[str, str] = {}
            for k, v in client.cookies.items():
                if k:
                    all_cookies[str(k)] = str(v)
            for k, v in resp.cookies.items():
                if k:
                    all_cookies[str(k)] = str(v)
    except httpx.HTTPError as exc:
        return ResolvedAuth(
            auth_mode="login_api",
            secret_values=_dedup_nonempty(secret_values),
            warnings=[redact_secrets(f"Login API request failed: {exc}", secret_values)],
            login_succeeded=False,
        )

    headers: dict[str, str] = {}
    warnings: list[str] = []

    if all_cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items())
        headers["Cookie"] = cookie_str
        secret_values.append(cookie_str)
        secret_values.extend(v for v in all_cookies.values() if v)

    extracted_token: str | None = None
    try:
        body_json = resp.json()
    except Exception:
        body_json = None

    if body_json is not None:
        if token_jsonpath:
            extracted_token = extract_json_path(body_json, token_jsonpath)
        if not extracted_token:
            extracted_token = auto_extract_token(body_json)

    if not extracted_token:
        resp_hdr_lower = {k.lower(): v for k, v in resp.headers.items()}
        for cand_hdr in _RESPONSE_TOKEN_HEADERS:
            cand_val = (resp_hdr_lower.get(cand_hdr) or "").strip()
            if cand_val:
                extracted_token = cand_val
                break

    if extracted_token:
        secret_values.append(extracted_token)
        header_val = _apply_token_prefix(extracted_token, token_prefix)
        headers[token_header] = header_val
        secret_values.append(header_val)

    if resp.status_code >= 400 or not headers:
        if resp.status_code >= 400:
            warnings.append(
                redact_secrets(
                    f"Login API returned HTTP {resp.status_code}; authentication did not succeed.",
                    secret_values,
                )
            )
            headers = {}
        else:
            warnings.append(
                redact_secrets(
                    f"Login API returned HTTP {resp.status_code}, but neither a token nor a session cookie was found.",
                    secret_values,
                )
            )
        return ResolvedAuth(
            headers=headers,
            auth_header_names=sorted(headers.keys()),
            auth_mode="login_api",
            secret_values=_dedup_nonempty(secret_values),
            warnings=warnings,
            login_status_code=resp.status_code,
            login_succeeded=False,
        )

    return ResolvedAuth(
        headers=headers,
        auth_header_names=sorted(headers.keys()),
        auth_mode="login_api",
        secret_values=_dedup_nonempty(secret_values),
        warnings=warnings,
        login_status_code=resp.status_code,
        login_succeeded=True,
    )


def execute_form_login(config: dict[str, Any], *, timeout_sec: float = 25.0) -> ResolvedAuth:
    """Perform a headless Playwright browser form login (stops immediately on CAPTCHA/MFA)."""
    cfg = dict(config or {})
    login_url = str(cfg.get("login_url") or "").strip()
    if is_unsafe_auth_url(login_url):
        return ResolvedAuth(
            auth_mode="form_login",
            login_succeeded=False,
            warnings=["Form login URL is missing or blocked by security policy."],
        )

    username = resolve_env_ref(str(cfg.get("username") or ""))
    password = resolve_env_ref(str(cfg.get("password") or ""))
    secret_values: list[str] = [password] if password else []

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        return ResolvedAuth(
            auth_mode="form_login",
            secret_values=_dedup_nonempty(secret_values),
            warnings=["Playwright is not installed; use Login API or Token/Cookie auth instead."],
            login_succeeded=False,
        )

    from reconqa.modules.recorder.playwright_rec import _is_challenge_page

    username_selector = (
        str(cfg.get("username_selector") or "").strip()
        or "input[type='email'], input[name*='user'], input[name*='account'], input[name*='phone'], input[type='text']"
    )
    password_selector = (
        str(cfg.get("password_selector") or "").strip() or "input[type='password']"
    )
    submit_selector = (
        str(cfg.get("submit_selector") or "").strip()
        or "button[type='submit'], input[type='submit']"
    )
    token_header = str(cfg.get("token_header") or "Authorization").strip() or "Authorization"
    token_prefix = "Bearer " if cfg.get("token_prefix") is None else str(cfg.get("token_prefix"))

    timeout_ms = max(5000, int(timeout_sec * 1000))
    headers: dict[str, str] = {}
    storage_state_data: dict[str, Any] | None = None
    warnings: list[str] = []
    status_code: int | None = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()

                unsafe_redirect_hit = False

                def _route_guard(route: Any) -> None:
                    nonlocal unsafe_redirect_hit
                    req_url = str(getattr(route.request, "url", "") or "")
                    if req_url and not req_url.startswith(("data:", "blob:", "about:")) and is_unsafe_auth_url(req_url):
                        unsafe_redirect_hit = True
                        route.abort("blockedbyclient")
                    else:
                        route.continue_()

                def _resp_redirect_guard(pw_resp: Any) -> None:
                    nonlocal unsafe_redirect_hit
                    try:
                        st = int(getattr(pw_resp, "status", 0) or 0)
                        if 300 <= st < 400:
                            hdrs = getattr(pw_resp, "headers", None) or {}
                            loc = hdrs.get("location") if isinstance(hdrs, dict) else None
                            if loc:
                                resolved = urljoin(str(getattr(pw_resp, "url", login_url)), str(loc).strip())
                                if is_unsafe_auth_url(resolved):
                                    unsafe_redirect_hit = True
                    except Exception:
                        pass

                page.route("**/*", _route_guard)
                page.on("response", _resp_redirect_guard)
                resp = page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
                if resp is not None:
                    status_code = resp.status
                final_page_url = str(getattr(page, "url", "") or "")
                if unsafe_redirect_hit or (
                    final_page_url
                    and not final_page_url.startswith(("data:", "blob:", "about:"))
                    and is_unsafe_auth_url(final_page_url)
                ):
                    return ResolvedAuth(
                        auth_mode="form_login",
                        secret_values=_dedup_nonempty(secret_values),
                        warnings=["Blocked unsafe redirect target during browser form login."],
                        login_status_code=status_code,
                        login_succeeded=False,
                    )

                if _is_challenge_page(page):
                    return ResolvedAuth(
                        auth_mode="form_login",
                        secret_values=_dedup_nonempty(secret_values),
                        warnings=[
                            "CAPTCHA, MFA, or anti-bot verification detected on login page; aborted without bypassing."
                        ],
                        login_status_code=status_code,
                        login_succeeded=False,
                    )

                user_loc = page.locator(username_selector).first
                if user_loc.count() > 0:
                    user_loc.fill(username, timeout=min(5000, timeout_ms))

                pass_loc = page.locator(password_selector).first
                if pass_loc.count() > 0:
                    pass_loc.fill(password, timeout=min(5000, timeout_ms))

                submit_loc = page.locator(submit_selector).first
                if submit_loc.count() > 0:
                    submit_loc.click(timeout=min(5000, timeout_ms))
                elif pass_loc.count() > 0:
                    pass_loc.press("Enter")

                try:
                    page.wait_for_load_state("networkidle", timeout=min(8000, timeout_ms))
                except Exception:
                    pass
                time.sleep(0.8)

                if _is_challenge_page(page):
                    return ResolvedAuth(
                        auth_mode="form_login",
                        secret_values=_dedup_nonempty(secret_values),
                        warnings=[
                            "CAPTCHA, MFA, or anti-bot verification appeared after form submission; aborted without bypassing."
                        ],
                        login_status_code=status_code,
                        login_succeeded=False,
                    )

                cookies = context.cookies()
                cookie_pairs: list[str] = []
                for ck in cookies or []:
                    if not isinstance(ck, dict):
                        continue
                    cname = str(ck.get("name") or "").strip()
                    cval = str(ck.get("value") or "")
                    if cname:
                        cookie_pairs.append(f"{cname}={cval}")
                        if cval:
                            secret_values.append(cval)
                if cookie_pairs:
                    cookie_hdr = "; ".join(cookie_pairs)
                    headers["Cookie"] = cookie_hdr
                    secret_values.append(cookie_hdr)

                storage_token: str | None = None
                try:
                    found_tok = page.evaluate(
                        """(keys) => {
                            for (const store of [window.localStorage, window.sessionStorage]) {
                                if (!store) continue;
                                for (const k of keys) {
                                    const v = store.getItem(k);
                                    if (v && String(v).trim()) return String(v).trim();
                                }
                            }
                            return null;
                        }""",
                        list(_STORAGE_TOKEN_KEYS),
                    )
                    if isinstance(found_tok, str) and found_tok.strip():
                        storage_token = found_tok.strip().strip('"')
                except Exception:
                    storage_token = None

                if storage_token:
                    secret_values.append(storage_token)
                    hdr_val = _apply_token_prefix(storage_token, token_prefix)
                    headers[token_header] = hdr_val
                    secret_values.append(hdr_val)

                state_raw = context.storage_state()
                if isinstance(state_raw, dict):
                    storage_state_data = state_raw
                context.close()
            finally:
                browser.close()
    except Exception as exc:
        return ResolvedAuth(
            auth_mode="form_login",
            secret_values=_dedup_nonempty(secret_values),
            warnings=[redact_secrets(f"Form login failed: {exc}", secret_values)],
            login_status_code=status_code,
            login_succeeded=False,
        )

    if not headers:
        warnings.append("Form login completed but no cookies or storage tokens were captured.")
        return ResolvedAuth(
            headers={},
            auth_header_names=[],
            auth_mode="form_login",
            secret_values=_dedup_nonempty(secret_values),
            warnings=warnings,
            login_status_code=status_code,
            login_succeeded=False,
            storage_state_data=storage_state_data,
        )

    return ResolvedAuth(
        headers=headers,
        auth_header_names=sorted(headers.keys()),
        auth_mode="form_login",
        secret_values=_dedup_nonempty(secret_values),
        warnings=warnings,
        login_status_code=status_code,
        login_succeeded=True,
        storage_state_data=storage_state_data,
    )


def resolve_auth_from_profile(
    profile: AuthProfile,
    target_host: str = "",
    *,
    force_refresh: bool = False,
) -> ResolvedAuth:
    """Resolve runtime HTTP headers & redaction secrets from a persisted AuthProfile."""
    headers: dict[str, str] = {}
    secret_values: list[str] = []
    warnings: list[str] = []
    login_status_code: int | None = None
    login_succeeded: bool | None = None
    storage_state_data: dict[str, Any] | None = None

    clean_host = (target_host or "").strip().lower()
    if clean_host.startswith(("http://", "https://")):
        clean_host = (urlparse(clean_host).hostname or "").lower()

    non_storage_headers: dict[str, str] = {}
    storage_cookie_hdr, storage_secrets = load_storage_state_cookies_for_host(
        profile.storage_state_path, clean_host
    )
    if storage_cookie_hdr:
        headers["Cookie"] = storage_cookie_hdr
        secret_values.extend(storage_secrets)
    elif profile.storage_state_path and not Path(profile.storage_state_path).is_file():
        warnings.append("Configured storage_state file was not found.")

    for k, v in (profile.extra_headers_ref or {}).items():
        h_name = str(k).strip()
        if not h_name or h_name.startswith(":"):
            continue
        resolved_val = resolve_env_ref(str(v))
        if resolved_val:
            headers[h_name] = resolved_val
            non_storage_headers[h_name] = resolved_val
            secret_values.append(resolved_val)

    auth_type = str(getattr(profile, "auth_type", None) or "headers").strip().lower()
    login_cfg = getattr(profile, "login_config", None)
    cfg = dict(login_cfg) if isinstance(login_cfg, dict) else {}

    has_authz = any(k.lower() == "authorization" and v for k, v in headers.items())

    if auth_type == "basic" and (force_refresh or not has_authz):
        uname = resolve_env_ref(str(cfg.get("username") or ""))
        pwd = resolve_env_ref(str(cfg.get("password") or ""))
        if uname or pwd:
            basic_val, basic_secrets = _format_basic_auth(uname, pwd)
            headers["Authorization"] = basic_val
            non_storage_headers["Authorization"] = basic_val
            secret_values.extend(basic_secrets)
    elif auth_type == "bearer" and not headers:
        tok = resolve_env_ref(str(cfg.get("token") or ""))
        if tok:
            tok_hdr = str(cfg.get("token_header") or "Authorization").strip() or "Authorization"
            tok_pfx = "Bearer " if cfg.get("token_prefix") is None else str(cfg.get("token_prefix"))
            hdr_val = _apply_token_prefix(tok, tok_pfx)
            headers[tok_hdr] = hdr_val
            non_storage_headers[tok_hdr] = hdr_val
            secret_values.extend([tok, hdr_val])
    elif auth_type == "cookie" and "Cookie" not in headers:
        ck_raw = resolve_env_ref(str(cfg.get("cookie") or ""))
        if ck_raw.lower().startswith("cookie:"):
            ck_raw = ck_raw[7:].strip()
        if ck_raw:
            headers["Cookie"] = ck_raw
            non_storage_headers["Cookie"] = ck_raw
            secret_values.append(ck_raw)
    elif auth_type in {"login_api", "form_login"} and (force_refresh or not headers):
        if cfg.get("login_url"):
            exchanged = (
                execute_login_api(cfg) if auth_type == "login_api" else execute_form_login(cfg)
            )
            login_status_code = exchanged.login_status_code
            login_succeeded = exchanged.login_succeeded
            storage_state_data = exchanged.storage_state_data
            warnings.extend(exchanged.warnings)
            secret_values.extend(exchanged.secret_values)
            if exchanged.headers:
                lower_map = {k.lower(): k for k in headers}
                ns_lower_map = {k.lower(): k for k in non_storage_headers}
                for hk, hv in exchanged.headers.items():
                    prev = lower_map.get(hk.lower())
                    if prev and prev != hk:
                        headers.pop(prev, None)
                    headers[hk] = hv
                    lower_map[hk.lower()] = hk
                    prev_ns = ns_lower_map.get(hk.lower())
                    if prev_ns and prev_ns != hk:
                        non_storage_headers.pop(prev_ns, None)
                    non_storage_headers[hk] = hv
                    ns_lower_map[hk.lower()] = hk

    raw_scope = cfg.get("scope_domains")
    prof_scope = (
        [str(d).strip() for d in raw_scope if str(d).strip()]
        if isinstance(raw_scope, list)
        else []
    )
    prof_verify_url = str(cfg.get("verify_url") or "").strip() or None
    prof_login_url = str(cfg.get("login_url") or "").strip() or None

    return ResolvedAuth(
        headers=headers,
        auth_header_names=sorted(headers.keys()),
        auth_mode="profile",
        profile_id=profile.id,
        profile_name=profile.name,
        secret_values=_dedup_nonempty(secret_values),
        warnings=[redact_secrets(w, secret_values) for w in warnings],
        login_status_code=login_status_code,
        login_succeeded=login_succeeded,
        storage_state_data=storage_state_data,
        scope_domains=prof_scope,
        verify_url=prof_verify_url,
        storage_state_path=profile.storage_state_path,
        non_storage_headers=non_storage_headers,
        login_url=prof_login_url,
    )


def resolve_explore_auth(
    auth_config: dict[str, Any] | None,
    *,
    profile: AuthProfile | None = None,
    target_host: str = "",
) -> ResolvedAuth:
    """Unified auth resolver supporting all 8 modes for Explore requests."""
    cfg = dict(auth_config or {})
    mode = str(cfg.get("auth_type") or "none").strip().lower()
    raw_scope = cfg.get("scope_domains")
    cfg_scope = (
        [str(d).strip() for d in raw_scope if str(d).strip()]
        if isinstance(raw_scope, list)
        else []
    )
    cfg_verify_url = str(cfg.get("verify_url") or "").strip() or None
    cfg_login_url = str(cfg.get("login_url") or "").strip() or None

    def _attach_meta(res: ResolvedAuth) -> ResolvedAuth:
        if cfg_scope:
            res.scope_domains = list(dict.fromkeys([*res.scope_domains, *cfg_scope]))
        if cfg_verify_url:
            res.verify_url = cfg_verify_url
        if cfg_login_url and not res.login_url:
            res.login_url = cfg_login_url
        if not res.non_storage_headers and res.headers and not res.storage_state_path:
            res.non_storage_headers = dict(res.headers)
        return res

    if mode == "none":
        if profile is not None:
            return _attach_meta(resolve_auth_from_profile(profile, target_host=target_host))
        return _attach_meta(ResolvedAuth(auth_mode="none"))

    if mode == "profile":
        if profile is None:
            return _attach_meta(
                ResolvedAuth(
                    auth_mode="profile",
                    warnings=["Requested auth profile was not found."],
                )
            )
        return _attach_meta(resolve_auth_from_profile(profile, target_host=target_host))

    if mode == "bearer":
        token = resolve_env_ref(str(cfg.get("token") or ""))
        token_header = str(cfg.get("token_header") or "Authorization").strip() or "Authorization"
        token_prefix = "Bearer " if cfg.get("token_prefix") is None else str(cfg.get("token_prefix"))
        if not token:
            return _attach_meta(
                ResolvedAuth(
                    auth_mode="bearer",
                    warnings=["Bearer auth selected, but token is empty or unresolved."],
                )
            )
        hdr_val = _apply_token_prefix(token, token_prefix)
        headers = {token_header: hdr_val}
        return _attach_meta(
            ResolvedAuth(
                headers=headers,
                auth_header_names=sorted(headers.keys()),
                auth_mode="bearer",
                secret_values=_dedup_nonempty([token, hdr_val]),
            )
        )

    if mode == "cookie":
        raw_cookie = resolve_env_ref(str(cfg.get("cookie") or ""))
        if raw_cookie.lower().startswith("cookie:"):
            raw_cookie = raw_cookie[7:].strip()
        if not raw_cookie:
            return _attach_meta(
                ResolvedAuth(
                    auth_mode="cookie",
                    warnings=["Cookie auth selected, but cookie string is empty or unresolved."],
                )
            )
        secrets = [raw_cookie]
        for part in raw_cookie.split(";"):
            if "=" in part:
                _, v = part.split("=", 1)
                if v.strip():
                    secrets.append(v.strip())
        return _attach_meta(
            ResolvedAuth(
                headers={"Cookie": raw_cookie},
                auth_header_names=["Cookie"],
                auth_mode="cookie",
                secret_values=_dedup_nonempty(secrets),
            )
        )

    if mode == "headers":
        raw_headers = cfg.get("headers") or {}
        headers: dict[str, str] = {}
        secrets: list[str] = []
        if isinstance(raw_headers, dict):
            for k, v in raw_headers.items():
                h_name = str(k).strip()
                if not h_name or h_name.startswith(":") or h_name.lower() in _HOP_BY_HOP_HEADERS:
                    continue
                resolved_val = resolve_env_ref(str(v))
                if resolved_val:
                    headers[h_name] = resolved_val
                    secrets.append(resolved_val)
        warnings = (
            []
            if headers
            else ["Custom headers auth selected, but no non-empty headers were provided."]
        )
        return _attach_meta(
            ResolvedAuth(
                headers=headers,
                auth_header_names=sorted(headers.keys()),
                auth_mode="headers",
                secret_values=_dedup_nonempty(secrets),
                warnings=warnings,
            )
        )

    if mode == "basic":
        username = resolve_env_ref(str(cfg.get("username") or ""))
        password = resolve_env_ref(str(cfg.get("password") or ""))
        if not username and not password:
            return _attach_meta(
                ResolvedAuth(
                    auth_mode="basic",
                    warnings=["Basic auth selected, but username and password are empty."],
                )
            )
        basic_val, basic_secrets = _format_basic_auth(username, password)
        return _attach_meta(
            ResolvedAuth(
                headers={"Authorization": basic_val},
                auth_header_names=["Authorization"],
                auth_mode="basic",
                secret_values=basic_secrets,
            )
        )

    if mode == "login_api":
        return _attach_meta(execute_login_api(cfg))

    if mode == "form_login":
        return _attach_meta(execute_form_login(cfg))

    return _attach_meta(
        ResolvedAuth(
            auth_mode="none",
            warnings=[f"Unsupported auth_type '{mode}'; proceeding without authentication."],
        )
    )
