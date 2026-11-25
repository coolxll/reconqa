"""First-class Scenario entity CRUD, auto-hydration, and testing effectiveness endpoints."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from reconqa.models.db import get_db
from reconqa.models.schemas import (
    AssertionEvalResult,
    InventoryEndpoint,
    ProjectCapabilityMapResponse,
    PromoteCapabilityRequest,
    ScenarioAssertion,
    ScenarioCreateRequest,
    ScenarioDetailResponse,
    ScenarioEffectivenessLevel,
    ScenarioListResponse,
    ScenarioNegativeCase,
    ScenarioResponse,
    ScenarioRunRef,
    ScenarioUpdateRequest,
)
from reconqa.models.tables import Project, Run, ScenarioModel
from reconqa.modules.hub.capability_map import build_capability_map, endpoint_capability_key
from reconqa.modules.hub.har_parser import _VERSION_SEG_RE, normalize_path
from reconqa.modules.hub.inventory import build_project_inventory
from reconqa.server.routers.explore import find_project_explores
from reconqa.utils.io import slugify

router = APIRouter(tags=["scenarios"])

_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_ALL_PRIORITIES = ("P0", "P1", "P2", "P3")
_ALL_EFFECTIVENESS_LEVELS = ("L0", "L1", "L2", "L3", "L4")
_EFFECTIVE_LEVELS = {"L3", "L4"}
_SMOKE_OR_SEC_MODULES = {"java_gen", "security"}


def _natural_sort_key(value: str) -> list[Any]:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", value or "")]


def normalize_scenario_slug(raw: str) -> str:
    """Normalize a scenario identifier preserving underscores/hyphens used in HAR scenario names."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", (raw or "").strip().lower()).strip("_")
    return cleaned or "default"


def humanize_scenario_slug(slug: str) -> str:
    """Turn a slug like '01_dashboard_overview' into '01 Dashboard Overview'."""
    tokens = [tok for tok in re.split(r"[_\-\s]+", (slug or "").strip()) if tok]
    if not tokens:
        return "Default Scenario"
    formatted: list[str] = []
    for tok in tokens:
        if tok.isdigit():
            formatted.append(tok)
        elif tok.isupper() and len(tok) <= 4:
            formatted.append(tok)
        else:
            formatted.append(tok[0].upper() + tok[1:])
    return " ".join(formatted)


def _derive_run_label(run: Run) -> str:
    cfg = run.config_snapshot or {}
    if isinstance(cfg, dict) and str(cfg.get("run_name") or "").strip():
        return str(cfg["run_name"]).strip()
    target_url = str(cfg.get("target_url") or "") if isinstance(cfg, dict) else ""
    host = (urlparse(target_url).hostname or "") if target_url else ""
    if host:
        return f"{host} · #{run.id[:8]}"
    return f"Run #{run.id[:8]}"


def _safe_json_loads_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _parse_tags(row: ScenarioModel) -> list[str]:
    return [str(t).strip() for t in _safe_json_loads_list(row.tags_json) if str(t).strip()]


def _parse_assertions(row: ScenarioModel) -> list[ScenarioAssertion]:
    out: list[ScenarioAssertion] = []
    for item in _safe_json_loads_list(row.assertions_json):
        if isinstance(item, dict):
            try:
                out.append(ScenarioAssertion.model_validate(item))
            except Exception:
                continue
    return out


def _parse_negative_cases(row: ScenarioModel) -> list[ScenarioNegativeCase]:
    out: list[ScenarioNegativeCase] = []
    for item in _safe_json_loads_list(row.negative_cases_json):
        if isinstance(item, dict):
            try:
                out.append(ScenarioNegativeCase.model_validate(item))
            except Exception:
                continue
    return out


def compute_effectiveness_level(
    *,
    endpoint_count: int,
    status_value: str,
    has_smoke_or_security_run: bool,
    assertion_count: int,
    negative_cases_covered_count: int,
) -> ScenarioEffectivenessLevel:
    """Compute L0..L4 Testing Effectiveness maturity level."""
    if endpoint_count <= 0:
        return "L0"
    if assertion_count >= 1 and negative_cases_covered_count >= 1:
        return "L4"
    if assertion_count >= 1:
        return "L3"
    if status_value == "verified" or has_smoke_or_security_run:
        return "L2"
    return "L1"


def _run_completed_smoke_or_security(run: Run) -> bool:
    for job in getattr(run, "jobs", None) or []:
        if job.module in _SMOKE_OR_SEC_MODULES and job.status == "completed":
            return True
    if run.status == "completed":
        metrics = run.summary_metrics or {}
        if isinstance(metrics, dict):
            executed = metrics.get("modules_executed")
            if isinstance(executed, list) and any(
                str(m) in _SMOKE_OR_SEC_MODULES for m in executed
            ):
                return True
    return False


async def _get_project_or_404(db: AsyncSession, project_id: str) -> Project:
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return proj


async def _load_project_inventory_context(
    db: AsyncSession,
    project: Project,
) -> tuple[list[Run], dict[str, Any]]:
    runs_res = await db.execute(
        select(Run)
        .options(selectinload(Run.jobs))
        .where(Run.project_id == project.id, Run.status == "completed")
        .order_by(Run.started_at.asc(), Run.finished_at.asc())
    )
    completed_runs = list(runs_res.scalars().all())
    explores = await find_project_explores(db, project.id)
    inventory, _ = build_project_inventory(
        project_id=project.id,
        project_name=project.name,
        project_slug=project.slug,
        runs=completed_runs,
        explore_results=explores,
    )
    return completed_runs, inventory


