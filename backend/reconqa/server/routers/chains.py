"""API Chain Execution, Checkpoint Listing, and Resume (断点续跑) endpoints."""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.models.db import get_db
from reconqa.models.schemas import (
    AssertionEvalResult,
    ChainBatchExecuteRequest,
    ChainBatchExecuteResponse,
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
    save_checkpoint,
)
from reconqa.server.routers.scenarios import (
    _split_path_and_fragment,
    canonical_path_shape,
    clean_endpoint_path,
    collect_project_scenario_assertions,
    match_endpoint_scenario_assertions,
    sync_assertion_evals_to_scenarios,
)
from reconqa.utils.io import read_json

router = APIRouter(tags=["chains"])

_SINGLE_PATH_PARAM_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_PATH_PARAM_SEG_RE = re.compile(
    r"^(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|:([A-Za-z_][A-Za-z0-9_]*)|\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\})$"
)
_ANY_PATH_PARAM_RE = re.compile(
    r"\{[A-Za-z_][A-Za-z0-9_]*\}|(?<=/)[:][A-Za-z_][A-Za-z0-9_]*|\{\{\s*[A-Za-z_][A-Za-z0-9_.-]*\s*\}\}"
)


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


def _extract_param_name_from_token(token: str) -> str:
    if token.startswith("{{") and token.endswith("}}"):
        return token[2:-2].strip()
    if token.startswith("{") and token.endswith("}"):
        return token[1:-1].strip()
    if token.startswith(":"):
        return token[1:].strip()
    return token.strip()


def _replace_bound_tokens_in_path(path_str: str, bound_tokens: dict[str, str]) -> str:
    """Replace {param}, :param, or {{param}} placeholders with bound variable templates."""
    if not bound_tokens:
        return path_str
    tpl_param_names = {
        _extract_param_name_from_token(m.group(0))
        for m in _ANY_PATH_PARAM_RE.finditer(path_str)
    }
    unmatched_bounds = [
        val for key, val in bound_tokens.items() if key not in tpl_param_names
    ]

    def _repl(match: re.Match[str]) -> str:
        p_name = _extract_param_name_from_token(match.group(0))
        if p_name in bound_tokens:
            return bound_tokens[p_name]
        if len(bound_tokens) == 1 and unmatched_bounds:
            return unmatched_bounds.pop(0)
        return match.group(0)

    return _ANY_PATH_PARAM_RE.sub(_repl, path_str)


def _merge_recorded_and_bound_path(
    template_path: str,
    recorded_path: str,
    bound_tokens: dict[str, str],
) -> str:
    """Preserve concrete recorded segments for unbound path params and #opName fragments while binding correlated ones."""
    tpl_base, tpl_frag = _split_path_and_fragment(template_path)
    if not recorded_path or recorded_path == "/":
        result = _replace_bound_tokens_in_path(tpl_base, bound_tokens)
        return f"{result}#{tpl_frag}" if tpl_frag else result

    rec_base, rec_frag = _split_path_and_fragment(recorded_path)
    eff_frag = tpl_frag or rec_frag
    tpl_segs = [s for s in tpl_base.split("/") if s]
    rec_segs = [s for s in rec_base.split("/") if s]
    if len(tpl_segs) != len(rec_segs):
        if bound_tokens:
            result = _replace_bound_tokens_in_path(tpl_base, bound_tokens)
            return f"{result}#{eff_frag}" if eff_frag else result
        return f"{rec_base}#{eff_frag}" if eff_frag else rec_base

    tpl_param_names: set[str] = set()
    for t_seg in tpl_segs:
        m = _PATH_PARAM_SEG_RE.fullmatch(t_seg)
        if m:
            p_name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if p_name:
                tpl_param_names.add(p_name)

    unmatched_bounds = [
        val for key, val in bound_tokens.items() if key not in tpl_param_names
    ]

    merged: list[str] = []
    for t_seg, r_seg in zip(tpl_segs, rec_segs, strict=True):
        m = _PATH_PARAM_SEG_RE.fullmatch(t_seg)
        if m:
            is_mustache = m.group(3) is not None
            p_name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if p_name in bound_tokens:
                merged.append(bound_tokens[p_name])
            elif len(bound_tokens) == 1 and unmatched_bounds:
                merged.append(unmatched_bounds.pop(0))
            elif is_mustache:
                # Preserve already-bound {{var}} placeholders when not overridden by uses
                merged.append(t_seg)
            else:
                # Keep concrete recorded ID for unbound path parameter
                merged.append(r_seg)
        else:
            merged.append(r_seg or t_seg)
    merged_base = "/" + "/".join(merged)
    if tpl_base.endswith("/") and merged_base != "/":
        merged_base += "/"
    return f"{merged_base}#{eff_frag}" if eff_frag else merged_base


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
    _, raw_frag = _split_path_and_fragment(raw_url) if raw_url else ("", "")
    if bound_path_tokens or (raw_url and recorded_path):
        path = _merge_recorded_and_bound_path(path, recorded_path, bound_path_tokens)
    if raw_frag and "#" not in path:
        path = f"{path}#{raw_frag}"

    # Strip recorded query string to origin-only so httpx `params=query_params` never duplicates params,
    # while `path` now preserves concrete recorded IDs for any unbound segments and non-SPA #opName fragments.
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
    return clean_endpoint_path(raw_path)


