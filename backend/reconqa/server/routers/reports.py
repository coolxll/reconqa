"""Findings, security/performance reports, and historical project trend endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.models.db import get_db
from reconqa.models.schemas import FindingOut
from reconqa.models.tables import Finding, Project, Run
from reconqa.utils.io import read_json, read_jsonl

router = APIRouter(tags=["reports"])


async def _get_run_or_404(db: AsyncSession, run_id: str) -> Run:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


@router.get("/runs/{run_id}/findings", response_model=list[FindingOut])
async def list_run_findings(
    run_id: str,
    category: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[Finding]:
    await _get_run_or_404(db, run_id)
    stmt = select(Finding).where(Finding.run_id == run_id)
    if category:
        stmt = stmt.where(Finding.category == category.lower())
    if severity:
        stmt = stmt.where(Finding.severity == severity.lower())
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.get("/runs/{run_id}/performance")
async def get_run_performance(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    perf_dir = Path(run.workspace_dir) / "performance"
    har_report_path = perf_dir / "har_perf_report.json"
    k6_analysis_path = perf_dir / "k6" / "k6_analysis.json"
    k6_script_path = perf_dir / "k6" / "scenario_ramp.js"

    if not har_report_path.is_file() and not k6_analysis_path.is_file():
        raise HTTPException(status_code=404, detail="Performance report not found for this run")

    return {
        "run_id": run_id,
        "har_profiler": read_json(har_report_path) if har_report_path.is_file() else None,
        "k6_analysis": read_json(k6_analysis_path) if k6_analysis_path.is_file() else None,
        "k6_script_available": k6_script_path.is_file(),
    }


@router.get("/runs/{run_id}/security")
async def get_run_security(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    sec_dir = Path(run.workspace_dir) / "security"
    static_path = sec_dir / "static_audit.json"
    auth_path = sec_dir / "auth_matrix.json"
    nuclei_path = sec_dir / "nuclei.jsonl"
    burp_dir = sec_dir / "burp_exports"

    if not static_path.is_file() and not auth_path.is_file() and not nuclei_path.is_file():
        raise HTTPException(status_code=404, detail="Security report not found for this run")

    return {
        "run_id": run_id,
        "static_audit": read_json(static_path) if static_path.is_file() else None,
        "auth_matrix": read_json(auth_path) if auth_path.is_file() else None,
        "nuclei": read_jsonl(nuclei_path) if nuclei_path.is_file() else [],
        "burp_exports_available": burp_dir.is_dir() and any(burp_dir.iterdir()),
    }


@router.get("/projects/{project_id}/trends")
async def get_project_trends(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    res = await db.execute(
        select(Run).where(Run.project_id == project_id).order_by(Run.started_at.asc())
    )
    runs = list(res.scalars().all())
    points: list[dict[str, Any]] = []
    for r in runs:
        m = r.summary_metrics or {}
        points.append(
            {
                "run_id": r.id,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "total_endpoints": m.get("total_endpoints", 0),
                "har_live_endpoints": m.get("har_live_endpoints", 0),
                "coverage_pct": m.get("coverage_pct", 0.0),
                "correlation_chains": m.get("correlation_chains", 0),
                "findings_total": m.get("findings_total", 0),
                "findings_by_severity": m.get("findings_by_severity", {}),
                "slow_endpoints_count": m.get("slow_endpoints_count", 0),
            }
        )

    return {
        "project_id": project_id,
        "project_name": project.name,
        "total_runs": len(points),
        "trends": points,
    }
