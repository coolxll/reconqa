"""Abstract TaskRunner protocol and default in-process asyncio.Queue worker."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from reconqa.config import settings
from reconqa.models.db import get_session_factory
from reconqa.models.tables import Artifact, AuthProfile, Finding, Job, Run, Target
from reconqa.pipeline import DEFAULT_MODULES, run_pipeline


def _utcnow() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class TaskRunner(Protocol):
    """Pluggable task runner interface (Phase 1: in-process; future: Redis/ARQ)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def enqueue(self, run_id: str) -> None: ...

    def subscribe_events(self, run_id: str) -> AsyncIterator[dict[str, Any]]: ...


class InProcessTaskRunner:
    """Default asyncio.Queue-backed worker with SQLAlchemy persistence and SSE fan-out."""

    def __init__(self, max_history_per_run: int = 200) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._max_history = max_history_per_run
        self._running = False
        self._db_lock: asyncio.Lock = asyncio.Lock()

    async def _reconcile_inconsistent_jobs(self) -> int:
        """Self-heal inconsistent completed runs and orphaned running/queued runs on fresh startup."""
        reconciled = 0
        try:
            async with self._db_lock:
                session_factory = get_session_factory()
                async with session_factory() as session:
                    now = _utcnow()

                    # 1. Completed runs whose child jobs were left in running/queued state
                    res = await session.execute(
                        select(Job, Run)
                        .join(Run, Job.run_id == Run.id)
                        .where(
                            Run.status == "completed",
                            Job.status.in_(("running", "queued")),
                        )
                    )
                    for job, run in res.all():
                        job.status = "completed"
                        job.finished_at = job.finished_at or run.finished_at or now
                        reconciled += 1

                    # 2. Orphaned in-flight runs (fresh startup has an empty in-memory queue)
                    orphan_runs_res = await session.execute(
                        select(Run).where(Run.status.in_(("running", "queued")))
                    )
                    for run in orphan_runs_res.scalars().all():
                        jobs_res = await session.execute(
                            select(Job).where(Job.run_id == run.id)
                        )
                        child_jobs = list(jobs_res.scalars().all())

                        cfg_snap = dict(run.config_snapshot or {})
                        cfg_snap.pop("pending_modules", None)
                        run.config_snapshot = cfg_snap

                        if child_jobs and all(j.status == "completed" for j in child_jobs):
                            run.status = "completed"
                            run.finished_at = run.finished_at or now
                        else:
                            for job in child_jobs:
                                if job.status in {"running", "queued"}:
                                    job.status = "failed"
                                    job.finished_at = now
                                    meta = dict(job.output_meta or {})
                                    meta["error"] = (
                                        "Interrupted by server restart — please re-run this module"
                                    )
                                    job.output_meta = meta
                            run.status = "failed"
                            run.finished_at = now
                            metrics = dict(run.summary_metrics or {})
                            metrics["error"] = (
                                "Interrupted by server restart — please re-run incomplete module(s)"
                            )
                            run.summary_metrics = metrics
                        reconciled += 1

                    if reconciled:
                        await session.commit()
        except SQLAlchemyError:
            return 0
        return reconciled

    async def start(self) -> None:
        if self._running and self._worker_task is not None and not self._worker_task.done():
            return
        self._loop = asyncio.get_running_loop()
        await self._reconcile_inconsistent_jobs()
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop(), name="reconqa-task-worker")

    async def stop(self) -> None:
        self._running = False
        shutdown_event = {
            "event": "server_shutdown",
            "timestamp": _utcnow().isoformat(),
        }
        for sub_set in list(self._subscribers.values()):
            for sub_q in list(sub_set):
                if sub_q.full():
                    try:
                        sub_q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    sub_q.put_nowait(shutdown_event)
                except asyncio.QueueFull:
                    pass

        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def enqueue(self, run_id: str) -> None:
        if not self._running:
            await self.start()
        self._publish(
            run_id,
            {
                "event": "run_queued",
                "run_id": run_id,
                "timestamp": _utcnow().isoformat(),
            },
        )
        await self._queue.put(run_id)

    def get_history(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._history.get(run_id, []))

    def _publish(self, run_id: str, payload: dict[str, Any]) -> None:
        hist = self._history[run_id]
        hist.append(payload)
        if len(hist) > self._max_history:
            del hist[: len(hist) - self._max_history]
        for sub_q in list(self._subscribers.get(run_id, set())):
            try:
                sub_q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def _publish_threadsafe(self, run_id: str, payload: dict[str, Any]) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._publish, run_id, payload)
        else:
            self._publish(run_id, payload)

    async def subscribe_events(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        sub_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers[run_id].add(sub_q)
        try:
            while True:
                event = await sub_q.get()
                yield event
                if event.get("event") in {"run_completed", "run_failed", "server_shutdown"}:
                    break
        finally:
            self._subscribers[run_id].discard(sub_q)
            if not self._subscribers[run_id]:
                self._subscribers.pop(run_id, None)

    async def _worker_loop(self) -> None:
        while self._running:
            run_id = await self._queue.get()
            try:
                await self._execute_run(run_id)
            except Exception as exc:
                await self._mark_run_failed(run_id, str(exc))
            finally:
                self._queue.task_done()

    async def _update_job_status(
        self,
        run_id: str,
        module: str,
        status: str,
        output_meta: dict[str, Any] | None = None,
    ) -> None:
        async with self._db_lock:
            session_factory = get_session_factory()
            async with session_factory() as session:
                result = await session.execute(
                    select(Job).where(Job.run_id == run_id, Job.module == module)
                )
                job = result.scalar_one_or_none()
                now = _utcnow()
                if job is None:
                    job = Job(
                        run_id=run_id,
                        module=module,
                        status=status,
                        output_meta=output_meta or {},
                        started_at=now if status == "running" else None,
                        finished_at=now if status in {"completed", "failed"} else None,
                    )
                    session.add(job)
                else:
                    # Monotonic transition guard: never downgrade terminal job back to running
                    if status == "running" and job.status in {"completed", "failed"}:
                        return
                    job.status = status
                    if status == "running":
                        job.started_at = job.started_at or now
                    if status in {"completed", "failed"}:
                        job.finished_at = now
                    if output_meta is not None:
                        job.output_meta = output_meta
                await session.commit()

    async def _mark_run_failed(self, run_id: str, error_msg: str) -> None:
        async with self._db_lock:
            session_factory = get_session_factory()
            async with session_factory() as session:
                now = _utcnow()
                run = await session.get(Run, run_id)
                if run is not None:
                    run.status = "failed"
                    run.finished_at = now
                    metrics = dict(run.summary_metrics or {})
                    metrics["error"] = error_msg
                    run.summary_metrics = metrics
                    cfg_snap = dict(run.config_snapshot or {})
                    cfg_snap.pop("pending_modules", None)
                    run.config_snapshot = cfg_snap

                    jobs_res = await session.execute(select(Job).where(Job.run_id == run_id))
                    for job in jobs_res.scalars().all():
                        if job.status in {"running", "queued"}:
                            job.status = "failed"
                            job.finished_at = now

                    await session.commit()
        self._publish(
            run_id,
            {
                "event": "run_failed",
                "run_id": run_id,
                "error": error_msg,
                "timestamp": _utcnow().isoformat(),
            },
        )

    async def _execute_run(self, run_id: str) -> None:
        session_factory = get_session_factory()
        async with session_factory() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return

            run.status = "running"
            if run.started_at is None:
                run.started_at = _utcnow()
            await session.commit()

            cfg = dict(run.config_snapshot or {})
            target_url: str | None = cfg.get("target_url")
            if not target_url and run.target_id:
                target = await session.get(Target, run.target_id)
                if target is not None:
                    target_url = target.base_url

            pending = cfg.get("pending_modules")
            if isinstance(pending, list) and pending:
                modules: list[str] = [str(m) for m in pending if str(m).strip()]
            else:
                modules = list(cfg.get("modules") or DEFAULT_MODULES)
            workspace_dir = Path(run.workspace_dir)

            # Resolve optional auth profiles for the project
            auth_a_path: Path | None = None
            auth_b_path: Path | None = None
            auth_a_headers: dict[str, str] | None = None
            auth_b_headers: dict[str, str] | None = None

            profiles_res = await session.execute(
                select(AuthProfile).where(AuthProfile.project_id == run.project_id)
            )
            all_profiles = list(profiles_res.scalars().all())
            profiles_by_id = {p.id: p for p in all_profiles}

            explicit_auth = (
                "auth_a_profile_id" in cfg or "auth_b_profile_id" in cfg
            )
            if explicit_auth:
                prof_a = profiles_by_id.get(str(cfg.get("auth_a_profile_id") or ""))
                prof_b = profiles_by_id.get(str(cfg.get("auth_b_profile_id") or ""))
                if prof_a is not None:
                    if prof_a.storage_state_path and Path(prof_a.storage_state_path).is_file():
                        auth_a_path = Path(prof_a.storage_state_path)
                    if prof_a.extra_headers_ref:
                        auth_a_headers = {str(k): str(v) for k, v in prof_a.extra_headers_ref.items()}
                if prof_b is not None:
                    if prof_b.storage_state_path and Path(prof_b.storage_state_path).is_file():
                        auth_b_path = Path(prof_b.storage_state_path)
                    if prof_b.extra_headers_ref:
                        auth_b_headers = {str(k): str(v) for k, v in prof_b.extra_headers_ref.items()}
            else:
                for prof in all_profiles:
                    if prof.role_tag == "account_a":
                        if prof.storage_state_path and Path(prof.storage_state_path).is_file():
                            auth_a_path = Path(prof.storage_state_path)
                        if prof.extra_headers_ref:
                            auth_a_headers = {
                                str(k): str(v) for k, v in prof.extra_headers_ref.items()
                            }
                    elif prof.role_tag == "account_b":
                        if prof.storage_state_path and Path(prof.storage_state_path).is_file():
                            auth_b_path = Path(prof.storage_state_path)
                        if prof.extra_headers_ref:
                            auth_b_headers = {
                                str(k): str(v) for k, v in prof.extra_headers_ref.items()
                            }

            project_id = run.project_id
            scenario_assertions: list[dict[str, Any]] = []
            if "java_gen" in modules:
                try:
                    from reconqa.server.routers.scenarios import (
                        collect_project_scenario_assertions,
                    )

                    scenario_assertions = await collect_project_scenario_assertions(
                        session, project_id
                    )
                except Exception:
                    scenario_assertions = []

        self._publish(
            run_id,
            {
                "event": "run_started",
                "run_id": run_id,
                "modules": modules,
                "timestamp": _utcnow().isoformat(),
            },
        )

        loop = asyncio.get_running_loop()
        module_events: list[tuple[str, str, dict[str, Any]]] = []
        pending_updates: list[concurrent.futures.Future[None]] = []

        def on_log(message: str) -> None:
            self._publish_threadsafe(
                run_id,
                {
                    "event": "log",
                    "run_id": run_id,
                    "message": message,
                    "timestamp": _utcnow().isoformat(),
                },
            )

        def on_module(module: str, status: str, meta: dict[str, Any]) -> None:
            module_events.append((module, status, meta))
            self._publish_threadsafe(
                run_id,
                {
                    "event": f"job_{status}",
                    "run_id": run_id,
                    "module": module,
                    "status": status,
                    "timestamp": _utcnow().isoformat(),
                },
            )
            fut = asyncio.run_coroutine_threadsafe(
                self._update_job_status(run_id, module, status, meta),
                loop,
            )
            pending_updates.append(fut)

        har_sources_raw = cfg.get("har_sources")
        har_sources: list[Path] | None = None
        if isinstance(har_sources_raw, list) and har_sources_raw:
            data_root = settings.data_dir.resolve()
            safe_sources: list[Path] = []
            for p in har_sources_raw:
                try:
                    resolved = Path(str(p)).resolve()
                    if resolved.is_relative_to(data_root):
                        safe_sources.append(resolved)
                except Exception:
                    continue
            har_sources = safe_sources or None
        config_path_raw = cfg.get("config_path")
        config_path = Path(config_path_raw) if config_path_raw else None

        try:
            summary = await asyncio.to_thread(
                run_pipeline,
                workspace_dir,
                target_url=target_url,
                modules=modules,
                har_sources=har_sources,
                config_path=config_path,
                auth_a=auth_a_path,
                auth_b=auth_b_path,
                include_mutations=bool(cfg.get("include_mutations", False)),
                discovery_mode=str(cfg.get("discovery_mode", "passive")),
                run_nuclei=bool(cfg.get("run_nuclei", False)),
                run_k6=bool(cfg.get("run_k6", False)),
                run_schemathesis=bool(cfg.get("run_schemathesis", False)),
                log_cb=on_log,
                auth_a_headers=auth_a_headers,
                auth_b_headers=auth_b_headers,
                module_cb=on_module,
                java_package=cfg.get("java_package"),
                java_version=str(cfg["java_version"]) if cfg.get("java_version") else None,
                scenario_assertions=scenario_assertions or None,
            )
        finally:
            if pending_updates:
                await asyncio.gather(
                    *(asyncio.wrap_future(f) for f in pending_updates),
                    return_exceptions=True,
                )

        # Ensure all module statuses & findings/artifacts are persisted cleanly
        async with self._db_lock, session_factory() as session:
            seen_final: dict[str, tuple[str, dict[str, Any]]] = {}
            for mod, st, meta in module_events:
                seen_final[mod] = (st, meta)

            jobs_res = await session.execute(select(Job).where(Job.run_id == run_id))
            existing_jobs = {j.module: j for j in jobs_res.scalars().all()}
            now = _utcnow()
            for mod, (st, meta) in seen_final.items():
                if mod in existing_jobs:
                    existing_jobs[mod].status = st
                    existing_jobs[mod].output_meta = meta
                    if st in {"completed", "failed"}:
                        existing_jobs[mod].finished_at = now
                else:
                    session.add(
                        Job(
                            run_id=run_id,
                            module=mod,
                            status=st,
                            output_meta=meta,
                            started_at=now,
                            finished_at=now,
                        )
                    )

            # Refresh findings from full workspace when security/perf ran or findings exist
            new_findings = summary.get("findings", [])
            if any(m in {"security", "perf"} for m in modules) or new_findings:
                await session.execute(delete(Finding).where(Finding.run_id == run_id))
                for f in new_findings:
                    session.add(
                        Finding(
                            run_id=run_id,
                            category=str(f.get("category", "general")),
                            severity=str(f.get("severity", "info")),
                            endpoint=str(f.get("endpoint", "")),
                            title=str(f.get("title", "Finding"))[:255],
                            evidence=f.get("evidence")
                            if isinstance(f.get("evidence"), dict)
                            else {},
                        )
                    )

            # Upsert artifacts by (kind, rel_path) so earlier artifacts are preserved
            existing_art_res = await session.execute(
                select(Artifact).where(Artifact.run_id == run_id)
            )
            existing_arts = {
                (a.kind, a.rel_path): a for a in existing_art_res.scalars().all()
            }
            for art in summary.get("artifacts", []):
                kind = str(art.get("kind", "file"))
                rel_path = str(art.get("rel_path", ""))
                key = (kind, rel_path)
                if key in existing_arts:
                    existing_arts[key].size_bytes = int(art.get("size_bytes", 0))
                    existing_arts[key].sha256 = str(art.get("sha256", ""))
                else:
                    session.add(
                        Artifact(
                            run_id=run_id,
                            kind=kind,
                            rel_path=rel_path,
                            size_bytes=int(art.get("size_bytes", 0)),
                            sha256=str(art.get("sha256", "")),
                        )
                    )

            all_job_statuses = {m: j.status for m, j in existing_jobs.items()}
            for m, (st, _) in seen_final.items():
                all_job_statuses[m] = st
            failed_modules = [m for m, st in all_job_statuses.items() if st == "failed"]
            terminal_status = "failed" if failed_modules else "completed"

            run = await session.get(Run, run_id)
            merged_metrics: dict[str, Any] = {}
            if run is not None:
                prev_metrics = dict(run.summary_metrics or {})
                new_metrics = dict(summary.get("summary_metrics", {}))
                prev_mods = list(prev_metrics.get("modules_executed") or [])
                merged_mods = list(dict.fromkeys([*prev_mods, *modules]))
                merged_metrics = {**prev_metrics, **new_metrics, "modules_executed": merged_mods}
                if failed_modules:
                    merged_metrics["error"] = f"Module(s) failed: {', '.join(failed_modules)}"
                else:
                    merged_metrics.pop("error", None)

                cfg_snap = dict(run.config_snapshot or {})
                cfg_snap.pop("pending_modules", None)
                run.config_snapshot = cfg_snap
                run.status = terminal_status
                run.summary_metrics = merged_metrics
                run.finished_at = _utcnow()

            await session.commit()

            # Best-effort non-fatal auto-hydration of Scenario entities after merge completes
            if "merge" in modules and "merge" not in failed_modules:
                try:
                    from reconqa.server.routers.scenarios import sync_project_scenarios

                    await sync_project_scenarios(session, project_id)
                except Exception:
                    pass

        terminal_event = "run_failed" if terminal_status == "failed" else "run_completed"
        payload: dict[str, Any] = {
            "event": terminal_event,
            "run_id": run_id,
            "status": terminal_status,
            "summary_metrics": merged_metrics,
            "timestamp": _utcnow().isoformat(),
        }
        if failed_modules:
            payload["error"] = f"Module(s) failed: {', '.join(failed_modules)}"
        self._publish(run_id, payload)