def _canonical_path_shape(path: str) -> str:
    """Canonicalize all path parameter placeholders ({id}, {order_id}, {{order_id}}) to {param}."""
    return canonical_path_shape(path)


def _match_step_scenario_assertions(
    method: str,
    step_path: str,
    raw_url: str,
    scenario_assertions: list[dict[str, Any]],
) -> list[ScenarioAssertion]:
    """Match saved project ScenarioAssertions targeting this step's (method, path)."""
    primary = step_path or raw_url
    _, primary_frag = _split_path_and_fragment(primary)
    _, raw_frag = _split_path_and_fragment(raw_url)
    extra = ""
    if step_path and raw_url and raw_url != step_path:
        if not primary_frag or raw_frag:
            extra = raw_url
    return match_endpoint_scenario_assertions(
        method,
        primary,
        scenario_assertions,
        extra_path=extra,
    )


async def _sync_chain_out_to_scenarios(
    db: AsyncSession,
    project_id: str,
    steps: list[ChainStepInput],
    out: ChainRunOut,
    *,
    step_results: list[ChainStepResult] | None = None,
) -> ChainRunOut:
    """Sync non-skipped step assertions in ``step_results`` (or ``out.step_results``) back to ScenarioModel.

    On resume, callers pass ONLY ``out.step_results[start_index:]`` (newly re-executed steps)
    so prior un-re-run steps never overwrite newer ScenarioModel assertion statuses.
    """
    target_results = out.step_results if step_results is None else step_results
    eval_items: list[
        tuple[str, str, list[AssertionEvalResult], list[ScenarioAssertion] | None]
    ] = []
    for sr in target_results:
        if sr.status == "skipped" or not sr.assertions:
            continue
        st_path = (sr.virtual_path or "").strip()
        st_raw = ""
        step_frag = ""
        if 0 <= sr.step_index < len(steps):
            st = steps[sr.step_index]
            st_path = (st.path or "").strip() or st_path
            st_raw = (st.raw_url or "").strip()
            _, path_frag = _split_path_and_fragment(st_path)
            _, raw_frag = _split_path_and_fragment(st_raw)
            step_frag = path_frag or raw_frag
        elif st_path:
            _, step_frag = _split_path_and_fragment(st_path)

        st_assertions = steps[sr.step_index].assertions if 0 <= sr.step_index < len(steps) else []
        if step_frag:
            base_sr = sr.url.split("#", 1)[0]
            actual_endpoint = f"{base_sr}#{step_frag}"
        else:
            actual_endpoint = sr.url
        eval_items.append((sr.method, actual_endpoint, sr.assertions, st_assertions or None))
    if not eval_items:
        return out

    synced_scenarios, synced_assertions = await sync_assertion_evals_to_scenarios(
        db,
        project_id,
        eval_items,
    )
    out.synced_scenarios = synced_scenarios
    out.synced_assertions = synced_assertions
    cp = load_checkpoint(out.chain_run_id)
    if isinstance(cp, dict):
        cp["synced_scenarios"] = synced_scenarios
        cp["synced_assertions"] = synced_assertions
        save_checkpoint(cp)
    return out


