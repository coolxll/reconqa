"""API Chain Execution, Checkpoint Listing, and Resume (断点续跑) endpoints."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.models.db import get_db
from reconqa.models.schemas import (
    ChainExecuteRequest,
    ChainResumeRequest,
    ChainRunOut,
    ChainStepInput,
    ChainStepResult,
    ScenarioAssertion,
)
from reconqa.models.tables import AuthProfile, Project, Run, Target
from reconqa.modules.discovery.auth_resolver import sanitize_url_for_storage
from reconqa.modules.hub.assertion_engine import _tokenize_jsonpath
from reconqa.modules.hub.chain_runner import (
    checkpoint_to_out,
    execute_chain,
    list_checkpoints,
    load_checkpoint,
)
from reconqa.modules.hub.har_parser import normalize_path
from reconqa.server.routers.scenarios import collect_project_scenario_assertions
from reconqa.utils.io import read_json

router = APIRouter(tags=["chains"])

_SINGLE_PATH_PARAM_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


async def _get_project_or_404(db: AsyncSession, project_id: str) -> Project:
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return proj


async def _get_project_scope_domains(db: AsyncSession, project_id: str) -> list[str]:
    res = await db.execute(select(Target).where(Target.project_id == project_id))
    targets = list(res.scalars().all())
    return [t.root_domain for t in targets if t.root_domain]


async def _load_profile_or_error(
    db: AsyncSession,
    project_id: str,
    credential_id: str | None,
    *,
    from_checkpoint: bool = False,
) -> AuthProfile | None:
    if not credential_id:
        return None
    profile = await db.get(AuthProfile, credential_id)
    if profile is None or profile.project_id != project_id:
        if from_checkpoint:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Saved credential '{credential_id}' is no longer available in project '{project_id}'; "
                    "please select a new credential_id to resume."
                ),
            )
        raise HTTPException(
            status_code=404,
            detail=f"Credential '{credential_id}' not found in project '{project_id}'",
        )
    return profile


def _set_nested_body_value(body: Any, param_path: str, value: Any) -> Any:
    """Set a nested dot/[idx] path (e.g. data.order_id, items[0].id) inside a JSON body copy."""
    if body is None:
        return body
    tokens = _tokenize_jsonpath(param_path)
    if not tokens:
        return body

    cur = body
    for tok in tokens[:-1]:
        if isinstance(tok, int):
            if isinstance(cur, list) and -len(cur) <= tok < len(cur):
                cur = cur[tok]
            else:
                return body
        elif isinstance(cur, dict) and tok in cur:
            cur = cur[tok]
        else:
            return body

    leaf = tokens[-1]
    if isinstance(leaf, int) and isinstance(cur, list) and -len(cur) <= leaf < len(cur):
        cur[leaf] = value
    elif isinstance(leaf, str) and isinstance(cur, dict) and leaf in cur:
        cur[leaf] = value
    return body


def _origin_only(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _merge_recorded_and_bound_path(
    template_path: str,
    recorded_path: str,
    bound_tokens: dict[str, str],
) -> str:
    """Preserve concrete recorded segments for unbound path params while binding correlated ones."""
    if not recorded_path or recorded_path == "/":
        result = template_path
        for param, tpl_str in bound_tokens.items():
            tok = "{" + param + "}"
            if tok in result:
                result = result.replace(tok, tpl_str)
            elif _SINGLE_PATH_PARAM_RE.search(result):
                result = _SINGLE_PATH_PARAM_RE.sub(tpl_str, result, count=1)
        return result

    tpl_segs = template_path.split("/")
    rec_segs = recorded_path.split("/")
    if len(tpl_segs) != len(rec_segs):
        if bound_tokens:
            result = template_path
            for param, tpl_str in bound_tokens.items():
                tok = "{" + param + "}"
                if tok in result:
                    result = result.replace(tok, tpl_str)
                elif _SINGLE_PATH_PARAM_RE.search(result):
                    result = _SINGLE_PATH_PARAM_RE.sub(tpl_str, result, count=1)
            return result
        return recorded_path

    merged: list[str] = []
    remaining_bounds = list(bound_tokens.values())
    for t_seg, r_seg in zip(tpl_segs, rec_segs, strict=True):
        m = _SINGLE_PATH_PARAM_RE.fullmatch(t_seg)
        if m:
            p_name = t_seg[1:-1].strip()
            if p_name in bound_tokens:
                merged.append(bound_tokens[p_name])
                if bound_tokens[p_name] in remaining_bounds:
                    remaining_bounds.remove(bound_tokens[p_name])
            elif remaining_bounds and len(bound_tokens) == 1:
                merged.append(remaining_bounds.pop(0))
            else:
                # Keep concrete recorded ID for unbound path parameter
                merged.append(r_seg)
        else:
            merged.append(r_seg or t_seg)
    return "/".join(merged) or "/"


def _apply_uses_to_step(step_dict: dict[str, Any]) -> ChainStepInput:
    """Convert a CorrelationChainStep dict into a runnable ChainStepInput with full path/body/query/header bindings."""
    headers = dict(step_dict.get("request_headers") or {})
    query_params = dict(step_dict.get("query_params") or {})
    body = copy.deepcopy(step_dict.get("request_body"))
    path = str(step_dict.get("path") or "/")
    raw_url = str(step_dict.get("raw_url") or "")
    uses = step_dict.get("uses") or {}
    bound_path_tokens: dict[str, str] = {}

    if isinstance(uses, dict):
        for loc_param, var_tpl in uses.items():
            if not isinstance(loc_param, str) or ":" not in loc_param:
                continue
            loc, param = loc_param.split(":", 1)
            loc = loc.strip().lower()
            param = param.strip()
            tpl_str = str(var_tpl)
            if not param:
                continue
            if loc == "path":
                bound_path_tokens[param] = tpl_str
            elif loc == "query":
                query_params[param] = tpl_str
            elif loc == "header":
                headers[param] = tpl_str
            elif loc == "body":
                body = _set_nested_body_value(body, param, tpl_str)

    recorded_path = urlparse(raw_url).path if raw_url else ""
    if bound_path_tokens or (raw_url and recorded_path):
        path = _merge_recorded_and_bound_path(path, recorded_path, bound_path_tokens)

    # Strip recorded query string to origin-only so httpx `params=query_params` never duplicates params,
    # while `path` now preserves concrete recorded IDs for any unbound segments.
    if raw_url and (bound_path_tokens or query_params):
        origin = _origin_only(raw_url)
        if origin:
            raw_url = origin

    default_name = f"{step_dict.get('method', 'GET')} {sanitize_url_for_storage(path)}"
    return ChainStepInput(
        step=int(step_dict.get("step") or 1),
        name=str(step_dict.get("name") or default_name),
        method=str(step_dict.get("method") or "GET").upper(),
        path=path,
        raw_url=raw_url,
        request_headers=headers,
        query_params=query_params,
        request_body=body,
        expected_status=(
            int(step_dict["expected_status"])
            if step_dict.get("expected_status") is not None
            else None
        ),
        extracts={str(k): str(v) for k, v in (step_dict.get("extracts") or {}).items()},
        assertions=[],
    )


def _clean_endpoint_path(raw_path: str) -> str:
    p = (raw_path or "").strip()
    if p.startswith(("http://", "https://")):
        p = urlparse(p).path
    p = p.split("?", 1)[0].strip()
    # Normalize {{var}} templates to {var} so normalize_path matches scenario targets
    p = re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}", r"{\1}", p)
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


def _canonical_path_shape(path: str) -> str:
    """Canonicalize all path parameter placeholders ({id}, {order_id}, {{order_id}}) to {param}."""
    cleaned = _clean_endpoint_path(path)
    norm, _ = normalize_path(cleaned)
    return re.sub(r"\{[^}]+\}", "{param}", _clean_endpoint_path(norm))


def _match_step_scenario_assertions(
    method: str,
    step_path: str,
    raw_url: str,
    scenario_assertions: list[dict[str, Any]],
) -> list[ScenarioAssertion]:
    """Match saved project ScenarioAssertions targeting this step's (method, path)."""
    if not scenario_assertions:
        return []

    ep_method = (method or "GET").strip().upper()
    ep_clean = _clean_endpoint_path(step_path or raw_url)
    ep_shape = _canonical_path_shape(step_path or raw_url)

    matched: list[ScenarioAssertion] = []
    seen_ids: set[str] = set()
    for raw in scenario_assertions:
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("target_endpoint") or "").strip()
        if not target:
            continue
        parts = target.split(None, 1)
        if len(parts) == 2 and parts[0].upper() in _HTTP_METHODS:
            t_method: str | None = parts[0].upper()
            t_path_raw = parts[1].strip()
        else:
            t_method = None
            t_path_raw = target

        if t_method is not None and t_method != ep_method:
            continue

        t_clean = _clean_endpoint_path(t_path_raw)
        t_shape = _canonical_path_shape(t_path_raw)
        if t_clean == ep_clean or t_shape == ep_shape:
            try:
                parsed = ScenarioAssertion.model_validate(raw)
            except Exception:
                continue
            if parsed.id not in seen_ids:
                seen_ids.add(parsed.id)
                matched.append(parsed)
    return matched


