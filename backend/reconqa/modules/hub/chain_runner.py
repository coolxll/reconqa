"""Lightweight, security-hardened API Chain Runner with Checkpoint & Resume."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from reconqa.config import settings
from reconqa.models.schemas import (
    AssertionEvalResult,
    ChainRunOut,
    ChainStepInput,
    ChainStepResult,
    ScenarioAssertion,
)
from reconqa.modules.discovery.auth_resolver import (
    _SENSITIVE_AUTH_HEADER_KEYS,
    _SENSITIVE_QUERY_KEYS,
    headers_for_url,
    load_storage_state_cookies_for_host,
    make_async_scope_request_hook,
    redact_secrets,
    resolve_auth_from_profile,
    sanitize_url_for_storage,
)
from reconqa.modules.hub.assertion_engine import (
    SINGLE_VAR_RE,
    TEMPLATE_VAR_RE,
    evaluate_assertion,
    extract_jsonpath,
    is_allowed_env_dsn_ref,
    is_valid_dynamic_var,
    render_template_str,
    resolve_dynamic_var,
)
from reconqa.utils.io import SENSITIVE_HEADER_NAMES, ensure_dir, read_json, write_json

if TYPE_CHECKING:
    from reconqa.models.tables import AuthProfile

_CHAIN_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")
_SINGLE_BRACE_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DOUBLE_BRACE_VAR_RE = TEMPLATE_VAR_RE
_EXACT_DOUBLE_VAR_RE = SINGLE_VAR_RE
_MAX_BODY_BYTES = 256 * 1024
_MAX_BODY_PREVIEW_CHARS = 1000
_SENSITIVE_HEADER_SET = frozenset(SENSITIVE_HEADER_NAMES | _SENSITIVE_AUTH_HEADER_KEYS)
_SENSITIVE_FIELD_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "cookie",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "access_token",
    "refresh_token",
    "jwt",
)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _chain_runs_dir() -> Path:
    return ensure_dir(settings.data_dir.resolve() / "chain_runs")


def validate_chain_run_id(chain_run_id: str) -> str:
    clean = str(chain_run_id or "").strip()
    if not _CHAIN_RUN_ID_RE.fullmatch(clean):
        raise ValueError("Invalid chain_run_id format.")
    return clean


def _checkpoint_path(chain_run_id: str) -> Path:
    clean = validate_chain_run_id(chain_run_id)
    base_dir = _chain_runs_dir()
    candidate = (base_dir / f"{clean}.json").resolve()
    if candidate.parent != base_dir:
        raise ValueError("Invalid chain_run_id path.")
    return candidate


def _is_sensitive_header_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in _SENSITIVE_HEADER_SET:
        return True
    return any(
        kw in low for kw in ("authorization", "cookie", "token", "secret", "password", "api-key", "apikey")
    )


def _is_sensitive_query_key(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in _SENSITIVE_QUERY_KEYS:
        return True
    return any(
        kw in low for kw in ("token", "secret", "password", "passwd", "pwd", "apikey", "api_key", "api-key")
    )


def _is_sensitive_field_key(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    return any(kw in low for kw in _SENSITIVE_FIELD_KEYWORDS)


def _contains_template(val: Any) -> bool:
    return isinstance(val, str) and bool(_DOUBLE_BRACE_VAR_RE.search(val))


def _redact_recursive(obj: Any, secrets: list[str]) -> Any:
    if not secrets:
        return obj
    if isinstance(obj, str):
        return redact_secrets(obj, secrets)
    if isinstance(obj, dict):
        return {str(k): _redact_recursive(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_recursive(item, secrets) for item in obj]
    return obj


def _mask_sensitive_dict_recursive(obj: Any, secrets: list[str]) -> Any:
    """Mask sensitive dict keys (preserving {{var}} templates) and redact known secret substrings."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key_str = str(k)
            if _is_sensitive_field_key(key_str) and not isinstance(v, (dict, list)):
                if _contains_template(v) or v is None or v == "":
                    out[key_str] = v
                else:
                    out[key_str] = "[REDACTED]"
            else:
                out[key_str] = _mask_sensitive_dict_recursive(v, secrets)
        return out
    if isinstance(obj, list):
        return [_mask_sensitive_dict_recursive(item, secrets) for item in obj]
    if isinstance(obj, str):
        return redact_secrets(obj, secrets) if secrets else obj
    return obj