async def sync_project_scenarios(
    db: AsyncSession,
    project_id: str,
    *,
    inventory: dict[str, Any] | None = None,
) -> list[ScenarioModel]:
    """Auto-hydrate missing ScenarioModel rows from completed runs/inventory without overwriting."""
    project = await db.get(Project, project_id)
    if project is None:
        return []

    if inventory is None:
        _, inventory = await _load_project_inventory_context(db, project)

    discovered_slugs = [
        str(s).strip() for s in (inventory.get("scenarios") or []) if str(s).strip()
    ]
    if not discovered_slugs:
        return []

    existing_res = await db.execute(
        select(ScenarioModel).where(ScenarioModel.project_id == project_id)
    )
    existing_slugs = {row.slug for row in existing_res.scalars().all()}

    now = datetime.now(UTC)
    created: list[ScenarioModel] = []
    for slug in discovered_slugs:
        if slug in existing_slugs:
            continue
        existing_slugs.add(slug)
        row = ScenarioModel(
            project_id=project_id,
            slug=slug,
            name=humanize_scenario_slug(slug),
            description="",
            preconditions="",
            business_domain="",
            priority="P2",
            status="active",
            owner="",
            source="har-auto",
            tags_json="[]",
            assertions_json="[]",
            negative_cases_json="[]",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        created.append(row)

    if created:
        await db.commit()
        for row in created:
            await db.refresh(row)

    return created


def _enrich_scenario(
    row: ScenarioModel,
    *,
    inventory: dict[str, Any],
    runs_by_id: dict[str, Run],
) -> tuple[ScenarioResponse, list[InventoryEndpoint], list[ScenarioRunRef]]:
    tags = _parse_tags(row)
    assertions = _parse_assertions(row)
    negative_cases = _parse_negative_cases(row)

    matched_endpoints: list[InventoryEndpoint] = []
    seen_run_ids: list[str] = []
    seen_run_set: set[str] = set()

    is_cap_scenario = row.slug.startswith("cap_")
    assertion_targets_norm: set[str] = set()
    if is_cap_scenario:
        for a in assertions:
            raw_t = (a.target_endpoint or "").strip()
            if not raw_t:
                continue
            parts = raw_t.split(None, 1)
            if len(parts) == 2:
                m_str = parts[0].upper().strip()
                p_str = clean_endpoint_path(parts[1]).lower()
                assertion_targets_norm.add(f"{m_str} {p_str}")
            else:
                p_str = clean_endpoint_path(raw_t).lower()
                assertion_targets_norm.add(p_str)

    for raw_ep in inventory.get("endpoints") or []:
        ep_scenarios = raw_ep.get("scenarios") or []
        matched = row.slug in ep_scenarios
        if not matched and is_cap_scenario:
            ep_method = str(raw_ep.get("method") or "GET").upper().strip()
            ep_path = str(raw_ep.get("path") or "/")
            ep_host = str(raw_ep.get("host") or "")
            ep_path_norm = clean_endpoint_path(ep_path).lower()
            ep_target_norm = f"{ep_method} {ep_path_norm}"
            if raw_ep.get("discovery_source") == "har-live":
                ep_cap_key = endpoint_capability_key(ep_path, ep_host)
                if normalize_scenario_slug(f"cap_{slugify(ep_cap_key)}") == row.slug:
                    matched = True
            if not matched and (
                ep_target_norm in assertion_targets_norm or ep_path_norm in assertion_targets_norm
            ):
                matched = True

        if matched:
            matched_endpoints.append(InventoryEndpoint.model_validate(raw_ep))
            for rref in raw_ep.get("seen_in_runs") or []:
                rid = str(rref.get("run_id") or "")
                if rid and rid not in seen_run_set:
                    seen_run_set.add(rid)
                    seen_run_ids.append(rid)

    run_refs: list[ScenarioRunRef] = []
    has_smoke_or_sec = False
    for rid in seen_run_ids:
        run_obj = runs_by_id.get(rid)
        if run_obj is not None:
            if _run_completed_smoke_or_security(run_obj):
                has_smoke_or_sec = True
            run_refs.append(
                ScenarioRunRef(
                    run_id=run_obj.id,
                    run_name=_derive_run_label(run_obj),
                    status=run_obj.status,
                    created_at=run_obj.started_at or run_obj.finished_at,
                )
            )
        else:
            run_refs.append(
                ScenarioRunRef(
                    run_id=rid,
                    run_name=f"Run #{rid[:8]}",
                    status="completed",
                    created_at=None,
                )
            )

    last_run_id = run_refs[-1].run_id if run_refs else None
    last_run_name = run_refs[-1].run_name if run_refs else None

    assertion_count = len(assertions)
    assertions_passed_count = sum(1 for a in assertions if a.status == "passed")
    negative_case_count = len(negative_cases)
    negative_cases_covered_count = sum(1 for nc in negative_cases if nc.status == "covered")

    eff_level = compute_effectiveness_level(
        endpoint_count=len(matched_endpoints),
        status_value=row.status,
        has_smoke_or_security_run=has_smoke_or_sec,
        assertion_count=assertion_count,
        negative_cases_covered_count=negative_cases_covered_count,
    )

    resp = ScenarioResponse(
        id=row.id,
        project_id=row.project_id,
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        preconditions=row.preconditions or "",
        business_domain=row.business_domain or "",
        priority=row.priority if row.priority in _PRIORITY_ORDER else "P2",  # type: ignore[arg-type]
        status=row.status if row.status in {"draft", "active", "verified", "archived"} else "active",  # type: ignore[arg-type]
        owner=row.owner or "",
        source=row.source if row.source in {"har-auto", "manual", "ai-agent"} else "manual",  # type: ignore[arg-type]
        tags=tags,
        assertions=assertions,
        negative_cases=negative_cases,
        created_at=row.created_at,
        updated_at=row.updated_at,
        endpoint_count=len(matched_endpoints),
        run_count=len(run_refs),
        last_run_id=last_run_id,
        last_run_name=last_run_name,
        assertion_count=assertion_count,
        assertions_passed_count=assertions_passed_count,
        negative_case_count=negative_case_count,
        negative_cases_covered_count=negative_cases_covered_count,
        effectiveness_level=eff_level,
    )
    return resp, matched_endpoints, run_refs


async def build_project_scenario_meta(
    db: AsyncSession,
    project_id: str,
    *,
    inventory: dict[str, Any],
    completed_runs: list[Run],
) -> dict[str, dict[str, Any]]:
    """Hydrate scenarios lazily and return compact scenario_meta keyed by slug for Inventory."""
    await sync_project_scenarios(db, project_id, inventory=inventory)
    res = await db.execute(
        select(ScenarioModel).where(ScenarioModel.project_id == project_id)
    )
    rows = list(res.scalars().all())
    runs_by_id = {r.id: r for r in completed_runs}
    meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        enriched, _, _ = _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)
        meta[row.slug] = {
            "id": enriched.id,
            "slug": enriched.slug,
            "name": enriched.name,
            "priority": enriched.priority,
            "business_domain": enriched.business_domain,
            "effectiveness_level": enriched.effectiveness_level,
            "description": enriched.description,
        }
    return meta


async def collect_project_scenario_assertions(
    db: AsyncSession,
    project_id: str,
) -> list[dict[str, Any]]:
    """Collect flattened ScenarioAssertion dicts with scenario priority/slug for Java codegen."""
    res = await db.execute(
        select(ScenarioModel).where(
            ScenarioModel.project_id == project_id,
            ScenarioModel.status != "archived",
        )
    )
    rows = sorted(
        res.scalars().all(),
        key=lambda r: (_PRIORITY_ORDER.get(r.priority, 2), _natural_sort_key(r.slug)),
    )
    collected: list[dict[str, Any]] = []
    for row in rows:
        for assertion in _parse_assertions(row):
            item = assertion.model_dump()
            item["priority"] = row.priority or "P2"
            item["scenario_slug"] = row.slug
            item["scenario_name"] = row.name
            collected.append(item)
    return collected


