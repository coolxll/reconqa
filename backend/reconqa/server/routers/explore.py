"""Separate Explore & Discovery workspace endpoints and Promote-to-Run bridge."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.config import settings
from reconqa.models.db import get_db
from reconqa.models.schemas import RunOut
from reconqa.models.tables import Job, Project, Run, Target
from reconqa.modules.discovery.seed_explorer import explore_from_seed
from reconqa.pipeline import DEFAULT_MODULES
from reconqa.server.routers.runs import _get_run_with_jobs, _get_runner, _upsert_har_artifacts
from reconqa.utils.io import ensure_dir, read_json, slugify, write_json

router = APIRouter(tags=["explore"])

_EXPLORE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_SYNTHETIC_RESP_TEXT = json.dumps({"code": 0, "msg": "ok", "data": {}})


class ExploreRequest(BaseModel):
    seed_curl: str | None = None
    target_url: str | None = None
    mode: Literal["passive", "active"] = "passive"
    probe_openapi_docs: bool = True
    crawl_js: bool = True
    depth: int = 2


class PromoteToRunRequest(BaseModel):
    project_id: str | None = None
    project_name: str | None = None
    modules: list[str] | None = None
    selected_endpoints: list[str] | None = None


def _explores_root() -> Path:
    return ensure_dir(settings.data_dir.resolve() / "explores")


def _safe_explore_dir(explore_id: str) -> Path:
    if not _EXPLORE_ID_RE.match(explore_id):
        raise HTTPException(status_code=400, detail="Invalid explore_id format")
    root = _explores_root()
    target = (root / explore_id).resolve()
    if target.parent != root:
        raise HTTPException(status_code=400, detail="Invalid explore path")
    return target


def _load_explore_or_404(explore_id: str) -> tuple[Path, dict[str, Any]]:
    exp_dir = _safe_explore_dir(explore_id)
    res_path = exp_dir / "explore_result.json"
    if not res_path.is_file():
        raise HTTPException(status_code=404, detail=f"Explore session '{explore_id}' not found")
    return exp_dir, read_json(res_path)


def _synthesize_discovered_har(
    endpoints: list[dict[str, Any]],
    base_url: str,
    dest_path: Path,
) -> int:
    if not endpoints:
        return 0
    base_ts = datetime.now(UTC)
    resp_bytes = len(_SYNTHETIC_RESP_TEXT.encode("utf-8"))
    entries: list[dict[str, Any]] = []

    for idx, ep in enumerate(endpoints):
        method = str(ep.get("method") or "GET").upper()
        path = str(ep.get("path") or "/")
        inferred = [str(p) for p in (ep.get("inferred_params") or []) if str(p).strip()]
        # Substitute OpenAPI path params like {itemId} with deterministic sample IDs
        concrete_path = re.sub(r"\{([^}]+)\}", lambda m: f"sample-{m.group(1)}", path)
        full_url = f"{base_url.rstrip('/')}{concrete_path}"

        query_string: list[dict[str, str]] = []
        post_data: dict[str, str] | None = None
        headers: list[dict[str, str]] = [{"name": "Accept", "value": "application/json"}]

        if method in {"GET", "DELETE", "HEAD"} and inferred:
            query_string = [{"name": k, "value": "sample"} for k in inferred[:8]]
            if "?" not in full_url:
                full_url = f"{full_url}?{urlencode([(q['name'], q['value']) for q in query_string])}"
        elif method in {"POST", "PUT", "PATCH"}:
            body_obj = {k: "sample" for k in inferred[:12]}
            body_text = json.dumps(body_obj, ensure_ascii=False)
            post_data = {"mimeType": "application/json", "text": body_text}
            headers.append({"name": "Content-Type", "value": "application/json"})

        har_req: dict[str, Any] = {
            "method": method,
            "url": full_url,
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": headers,
            "queryString": query_string,
            "headersSize": -1,
            "bodySize": len(post_data["text"].encode("utf-8")) if post_data else 0,
        }
        if post_data is not None:
            har_req["postData"] = post_data

        entries.append(
            {
                "startedDateTime": (base_ts + timedelta(milliseconds=idx * 100)).isoformat(),
                "time": 42,
                "request": har_req,
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "httpVersion": "HTTP/1.1",
                    "cookies": [],
                    "headers": [{"name": "Content-Type", "value": "application/json"}],
                    "content": {
                        "size": resp_bytes,
                        "mimeType": "application/json",
                        "text": _SYNTHETIC_RESP_TEXT,
                    },
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": resp_bytes,
                },
                "cache": {},
                "timings": {
                    "blocked": 0,
                    "dns": 0,
                    "connect": 0,
                    "send": 1,
                    "wait": 40,
                    "receive": 1,
                    "ssl": 0,
                },
            }
        )

    har_doc = {
        "log": {
            "version": "1.2",
            "creator": {"name": "reconqa-explore-promoter", "version": "0.1.0"},
            "pages": [],
            "entries": entries,
        }
    }
    write_json(dest_path, har_doc)
    return len(entries)


@router.post("/explore", status_code=status.HTTP_201_CREATED)
async def run_explore(body: ExploreRequest) -> dict[str, Any]:
    """Run a standalone Explore & Discovery session from a seed cURL or target URL."""
    has_curl = bool(body.seed_curl and body.seed_curl.strip())
    has_url = bool(body.target_url and body.target_url.strip())
    if not has_curl and not has_url:
        raise HTTPException(
            status_code=400,
            detail="Either seed_curl or target_url must be provided.",
        )

    explore_id = uuid.uuid4().hex[:16]
    out_dir = ensure_dir(_explores_root() / explore_id)
    try:
        result = await asyncio.to_thread(
            explore_from_seed,
            out_dir,
            seed_curl=body.seed_curl,
            target_url=body.target_url,
            mode=body.mode,
            probe_openapi_docs=body.probe_openapi_docs,
            crawl_js=body.crawl_js,
            depth=body.depth,
            explore_id=explore_id,
        )
    except ValueError as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.get("/explore")
async def list_explores() -> list[dict[str, Any]]:
    """List saved Explore & Discovery sessions (newest first)."""
    root = _explores_root()
    items: list[dict[str, Any]] = []
    for res_file in root.glob("*/explore_result.json"):
        try:
            data = read_json(res_file)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        items.append(
            {
                "explore_id": data.get("explore_id", res_file.parent.name),
                "created_at": data.get("created_at", ""),
                "mode": data.get("mode", "passive"),
                "seed": data.get("seed", {}),
                "summary": data.get("summary", {}),
            }
        )
    items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return items


@router.get("/explore/{explore_id}")
async def get_explore(explore_id: str) -> dict[str, Any]:
    """Return full ExploreResult for a saved explore session."""
    _, data = _load_explore_or_404(explore_id)
    return data


@router.post(
    "/explore/{explore_id}/promote-to-run",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def promote_explore_to_run(
    explore_id: str,
    body: PromoteToRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Run:
    """Promote discovered endpoints from an Explore session into a full Testing Run."""
    exp_dir, exp_data = _load_explore_or_404(explore_id)
    seed = exp_data.get("seed") or {}
    base_url = str(seed.get("base_url") or "")
    root_domain = str(seed.get("root_domain") or "example.com")

    # 1. Resolve or auto-create Project
    project: Project | None = None
    if body.project_id:
        project = await db.get(Project, body.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project '{body.project_id}' not found")
    else:
        proj_name = (body.project_name or "").strip() or root_domain or "Explore Discovery"
        proj_slug = slugify(proj_name)
        existing = await db.execute(select(Project).where(Project.slug == proj_slug))
        project = existing.scalar_one_or_none()
        if project is None:
            project = Project(
                name=proj_name,
                slug=proj_slug,
                description=f"Auto-created from Explore session {explore_id}",
            )
            db.add(project)
            await db.flush()

    # 2. Reuse or create Target
    target: Target | None = None
    if base_url:
        t_res = await db.execute(
            select(Target).where(Target.project_id == project.id, Target.base_url == base_url)
        )
        target = t_res.scalars().first()
        if target is None:
            target = Target(
                project_id=project.id,
                root_domain=root_domain,
                base_url=base_url,
                scope_rules={},
            )
            db.add(target)
            await db.flush()

    # 3. Create Run + Jobs
    raw_modules = list(body.modules) if body.modules else list(DEFAULT_MODULES)
    if "merge" not in raw_modules:
        raw_modules.insert(0, "merge")

    run = Run(
        project_id=project.id,
        target_id=target.id if target else None,
        status="queued",
        trigger_source="web",
        config_snapshot={
            "modules": raw_modules,
            "target_url": base_url or None,
            "promoted_from_explore_id": explore_id,
        },
    )
    db.add(run)
    await db.flush()

    workspace_dir = ensure_dir(settings.data_dir.resolve() / "runs" / run.id)
    har_dir = ensure_dir(workspace_dir / "har")
    run.workspace_dir = str(workspace_dir)

    for mod in raw_modules:
        db.add(Job(run_id=run.id, module=mod, status="queued"))

    await db.commit()

    # 4. Carry over seed HAR, JS bundles/endpoints, and synthesized discovered HAR
    seed_har = exp_dir / "seed_har" / "seed.har"
    has_seed_har = False
    if seed_har.is_file():
        shutil.copy2(seed_har, har_dir / "01_seed.har")
        has_seed_har = True

    src_js_bundles = exp_dir / "js_bundles"
    if src_js_bundles.is_dir():
        shutil.copytree(src_js_bundles, har_dir / "js_bundles", dirs_exist_ok=True)

    src_js_endpoints = exp_dir / "js_endpoints.json"
    if src_js_endpoints.is_file():
        shutil.copy2(src_js_endpoints, har_dir / "js_endpoints.json")

    all_discovered: list[dict[str, Any]] = list(exp_data.get("discovered_endpoints") or [])
    selected_set = set(body.selected_endpoints) if body.selected_endpoints else None
    to_synthesize: list[dict[str, Any]] = []
    for ep in all_discovered:
        ep_id = str(ep.get("id") or f"{ep.get('method', 'GET')} {ep.get('path', '/')}")
        ep_path = str(ep.get("path") or "/")
        if selected_set is not None and ep_id not in selected_set and ep_path not in selected_set:
            continue
        if has_seed_har and ep.get("source") == "seed_curl":
            continue
        to_synthesize.append(ep)

    if to_synthesize and base_url:
        _synthesize_discovered_har(to_synthesize, base_url, har_dir / "02_discovered.har")

    await _upsert_har_artifacts(db, run)

    runner = _get_runner(request)
    await runner.enqueue(run.id)
    return await _get_run_with_jobs(db, run.id)
