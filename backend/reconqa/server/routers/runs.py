"""Run lifecycle, HAR upload/reuse, execution trigger, and SSE streaming endpoints."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
from reconqa.models.tables import Artifact, Job, Project, Run, Target
from reconqa.modules.recorder.har_importer import import_hars
from reconqa.modules.recorder.har_slimmer import slim_har_file
from reconqa.modules.recorder.text_importer import import_traffic_text
from reconqa.pipeline import ALL_MODULES, DEFAULT_MODULES
from reconqa.server.worker import InProcessTaskRunner
from reconqa.utils.io import ensure_dir, read_json, sha256_file

router = APIRouter(tags=["runs"])


class HarReuseRequest(BaseModel):
    source_run_id: str
    scenarios: list[str] | None = None


class ImportTextRequest(BaseModel):
    content: str
    scenario: str = "01_devtools"
    format: str = "auto"
    target_domain: str | None = None


class ExecuteModulesRequest(BaseModel):
    modules: list[str]
    config: dict[str, Any] = {}


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


def _detect_primary_origin(har_paths: list[Path]) -> str | None:
    counts: Counter[str] = Counter()
    for p in har_paths:
        if not p.is_file():
            continue
        try:
            doc = read_json(p)
        except Exception:
            continue
        entries = (doc.get("log") or {}).get("entries") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            url = str((entry.get("request") or {}).get("url") or "") if isinstance(entry, dict) else ""
            parsed = urlparse(url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                counts[f"{parsed.scheme}://{parsed.netloc}"] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


async def _sync_run_target_url_from_hars(
    db: AsyncSession,
    run: Run,
    har_paths: list[Path],
    explicit_target_domain: str | None,
) -> None:
    if explicit_target_domain:
        return
    primary_origin = _detect_primary_origin(har_paths)
    if not primary_origin:
        return
    imported_host = (urlparse(primary_origin).hostname or "").lower()
    if not imported_host:
        return

    cfg = dict(run.config_snapshot or {})
    configured_url = cfg.get("target_url")
    if not configured_url and run.target_id:
        target_obj = await db.get(Target, run.target_id)
        if target_obj is not None:
            configured_url = target_obj.base_url

    configured_host = (urlparse(str(configured_url)).hostname or "").lower() if configured_url else ""
    if not configured_url or (
        configured_host
        and imported_host != configured_host
        and not imported_host.endswith(f".{configured_host}")
    ):
        cfg["target_url"] = primary_origin
        run.config_snapshot = cfg
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
    if body.name and body.name.strip():
        config_snapshot["run_name"] = body.name.strip()
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


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink a file for zero extra disk usage, falling back to copy2 across filesystems."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


@router.post("/runs/{run_id}/hars")
async def upload_run_hars(
    run_id: str,
    files: list[UploadFile] = File(...),
    scenario: str | None = Form(default=None),
    target_domain: str | None = Form(default=None),
    slim: bool = Form(default=True),
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
            extract_js=not slim,
        )

    slim_stats: dict[str, Any] = {
        "enabled": slim,
        "files_slimmed": 0,
        "original_bytes": 0,
        "slimmed_bytes": 0,
        "saved_pct": 0.0,
        "stripped_binary_count": 0,
        "stripped_js_count": 0,
        "extracted_js_files": 0,
    }
    if slim:
        js_bundles_dir = har_dir / "js_bundles"
        imported_files = [
            Path(f["dest"])
            for f in (import_res.get("imported") or [])
            if isinstance(f, dict) and f.get("dest")
        ]
        target_har_paths = [p for p in imported_files if p.is_file()] or sorted(har_dir.glob("*.har"))
        for hp in target_har_paths:
            st = slim_har_file(hp, js_bundles_dir=js_bundles_dir)
            if st.get("slimmed"):
                slim_stats["files_slimmed"] += 1
                slim_stats["original_bytes"] += int(st.get("original_bytes") or 0)
                slim_stats["slimmed_bytes"] += int(st.get("slimmed_bytes") or 0)
                slim_stats["stripped_binary_count"] += int(st.get("stripped_binary_count") or 0)
                slim_stats["stripped_js_count"] += int(st.get("stripped_js_count") or 0)
                slim_stats["extracted_js_files"] += int(st.get("extracted_js_files") or 0)
        if slim_stats["original_bytes"] > 0:
            slim_stats["saved_pct"] = round(
                (
                    (slim_stats["original_bytes"] - slim_stats["slimmed_bytes"])
                    / slim_stats["original_bytes"]
                )
                * 100.0,
                2,
            )

    await _upsert_har_artifacts(db, run)
    har_paths = sorted(har_dir.glob("*.har"))
    await _sync_run_target_url_from_hars(db, run, har_paths, target_domain)
    har_files = [p.name for p in har_paths]

    cfg = dict(run.config_snapshot or {})
    if not str(cfg.get("run_name") or "").strip() and har_paths:
        first_stem = scenario if (scenario and len(har_paths) == 1) else har_paths[0].stem
        suffix = f" (+{len(har_paths) - 1})" if len(har_paths) > 1 else ""
        cfg["run_name"] = f"HAR · {first_stem}{suffix}"
        run.config_snapshot = cfg
        await db.commit()

    return {
        "run_id": run_id,
        "import_result": import_res,
        "slim_stats": slim_stats,
        "har_files": har_files,
        "total_hars": len(har_files),
    }


@router.post("/runs/{run_id}/import-text")
async def import_run_text(
    run_id: str,
    body: ImportTextRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_with_jobs(db, run_id)
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="Traffic text content must not be empty")

    har_dir = ensure_dir(Path(run.workspace_dir) / "har")
    result = import_traffic_text(
        body.content,
        har_dir,
        scenario_name=body.scenario,
        format_hint=body.format,
        target_domain=body.target_domain,
    )
    if result.get("requests_imported", 0) == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No valid HTTP requests could be parsed (detected_format="
                f"'{result.get('detected_format', 'unknown')}'). "
                "Supported formats: Chrome DevTools Copy as cURL, Copy as fetch, "
                "Raw HTTP (.http), or HAR 1.2 JSON."
            ),
        )

    await _upsert_har_artifacts(db, run)
    imported_har = Path(result["har_path"]) if result.get("har_path") else None
    har_candidates = (
        [imported_har] if imported_har and imported_har.is_file() else sorted(har_dir.glob("*.har"))
    )
    await _sync_run_target_url_from_hars(db, run, har_candidates, body.target_domain)

    cfg = dict(run.config_snapshot or {})
    if not str(cfg.get("run_name") or "").strip():
        fmt_map = {
            "curl": "cURL",
            "fetch": "fetch",
            "raw_http": "Raw HTTP",
            "har_json": "HAR",
        }
        fmt_label = fmt_map.get(str(result.get("detected_format") or "").lower(), "Traffic")
        origin = _detect_primary_origin(har_candidates)
        host = (urlparse(origin).hostname or "") if origin else ""
        label_target = host or str(result.get("scenario") or body.scenario or "capture")
        cfg["run_name"] = f"{fmt_label} · {label_target}"
        run.config_snapshot = cfg
        await db.commit()

    return result


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
        _link_or_copy(src_har, dst_har)
        existing_hashes.add(digest)
        reused.append(dst_har.name)

    # Carry over extracted JS bundles (both deduplicated flat files and scenario subdirs) via hardlink
    src_js_root = src_har_dir / "js_bundles"
    if src_js_root.is_dir():
        dst_js_root = ensure_dir(dst_har_dir / "js_bundles")
        for js_file in sorted(src_js_root.rglob("*")):
            if not js_file.is_file():
                continue
            rel = js_file.relative_to(src_js_root)
            dst_js_file = dst_js_root / rel
            if dst_js_file.exists():
                continue
            ensure_dir(dst_js_file.parent)
            _link_or_copy(js_file, dst_js_file)

    await _upsert_har_artifacts(db, target_run)
    cfg = dict(target_run.config_snapshot or {})
    if not str(cfg.get("run_name") or "").strip() and reused:
        first_stem = Path(reused[0]).stem
        suffix = f" (+{len(reused) - 1})" if len(reused) > 1 else ""
        cfg["run_name"] = f"HAR · {first_stem}{suffix}"
        target_run.config_snapshot = cfg
        await db.commit()
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


@router.post("/runs/{run_id}/execute-modules", response_model=RunOut)
async def execute_run_modules(
    run_id: str,
    body: ExecuteModulesRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Run:
    """Incrementally execute one or more capability modules on an existing run."""
    run = await _get_run_with_jobs(db, run_id)
    if run.status == "running":
        raise HTTPException(status_code=409, detail="Run is currently executing")

    requested = list(dict.fromkeys(m.strip() for m in body.modules if m and m.strip()))
    if not requested:
        raise HTTPException(status_code=400, detail="At least one module is required")
    invalid = [m for m in requested if m not in ALL_MODULES]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported module(s): {', '.join(invalid)}. Allowed: {', '.join(ALL_MODULES)}",
        )

    cfg = dict(run.config_snapshot or {})
    if body.config:
        for k, v in body.config.items():
            cfg[k] = v

    existing_mods = list(cfg.get("modules") or [])
    cfg["modules"] = list(dict.fromkeys([*existing_mods, *requested]))
    cfg["pending_modules"] = requested
    run.config_snapshot = cfg
    run.status = "queued"

    existing_jobs = {j.module: j for j in run.jobs}
    for mod in requested:
        if mod in existing_jobs:
            existing_jobs[mod].status = "queued"
            existing_jobs[mod].finished_at = None
        else:
            db.add(Job(run_id=run.id, module=mod, status="queued"))

    await db.commit()

    runner = _get_runner(request)
    await runner.enqueue(run_id)
    return await _get_run_with_jobs(db, run_id)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)) -> Run:
    return await _get_run_with_jobs(db, run_id)


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Delete a run (cascading jobs/findings/artifacts) and clean up its workspace directory."""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    if run.status == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a run that is currently running")

    ws_str = run.workspace_dir or ""
    await db.delete(run)
    await db.commit()

    if ws_str:
        runs_root = (settings.data_dir.resolve() / "runs").resolve()
        ws_path = Path(ws_str).resolve()
        if runs_root in ws_path.parents and ws_path.is_dir():
            shutil.rmtree(ws_path, ignore_errors=True)

    return {"deleted": True, "id": run_id}


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