_SYNC_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_MUSTACHE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
_COLON_PATH_PARAM_RE = re.compile(r"(?<=/)[:]([A-Za-z_][A-Za-z0-9_]*)")
_BRACE_PARAM_RE = re.compile(r"\{[^}]+\}")
_STATIC_REST_SEGMENTS = frozenset(
    {
        "api",
        "rest",
        "rpc",
        "graphql",
        "oauth",
        "oauth2",
        "openid",
        "v1",
        "v2",
        "v3",
        "v4",
        "submit",
        "create",
        "list",
        "search",
        "query",
        "find",
        "filter",
        "export",
        "import",
        "batch",
        "bulk",
        "count",
        "stats",
        "summary",
        "overview",
        "health",
        "healthz",
        "ready",
        "readyz",
        "live",
        "livez",
        "status",
        "ping",
        "version",
        "info",
        "config",
        "settings",
        "metrics",
        "docs",
        "openapi",
        "swagger",
        "schema",
        "login",
        "logout",
        "signin",
        "signout",
        "signup",
        "register",
        "auth",
        "token",
        "refresh",
        "revoke",
        "verify",
        "confirm",
        "cancel",
        "pay",
        "checkout",
        "refund",
        "approve",
        "reject",
        "close",
        "reopen",
        "archive",
        "restore",
        "activate",
        "deactivate",
        "enable",
        "disable",
        "upload",
        "download",
        "preview",
        "validate",
        "check",
        "reset",
        "sync",
        "merge",
        "run",
        "execute",
        "resume",
        "replay",
        "me",
        "self",
        "profile",
        "account",
        "dashboard",
        "hidden",
        "add",
        "remove",
        "update",
        "delete",
        "edit",
        "new",
        "all",
        "callback",
        "webhook",
        "webhooks",
        "public",
        "private",
        "internal",
    }
)


def _split_path_and_fragment(raw_path: str) -> tuple[str, str]:
    p = (raw_path or "").strip()
    if not p:
        return "/", ""
    if p.startswith(("http://", "https://")):
        parsed = urlparse(p)
        base_p = parsed.path or "/"
        frag = (parsed.fragment or "").split("?", 1)[0].strip()
    elif "#" in p:
        before_frag, raw_frag = p.split("#", 1)
        base_p = before_frag.split("?", 1)[0].strip()
        frag = raw_frag.split("?", 1)[0].strip()
    else:
        base_p = p.split("?", 1)[0].strip()
        frag = ""
    if frag.startswith(("/", "!")):
        frag = ""
    return base_p, frag


def clean_endpoint_path(raw_path: str) -> str:
    """Normalize URL/path into a clean path preserving non-SPA #opName fragments and converting {{var}}/:var."""
    base_p, frag = _split_path_and_fragment(raw_path)
    base_p = _MUSTACHE_VAR_RE.sub(r"{\1}", base_p)
    base_p = _COLON_PATH_PARAM_RE.sub(r"{\1}", base_p)
    base_p = re.sub(r"/{2,}", "/", base_p)
    if not base_p.startswith("/"):
        base_p = "/" + base_p
    base_p = base_p.rstrip("/") or "/"
    return f"{base_p}#{frag}" if frag else base_p


def canonical_path_shape(path: str) -> str:
    """Canonicalize all path parameter placeholders ({id}, :id, {{id}}, dynamic segments) to {param}."""
    cleaned = clean_endpoint_path(path)
    base_p, frag = _split_path_and_fragment(cleaned)
    norm, _ = normalize_path(base_p)
    norm = _COLON_PATH_PARAM_RE.sub("{param}", norm)
    norm = _BRACE_PARAM_RE.sub("{param}", norm)
    if not norm.startswith("/"):
        norm = "/" + norm
    norm = norm.rstrip("/") or "/"
    return f"{norm}#{frag}" if frag else norm


def _split_method_and_target(raw_target: str) -> tuple[str | None, str]:
    target = (raw_target or "").strip()
    parts = target.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in _SYNC_HTTP_METHODS:
        return parts[0].upper(), parts[1].strip()
    return None, target


def _match_endpoint_specificity(method: str, url_or_path: str, target_endpoint: str) -> int:
    """Return match specificity: 3=exact, 2=canonical_shape, 1=directional segment template, 0=none."""
    raw_target = (target_endpoint or "").strip()
    if not raw_target:
        return 0
    ep_method = (method or "GET").strip().upper()
    t_method, t_path_raw = _split_method_and_target(raw_target)
    if t_method is not None and t_method != ep_method:
        return 0
    ep_clean = clean_endpoint_path(url_or_path)
    t_clean = clean_endpoint_path(t_path_raw)
    if ep_clean == t_clean:
        return 3
    ep_shape = canonical_path_shape(url_or_path)
    t_shape = canonical_path_shape(t_path_raw)
    if ep_shape == t_shape:
        return 2

    ep_base, ep_frag = _split_path_and_fragment(ep_shape)
    t_base, t_frag = _split_path_and_fragment(t_shape)
    if ep_frag != t_frag:
        return 0

    ep_cbase, _ = _split_path_and_fragment(ep_clean)
    t_cbase, _ = _split_path_and_fragment(t_clean)
    ep_shape_segs = [s for s in ep_base.split("/") if s]
    t_shape_segs = [s for s in t_base.split("/") if s]
    ep_clean_segs = [s for s in ep_cbase.split("/") if s]
    t_clean_segs = [s for s in t_cbase.split("/") if s]
    if not ep_shape_segs or not (
        len(ep_shape_segs) == len(t_shape_segs) == len(ep_clean_segs) == len(t_clean_segs)
    ):
        return 0

    for ep_s, ep_c, t_s, t_c in zip(
        ep_shape_segs, ep_clean_segs, t_shape_segs, t_clean_segs, strict=True
    ):
        if ep_s == t_s:
            continue
        if t_s == "{param}" and _BRACE_PARAM_RE.fullmatch(t_c):
            if (
                not ep_c
                or _BRACE_PARAM_RE.fullmatch(ep_c)
                or ep_c.lower() in _STATIC_REST_SEGMENTS
                or _VERSION_SEG_RE.match(ep_c)
            ):
                return 0
            continue
        return 0
    return 1


def endpoint_matches_target(method: str, url_or_path: str, target_endpoint: str) -> bool:
    """Return True if (method, url_or_path) matches ScenarioAssertion.target_endpoint."""
    return _match_endpoint_specificity(method, url_or_path, target_endpoint) > 0