def _mask_context_vars(ctx: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    """Mask sensitive-named context variables (top-level and nested) while preserving non-secret IDs."""
    out: dict[str, Any] = {}
    for k, v in (ctx or {}).items():
        key_str = str(k)
        if _is_sensitive_field_key(key_str):
            out[key_str] = "[REDACTED]" if v not in (None, "") else v
        else:
            out[key_str] = _mask_sensitive_dict_recursive(v, secrets)
    return out


def _sanitize_step_for_checkpoint(
    step: ChainStepInput,
    secrets: list[str],
) -> dict[str, Any]:
    """Strip sensitive auth headers, sanitize URLs/paths/query/body, and mask non-ENV db_dsn_ref."""
    dumped = step.model_dump(mode="json")
    clean_headers: dict[str, str] = {}
    for hk, hv in (dumped.get("request_headers") or {}).items():
        if _is_sensitive_header_name(str(hk)):
            continue
        clean_headers[str(hk)] = redact_secrets(str(hv), secrets)
    dumped["request_headers"] = clean_headers

    if dumped.get("name"):
        dumped["name"] = redact_secrets(sanitize_url_for_storage(str(dumped["name"])), secrets)
    if dumped.get("path"):
        dumped["path"] = redact_secrets(sanitize_url_for_storage(str(dumped["path"])), secrets)
    if dumped.get("raw_url"):
        dumped["raw_url"] = redact_secrets(sanitize_url_for_storage(str(dumped["raw_url"])), secrets)

    if dumped.get("query_params"):
        sanitized_q: dict[str, str] = {}
        for k, v in dumped["query_params"].items():
            ks, vs = str(k), str(v)
            if _is_sensitive_query_key(ks) and vs and not _contains_template(vs):
                sanitized_q[ks] = "[REDACTED]"
            else:
                sanitized_q[ks] = redact_secrets(vs, secrets)
        dumped["query_params"] = sanitized_q

    if dumped.get("request_body") is not None:
        dumped["request_body"] = _mask_sensitive_dict_recursive(dumped["request_body"], secrets)

    return dumped


def _redact_assertion_results(
    results: list[AssertionEvalResult],
    secrets: list[str],
) -> list[AssertionEvalResult]:
    out: list[AssertionEvalResult] = []
    for r in results:
        out.append(
            AssertionEvalResult(
                id=r.id,
                title=redact_secrets(r.title, secrets) if secrets else r.title,
                operator=r.operator,
                passed=r.passed,
                actual=_mask_sensitive_dict_recursive(r.actual, secrets),
                expected=redact_secrets(r.expected, secrets) if secrets else r.expected,
                message=redact_secrets(r.message, secrets) if secrets else r.message,
            )
        )
    return out


def save_checkpoint(data: dict[str, Any]) -> Path:
    path = _checkpoint_path(str(data["chain_run_id"]))
    return write_json(path, data)


def load_checkpoint(chain_run_id: str) -> dict[str, Any] | None:
    path = _checkpoint_path(chain_run_id)
    if not path.is_file():
        return None
    raw = read_json(path)
    return raw if isinstance(raw, dict) else None


def checkpoint_to_out(data: dict[str, Any]) -> ChainRunOut:
    public = {k: v for k, v in data.items() if not str(k).startswith("_")}
    return ChainRunOut.model_validate(public)


def list_checkpoints(
    project_id: str,
    *,
    run_id: str | None = None,
    batch_id: str | None = None,
    limit: int = 50,
) -> list[ChainRunOut]:
    base_dir = _chain_runs_dir()
    items: list[ChainRunOut] = []
    for path in base_dir.glob("*.json"):
        if not path.is_file():
            continue
        try:
            raw = read_json(path)
            if not isinstance(raw, dict):
                continue
            if raw.get("project_id") != project_id:
                continue
            if run_id and raw.get("run_id") != run_id:
                continue
            if batch_id and raw.get("batch_id") != batch_id:
                continue
            items.append(checkpoint_to_out(raw))
        except Exception:
            continue
    items.sort(key=lambda item: (item.updated_at or item.created_at or ""), reverse=True)
    return items[: max(1, min(limit, 200))]


# --- Template resolution & unresolved variable detection ---


def _render_path_str(raw: str, context_vars: dict[str, Any]) -> str:
    """Resolve both {{var}} and single-brace {var} path templates when var is in context_vars."""
    rendered = render_template_str(raw, context_vars)
    if not context_vars or "{" not in rendered:
        return rendered

    def _repl_single(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context_vars and context_vars[key] is not None:
            return str(context_vars[key])
        return match.group(0)

    return _SINGLE_BRACE_VAR_RE.sub(_repl_single, rendered)


def _render_body_value(value: Any, context_vars: dict[str, Any]) -> Any:
    if isinstance(value, str):
        exact = _EXACT_DOUBLE_VAR_RE.match(value.strip())
        if exact:
            key = exact.group(1).strip()
            if key in context_vars:
                return context_vars[key]
            ok, dyn_val = resolve_dynamic_var(key)
            if ok:
                return dyn_val
        return render_template_str(value, context_vars)
    if isinstance(value, dict):
        return {str(k): _render_body_value(v, context_vars) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_body_value(item, context_vars) for item in value]
    return value


def _find_unresolved_vars(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, str):
        for m in _DOUBLE_BRACE_VAR_RE.finditer(obj):
            token = m.group(1).strip()
            if is_valid_dynamic_var(token):
                continue
            found.add(token)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.update(_find_unresolved_vars(v))
    elif isinstance(obj, list):
        for item in obj:
            found.update(_find_unresolved_vars(item))
    return found


def _strip_url_fragment(raw_url: str) -> str:
    return str(raw_url or "").split("#", 1)[0]


def _origin_of_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _build_step_url(
    step: ChainStepInput,
    base_url: str,
    context_vars: dict[str, Any],
    *,
    rebase_raw_url: bool = False,
) -> str:
    raw_url = _strip_url_fragment((step.raw_url or "").strip())
    path = _strip_url_fragment((step.path or "").strip())
    clean_base = (base_url or "").strip().rstrip("/")
    has_path_template = "{" in path
    has_query_params = bool(step.query_params)

    if path.startswith(("http://", "https://")):
        return _strip_url_fragment(_render_path_str(path, context_vars))

    # When step.path is templated, query_params exist, or explicit rebase was requested,
    # always build from origin + rendered path (stripping stale recorded query strings & IDs).
    if has_path_template or has_query_params or (rebase_raw_url and clean_base):
        origin = clean_base if (rebase_raw_url and clean_base) else (_origin_of_url(raw_url) or clean_base)
        rel_path = path
        if (not rel_path or rel_path == "/") and not has_path_template and raw_url.startswith(("http://", "https://")):
            rel_path = urlparse(raw_url).path or "/"
        rendered_path = _strip_url_fragment(_render_path_str(rel_path or "/", context_vars))
        if not rendered_path.startswith("/"):
            rendered_path = f"/{rendered_path}"
        if origin:
            return f"{origin.rstrip('/')}{rendered_path}"
        return rendered_path

    if raw_url.startswith(("http://", "https://")):
        return _strip_url_fragment(_render_path_str(raw_url, context_vars))

    if clean_base:
        rendered_path = _strip_url_fragment(_render_path_str(path or "/", context_vars))
        if not rendered_path.startswith("/"):
            rendered_path = f"/{rendered_path}"
        return f"{clean_base}{rendered_path}"

    return _strip_url_fragment(_render_path_str(raw_url or path, context_vars))


def _build_step_virtual_path(step: ChainStepInput, secrets: list[str]) -> str:
    """Persist sanitized step.path (preserving GraphQL #OperationName fragments)."""
    raw_vp = (step.path or "").strip()
    if (not raw_vp or raw_vp == "/") and step.raw_url:
        parsed = urlparse(step.raw_url.strip())
        vp_path = parsed.path or "/"
        vp_frag = (parsed.fragment or "").strip()
        raw_vp = f"{vp_path}#{vp_frag}" if vp_frag else vp_path
    if not raw_vp:
        return ""
    return redact_secrets(sanitize_url_for_storage(raw_vp), secrets)


async def execute_chain(
    *,
    project_id: str,
    chain_name: str,
    steps: list[ChainStepInput],
    base_url: str = "",
    rebase_raw_url: bool = False,
    run_id: str | None = None,
    chain_index: int | None = None,
    batch_id: str | None = None,
    credential_id: str | None = None,
    profile: AuthProfile | None = None,
    scope_domains: list[str] | None = None,
    initial_vars: dict[str, Any] | None = None,
    db_dsn_ref: str | None = None,
    stop_on_failure: bool = True,
    timeout_sec: float = 15.0,
    start_index: int = 0,
    prior_step_results: list[ChainStepResult] | None = None,
    chain_run_id: str | None = None,
    created_at: str | None = None,
    resumed_from_index: int | None = None,
) -> ChainRunOut:
    """Execute a multi-step API chain with variable extraction, deep assertions, and checkpointing."""
    from reconqa.server.routers.projects import (
        _should_bypass_env_proxy,
        _validate_replay_target,
    )

    if not steps:
        raise ValueError("Chain must contain at least one step.")

    eff_chain_run_id = validate_chain_run_id(chain_run_id or f"cr_{uuid.uuid4().hex[:14]}")
    eff_created_at = created_at or _utcnow_iso()
    clamped_timeout = max(1.0, min(60.0, float(timeout_sec or 15.0)))
    context_vars: dict[str, Any] = dict(initial_vars or {})

    # Determine base host & resolve optional AuthProfile in worker thread (asyncio-safe)
    base_host = (urlparse(base_url).hostname or "").strip().lower() if base_url else ""
    if not base_host:
        for st in steps:
            cand = st.raw_url or st.path
            if cand.startswith(("http://", "https://")):
                base_host = (urlparse(cand).hostname or "").strip().lower()
                if base_host:
                    break

    base_auth_headers: dict[str, str] = {}
    storage_state_path: str | None = None
    secret_values: list[str] = []
    strict_profile_scope: list[str] = []
    fallback_project_scope: list[str] = list(scope_domains or [])

    if profile is not None:
        resolved_auth = await asyncio.to_thread(
            resolve_auth_from_profile, profile, target_host=base_host
        )
        storage_state_path = resolved_auth.storage_state_path
        # Separate non-storage headers so storage_state cookies are filtered strictly per step host
        if storage_state_path:
            base_auth_headers = dict(resolved_auth.non_storage_headers)
        else:
            base_auth_headers = dict(resolved_auth.headers)
        secret_values.extend(resolved_auth.secret_values)
        if resolved_auth.scope_domains:
            strict_profile_scope = [str(d).strip() for d in resolved_auth.scope_domains if str(d).strip()]

    step_results: list[ChainStepResult] = (
        list(prior_step_results[:start_index]) if prior_step_results else []
    )
    failed_step_index: int | None = None

    for idx in range(start_index, len(steps)):
        st = steps[idx]
        planned_url = _build_step_url(
            st,
            base_url,
            context_vars,
            rebase_raw_url=rebase_raw_url,
        )

        if failed_step_index is not None and stop_on_failure:
            step_results.append(
                ChainStepResult(
                    step_index=idx,
                    step=st.step,
                    name=st.name,
                    method=(st.method or "GET").strip().upper(),
                    url=redact_secrets(sanitize_url_for_storage(planned_url), secret_values),
                    virtual_path=_build_step_virtual_path(st, secret_values),
                    status="skipped",
                )
            )
            continue

        # Security validation (SSRF / cloud metadata / http(s) check)
        method, resolved_url, target_host = _validate_replay_target(st.method, planned_url)

        # Build per-step auth headers with strict Burp Scope Isolation & per-host storage_state cookies
        step_auth_candidates: dict[str, str] = dict(base_auth_headers)
        if storage_state_path:
            host_cookie_hdr, host_cookie_secrets = load_storage_state_cookies_for_host(
                storage_state_path, target_host
            )
            if host_cookie_secrets:
                secret_values.extend(host_cookie_secrets)
            if host_cookie_hdr and not any(k.lower() == "cookie" for k in step_auth_candidates):
                step_auth_candidates["Cookie"] = host_cookie_hdr

        if strict_profile_scope:
            eff_allowed_root = ""
            eff_scope_list = strict_profile_scope
        else:
            eff_allowed_root = base_host or target_host
            eff_scope_list = fallback_project_scope

        scoped_auth = (
            headers_for_url(
                resolved_url,
                step_auth_candidates,
                allowed_root_domain=eff_allowed_root,
                scope_domains=eff_scope_list,
            )
            if step_auth_candidates
            else {}
        )

        merged_headers: dict[str, str] = dict(scoped_auth)
        lower_to_key = {k.lower(): k for k in merged_headers}
        for hk, hv in (st.request_headers or {}).items():
            clean_k = str(hk).strip()
            if not clean_k or clean_k.startswith(":"):
                continue
            rendered_hv = render_template_str(str(hv), context_vars)
            if _is_sensitive_header_name(clean_k) and rendered_hv.strip() and not _contains_template(rendered_hv):
                secret_values.append(rendered_hv.strip())
            prev_k = lower_to_key.get(clean_k.lower())
            if prev_k and prev_k != clean_k:
                merged_headers.pop(prev_k, None)
            merged_headers[clean_k] = rendered_hv
            lower_to_key[clean_k.lower()] = clean_k

        rendered_query = {
            str(k): render_template_str(str(v), context_vars)
            for k, v in (st.query_params or {}).items()
        }
        rendered_body = (
            _render_body_value(st.request_body, context_vars)
            if st.request_body is not None
            else None
        )

        # Fail step cleanly before network I/O if any {{var_name}} template remained unresolved
        missing_vars: set[str] = set()
        missing_vars.update(_find_unresolved_vars(resolved_url))
        missing_vars.update(_find_unresolved_vars(rendered_query))
        missing_vars.update(_find_unresolved_vars(merged_headers))
        if rendered_body is not None:
            missing_vars.update(_find_unresolved_vars(rendered_body))

        if missing_vars:
            err_msg = f"Missing required chain variable(s): {', '.join(sorted(missing_vars))}"
            if failed_step_index is None:
                failed_step_index = idx
            step_results.append(
                ChainStepResult(
                    step_index=idx,
                    step=st.step,
                    name=st.name,
                    method=method,
                    url=redact_secrets(sanitize_url_for_storage(resolved_url), secret_values),
                    virtual_path=_build_step_virtual_path(st, secret_values),
                    status="failed",
                    status_code=0,
                    duration_ms=0.0,
                    extracted_vars={},
                    assertions=[],
                    body_preview="",
                    error=redact_secrets(err_msg, secret_values),
                )
            )
            continue

        req_kwargs: dict[str, Any] = {
            "headers": merged_headers,
            "params": rendered_query or None,
        }
        if rendered_body is not None and method not in {"GET", "HEAD"}:
            if isinstance(rendered_body, (dict, list)):
                req_kwargs["json"] = rendered_body
            elif isinstance(rendered_body, str):
                req_kwargs["content"] = rendered_body.encode("utf-8")
            else:
                req_kwargs["json"] = rendered_body

        event_hooks = None
        if step_auth_candidates:
            event_hooks = {
                "request": [
                    make_async_scope_request_hook(
                        allowed_root_domain=eff_allowed_root,
                        scope_domains=eff_scope_list,
                        auth_header_keys=step_auth_candidates.keys(),
                    )
                ]
            }

        trust_env = not _should_bypass_env_proxy(target_host)
        t0 = time.perf_counter()
        status_code = 0
        resp_headers: dict[str, str] = {}
        body_text = ""
        body_json: Any = None
        error_msg: str | None = None

        try:
            async with httpx.AsyncClient(
                verify=False,
                follow_redirects=False,
                timeout=clamped_timeout,
                trust_env=trust_env,
                event_hooks=event_hooks,
            ) as client:
                resp = await client.request(method, resolved_url, **req_kwargs)
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            status_code = resp.status_code
            resp_headers = {k: v for k, v in resp.headers.items()}
            raw_bytes = resp.content or b""
            sliced = raw_bytes[:_MAX_BODY_BYTES]
            body_text = sliced.decode("utf-8", errors="replace")
            content_type = resp.headers.get("content-type", "")
            if len(raw_bytes) <= _MAX_BODY_BYTES and body_text.strip():
                if "json" in content_type.lower() or body_text.lstrip().startswith(("{", "[")):
                    try:
                        body_json = json.loads(body_text)
                    except Exception:
                        body_json = None
        except httpx.HTTPError as exc:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            error_msg = str(exc)

        # Variable extraction (on 2xx or matching expected_status when JSON present)
        extracted_vars: dict[str, Any] = {}
        if error_msg is None and body_json is not None and (
            200 <= status_code < 300
            or (st.expected_status is not None and status_code == st.expected_status)
        ):
            for var_name, jp in (st.extracts or {}).items():
                clean_var = str(var_name).strip()
                if not clean_var:
                    continue
                found, val = extract_jsonpath(body_json, str(jp))
                if found:
                    extracted_vars[clean_var] = val
                    context_vars[clean_var] = val
                    if _is_sensitive_field_key(clean_var) and val not in (None, ""):
                        secret_values.append(str(val))

        # Build & evaluate assertions (offloaded to thread pool so SQL queries never block event loop)
        step_assertions: list[ScenarioAssertion] = []
        has_explicit_status = any(a.operator == "status_code" for a in (st.assertions or []))
        if st.expected_status is not None and not has_explicit_status:
            step_assertions.append(
                ScenarioAssertion(
                    id=f"status-{idx}",
                    title=f"HTTP status == {st.expected_status}",
                    operator="status_code",
                    expected=str(st.expected_status),
                )
            )
        step_assertions.extend(st.assertions or [])

        eval_results: list[AssertionEvalResult] = []
        if error_msg is None:
            for assertion in step_assertions:
                res_item = await asyncio.to_thread(
                    evaluate_assertion,
                    assertion,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    response_headers=resp_headers,
                    body_json=body_json,
                    body_text=body_text,
                    context_vars=context_vars,
                    db_dsn_ref=db_dsn_ref,
                )
                eval_results.append(res_item)

        if error_msg is not None:
            step_status: str = "failed"
        elif any(not r.passed for r in eval_results):
            step_status = "failed"
            failed_msgs = [r.message for r in eval_results if not r.passed and r.message]
            error_msg = "; ".join(failed_msgs[:3]) or "One or more assertions failed"
        elif not step_assertions and status_code >= 400:
            step_status = "failed"
            error_msg = f"HTTP {status_code}"
        else:
            step_status = "passed"

        if step_status == "failed" and failed_step_index is None:
            failed_step_index = idx

        safe_name = redact_secrets(sanitize_url_for_storage(st.name), secret_values) if st.name else ""
        safe_url = redact_secrets(sanitize_url_for_storage(resolved_url), secret_values)
        safe_virtual_path = _build_step_virtual_path(st, secret_values)
        safe_preview = redact_secrets(body_text[:_MAX_BODY_PREVIEW_CHARS], secret_values)
        safe_error = redact_secrets(error_msg, secret_values) if error_msg else None
        safe_extracted = _mask_context_vars(extracted_vars, secret_values)
        safe_eval_results = _redact_assertion_results(eval_results, secret_values)

        step_results.append(
            ChainStepResult(
                step_index=idx,
                step=st.step,
                name=safe_name,
                method=method,
                url=safe_url,
                virtual_path=safe_virtual_path,
                status=step_status,  # type: ignore[arg-type]
                status_code=status_code,
                duration_ms=duration_ms,
                extracted_vars=safe_extracted,
                assertions=safe_eval_results,
                body_preview=safe_preview,
                error=safe_error,
            )
        )

    final_failed_idx = next(
        (r.step_index for r in step_results if r.status == "failed"),
        None,
    )
    overall_status = "passed" if all(r.status == "passed" for r in step_results) else "failed"
    completed_steps = sum(1 for r in step_results if r.status == "passed")
    now_iso = _utcnow_iso()

    safe_context_vars = _mask_context_vars(context_vars, secret_values)
    safe_base_url = redact_secrets(sanitize_url_for_storage(base_url), secret_values)
    safe_dsn_ref = db_dsn_ref if is_allowed_env_dsn_ref(db_dsn_ref) else None

    out = ChainRunOut(
        chain_run_id=eff_chain_run_id,
        project_id=project_id,
        run_id=run_id,
        chain_name=chain_name,
        chain_index=chain_index,
        batch_id=batch_id,
        credential_id=credential_id,
        db_dsn_ref=safe_dsn_ref,
        status=overall_status,  # type: ignore[arg-type]
        total_steps=len(steps),
        completed_steps=completed_steps,
        failed_step_index=final_failed_idx,
        resumed_from_index=resumed_from_index,
        base_url=safe_base_url,
        context_vars=safe_context_vars,
        step_results=step_results,
        created_at=eff_created_at,
        updated_at=now_iso,
    )

    checkpoint_payload = out.model_dump(mode="json")
    checkpoint_payload["_steps_snapshot"] = [
        _sanitize_step_for_checkpoint(st, secret_values) for st in steps
    ]
    checkpoint_payload["_db_dsn_ref"] = safe_dsn_ref
    checkpoint_payload["_rebase_raw_url"] = rebase_raw_url
    save_checkpoint(checkpoint_payload)
    return out
