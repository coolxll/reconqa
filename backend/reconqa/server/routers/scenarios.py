"""First-class Scenario entity CRUD, auto-hydration, and testing effectiveness endpoints."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from reconqa.models.db import get_db
from reconqa.models.schemas import (
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
from reconqa.modules.hub.capability_map import build_capability_map
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

    for raw_ep in inventory.get("endpoints") or []:
        ep_scenarios = raw_ep.get("scenarios") or []
        if row.slug in ep_scenarios:
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


@router.get("/projects/{project_id}/capabilities", response_model=ProjectCapabilityMapResponse)
async def get_project_capabilities(
    project_id: str,
    module: str | None = Query(default=None),
    role: str | None = Query(default=None),
    live_only: bool = Query(default=False),
    has_writes: bool = Query(default=False),
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
    )

    filtered_caps = cap_map["capabilities"]
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
    actions_str = ", ".join(cap["actions"]) or "read"
    statuses_lines = (
        "\n".join(
            f"- `{field}`: `{', '.join(vals)}`"
            for field, vals in (cap["observed_statuses"] or {}).items()
        )
        if cap["observed_statuses"]
        else "- (No status enum fields observed in HAR examples yet)"
    )
    ep_sample_lines = "\n".join(
        f"- `{e['method']} {e['path']}` (`{e['role']}` · `{e['action']}` · `{e['discovery_source']}`)"
        for e in cap_eps[:8]
    )

    description_md = f"""### Business Capability: `{cap['display_name']}`

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
    scenario_name = (body.name or "").strip() or f"{cap['module']} / {cap['resource']} 核心能力验证"
    scenario_priority = body.priority or cap["recommended_priority"]

    seeded_assertions = [
        ScenarioAssertion(
            title=f"[Happy Path] {cap['display_name']} returns valid business payload",
            target_endpoint=primary_target,
            jsonpath="$.data",
            operator="not_empty",
            note="Verify primary capability endpoint returns non-empty business data payload.",
            status="pending",
        ),
        ScenarioAssertion(
            title=f"[Invariant] {cap['display_name']} state & cross-resource consistency",
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
            title=f"[Illegal State] Block invalid state transition on {cap['display_name']}",
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