def match_endpoint_scenario_assertions(
    method: str,
    path_or_url: str,
    scenario_assertions: list[dict[str, Any]],
    *,
    extra_path: str = "",
) -> list[ScenarioAssertion]:
    """Match saved project ScenarioAssertions targeting (method, path_or_url) or extra_path."""
    if not scenario_assertions:
        return []

    scored: list[tuple[int, ScenarioAssertion]] = []
    seen_ids: set[str] = set()
    for raw in scenario_assertions:
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("target_endpoint") or "").strip()
        if not target:
            continue
        score = _match_endpoint_specificity(method, path_or_url, target)
        if extra_path:
            score = max(score, _match_endpoint_specificity(method, extra_path, target))
        if score <= 0:
            continue
        try:
            parsed = ScenarioAssertion.model_validate(raw)
        except Exception:
            continue
        if parsed.id not in seen_ids:
            seen_ids.add(parsed.id)
            scored.append((score, parsed))

    if not scored:
        return []
    if max(score for score, _ in scored) >= 2:
        return [item for score, item in scored if score >= 2]
    return [item for _, item in scored]


def _normalize_assertion_jsonpath(op: str, jp: str) -> str:
    raw = (jp or "").strip()
    if not raw:
        return ""
    op_low = (op or "").strip().lower()
    if op_low == "header_equals":
        return raw.lower()
    if op_low in {"sql_equals", "sql_exists"}:
        return re.sub(r"\s+", " ", raw).strip().lower()
    if raw == "$":
        return ""
    if raw.startswith("$."):
        return raw[2:].strip()
    if raw.startswith("$"):
        return raw[1:].strip()
    return raw


def _assertion_definition_matches(
    persisted: ScenarioAssertion,
    ev: AssertionEvalResult,
    src_assertion: ScenarioAssertion | None = None,
) -> bool:
    """Verify operator and (when provided) jsonpath/expected match before mutating persisted status."""
    p_op = str(persisted.operator or "").strip().lower()
    ev_op = str(ev.operator or "").strip().lower()
    if ev_op and p_op != ev_op:
        return False

    if src_assertion is not None:
        src_op = str(src_assertion.operator or "").strip().lower()
        if src_op and p_op != src_op:
            return False
        p_jp = _normalize_assertion_jsonpath(p_op, persisted.jsonpath)
        s_jp = _normalize_assertion_jsonpath(p_op, src_assertion.jsonpath)
        if p_jp != s_jp:
            return False
        p_exp = str(persisted.expected or "").strip()
        s_exp = str(src_assertion.expected or "").strip()
        if p_exp != s_exp:
            return False
        return True

    ev_jp_raw = getattr(ev, "jsonpath", None)
    if isinstance(ev_jp_raw, str) and ev_jp_raw.strip():
        if _normalize_assertion_jsonpath(p_op, persisted.jsonpath) != _normalize_assertion_jsonpath(
            p_op, ev_jp_raw
        ):
            return False

    p_exp = str(persisted.expected or "").strip()
    ev_exp = str(ev.expected or "").strip()
    if ev_exp and p_exp and "{{" not in p_exp and ev_exp != p_exp:
        return False
    return True


async def sync_assertion_evals_to_scenarios(
    db: AsyncSession,
    project_id: str,
    eval_items: list[
        tuple[str, str, list[AssertionEvalResult]]
        | tuple[str, str, list[AssertionEvalResult], list[ScenarioAssertion] | None]
    ],
    *,
    executed_method: str | None = None,
    executed_path: str | None = None,
    evaluated_assertions: list[ScenarioAssertion] | None = None,
) -> tuple[int, int]:
    """Sync evaluated AssertionEvalResult statuses back to non-archived ScenarioModel assertions.

    Each entry in ``eval_items`` may be a 3-tuple ``(method, endpoint, results)`` or a 4-tuple
    ``(method, endpoint, results, evaluated_assertions)`` supplying per-step assertion definitions.

    Returns ``(synced_scenarios, synced_assertions)`` counting distinct modified ScenarioModel rows
    and updated assertions, committing only when at least one row changed.
    """
    non_empty_items: list[
        tuple[str, str, list[AssertionEvalResult], list[ScenarioAssertion] | None]
    ] = []
    for item in eval_items:
        if len(item) == 4:
            m, u, res_list, item_defs = item
        else:
            m, u, res_list = item
            item_defs = None
        if res_list:
            non_empty_items.append((m, u, res_list, item_defs))
    if not non_empty_items:
        return 0, 0

    res = await db.execute(
        select(ScenarioModel).where(
            ScenarioModel.project_id == project_id,
            ScenarioModel.status != "archived",
        )
    )
    rows = list(res.scalars().all())
    if not rows:
        return 0, 0

    rows_assertions: list[tuple[ScenarioModel, list[ScenarioAssertion]]] = []
    all_persisted_ids: set[str] = set()
    for row in rows:
        parsed_assertions = _parse_assertions(row)
        if not parsed_assertions:
            continue
        rows_assertions.append((row, parsed_assertions))
        for a in parsed_assertions:
            if a.id:
                all_persisted_ids.add(a.id)

    if not rows_assertions:
        return 0, 0

    src_by_idx: list[ScenarioAssertion] = list(evaluated_assertions or [])
    by_id: dict[str, list[tuple[str, str, AssertionEvalResult, ScenarioAssertion | None]]] = {}
    fallback_items: list[tuple[str, str, AssertionEvalResult, ScenarioAssertion | None]] = []
    for item_idx, (method, url_or_path, results, item_defs) in enumerate(non_empty_items):
        eff_method = (executed_method or method or "GET").strip().upper()
        eff_path = (executed_path or url_or_path or "").strip()
        src_list = (
            list(item_defs)
            if item_defs
            else (src_by_idx if len(non_empty_items) == 1 and item_idx == 0 else [])
        )
        src_by_id: dict[str, list[ScenarioAssertion]] = {}
        for sa in src_list:
            if sa.id:
                src_by_id.setdefault(sa.id, []).append(sa)
        src_id_cursor: dict[str, int] = {}
        has_leading_synthetic_status = (
            len(results) == len(src_list) + 1
            and bool(results)
            and str(results[0].id or "").startswith("status-")
            and str(results[0].operator or "").strip().lower() == "status_code"
            and all(str(sa.id or "") != str(results[0].id or "") for sa in src_list)
        )
        for ev_idx, ev in enumerate(results):
            if str(ev.operator or "").strip().lower() == "custom":
                continue
            src_a: ScenarioAssertion | None = None
            if ev.id and ev.id in src_by_id:
                cur = src_id_cursor.get(ev.id, 0)
                bucket = src_by_id[ev.id]
                if cur < len(bucket):
                    src_a = bucket[cur]
                    src_id_cursor[ev.id] = cur + 1
            elif len(src_list) == len(results):
                src_a = src_list[ev_idx]
            elif has_leading_synthetic_status and ev_idx > 0:
                src_a = src_list[ev_idx - 1]
            if ev.id and ev.id in all_persisted_ids:
                by_id.setdefault(ev.id, []).append((eff_method, eff_path, ev, src_a))
            else:
                fallback_items.append((eff_method, eff_path, ev, src_a))

    fallback_by_assertion_key: dict[tuple[int, int], list[AssertionEvalResult]] = {}
    if fallback_items:
        for method, url_or_path, ev, src_a in fallback_items:
            ev_title = (ev.title or "").strip().lower()
            ev_op = (ev.operator or "").strip().lower()
            if not (ev_title and ev_op) or ev_op == "custom":
                continue
            max_endpoint_score = 0
            matched_keys: list[tuple[int, int, int]] = []
            for row_idx, (_row, assertions) in enumerate(rows_assertions):
                for a_idx, a in enumerate(assertions):
                    if str(a.operator or "").strip().lower() == "custom":
                        continue
                    target = a.target_endpoint.strip()
                    if not target:
                        continue
                    score = _match_endpoint_specificity(method, url_or_path, target)
                    if score <= 0:
                        continue
                    if score > max_endpoint_score:
                        max_endpoint_score = score
                    if (
                        (a.title or "").strip().lower() == ev_title
                        and str(a.operator or "").strip().lower() == ev_op
                        and _assertion_definition_matches(a, ev, src_a)
                    ):
                        matched_keys.append((score, row_idx, a_idx))
            min_score = 2 if max_endpoint_score >= 2 else 1
            for score, row_idx, a_idx in matched_keys:
                if score >= min_score:
                    fallback_by_assertion_key.setdefault((row_idx, a_idx), []).append(ev)

    now = datetime.now(UTC)
    synced_scenarios = 0
    synced_assertions = 0

    for row_idx, (row, assertions) in enumerate(rows_assertions):
        row_changed = False
        for a_idx, a in enumerate(assertions):
            if str(a.operator or "").strip().lower() == "custom":
                continue

            matched_evals: list[AssertionEvalResult] = []
            target = a.target_endpoint.strip()
            for eff_method, eff_path, ev, src_a in by_id.get(a.id, ()):
                if target and not endpoint_matches_target(eff_method, eff_path, target):
                    continue
                if not _assertion_definition_matches(a, ev, src_a):
                    continue
                matched_evals.append(ev)

            matched_evals.extend(fallback_by_assertion_key.get((row_idx, a_idx), ()))

            if not matched_evals:
                continue

            new_status: Literal["passed", "failed"] = (
                "failed" if any(not ev.passed for ev in matched_evals) else "passed"
            )
            if a.status != new_status:
                a.status = new_status
                row_changed = True
                synced_assertions += 1

        if row_changed:
            row.assertions_json = json.dumps(
                [a.model_dump() for a in assertions],
                ensure_ascii=False,
            )
            row.updated_at = now
            synced_scenarios += 1

    if synced_scenarios > 0:
        await db.commit()

    return synced_scenarios, synced_assertions