@router.get("/projects/{project_id}/chain-runs", response_model=list[ChainRunOut])
async def list_project_chain_runs(
    project_id: str,
    run_id: str | None = Query(default=None),
    batch_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[ChainRunOut]:
    """List saved chain execution checkpoints for a project (newest first, max 50)."""
    await _get_project_or_404(db, project_id)
    return list_checkpoints(project_id, run_id=run_id, batch_id=batch_id, limit=50)


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
        out = await execute_chain(
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

    if body.sync_scenarios:
        out = await _sync_chain_out_to_scenarios(db, project_id, steps, out)
    return out


async def execute_project_chain_batch(
    db: AsyncSession,
    project_id: str,
    body: ChainBatchExecuteRequest,
) -> ChainBatchExecuteResponse:
    """Execute all (or selected) correlation chains for a run sequentially under a single batch_id."""
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
    else:
        runs_res = await db.execute(
            select(Run)
            .where(Run.project_id == project_id, Run.status == "completed")
            .order_by(Run.finished_at.desc(), Run.started_at.desc())
            .limit(1)
        )
        run_obj = runs_res.scalar_one_or_none()
        if run_obj is None:
            raise HTTPException(
                status_code=404,
                detail=f"No completed run found in project '{project_id}' for batch chain execution.",
            )

    corr_path = Path(run_obj.workspace_dir) / "hub" / "correlation.json"
    if not corr_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Correlation report not found for run '{run_obj.id}'.",
        )
    corr_data = read_json(corr_path)
    chains = [
        ch for ch in (corr_data.get("chains") or []) if isinstance(ch, dict)
    ] if isinstance(corr_data, dict) else []

    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    if not chains:
        return ChainBatchExecuteResponse(
            batch_id=batch_id,
            project_id=project_id,
            run_id=run_obj.id,
            total_chains=0,
            passed_chains=0,
            failed_chains=0,
            chain_runs=[],
        )

    if body.chain_indices is not None:
        selected_indices: list[int] = []
        seen_idx: set[int] = set()
        for raw_idx in body.chain_indices:
            idx = int(raw_idx)
            if idx < 0 or idx >= len(chains):
                raise HTTPException(
                    status_code=400,
                    detail=f"chain_index {idx} out of range (available: {len(chains)}).",
                )
            if idx not in seen_idx:
                seen_idx.add(idx)
                selected_indices.append(idx)
        if len(selected_indices) > body.max_chains:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Requested {len(selected_indices)} chains which exceeds max_chains={body.max_chains}; "
                    "increase max_chains or select fewer chain_indices."
                ),
            )
    else:
        if len(chains) > body.max_chains:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Run contains {len(chains)} chains which exceeds max_chains={body.max_chains}; "
                    "increase max_chains or pass explicit chain_indices."
                ),
            )
        selected_indices = list(range(len(chains)))

    explicit_base_url = (body.base_url or "").strip()
    base_url = explicit_base_url
    if not base_url and isinstance(run_obj.config_snapshot, dict):
        base_url = str(run_obj.config_snapshot.get("target_url") or "").strip()
    if not base_url:
        res_t = await db.execute(select(Target).where(Target.project_id == project_id).limit(1))
        first_target = res_t.scalar_one_or_none()
        if first_target and first_target.base_url:
            base_url = first_target.base_url.strip()

    project_assertions: list[dict[str, Any]] = []
    if body.include_scenario_assertions:
        project_assertions = await collect_project_scenario_assertions(db, project_id)

    chain_runs: list[ChainRunOut] = []
    for idx in selected_indices:
        ch = chains[idx]
        chain_name = str(ch.get("name") or f"Chain {idx}")
        raw_steps = list(ch.get("steps") or [])
        steps = [_apply_uses_to_step(st) for st in raw_steps if isinstance(st, dict)]
        if not steps:
            continue

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

        try:
            out = await execute_chain(
                project_id=project_id,
                chain_name=chain_name,
                steps=steps,
                base_url=base_url,
                rebase_raw_url=bool(explicit_base_url),
                run_id=run_obj.id,
                chain_index=idx,
                batch_id=batch_id,
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

        if body.sync_scenarios:
            out = await _sync_chain_out_to_scenarios(db, project_id, steps, out)
        chain_runs.append(out)

    passed_chains = sum(1 for r in chain_runs if r.status == "passed")
    failed_chains = sum(1 for r in chain_runs if r.status != "passed")
    return ChainBatchExecuteResponse(
        batch_id=batch_id,
        project_id=project_id,
        run_id=run_obj.id,
        total_chains=len(chain_runs),
        passed_chains=passed_chains,
        failed_chains=failed_chains,
        chain_runs=chain_runs,
    )


@router.post("/projects/{project_id}/chains/execute-batch", response_model=ChainBatchExecuteResponse)
async def execute_project_chains_batch(
    project_id: str,
    body: ChainBatchExecuteRequest,
    db: AsyncSession = Depends(get_db),
) -> ChainBatchExecuteResponse:
    """Execute all (or selected) correlation chains sequentially and tag checkpoints with a shared batch_id."""
    return await execute_project_chain_batch(db, project_id, body)


@router.post("/projects/{project_id}/chain-runs/{chain_run_id}/resume", response_model=ChainRunOut)
async def resume_project_chain_run(
    project_id: str,
    chain_run_id: str,
    body: ChainResumeRequest,
    db: AsyncSession = Depends(get_db),
) -> ChainRunOut:
    """Resume a failed or partial chain run from a checkpoint step without re-executing prior steps.

    When ``sync_scenarios=True``, syncs ONLY the newly re-executed steps
    (``out.step_results[start_index:]``) to matching ``ScenarioModel`` assertions.
    """
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
        out = await execute_chain(
            project_id=project_id,
            chain_name=str(checkpoint.get("chain_name") or "Resumed Chain"),
            steps=steps,
            base_url=base_url,
            rebase_raw_url=eff_rebase,
            run_id=checkpoint.get("run_id"),
            chain_index=checkpoint.get("chain_index"),
            batch_id=checkpoint.get("batch_id"),
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

    if body.sync_scenarios:
        out = await _sync_chain_out_to_scenarios(
            db,
            project_id,
            steps,
            out,
            step_results=out.step_results[start_index:],
        )
    return out
