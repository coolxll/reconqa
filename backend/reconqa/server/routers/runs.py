"""Run lifecycle, HAR upload/reuse, execution trigger, and SSE streaming endpoints."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from reconqa.config import settings
from reconqa.models.db import get_db
from reconqa.models.schemas import RunCreate, RunOut
from reconqa.models.tables import Artifact, Job, Project, Run
from reconqa.modules.recorder.har_importer import import_hars
from reconqa.pipeline import DEFAULT_MODULES
from reconqa.server.worker import InProcessTaskRunner
from reconqa.utils.io import ensure_dir, sha256_file

router = APIRouter(tags=["runs"])


class HarReuseRequest(BaseModel):
    source_run_id: str
    scenarios: list[str] | None = None


def _get_runner(request: Request) -> InProcessTaskRunner:
    runner = getattr(request.app.state, "task_runner", None)
    if runner is None:
        runner = InProcessTaskRunner()
        request.app.state.task_runner = runner
    return runner


async def _get_run_with_jobs(db: AsyncSession, run_id: str) -> Run:
    res = await db.execute(
        select(Run).options(selectinload(Run.jobs)).where(Run.id == run_id)
    )
    run = res.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


async def _upsert_har_artifacts(db: AsyncSession, run: Run) -> None:
    run_dir = Path(run.workspace_dir)
    har_dir = run_dir / "har"
    if not har_dir.is_dir():
        return
    existing_res = await db.execute(
        select(Artifact).where(Artifact.run_id == run.id, Artifact.kind == "har")
    )
    existing_by_path = {a.rel_path: a for a in existing_res.scalars().all()}
    for har_file in sorted(har_dir.glob("*.har")):
        rel = har_file.relative_to(run_dir).as_posix()
        digest = sha256_file(har_file)
        size = har_file.stat().st_size
        if rel in existing_by_path:
            existing_by_path[rel].sha256 = digest
            existing_by_path[rel].size_bytes = size
        else:
            db.add(
                Artifact(
                    run_id=run.id,
                    kind="har",
                    rel_path=rel,
                    size_bytes=size,
                    sha256=digest,
                )
            )
    await db.commit()


@router.post(
    "/projects/{project_id}/runs",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    project_id: str,
    body: RunCreate,
    request: Request,
    auto_start: bool = Query(
        default=True,
        description="Enqueue immediately if True; leave pending for HAR upload if False.",
    ),
    db: AsyncSession = Depends(get_db),
) -> Run:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    effective_auto_start = auto_start
    if "auto_start" in body.config and body.config["auto_start"] is False:
        effective_auto_start = False

    modules = list(body.modules or DEFAULT_MODULES)
    config_snapshot: dict[str, Any] = dict(body.config or {})
    config_snapshot["modules"] = modules
    if body.target_url:
        config_snapshot["target_url"] = body.target_url

    run = Run(
        project_id=project_id,
        target_id=body.target_id,
        status="queued" if effective_auto_start else "pending",
        trigger_source=body.trigger_source,
        config_snapshot=config_snapshot,
    )
    db.add(run)
    await db.flush()

    workspace_dir = ensure_dir(settings.data_dir.resolve() / "runs" / run.id)
    ensure_dir(workspace_dir / "har")
    run.workspace_dir = str(workspace_dir)

    for mod in modules:
        db.add(
            Job(
                run_id=run.id,
                module=mod,
                status="queued" if effective_auto_start else "pending",
            )
        )

    await db.commit()
    run = await _get_run_with_jobs(db, run.id)

    if effective_auto_start:
        runner = _get_runner(request)
        await runner.enqueue(run.id)

    return run


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
async def list_project_runs(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[Run]:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    res = await db.execute(
        select(Run)
        .options(selectinload(Run.jobs))
        .where(Run.project_id == project_id)
        .order_by(Run.started_at.desc())
    )
    return list(res.scalars().all())


@router.post("/runs/{run_id}/hars")
async def upload_run_hars(
    run_id: str,
    files: list[UploadFile] = File(...),
    scenario: str | None = Form(default=None),
    target_domain: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_with_jobs(db, run_id)
    if not files:
        raise HTTPException(status_code=400, detail="At least one HAR file is required")

    run_dir = Path(run.workspace_dir)
    har_dir = ensure_dir(run_dir / "har")

    with tempfile.TemporaryDirectory(prefix="reconqa_har_upload_") as tmp_str:
        tmp_dir = Path(tmp_str)
        saved_paths: list[Path] = []
        for idx, upload in enumerate(files):
            raw_name = Path(upload.filename or f"upload_{idx}.har").name
            if not raw_name.endswith(".har"):
                raw_name = f"{raw_name}.har"
            dest = tmp_dir / raw_name
            content = await upload.read()
            dest.write_bytes(content)
            saved_paths.append(dest)

        scenario_override = scenario if (scenario and len(saved_paths) == 1) else None
        import_res = import_hars(
            saved_paths,
            har_dir,
            scenario_name=scenario_override,
            target_domain=target_domain,
            extract_js=True,
        )

    await _upsert_har_artifacts(db, run)
    har_files = [p.name for p in sorted(har_dir.glob("*.har"))]
    return {
        "run_id": run_id,
        "import_result": import_res,
        "har_files": har_files,
        "total_hars": len(har_files),
    }


@router.post("/runs/{run_id}/hars/reuse")
async def reuse_run_hars(
    run_id: str,
    body: HarReuseRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    target_run = await _get_run_with_jobs(db, run_id)
    source_run = await db.get(Run, body.source_run_id)
    if source_run is None:
        raise HTTPException(status_code=404, detail=f"Source run '{body.source_run_id}' not found")
    if source_run.project_id != target_run.project_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot reuse HARs across different projects",
        )

    src_har_dir = Path(source_run.workspace_dir) / "har"
    if not src_har_dir.is_dir():
        raise HTTPException(status_code=404, detail="Source run has no HAR directory")

    dst_har_dir = ensure_dir(Path(target_run.workspace_dir) / "har")
    existing_hashes = {sha256_file(p) for p in dst_har_dir.glob("*.har")}

    reused: list[str] = []
    skipped_duplicates: list[str] = []
    allowed_scenarios = set(body.scenarios) if body.scenarios else None

    for src_har in sorted(src_har_dir.glob("*.har")):
        if allowed_scenarios is not None and src_har.stem not in allowed_scenarios:
            continue
        digest = sha256_file(src_har)
        if digest in existing_hashes:
            skipped_duplicates.append(src_har.name)
            continue

        dst_har = dst_har_dir / src_har.name
        if dst_har.exists():
            dst_har = dst_har_dir / f"{src_har.stem}_{digest[:8]}.har"
        shutil.copy2(src_har, dst_har)
        existing_hashes.add(digest)
        reused.append(dst_har.name)

        # Also carry over extracted JS bundles for this scenario if present
        src_js_dir = src_har_dir / "js_bundles" / src_har.stem
        if src_js_dir.is_dir():
            dst_js_dir = dst_har_dir / "js_bundles" / dst_har.stem
            shutil.copytree(src_js_dir, dst_js_dir, dirs_exist_ok=True)

    await _upsert_har_artifacts(db, target_run)
    total_hars = len(list(dst_har_dir.glob("*.har")))
    return {
        "run_id": run_id,
        "source_run_id": body.source_run_id,
        "reused": reused,
        "skipped_duplicates": skipped_duplicates,
        "total_hars": total_hars,
    }


@router.post("/runs/{run_id}/start", response_model=RunOut)
async def start_run(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Run:
    run = await _get_run_with_jobs(db, run_id)
    if run.status == "running":
        raise HTTPException(status_code=409, detail="Run is already running")

    run.status = "queued"
    for job in run.jobs:
        if job.status in {"pending", "failed"}:
            job.status = "queued"
    await db.commit()

    runner = _get_runner(request)
    await runner.enqueue(run_id)
    return await _get_run_with_jobs(db, run_id)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)) -> Run:
    return await _get_run_with_jobs(db, run_id)


def _format_sse(event_name: str, data: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    run = await _get_run_with_jobs(db, run_id)
    runner = _get_runner(request)

    snapshot = {
        "event": "snapshot",
        "run_id": run.id,
        "status": run.status,
        "summary_metrics": run.summary_metrics or {},
        "jobs": [
            {"id": j.id, "module": j.module, "status": j.status}
            for j in run.jobs
        ],
    }
    history = runner.get_history(run_id)
    is_terminal = run.status in {"completed", "failed"}

    async def event_generator() -> AsyncIterator[str]:
        yield _format_sse("snapshot", snapshot)
        saw_terminal = False
        for item in history:
            ev_name = str(item.get("event", "message"))
            yield _format_sse(ev_name, item)
            if ev_name in {"run_completed", "run_failed"}:
                saw_terminal = True

        if is_terminal:
            if not saw_terminal:
                term_event = "run_completed" if run.status == "completed" else "run_failed"
                yield _format_sse(
                    term_event,
                    {
                        "event": term_event,
                        "run_id": run.id,
                        "status": run.status,
                        "summary_metrics": run.summary_metrics or {},
                    },
                )
            return

        async for event in runner.subscribe_events(run_id):
            if await request.is_disconnected():
                break
            ev_name = str(event.get("event", "message"))
            yield _format_sse(ev_name, event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