async def _get_scenario_or_404(
    db: AsyncSession,
    project_id: str,
    scenario_id: str,
) -> ScenarioModel:
    await _get_project_or_404(db, project_id)
    row = await db.get(ScenarioModel, scenario_id)
    if row is not None and row.project_id == project_id:
        return row
    # Fallback lookup by slug within the same project
    res = await db.execute(
        select(ScenarioModel).where(
            ScenarioModel.project_id == project_id,
            ScenarioModel.slug == scenario_id,
        )
    )
    by_slug = res.scalar_one_or_none()
    if by_slug is not None:
        return by_slug
    raise HTTPException(
        status_code=404,
        detail=f"Scenario '{scenario_id}' not found in project '{project_id}'",
    )


def _build_scenario_list_response(
    project_id: str,
    all_enriched: list[ScenarioResponse],
    *,
    priority: str | None = None,
    status_filter: str | None = None,
    business_domain: str | None = None,
    effectiveness_level: str | None = None,
    q: str | None = None,
) -> ScenarioListResponse:
    sorted_all = sorted(
        all_enriched,
        key=lambda s: (_PRIORITY_ORDER.get(s.priority, 2), _natural_sort_key(s.slug)),
    )

    by_priority = {p: 0 for p in _ALL_PRIORITIES}
    by_effectiveness = {lvl: 0 for lvl in _ALL_EFFECTIVENESS_LEVELS}
    p0_p1_total = 0
    p0_p1_effective_count = 0

    for sc in sorted_all:
        by_priority[sc.priority] = by_priority.get(sc.priority, 0) + 1
        by_effectiveness[sc.effectiveness_level] = (
            by_effectiveness.get(sc.effectiveness_level, 0) + 1
        )
        if sc.priority in {"P0", "P1"}:
            p0_p1_total += 1
            if sc.effectiveness_level in _EFFECTIVE_LEVELS:
                p0_p1_effective_count += 1

    p0_p1_effective_pct = (
        round((p0_p1_effective_count / p0_p1_total) * 100.0, 2) if p0_p1_total else 0.0
    )

    filtered = sorted_all
    if priority and priority.strip():
        want_p = priority.strip().upper()
        filtered = [s for s in filtered if s.priority.upper() == want_p]
    if status_filter and status_filter.strip():
        want_st = status_filter.strip().lower()
        filtered = [s for s in filtered if s.status.lower() == want_st]
    if business_domain and business_domain.strip():
        want_bd = business_domain.strip().lower()
        filtered = [s for s in filtered if s.business_domain.strip().lower() == want_bd]
    if effectiveness_level and effectiveness_level.strip():
        want_eff = effectiveness_level.strip().upper()
        filtered = [s for s in filtered if s.effectiveness_level.upper() == want_eff]
    if q and q.strip():
        needle = q.strip().lower()
        filtered = [
            s
            for s in filtered
            if needle
            in " ".join(
                [
                    s.slug,
                    s.name,
                    s.description,
                    s.preconditions,
                    s.business_domain,
                    s.owner,
                    " ".join(s.tags),
                ]
            ).lower()
        ]

    return ScenarioListResponse(
        project_id=project_id,
        total=len(filtered),
        by_priority=by_priority,
        by_effectiveness=by_effectiveness,
        p0_p1_total=p0_p1_total,
        p0_p1_effective_count=p0_p1_effective_count,
        p0_p1_effective_pct=p0_p1_effective_pct,
        scenarios=filtered,
    )