@router.get("/projects/{project_id}/chain-runs", response_model=list[ChainRunOut])
async def list_project_chain_runs(
    project_id: str,
    run_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[ChainRunOut]:
    """List saved chain execution checkpoints for a project (newest first, max 50)."""
    await _get_project_or_404(db, project_id)
    return list_checkpoints(project_id, run_id=run_id, limit=50)


@router.get("/projects/{project_id}/chain-runs/{chain_run_id}", response_model=ChainRunOut)
async def get_project_chain_run(
    project_id: str,
    chain_run_id: str,
    db: AsyncSession = Depends(get_db),
) -> ChainRunOut:
    """Fetch a single saved chain run checkpoint."""
    await _get_project_or_404(db, project_id)
    try:
        data = load_checkpoint(chain_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if data is None or data.get("project_id") != project_id:
        raise HTTPException(
            status_code=404,
            detail=f"Chain run '{chain_run_id}' not found in project '{project_id}'",
        )
    return checkpoint_to_out(data)


@router.post("/projects/{project_id}/chains/execute", response_model=ChainRunOut)
async def execute_project_chain(
    project_id: str,
    body: ChainExecuteRequest,
    db: AsyncSession = Depends(get_db),
) -> ChainRunOut:
    """Execute a multi-step API chain (inline steps or from a run's correlation.json) with checkpointing."""
    await _get_project_or_404(db, project_id)
    profile = await _load_profile_or_error(db, project_id, body.credential_id)
    scope_domains = await _get_project_scope_domains(db, project_id)

    run_obj: Run | None = None
    if body.run_id:
        run_obj = await db.get(Run, body.run_id)
        if run_obj is None or run_obj.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{body.run_id}' not found in project '{project_id}'",
            )

    steps: list[ChainStepInput] = []
    chain_name = (body.chain_name or "").strip()
    selected_chain_index: int | None = body.chain_index

    if body.steps:
        steps = [ChainStepInput.model_validate(s) for s in body.steps]
        if not chain_name:
            chain_name = "Ad-hoc Chain"
    else:
        if run_obj is None:
            raise HTTPException(
                status_code=400,
                detail="Provide either explicit `steps` or a `run_id` containing hub/correlation.json.",
            )
        corr_path = Path(run_obj.workspace_dir) / "hub" / "correlation.json"
        if not corr_path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Correlation report not found for run '{run_obj.id}'.",
            )
        corr_data = read_json(corr_path)
        chains = list(corr_data.get("chains") or []) if isinstance(corr_data, dict) else []
        if not chains:
            raise HTTPException(
                status_code=404,
                detail=f"No correlation chains found in run '{run_obj.id}'.",
            )

        selected_chain: dict[str, Any] | None = None
        if body.chain_index is not None:
            idx = int(body.chain_index)
            if idx < 0 or idx >= len(chains):
                raise HTTPException(
                    status_code=400,
                    detail=f"chain_index {idx} out of range (available: {len(chains)}).",
                )
            selected_chain = chains[idx]
            selected_chain_index = idx
        elif chain_name:
            for idx, ch in enumerate(chains):
                if isinstance(ch, dict) and str(ch.get("name") or "").strip() == chain_name:
                    selected_chain = ch
                    selected_chain_index = idx
                    break
            if selected_chain is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Chain '{chain_name}' not found in run '{run_obj.id}'.",
                )
        else:
            selected_chain = chains[0]
            selected_chain_index = 0

        chain_name = str(selected_chain.get("name") or f"Chain {selected_chain_index or 0}")
        raw_steps = list(selected_chain.get("steps") or [])
        if not raw_steps:
            raise HTTPException(status_code=400, detail=f"Chain '{chain_name}' has no steps.")
        steps = [_apply_uses_to_step(st) for st in raw_steps if isinstance(st, dict)]

    # Auto-bind saved Scenario assertions matching each step's (method, path)
    if body.include_scenario_assertions:
        project_assertions = await collect_project_scenario_assertions(db, project_id)
        if project_assertions:
            for st in steps:
                existing_ids = {a.id for a in st.assertions}
                matched_sc = _match_step_scenario_assertions(
                    st.method, st.path, st.raw_url, project_assertions
                )
                for ma in matched_sc:
                    if ma.id not in existing_ids:
                        st.assertions.append(ma)
                        existing_ids.add(ma.id)

    # Merge any step_assertions keyed by step number string ("1", "2", ...)
    if body.step_assertions:
        for st in steps:
            extra = body.step_assertions.get(str(st.step))
            if extra:
                st.assertions = [*st.assertions, *extra]

    # Resolve base_url (request -> run target_url -> project first target)
    explicit_base_url = (body.base_url or "").strip()
    base_url = explicit_base_url
    if not base_url and run_obj is not None and isinstance(run_obj.config_snapshot, dict):
        base_url = str(run_obj.config_snapshot.get("target_url") or "").strip()
    if not base_url:
        res_t = await db.execute(select(Target).where(Target.project_id == project_id).limit(1))
        first_target = res_t.scalar_one_or_none()
        if first_target and first_target.base_url:
            base_url = first_target.base_url.strip()

    try:
        return await execute_chain(
            project_id=project_id,
            chain_name=chain_name,
            steps=steps,
            base_url=base_url,
            rebase_raw_url=bool(explicit_base_url),
            run_id=body.run_id,
            chain_index=selected_chain_index,
            credential_id=body.credential_id,
            profile=profile,
            scope_domains=scope_domains,
            initial_vars=body.initial_vars,
            db_dsn_ref=body.db_dsn_ref,
            stop_on_failure=body.stop_on_failure,
            timeout_sec=body.timeout_sec,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/projects/{project_id}/chain-runs/{chain_run_id}/resume", response_model=ChainRunOut)
async def resume_project_chain_run(
    project_id: str,
    chain_run_id: str,
    body: ChainResumeRequest,
    db: AsyncSession = Depends(get_db),
) -> ChainRunOut:
    """Resume a failed or partial chain run from a checkpoint step without re-executing prior steps."""
    await _get_project_or_404(db, project_id)
    try:
        checkpoint = load_checkpoint(chain_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if checkpoint is None or checkpoint.get("project_id") != project_id:
        raise HTTPException(
            status_code=404,
            detail=f"Chain run '{chain_run_id}' not found in project '{project_id}'",
        )

    raw_steps = list(checkpoint.get("_steps_snapshot") or [])
    if not raw_steps:
        raise HTTPException(
            status_code=400,
            detail=f"Checkpoint '{chain_run_id}' has no step snapshot to resume.",
        )
    steps = [ChainStepInput.model_validate(s) for s in raw_steps]

    if body.from_step_index is not None:
        start_index = int(body.from_step_index)
    elif checkpoint.get("failed_step_index") is not None:
        start_index = int(checkpoint["failed_step_index"])
    else:
        start_index = 0

    if start_index < 0 or start_index >= len(steps):
        raise HTTPException(
            status_code=400,
            detail=f"from_step_index {start_index} out of range (0..{len(steps) - 1}).",
        )

    # Apply overrides to context_vars and the target resume step
    merged_vars = dict(checkpoint.get("context_vars") or {})
    merged_vars.update(body.override_vars or {})

    target_step = steps[start_index]
    if body.override_step_body is not None:
        target_step.request_body = body.override_step_body
    if body.override_step_headers is not None:
        target_step.request_headers = {
            **target_step.request_headers,
            **body.override_step_headers,
        }
    if body.override_step_assertions is not None:
        target_step.assertions = list(body.override_step_assertions)

    # Reuse saved credential_id from checkpoint if omitted on resume (fail cleanly with 400 if deleted)
    from_checkpoint_cred = body.credential_id is None and bool(checkpoint.get("credential_id"))
    eff_credential_id = (
        body.credential_id
        if body.credential_id is not None
        else checkpoint.get("credential_id")
    )
    profile = await _load_profile_or_error(
        db,
        project_id,
        eff_credential_id,
        from_checkpoint=from_checkpoint_cred,
    )
    scope_domains = await _get_project_scope_domains(db, project_id)

    base_url = (body.base_url or "").strip() or str(checkpoint.get("base_url") or "")
    eff_rebase = bool((body.base_url or "").strip() or checkpoint.get("_rebase_raw_url", False))
    eff_dsn_ref = (
        body.db_dsn_ref
        if body.db_dsn_ref is not None
        else (checkpoint.get("db_dsn_ref") or checkpoint.get("_db_dsn_ref"))
    )
    prior_results = [
        ChainStepResult.model_validate(r) for r in (checkpoint.get("step_results") or [])
    ]

    try:
        return await execute_chain(
            project_id=project_id,
            chain_name=str(checkpoint.get("chain_name") or "Resumed Chain"),
            steps=steps,
            base_url=base_url,
            rebase_raw_url=eff_rebase,
            run_id=checkpoint.get("run_id"),
            chain_index=checkpoint.get("chain_index"),
            credential_id=eff_credential_id,
            profile=profile,
            scope_domains=scope_domains,
            initial_vars=merged_vars,
            db_dsn_ref=eff_dsn_ref,
            stop_on_failure=body.stop_on_failure,
            timeout_sec=body.timeout_sec,
            start_index=start_index,
            prior_step_results=prior_results,
            chain_run_id=chain_run_id,
            created_at=str(checkpoint.get("created_at") or ""),
            resumed_from_index=start_index,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
