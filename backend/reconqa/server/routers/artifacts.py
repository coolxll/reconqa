"""Artifact inspection, on-the-fly zip downloads, and on-demand Burp export."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.models.db import get_db
from reconqa.models.schemas import ArtifactOut
from reconqa.models.tables import Artifact, Run
from reconqa.modules.security.burp_exporter import export_burp_requests
from reconqa.utils.io import ensure_dir, read_json, read_yaml

router = APIRouter(tags=["artifacts"])

KIND_PATH_ALIASES: dict[str, str] = {
    "openapi_yaml": "hub/openapi.yaml",
    "openapi_json": "hub/openapi.json",
    "correlation_json": "hub/correlation.json",
    "correlation_mmd": "hub/correlation.mmd",
    "coverage_diff_json": "hub/coverage_diff.json",
    "coverage_diff_md": "hub/coverage_diff.md",
    "java-tests": "java-tests",
    "java_tests": "java-tests",
    "java_tests_dir": "java-tests",
    "burp-exports": "security/burp_exports",
    "burp_exports": "security/burp_exports",
    "burp_exports_dir": "security/burp_exports",
    "static_audit_json": "security/static_audit.json",
    "static_audit_md": "security/static_audit.md",
    "auth_matrix_json": "security/auth_matrix.json",
    "auth_matrix_md": "security/auth_matrix.md",
    "har_perf_json": "performance/har_perf_report.json",
    "har_perf_md": "performance/har_perf_report.md",
    "k6": "performance/k6",
    "k6_dir": "performance/k6",
    "k6_script": "performance/k6/scenario_ramp.js",
    "run_summary": "run_summary.json",
    "run_summary_json": "run_summary.json",
}


class BurpExportRequest(BaseModel):
    endpoints: list[str] | None = None
    target_domain: str | None = None


async def _get_run_or_404(db: AsyncSession, run_id: str) -> Run:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


def _safe_resolve(run_dir: Path, rel_path: str) -> Path:
    base = run_dir.resolve()
    target = (base / rel_path).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid artifact path")
    return target


def _zip_directory(directory: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(directory.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, arcname=file_path.relative_to(directory).as_posix())
    buf.seek(0)
    return buf


@router.get("/runs/{run_id}/openapi")
async def get_run_openapi(
    run_id: str,
    format: Literal["json", "yaml"] = Query(default="json"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    run = await _get_run_or_404(db, run_id)
    hub_dir = Path(run.workspace_dir) / "hub"
    json_path = hub_dir / "openapi.json"
    yaml_path = hub_dir / "openapi.yaml"

    if format == "yaml":
        if yaml_path.is_file():
            return PlainTextResponse(
                yaml_path.read_text(encoding="utf-8"),
                media_type="application/yaml",
            )
        if json_path.is_file():
            spec = read_json(json_path)
            return PlainTextResponse(
                yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
                media_type="application/yaml",
            )
        raise HTTPException(status_code=404, detail="OpenAPI spec not generated for this run")

    if json_path.is_file():
        return JSONResponse(content=read_json(json_path))
    if yaml_path.is_file():
        return JSONResponse(content=read_yaml(yaml_path))
    raise HTTPException(status_code=404, detail="OpenAPI spec not generated for this run")


@router.get("/runs/{run_id}/correlation")
async def get_run_correlation(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    hub_dir = Path(run.workspace_dir) / "hub"
    corr_json = hub_dir / "correlation.json"
    corr_mmd = hub_dir / "correlation.mmd"
    if not corr_json.is_file():
        raise HTTPException(status_code=404, detail="Correlation report not found for this run")
    data = read_json(corr_json)
    if isinstance(data, dict) and not data.get("mermaid") and corr_mmd.is_file():
        data["mermaid"] = corr_mmd.read_text(encoding="utf-8")
    return data


@router.get("/runs/{run_id}/coverage")
async def get_run_coverage(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    cov_path = Path(run.workspace_dir) / "hub" / "coverage_diff.json"
    if not cov_path.is_file():
        raise HTTPException(status_code=404, detail="Coverage diff report not found for this run")
    return read_json(cov_path)


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactOut])
async def list_run_artifacts(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[Artifact]:
    await _get_run_or_404(db, run_id)
    res = await db.execute(select(Artifact).where(Artifact.run_id == run_id))
    return list(res.scalars().all())


@router.get("/runs/{run_id}/artifacts/{kind}")
async def download_run_artifact(
    run_id: str,
    kind: str,
    db: AsyncSession = Depends(get_db),
) -> Any:
    run = await _get_run_or_404(db, run_id)
    run_dir = Path(run.workspace_dir)

    target_path: Path | None = None
    # 1. Check DB Artifact table by kind
    res = await db.execute(
        select(Artifact).where(Artifact.run_id == run_id, Artifact.kind == kind)
    )
    artifact_row = res.scalars().first()
    if artifact_row is not None:
        candidate = _safe_resolve(run_dir, artifact_row.rel_path)
        if candidate.exists():
            target_path = candidate

    # 2. Check known friendly kind aliases
    if target_path is None and kind in KIND_PATH_ALIASES:
        candidate = _safe_resolve(run_dir, KIND_PATH_ALIASES[kind])
        if candidate.exists():
            target_path = candidate

    if target_path is None or not target_path.exists():
        raise HTTPException(
            status_code=404, detail=f"Artifact '{kind}' not found for run '{run_id}'"
        )

    if target_path.is_dir():
        zip_buf = _zip_directory(target_path)
        return StreamingResponse(
            zip_buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{kind}.zip"'},
        )

    return FileResponse(path=target_path, filename=target_path.name)


@router.post("/runs/{run_id}/burp-export")
async def trigger_burp_export(
    run_id: str,
    body: BurpExportRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    run_dir = Path(run.workspace_dir)
    har_dir = run_dir / "har"
    if not har_dir.is_dir() or not any(har_dir.glob("*.har")):
        raise HTTPException(status_code=400, detail="No HAR files available in this run")

    sec_dir = ensure_dir(run_dir / "security")
    am_path = sec_dir / "auth_matrix.json"
    req = body or BurpExportRequest()
    result = export_burp_requests(
        har_dir,
        sec_dir,
        auth_matrix_path=am_path if am_path.is_file() else None,
        target_domain=req.target_domain,
        endpoints=req.endpoints,
    )

    burp_dir = sec_dir / "burp_exports"
    if burp_dir.is_dir():
        size = sum(p.stat().st_size for p in burp_dir.rglob("*") if p.is_file())
        existing_res = await db.execute(
            select(Artifact).where(Artifact.run_id == run_id, Artifact.kind == "burp_exports")
        )
        existing = existing_res.scalars().first()
        rel = burp_dir.relative_to(run_dir).as_posix()
        if existing is not None:
            existing.size_bytes = size
            existing.rel_path = rel
        else:
            db.add(
                Artifact(
                    run_id=run_id,
                    kind="burp_exports",
                    rel_path=rel,
                    size_bytes=size,
                    sha256="",
                )
            )
        await db.commit()

    return {"run_id": run_id, "burp_export": result}