@router.get("/projects/{project_id}/scenarios", response_model=ScenarioListResponse)
async def list_project_scenarios(
    project_id: str,
    priority: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    business_domain: str | None = Query(default=None),
    effectiveness_level: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ScenarioListResponse:
    """List project scenarios with lazy auto-hydration and L0..L4 effectiveness metrics."""
    project = await _get_project_or_404(db, project_id)
    completed_runs, inventory = await _load_project_inventory_context(db, project)
    await sync_project_scenarios(db, project_id, inventory=inventory)

    res = await db.execute(
        select(ScenarioModel).where(ScenarioModel.project_id == project_id)
    )
    rows = list(res.scalars().all())
    runs_by_id = {r.id: r for r in completed_runs}
    enriched = [
        _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)[0] for row in rows
    ]
    return _build_scenario_list_response(
        project_id,
        enriched,
        priority=priority,
        status_filter=status_filter,
        business_domain=business_domain,
        effectiveness_level=effectiveness_level,
        q=q,
    )


@router.post("/projects/{project_id}/scenarios/sync")
async def trigger_scenario_sync(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Explicitly hydrate new scenarios discovered in completed runs into the scenarios table."""
    project = await _get_project_or_404(db, project_id)
    completed_runs, inventory = await _load_project_inventory_context(db, project)
    created = await sync_project_scenarios(db, project_id, inventory=inventory)

    res = await db.execute(
        select(ScenarioModel).where(ScenarioModel.project_id == project_id)
    )
    rows = list(res.scalars().all())
    runs_by_id = {r.id: r for r in completed_runs}
    enriched = [
        _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)[0] for row in rows
    ]
    list_resp = _build_scenario_list_response(project_id, enriched)
    return {
        "synced": True,
        "created_count": len(created),
        "created_slugs": [row.slug for row in created],
        **list_resp.model_dump(mode="json"),
    }


@router.post(
    "/projects/{project_id}/scenarios",
    response_model=ScenarioResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_scenario(
    project_id: str,
    body: ScenarioCreateRequest,
    upsert: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> ScenarioResponse:
    """Create a first-class Scenario (or upsert by slug when upsert=true)."""
    project = await _get_project_or_404(db, project_id)
    raw_slug = (body.slug or "").strip()
    raw_name = (body.name or "").strip()
    if not raw_slug and not raw_name:
        raise HTTPException(
            status_code=422,
            detail="Either 'name' or 'slug' must be provided when creating a scenario.",
        )
    slug = normalize_scenario_slug(raw_slug or raw_name)
    resolved_name = raw_name or humanize_scenario_slug(slug)
    now = datetime.now(UTC)

    res = await db.execute(
        select(ScenarioModel).where(
            ScenarioModel.project_id == project_id,
            ScenarioModel.slug == slug,
        )
    )
    existing = res.scalar_one_or_none()

    clean_tags = list(dict.fromkeys(t.strip() for t in body.tags if t.strip()))
    assertions_dump = [a.model_dump() for a in body.assertions]
    neg_dump = [nc.model_dump() for nc in body.negative_cases]
    fields_set = body.model_fields_set

    if existing is not None:
        if not upsert:
            raise HTTPException(
                status_code=409,
                detail=f"Scenario with slug '{slug}' already exists in project '{project_id}'",
            )
        if "name" in fields_set and raw_name:
            existing.name = raw_name
        if "description" in fields_set:
            existing.description = body.description
        if "preconditions" in fields_set:
            existing.preconditions = body.preconditions
        if "business_domain" in fields_set:
            existing.business_domain = body.business_domain.strip()
        if "priority" in fields_set:
            existing.priority = body.priority
        if "status" in fields_set:
            existing.status = body.status
        if "owner" in fields_set:
            existing.owner = body.owner.strip()
        if "source" in fields_set:
            existing.source = body.source
        if "tags" in fields_set:
            existing.tags_json = json.dumps(clean_tags, ensure_ascii=False)
        if "assertions" in fields_set:
            existing.assertions_json = json.dumps(assertions_dump, ensure_ascii=False)
        if "negative_cases" in fields_set:
            existing.negative_cases_json = json.dumps(neg_dump, ensure_ascii=False)
        existing.updated_at = now
        row = existing
    else:
        row = ScenarioModel(
            project_id=project_id,
            slug=slug,
            name=resolved_name,
            description=body.description,
            preconditions=body.preconditions,
            business_domain=body.business_domain.strip(),
            priority=body.priority,
            status=body.status,
            owner=body.owner.strip(),
            source=body.source,
            tags_json=json.dumps(clean_tags, ensure_ascii=False),
            assertions_json=json.dumps(assertions_dump, ensure_ascii=False),
            negative_cases_json=json.dumps(neg_dump, ensure_ascii=False),
            created_at=now,
            updated_at=now,
        )
        db.add(row)

    await db.commit()
    await db.refresh(row)

    completed_runs, inventory = await _load_project_inventory_context(db, project)
    runs_by_id = {r.id: r for r in completed_runs}
    enriched, _, _ = _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)
    return enriched


@router.get("/projects/{project_id}/scenarios/{scenario_id}", response_model=ScenarioDetailResponse)
async def get_project_scenario(
    project_id: str,
    scenario_id: str,
    db: AsyncSession = Depends(get_db),
) -> ScenarioDetailResponse:
    """Get a single scenario with dynamically linked inventory endpoints and run history."""
    project = await _get_project_or_404(db, project_id)
    row = await _get_scenario_or_404(db, project_id, scenario_id)
    completed_runs, inventory = await _load_project_inventory_context(db, project)
    runs_by_id = {r.id: r for r in completed_runs}
    enriched, endpoints, runs = _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)
    return ScenarioDetailResponse(**enriched.model_dump(), endpoints=endpoints, runs=runs)


@router.patch("/projects/{project_id}/scenarios/{scenario_id}", response_model=ScenarioResponse)
async def update_project_scenario(
    project_id: str,
    scenario_id: str,
    body: ScenarioUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> ScenarioResponse:
    """Partially update editable fields, assertions, or negative cases of a scenario."""
    project = await _get_project_or_404(db, project_id)
    row = await _get_scenario_or_404(db, project_id, scenario_id)

    if body.name is not None:
        row.name = body.name.strip()
    if body.description is not None:
        row.description = body.description
    if body.preconditions is not None:
        row.preconditions = body.preconditions
    if body.business_domain is not None:
        row.business_domain = body.business_domain.strip()
    if body.priority is not None:
        row.priority = body.priority
    if body.status is not None:
        row.status = body.status
    if body.owner is not None:
        row.owner = body.owner.strip()
    if body.source is not None:
        row.source = body.source
    if body.tags is not None:
        clean_tags = list(dict.fromkeys(t.strip() for t in body.tags if t.strip()))
        row.tags_json = json.dumps(clean_tags, ensure_ascii=False)
    if body.assertions is not None:
        row.assertions_json = json.dumps(
            [a.model_dump() for a in body.assertions], ensure_ascii=False
        )
    if body.negative_cases is not None:
        row.negative_cases_json = json.dumps(
            [nc.model_dump() for nc in body.negative_cases], ensure_ascii=False
        )

    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)

    completed_runs, inventory = await _load_project_inventory_context(db, project)
    runs_by_id = {r.id: r for r in completed_runs}
    enriched, _, _ = _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)
    return enriched


@router.delete("/projects/{project_id}/scenarios/{scenario_id}")
async def delete_project_scenario(
    project_id: str,
    scenario_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Delete a scenario from a project."""
    row = await _get_scenario_or_404(db, project_id, scenario_id)
    deleted_id = row.id
    deleted_slug = row.slug
    await db.delete(row)
    await db.commit()
    return {"deleted": True, "id": deleted_id, "slug": deleted_slug}


# --- Business Capability Map & One-Click 5-Case Scenario Promotion ---


_ROLE_LABELS_ZH: dict[str, str] = {
    "admin": "管理员",
    "pharmacy": "药店",
    "doctor": "医生",
    "assistant": "医助",
    "patient": "患者",
    "common": "通用",
}


@router.get("/projects/{project_id}/capabilities", response_model=ProjectCapabilityMapResponse)
async def get_project_capabilities(
    project_id: str,
    module: str | None = Query(default=None),
    role: str | None = Query(default=None),
    core_only: bool = Query(default=False),
    live_only: bool = Query(default=False),
    has_writes: bool = Query(default=False),
    include_noise: bool = Query(default=False),
    q: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return Business Capability Map (Resource x Action x Role x State) + 5-dim QA metrics."""
    project = await _get_project_or_404(db, project_id)
    completed_runs, inventory = await _load_project_inventory_context(db, project)
    await sync_project_scenarios(db, project_id, inventory=inventory)

    res = await db.execute(select(ScenarioModel).where(ScenarioModel.project_id == project_id))
    rows = list(res.scalars().all())
    runs_by_id = {r.id: r for r in completed_runs}
    enriched_scenarios = [
        _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)[0] for row in rows
    ]

    cap_map = build_capability_map(
        inventory=inventory,
        scenarios=enriched_scenarios,
        project_id=project.id,
        project_name=project.name,
        include_noise=include_noise,
    )

    filtered_caps = cap_map["capabilities"]
    if core_only:
        filtered_caps = [c for c in filtered_caps if c["is_core"]]
    if live_only:
        filtered_caps = [c for c in filtered_caps if c["live_endpoints"] > 0]
    if has_writes:
        filtered_caps = [c for c in filtered_caps if c["write_endpoints"] > 0]
    if module and module.strip():
        want_mod = module.strip().lower()
        filtered_caps = [c for c in filtered_caps if c["module"].lower() == want_mod]
    if role and role.strip():
        want_role = role.strip().lower()
        filtered_caps = [
            c for c in filtered_caps if any(r.lower() == want_role for r in c["roles"])
        ]
    if q and q.strip():
        needle = q.strip().lower()
        filtered_caps = [
            c
            for c in filtered_caps
            if needle in c["key"].lower()
            or needle in c["display_name"].lower()
            or needle in c["title_zh"].lower()
            or needle in c["title_en"].lower()
            or any(needle in a.lower() for a in c["actions"])
            or any(needle in ep["path"].lower() for ep in c["endpoints"])
        ]

    return {
        "project_id": cap_map["project_id"],
        "project_name": cap_map["project_name"],
        "metrics": cap_map["metrics"],
        "modules": cap_map["modules"],
        "capabilities": filtered_caps,
    }


@router.post(
    "/projects/{project_id}/capabilities/promote-scenario",
    response_model=ScenarioResponse,
    status_code=status.HTTP_201_CREATED,
)
async def promote_capability_to_scenario(
    project_id: str,
    body: PromoteCapabilityRequest,
    db: AsyncSession = Depends(get_db),
) -> ScenarioResponse:
    """Materialize a 5-case effectiveness Scenario for a Business Capability (idempotent)."""
    project = await _get_project_or_404(db, project_id)
    completed_runs, inventory = await _load_project_inventory_context(db, project)

    res = await db.execute(select(ScenarioModel).where(ScenarioModel.project_id == project_id))
    rows = list(res.scalars().all())
    runs_by_id = {r.id: r for r in completed_runs}
    enriched_scenarios = [
        _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)[0] for row in rows
    ]

    cap_map = build_capability_map(
        inventory=inventory,
        scenarios=enriched_scenarios,
        project_id=project.id,
        project_name=project.name,
    )
    target_key = body.capability_key.strip().lower()
    cap = next(
        (c for c in cap_map["capabilities"] if c["key"].lower() == target_key),
        None,
    )
    if cap is None:
        raise HTTPException(
            status_code=404,
            detail=f"Capability '{body.capability_key}' not found in project '{project_id}'",
        )

    cap_slug = normalize_scenario_slug(f"cap_{slugify(cap['key'])}")
    existing_res = await db.execute(
        select(ScenarioModel).where(
            ScenarioModel.project_id == project_id,
            ScenarioModel.slug == cap_slug,
        )
    )
    existing = existing_res.scalar_one_or_none()
    if existing is not None:
        # Idempotent & non-destructive: preserve user-edited scenario fields on repeat calls
        enriched, _, _ = _enrich_scenario(existing, inventory=inventory, runs_by_id=runs_by_id)
        return enriched

    # Pick representative primary endpoint (prefer har-live write, then har-live read, then first)
    cap_eps = cap["endpoints"]
    primary_ep = cap_eps[0] if cap_eps else {"method": "GET", "path": f"/{cap['key']}"}
    primary_target = f"{primary_ep['method']} {primary_ep['path']}"
    write_ep = next(
        (e for e in cap_eps if e["method"] in {"POST", "PUT", "PATCH", "DELETE"}),
        primary_ep,
    )
    write_target = f"{write_ep['method']} {write_ep['path']}"

    roles_str = ", ".join(cap["roles"]) or "common"
    roles_zh_str = "、".join(_ROLE_LABELS_ZH.get(r, r) for r in (cap["roles"] or ["common"]))
    actions_str = ", ".join(cap["actions"]) or "read"
    ep_sample_lines = "\n".join(
        f"- `{e['method']} {e['path']}` (`{e['role']}` · `{e['action']}` · `{e['discovery_source']}`)"
        for e in cap_eps[:8]
    )

    scenario_priority = body.priority or cap["recommended_priority"]

    if body.lang == "en":
        statuses_lines = (
            "\n".join(
                f"- `{field}`: `{', '.join(vals)}`"
                for field, vals in (cap["observed_statuses"] or {}).items()
            )
            if cap["observed_statuses"]
            else "- (No status enum fields observed in HAR examples yet)"
        )
        cap_title = cap.get("title_en") or cap["display_name"]
        description_md = f"""### Business Capability: {cap_title} (`{cap['key']}`)

- **Actions**: `{actions_str}`
- **Roles**: `{roles_str}`
- **Endpoints**: `{cap['live_endpoints']} live / {cap['total_endpoints']} total ({cap['write_endpoints']} writes)`

#### Observed State-Machine Enums
{statuses_lines}

#### Representative Endpoints
{ep_sample_lines}

#### 4-Class Verification Oracle Checklist
1. **Schema Contract**: Validate response structure, field types, and non-null business payload.
2. **HAR Baseline**: Compare live status codes and business success envelope against captured HARs.
3. **Business Invariants**: Verify state enum legality, non-negative totals, and role scoping.
4. **Cross-Resource Consistency**: Confirm mutations reflect consistently on subsequent detail/list reads.
""".strip()
        preconditions_txt = f"Authenticated test credentials available for role(s): {roles_str}."
        scenario_name = (body.name or "").strip() or f"{cap_title} (Core Capability Verification)"
        seeded_assertions = [
            ScenarioAssertion(
                title=f"[Happy Path] {cap_title} returns valid business payload",
                target_endpoint=primary_target,
                jsonpath="$.data",
                operator="not_empty",
                note="Verify primary capability endpoint returns non-empty business data payload.",
                status="pending",
            ),
            ScenarioAssertion(
                title=f"[Invariant] {cap_title} state & cross-resource consistency",
                target_endpoint=write_target,
                jsonpath="",
                operator="custom",
                note=(
                    f"Verify state transition & read-after-write consistency across actions ({actions_str})."
                ),
                status="pending",
            ),
        ]
        seeded_negative_cases = [
            ScenarioNegativeCase(
                title=f"[Invalid Params] Reject missing/malformed inputs on {write_target}",
                category="validation",
                expected_behavior="Returns HTTP 400 or business validation error without 5xx.",
                status="pending",
            ),
            ScenarioNegativeCase(
                title=f"[Illegal State] Block invalid state transition on {cap_title}",
                category="state_machine",
                expected_behavior="Disallowed status transition is rejected cleanly without mutating state.",
                status="pending",
            ),
            ScenarioNegativeCase(
                title=f"[AuthZ / Role] Verify cross-role BFLA/IDOR isolation ({roles_str})",
                category="auth",
                expected_behavior="Anonymous and unauthorized roles receive 401/403 or business denial code.",
                status="pending",
            ),
            ScenarioNegativeCase(
                title=f"[Idempotency] Verify duplicate submission safety on {write_target}",
                category="idempotency",
                expected_behavior="Replaying identical mutation does not create duplicate records or double charges.",
                status="pending",
            ),
        ]
    else:
        statuses_lines_zh = (
            "\n".join(
                f"- `{field}`: `{', '.join(vals)}`"
                for field, vals in (cap["observed_statuses"] or {}).items()
            )
            if cap["observed_statuses"]
            else "- （当前 HAR 样本尚未观测到显式状态枚举字段）"
        )
        cap_title_zh = cap.get("title_zh") or cap["display_name"]
        description_md = f"""### 业务能力：{cap_title_zh}（`{cap['key']}`）

- **涉及动作**：`{actions_str}`
- **涉及角色**：{roles_zh_str}（`{roles_str}`）
- **接口规模**：活跃 `{cap['live_endpoints']}` / 总计 `{cap['total_endpoints']}`（含 `{cap['write_endpoints']}` 个写接口）

#### 观测到的状态机枚举
{statuses_lines_zh}

#### 代表性接口
{ep_sample_lines}

#### 4 类验证 Oracle 检查清单
1. **契约结构（Schema）**：校验响应字段结构、类型与业务数据非空。
2. **HAR 基线对照**：对比线上/测试环境 HTTP 状态码与业务成功码，确保无非预期回归。
3. **业务不变量**：验证状态机合法枚举、金额/数量非负与角色可见范围。
4. **跨资源一致性**：写操作完成后回查详情与列表接口，确认状态与数据一致落库。
""".strip()
        preconditions_txt = f"已就绪以下业务角色的测试账号凭证：{roles_zh_str}（{roles_str}）。"
        scenario_name = (body.name or "").strip() or f"{cap_title_zh}（核心能力验收）"
        seeded_assertions = [
            ScenarioAssertion(
                title=f"[正向流程] {cap_title_zh}主接口返回有效业务数据",
                target_endpoint=primary_target,
                jsonpath="$.data",
                operator="not_empty",
                note="验证主业务能力接口返回非空且结构合法的业务响应。",
                status="pending",
            ),
            ScenarioAssertion(
                title=f"[业务不变量] {cap_title_zh}状态流转与跨资源一致性校验",
                target_endpoint=write_target,
                jsonpath="",
                operator="custom",
                note=f"验证跨动作（{actions_str}）的状态迁移合法性与写后读一致性。",
                status="pending",
            ),
        ]
        seeded_negative_cases = [
            ScenarioNegativeCase(
                title=f"[缺参/非法参] {write_target} 拒绝缺失或非法参数输入",
                category="validation",
                expected_behavior="返回 HTTP 400 或明确业务参数校验错误，不出现 5xx。",
                status="pending",
            ),
            ScenarioNegativeCase(
                title=f"[非法状态迁移] {cap_title_zh}拦截非法状态跃迁",
                category="state_machine",
                expected_behavior="针对不合法前置状态的写操作被安全拒绝，且不脏改原始状态。",
                status="pending",
            ),
            ScenarioNegativeCase(
                title=f"[越权/错角色] 验证跨角色 BFLA/IDOR 隔离（{roles_zh_str}）",
                category="auth",
                expected_behavior="匿名或无权角色请求返回 401/403 或明确业务拒权错误码。",
                status="pending",
            ),
            ScenarioNegativeCase(
                title=f"[一致性/幂等] 验证 {write_target} 重复提交幂等安全",
                category="idempotency",
                expected_behavior="重复重放相同写请求不会产生重复单据、重复扣款或状态错乱。",
                status="pending",
            ),
        ]

    tags = list(dict.fromkeys([cap["module"], cap["resource"], *cap["roles"]]))
    now = datetime.now(UTC)
    row = ScenarioModel(
        project_id=project_id,
        slug=cap_slug,
        name=scenario_name,
        description=description_md,
        preconditions=preconditions_txt,
        business_domain=cap["module"],
        priority=scenario_priority,
        status="active",
        owner="",
        source="ai-agent",
        tags_json=json.dumps(tags, ensure_ascii=False),
        assertions_json=json.dumps([a.model_dump() for a in seeded_assertions], ensure_ascii=False),
        negative_cases_json=json.dumps(
            [nc.model_dump() for nc in seeded_negative_cases], ensure_ascii=False
        ),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    enriched, _, _ = _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)
    return enriched
