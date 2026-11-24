"""Chronological data-flow correlation engine with pseudo-dependency filtering and Mermaid output."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reconqa.models.schemas import (
    CorrelationChain,
    CorrelationChainStep,
    CorrelationEndpointRef,
    CorrelationReport,
    CorrelationVariable,
)
from reconqa.modules.hub.har_parser import HarEntry, parse_har_dir
from reconqa.utils.io import SENSITIVE_HEADER_NAMES, ensure_dir, write_json

_COMMON_NOISE_TOKENS = {
    "true",
    "false",
    "null",
    "none",
    "success",
    "succeeded",
    "failed",
    "fail",
    "error",
    "ok",
    "pending",
    "active",
    "inactive",
    "enabled",
    "disabled",
    "default",
    "unknown",
    "normal",
    "deleted",
    "created",
    "updated",
    "completed",
    "processing",
    "utf-8",
    "application/json",
    "bearer",
    "basic",
    "gzip",
    "json",
    "html",
    "text",
    "zh-cn",
    "en-us",
    "http",
    "https",
}

_DATE_RE = re.compile(
    r"^\d{4}[-/]\d{2}[-/]\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)

_ID_SEMANTIC_TOKENS = (
    "id",
    "no",
    "num",
    "token",
    "code",
    "key",
    "sku",
    "order",
    "cart",
    "item",
    "uid",
    "uuid",
    "session",
    "ticket",
    "trace",
    "sn",
    "ref",
    "seq",
)

_CLEANUP_PATH_TOKENS = {
    "delete",
    "remove",
    "cancel",
    "clear",
    "revoke",
    "destroy",
    "undo",
    "drop",
}

_IGNORED_CORRELATION_HEADERS = {
    "accept",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "connection",
    "content-length",
    "content-type",
    "dnt",
    "host",
    "origin",
    "pragma",
    "priority",
    "referer",
    "te",
    "upgrade-insecure-requests",
    "user-agent",
    "x-requested-with",
}

_HIGH_FREQ_THRESHOLD = 0.60

_AUTH_HEADER_NAMES = {
    "authorization",
    "cookie",
    "token",
    "x-token",
    "x-auth-token",
    "x-access-token",
    "access-token",
}

_AUTH_PRODUCER_PATH_RE = re.compile(
    r"(login|signin|sign-in|oauth|token|authenticate|session|sso)",
    re.IGNORECASE,
)


@dataclass
class _ProducerRecord:
    step: int
    scenario: str
    method: str
    path: str
    raw_url: str
    jsonpath: str
    key_name: str
    value: str


@dataclass
class _ConsumerMatch:
    step: int
    scenario: str
    method: str
    path: str
    raw_url: str
    location: str  # "path" | "query" | "header" | "body"
    param: str
    value: str


def _walk_json_scalars(
    obj: Any,
    jsonpath: str = "$",
    key_name: str = "",
) -> Iterator[tuple[str, str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_str = str(k)
            child_path = f"{jsonpath}.{k_str}"
            yield from _walk_json_scalars(v, child_path, k_str)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            child_path = f"{jsonpath}[{idx}]"
            yield from _walk_json_scalars(item, child_path, key_name)
    else:
        yield jsonpath, key_name, obj


def _walk_body_params(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_str = str(k)
            full_key = f"{prefix}.{k_str}" if prefix else k_str
            yield from _walk_body_params(v, full_key)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            full_key = f"{prefix}[{idx}]"
            yield from _walk_body_params(item, full_key)
    else:
        if prefix:
            yield prefix, obj


def _normalize_candidate_value(raw_val: Any) -> str | None:
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, (int, float)):
        val_str = str(raw_val)
    elif isinstance(raw_val, str):
        val_str = raw_val.strip()
    else:
        return None

    if len(val_str) < 4 or len(val_str) > 256:
        return None
    lower = val_str.lower()
    if lower in _COMMON_NOISE_TOKENS:
        return None
    if lower.startswith(("http://", "https://", "//", "/")):
        return None
    if _DATE_RE.match(val_str):
        return None
    if "\n" in val_str or "\r" in val_str:
        return None
    return val_str


def _extract_request_candidates(entry: HarEntry) -> list[tuple[str, str, str]]:
    """Return list of (location, param, normalized_value) across all 4 request dimensions."""
    results: list[tuple[str, str, str]] = []

    for param, raw_val in entry.path_params.items():
        val = _normalize_candidate_value(raw_val)
        if val is not None:
            results.append(("path", param, val))

    for param, raw_val in entry.query_params.items():
        val = _normalize_candidate_value(raw_val)
        if val is not None:
            results.append(("query", param, val))

    for h_name, raw_val in entry.request_headers.items():
        lower = h_name.lower()
        if lower in _IGNORED_CORRELATION_HEADERS or lower.startswith("sec-"):
            continue
        cleaned = raw_val.strip()
        if lower == "authorization" and cleaned.lower().startswith("bearer "):
            cleaned = cleaned[7:].strip()
        val = _normalize_candidate_value(cleaned)
        if val is not None:
            results.append(("header", h_name, val))

    if entry.request_json is not None:
        for param_path, raw_val in _walk_body_params(entry.request_json):
            val = _normalize_candidate_value(raw_val)
            if val is not None:
                results.append(("body", param_path, val))
    elif entry.request_form:
        for param, raw_val in entry.request_form.items():
            val = _normalize_candidate_value(raw_val)
            if val is not None:
                results.append(("body", param, val))

    return results


def _has_id_semantics(key_name: str) -> bool:
    leaf = key_name.split(".")[-1]
    leaf = re.sub(r"\[\d+\]$", "", leaf)
    lower = leaf.lower()
    if lower in _ID_SEMANTIC_TOKENS:
        return True
    parts = [p.lower() for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", leaf)]
    return any(p in _ID_SEMANTIC_TOKENS for p in parts)


def _compute_confidence(
    producer: _ProducerRecord,
    consumers: list[_ConsumerMatch],
) -> float:
    score = 0.55
    prod_semantic = _has_id_semantics(producer.key_name)
    cons_semantic = any(_has_id_semantics(c.param) for c in consumers)
    if prod_semantic or cons_semantic:
        score += 0.20
    if any(c.location == "path" for c in consumers):
        score += 0.10
    prod_leaf = producer.key_name.split(".")[-1].lower()
    if any(
        prod_leaf and (prod_leaf in c.param.lower() or c.param.lower() in prod_leaf)
        for c in consumers
    ):
        score += 0.08
    has_alpha = any(ch.isalpha() for ch in producer.value)
    has_digit = any(ch.isdigit() for ch in producer.value)
    if has_alpha and has_digit:
        score += 0.05
    return round(min(0.99, score), 2)


def _to_var_identifier(raw_name: str) -> str:
    leaf = raw_name.split(".")[-1]
    leaf = re.sub(r"\[\d+\]$", "", leaf)
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", leaf) if p]
    if not parts:
        return "var"
    if len(parts) == 1:
        token = parts[0]
        return token[0].lower() + token[1:] if token else "var"
    first = parts[0].lower()
    rest = [p[0].upper() + p[1:] for p in parts[1:]]
    ident = first + "".join(rest)
    if ident[0].isdigit():
        ident = "v_" + ident
    return ident


def _choose_var_name(
    producer: _ProducerRecord,
    consumers: list[_ConsumerMatch],
    used: set[str],
) -> str:
    prod_ident = _to_var_identifier(producer.key_name)
    # Prefer more specific consumer param name when producer key is generic "id"
    if prod_ident in {"id", "var", "value", "data"}:
        for c in consumers:
            c_ident = _to_var_identifier(c.param)
            if c_ident not in {"id", "var", "value", "data"}:
                prod_ident = c_ident
                break

    candidate = prod_ident
    suffix = 2
    while candidate in used:
        candidate = f"{prod_ident}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _is_cleanup_entry(entry: HarEntry) -> bool:
    if entry.method == "DELETE":
        return True
    segments = [
        tok.lower()
        for seg in entry.normalized_path.strip("/").split("/")
        if seg and not seg.startswith("{")
        for tok in re.split(r"[^A-Za-z0-9]+", seg)
        if tok
    ]
    return any(tok in _CLEANUP_PATH_TOKENS for tok in segments)


def _sanitize_step_headers(headers: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in SENSITIVE_HEADER_NAMES:
            continue
        if lower in _IGNORED_CORRELATION_HEADERS or lower.startswith("sec-"):
            continue
        clean[name] = value
    return clean


def _collect_auth_header_usage(
    entries: list[HarEntry],
) -> tuple[dict[str, int], dict[str, set[int]]]:
    """Track earliest request step and total request coverage for auth header tokens."""
    earliest_step: dict[str, int] = {}
    steps_by_val: dict[str, set[int]] = defaultdict(set)

    for entry in entries:
        for h_name, raw_val in entry.request_headers.items():
            lower = h_name.lower()
            if lower not in _AUTH_HEADER_NAMES:
                continue
            candidates: list[str] = []
            cleaned = raw_val.strip()
            if lower == "authorization" and cleaned.lower().startswith("bearer "):
                candidates.append(cleaned[7:].strip())
            elif lower == "cookie":
                for part in cleaned.split(";"):
                    if "=" in part:
                        _, c_val = part.split("=", 1)
                        candidates.append(c_val.strip())
            candidates.append(cleaned)

            for raw_cand in candidates:
                val = _normalize_candidate_value(raw_cand)
                if val is None:
                    continue
                if val not in earliest_step or entry.index < earliest_step[val]:
                    earliest_step[val] = entry.index
                steps_by_val[val].add(entry.index)

    return earliest_step, steps_by_val


def _find_high_frequency_constants(entries: list[HarEntry]) -> list[str]:
    """Identify high-frequency static constants appearing in >60% of JSON responses."""
    json_entries = [e for e in entries if e.response_json is not None]
    total_json = len(json_entries)
    if total_json < 3:
        return []

    resp_indices_by_val: dict[str, set[int]] = defaultdict(set)
    endpoints_by_val: dict[str, set[tuple[str, str]]] = defaultdict(set)
    unprompted_endpoints_by_val: dict[str, set[tuple[str, str]]] = defaultdict(set)
    scenarios_by_val: dict[str, set[str]] = defaultdict(set)
    json_count_by_scenario: dict[str, int] = defaultdict(int)

    for entry in json_entries:
        json_count_by_scenario[entry.scenario] += 1
        req_vals = {val for _, _, val in _extract_request_candidates(entry)}
        seen_in_entry: set[str] = set()
        for _, _, raw_val in _walk_json_scalars(entry.response_json):
            val = _normalize_candidate_value(raw_val)
            if val is None or val in seen_in_entry:
                continue
            seen_in_entry.add(val)
            resp_indices_by_val[val].add(entry.index)
            ep_key = (entry.method, entry.normalized_path)
            endpoints_by_val[val].add(ep_key)
            scenarios_by_val[val].add(entry.scenario)
            if val not in req_vals:
                unprompted_endpoints_by_val[val].add(ep_key)

    constants: list[str] = []
    for val, indices in sorted(resp_indices_by_val.items()):
        global_ratio = len(indices) / total_json
        unprompted_ep_count = len(unprompted_endpoints_by_val[val])
        distinct_ep_count = len(endpoints_by_val[val])

        # Guard against filtering a real entity ID that was produced once and echoed in detail
        if unprompted_ep_count < 2 and distinct_ep_count < 3:
            continue

        if global_ratio > _HIGH_FREQ_THRESHOLD:
            constants.append(val)
            continue

        # Also catch static constants that dominate (>60%) the scenarios where they appear
        # when returned unprompted across >= 2 distinct endpoints and >= 3 responses.
        if len(indices) >= 3 and unprompted_ep_count >= 2:
            scenario_total = sum(json_count_by_scenario[s] for s in scenarios_by_val[val])
            if scenario_total >= 3 and (len(indices) / scenario_total) > _HIGH_FREQ_THRESHOLD:
                constants.append(val)

    return constants


def _build_mermaid(
    entries_by_step: dict[int, HarEntry],
    variables: list[CorrelationVariable],
    chains: list[CorrelationChain],
) -> str:
    if not variables:
        return 'flowchart LR\n    EMPTY["No dynamic data-flow correlations detected"]\n'

    cleanup_steps = {
        c.cleanup_step.step for c in chains if c.cleanup_step is not None
    }
    involved_steps: set[int] = set()
    for var in variables:
        involved_steps.add(var.producer.step)
        for cons in var.consumers:
            involved_steps.add(cons.step)

    lines = ["flowchart LR"]
    for step in sorted(involved_steps):
        entry = entries_by_step[step]
        label = f"{entry.method} {entry.normalized_path}".replace('"', "'")
        lines.append(f'    S{step}["{label}"]')

    seen_edges: set[tuple[int, int, str]] = set()
    for var in variables:
        p_step = var.producer.step
        jp = var.producer.jsonpath or ""
        for cons in var.consumers:
            c_step = cons.step
            edge_key = (p_step, c_step, var.var_name)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            if c_step in cleanup_steps:
                edge_label = f"cleanup: {var.var_name}".replace('"', "'")
                lines.append(f'    S{p_step} -.->|"{edge_label}"| S{c_step}')
            else:
                edge_label = f"{var.var_name} ({jp})" if jp else var.var_name
                edge_label = edge_label.replace('"', "'")
                lines.append(f'    S{p_step} -->|"{edge_label}"| S{c_step}')

    return "\n".join(lines) + "\n"


def _make_chain_name(steps: list[CorrelationChainStep], idx: int) -> str:
    scenarios = list(dict.fromkeys(s.scenario for s in steps))
    if len(scenarios) == 1:
        base = scenarios[0]
    elif scenarios:
        base = f"{scenarios[0]}_to_{scenarios[-1]}"
    else:
        base = f"chain_{idx + 1}"
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_")
    return cleaned or f"chain_{idx + 1}"


def analyze_correlations(
    har_dir: Path,
    out_dir: Path,
    *,
    target_domain: str | None = None,
) -> dict[str, Any]:
    """Analyze chronological producer->consumer data-flow dependencies across HAR files."""
    entries = parse_har_dir(Path(har_dir), target_domain=target_domain)
    entries_by_step = {e.index: e for e in entries}

    filtered_constants = _find_high_frequency_constants(entries)
    filtered_set = set(filtered_constants)
    earliest_auth_step, auth_steps_by_val = _collect_auth_header_usage(entries)
    total_requests = max(1, len(entries))

    # Chronological producer index: value -> first _ProducerRecord
    producers_by_val: dict[str, _ProducerRecord] = {}
    matches_by_val: dict[str, list[_ConsumerMatch]] = defaultdict(list)

    for entry in entries:
        # 1. Match this request against already-seen upstream producers
        seen_match_keys: set[tuple[str, str, str]] = set()
        for location, param, val in _extract_request_candidates(entry):
            if val in filtered_set:
                continue
            prod = producers_by_val.get(val)
            if prod is None or prod.step >= entry.index:
                continue
            # Skip self-same endpoint repeat (duplicate request echoing its own value)
            if (
                prod.method == entry.method
                and prod.path == entry.normalized_path
                and prod.raw_url == entry.url
            ):
                continue
            m_key = (location, param, val)
            if m_key in seen_match_keys:
                continue
            seen_match_keys.add(m_key)
            matches_by_val[val].append(
                _ConsumerMatch(
                    step=entry.index,
                    scenario=entry.scenario,
                    method=entry.method,
                    path=entry.normalized_path,
                    raw_url=entry.url,
                    location=location,
                    param=param,
                    value=val,
                )
            )

        # 2. Index this entry's response scalar values as producers for later steps
        if entry.response_json is not None and 200 <= entry.status < 300:
            for jp, key_name, raw_val in _walk_json_scalars(entry.response_json):
                val = _normalize_candidate_value(raw_val)
                if val is None or val in filtered_set:
                    continue
                if val not in producers_by_val:
                    producers_by_val[val] = _ProducerRecord(
                        step=entry.index,
                        scenario=entry.scenario,
                        method=entry.method,
                        path=entry.normalized_path,
                        raw_url=entry.url,
                        jsonpath=jp,
                        key_name=key_name,
                        value=val,
                    )

    # Assemble CorrelationVariable records
    variables: list[CorrelationVariable] = []
    used_var_names: set[str] = set()
    extracts_by_step: dict[int, dict[str, str]] = defaultdict(dict)
    uses_by_step: dict[int, dict[str, str]] = defaultdict(dict)
    adj: dict[int, set[int]] = defaultdict(set)
    downstream_steps_of: dict[int, set[int]] = defaultdict(set)

    # Order variables chronologically by producer step
    matched_vals = sorted(
        matches_by_val.keys(),
        key=lambda v: (producers_by_val[v].step, producers_by_val[v].jsonpath),
    )

    for val in matched_vals:
        prod = producers_by_val[val]
        consumers = matches_by_val[val]

        # Filter pre-existing session auth tokens echoed by profile/info endpoints
        all_auth_consumers = all(
            c.location == "header" and c.param.lower() in _AUTH_HEADER_NAMES
            for c in consumers
        )
        if all_auth_consumers:
            first_auth_step = earliest_auth_step.get(val)
            is_preexisting = first_auth_step is not None and first_auth_step <= prod.step
            auth_ratio = len(auth_steps_by_val.get(val, set())) / total_requests
            is_non_auth_session_wide = (
                not _AUTH_PRODUCER_PATH_RE.search(prod.path) and auth_ratio >= 0.50
            )
            if is_preexisting or is_non_auth_session_wide:
                key_label = _to_var_identifier(prod.key_name) or "token"
                reason = "pre-existing" if is_preexisting else "session-wide"
                redacted_label = f"session-auth:{key_label} ({reason})"
                if redacted_label not in filtered_constants:
                    filtered_constants.append(redacted_label)
                continue

        var_name = _choose_var_name(prod, consumers, used_var_names)
        confidence = _compute_confidence(prod, consumers)

        prod_ref = CorrelationEndpointRef(
            step=prod.step,
            scenario=prod.scenario,
            method=prod.method,
            path=prod.path,
            raw_url=prod.raw_url,
            location="response",
            param=prod.key_name,
            jsonpath=prod.jsonpath,
            sample=prod.value,
        )
        cons_refs: list[CorrelationEndpointRef] = []
        extracts_by_step[prod.step][var_name] = prod.jsonpath

        for cons in consumers:
            cons_refs.append(
                CorrelationEndpointRef(
                    step=cons.step,
                    scenario=cons.scenario,
                    method=cons.method,
                    path=cons.path,
                    raw_url=cons.raw_url,
                    location=cons.location,  # type: ignore[arg-type]
                    param=cons.param,
                    sample=cons.value,
                )
            )
            uses_by_step[cons.step][f"{cons.location}:{cons.param}"] = f"{{{{{var_name}}}}}"
            adj[prod.step].add(cons.step)
            adj[cons.step].add(prod.step)
            downstream_steps_of[prod.step].add(cons.step)

        variables.append(
            CorrelationVariable(
                var_name=var_name,
                producer=prod_ref,
                consumers=cons_refs,
                confidence=confidence,
            )
        )

    # Assemble connected dependency chains (chronologically ordered)
    visited: set[int] = set()
    components: list[list[int]] = []
    for step in sorted(adj.keys()):
        if step in visited:
            continue
        comp: list[int] = []
        stack = [step]
        visited.add(step)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in adj[cur]:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        comp.sort()
        components.append(comp)

    chains: list[CorrelationChain] = []
    for idx, comp in enumerate(components):
        step_models: list[CorrelationChainStep] = []
        cleanup_step_model: CorrelationChainStep | None = None

        for step in comp:
            entry = entries_by_step[step]
            step_model = CorrelationChainStep(
                step=step,
                scenario=entry.scenario,
                method=entry.method,
                path=entry.normalized_path,
                raw_url=entry.url,
                request_headers=_sanitize_step_headers(entry.request_headers),
                query_params=dict(entry.query_params),
                request_body=entry.request_json if entry.request_json is not None else (entry.request_form or None),
                expected_status=entry.status or 200,
                extracts=dict(extracts_by_step.get(step, {})),
                uses=dict(uses_by_step.get(step, {})),
            )
            has_downstream = bool(downstream_steps_of.get(step))
            if (
                cleanup_step_model is None
                and step_model.uses
                and not has_downstream
                and _is_cleanup_entry(entry)
            ):
                cleanup_step_model = step_model
            else:
                step_models.append(step_model)

        if not step_models:
            continue
        chain_scenario = step_models[-1].scenario
        chains.append(
            CorrelationChain(
                name=_make_chain_name(step_models, idx),
                scenario=chain_scenario,
                steps=step_models,
                cleanup_step=cleanup_step_model,
            )
        )

    mermaid = _build_mermaid(entries_by_step, variables, chains)
    report = CorrelationReport(
        variables=variables,
        chains=chains,
        filtered_constants=filtered_constants,
        mermaid=mermaid,
    )
    payload = report.model_dump()

    ensure_dir(Path(out_dir))
    write_json(Path(out_dir) / "correlation.json", payload)
    (Path(out_dir) / "correlation.mmd").write_text(mermaid, encoding="utf-8")
    return payload
