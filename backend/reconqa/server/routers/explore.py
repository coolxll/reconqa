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
from reconqa.modules.discovery.seed_explorer import (
    explore_from_seed,
    extract_seed_root_domain,
    merge_explore_results,
)
from reconqa.pipeline import DEFAULT_MODULES
from reconqa.server.routers.runs import _get_run_with_jobs, _get_runner, _upsert_har_artifacts
from reconqa.utils.io import ensure_dir, read_json, slugify, write_json

router = APIRouter(tags=["explore"])

_EXPLORE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_SYNTHETIC_RESP_TEXT = json.dumps({"code": 0, "msg": "ok", "data": {}})


class ExploreRequest(BaseModel):
    seed_curl: str | None = None
    target_url: str | None = None
    mode: Literal["passive", "active"] = "passive"
    probe_openapi_docs: bool = True
    crawl_js: bool = True
    depth: int = 2
    merge: bool = True


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


def _domain_explore_id(root_domain: str) -> str:
    slug = slugify(root_domain)
    if not slug or slug == "default":
        return uuid.uuid4().hex[:16]
    if len(slug) < 3:
        return f"dom-{slug}"
    return slug[:120]


def _copy_explore_supporting_files(src_dir: Path, dst_dir: Path, suffix: str) -> None:
    src_seed_dir = src_dir / "seed_har"
    if src_seed_dir.is_dir():
        dst_seed_dir = ensure_dir(dst_dir / "seed_har")
        for har_file in sorted(src_seed_dir.glob("*.har")):
            dest_name = f"seed_{suffix}_{har_file.name}" if har_file.name == "seed.har" else har_file.name
            dest_path = dst_seed_dir / dest_name
            if dest_path.exists():
                dest_path = dst_seed_dir / f"{har_file.stem}_{suffix}.har"
            shutil.copy2(har_file, dest_path)

    src_js = src_dir / "js_bundles"
    if src_js.is_dir():
        shutil.copytree(src_js, dst_dir / "js_bundles", dirs_exist_ok=True)

    src_js_ep = src_dir / "js_endpoints.json"
    if src_js_ep.is_file():
        shutil.copy2(src_js_ep, dst_dir / "js_endpoints.json")


def _consolidate_legacy_explores() -> None:
    """Migrate and merge any legacy random-ID explore folders sharing a root_domain into domain keys."""
    root = _explores_root()
    by_domain: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for res_file in root.glob("*/explore_result.json"):
        folder = res_file.parent
        if folder.name.startswith("_tmp_"):
            continue
        try:
            data = read_json(res_file)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        root_domain = str((data.get("seed") or {}).get("root_domain") or "").strip().lower()
        if not root_domain:
            continue
        domain_key = _domain_explore_id(root_domain)
        by_domain.setdefault(domain_key, []).append((folder, data))

    for domain_key, items in by_domain.items():
        # Consolidate if multiple folders share this domain OR the single folder still uses a legacy ID
        needs_consolidation = len(items) > 1 or any(f.name != domain_key for f, _ in items)
        if not needs_consolidation:
            continue
        items.sort(key=lambda pair: str(pair[1].get("created_at") or ""))
        dest_dir = ensure_dir(root / domain_key)

        merged: dict[str, Any] | None = None
        for idx, (folder, data) in enumerate(items):
            if merged is None:
                merged = dict(data)
                merged["explore_id"] = domain_key
                if not merged.get("updated_at"):
                    merged["updated_at"] = merged.get("created_at") or datetime.now(UTC).isoformat()
                if not merged.get("scans"):
                    seed_info = merged.get("seed") or {}
                    merged["scans"] = [
                        {
                            "scan_id": str(data.get("explore_id") or folder.name),
                            "scanned_at": str(data.get("created_at") or merged["updated_at"]),
                            "mode": str(data.get("mode") or "passive"),
                            "seed_type": str(seed_info.get("type") or "url"),
                            "seed_url": str(seed_info.get("seed_url") or ""),
                            "new_endpoints": len(data.get("discovered_endpoints") or []),
                            "new_subdomains": len(data.get("subdomains") or []),
                        }
                    ]
                summary = dict(merged.get("summary") or {})
                summary["scans_count"] = len(merged["scans"])
                merged["summary"] = summary
            else:
                merged = merge_explore_results(merged, data, explore_id=domain_key)

            if folder.resolve() != dest_dir.resolve():
                _copy_explore_supporting_files(folder, dest_dir, f"leg{idx}_{folder.name[:6]}")

        if merged is not None:
            write_json(dest_dir / "explore_result.json", merged)

        for folder, _ in items:
            if folder.resolve() != dest_dir.resolve():
                shutil.rmtree(folder, ignore_errors=True)


@router.post("/explore", status_code=status.HTTP_201_CREATED)
async def run_explore(body: ExploreRequest) -> dict[str, Any]:
    """Run an Explore & Discovery scan, automatically merging assets by root domain when merge=True."""
    has_curl = bool(body.seed_curl and body.seed_curl.strip())
    has_url = bool(body.target_url and body.target_url.strip())
    if not has_curl and not has_url:
        raise HTTPException(
            status_code=400,
            detail="Either seed_curl or target_url must be provided.",
        )

    if body.merge:
        _consolidate_legacy_explores()

    scan_id = uuid.uuid4().hex[:12]
    tmp_dir = ensure_dir(_explores_root() / f"_tmp_{scan_id}")
    try:
        incoming = await asyncio.to_thread(
            explore_from_seed,
            tmp_dir,
            seed_curl=body.seed_curl,
            target_url=body.target_url,
            mode=body.mode,
            probe_openapi_docs=body.probe_openapi_docs,
            crawl_js=body.crawl_js,
            depth=body.depth,
            explore_id=scan_id,
        )
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved_domain = (
        str((incoming.get("seed") or {}).get("root_domain") or "").strip().lower()
        or extract_seed_root_domain(body.seed_curl, body.target_url)
    )
    final_id = _domain_explore_id(resolved_domain) if (body.merge and resolved_domain) else scan_id
    dest_dir = ensure_dir(_explores_root() / final_id)
    existing_path = dest_dir / "explore_result.json"

    ts_tag = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "_" + scan_id[:6]
    _copy_explore_supporting_files(tmp_dir, dest_dir, ts_tag)

    if body.merge and existing_path.is_file():
        existing = read_json(existing_path)
        final_result = merge_explore_results(existing, incoming, explore_id=final_id)
    else:
        final_result = dict(incoming)
        final_result["explore_id"] = final_id

    write_json(existing_path, final_result)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return final_result


@router.get("/explore")
async def list_explores() -> list[dict[str, Any]]:
    """List consolidated root-domain Explore inventories (most recently updated first)."""
    _consolidate_legacy_explores()
    root = _explores_root()
    items: list[dict[str, Any]] = []
    for res_file in root.glob("*/explore_result.json"):
        if res_file.parent.name.startswith("_tmp_"):
            continue
        try:
            data = read_json(res_file)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        scans = data.get("scans") or []
        summary = dict(data.get("summary") or {})
        scans_count = int(summary.get("scans_count") or len(scans) or 1)
        summary["scans_count"] = scans_count
        created_at = str(data.get("created_at") or "")
        updated_at = str(data.get("updated_at") or created_at)
        items.append(
            {
                "explore_id": data.get("explore_id", res_file.parent.name),
                "created_at": created_at,
                "updated_at": updated_at,
                "mode": data.get("mode", "passive"),
                "seed": data.get("seed", {}),
                "summary": summary,
                "scans_count": scans_count,
            }
        )
    items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return items


@router.get("/explore/{explore_id}")
async def get_explore(explore_id: str) -> dict[str, Any]:
    """Return full ExploreResult for a saved explore session."""
    _, data = _load_explore_or_404(explore_id)
    return data


@router.delete("/explore/{explore_id}")
async def delete_explore(explore_id: str) -> dict[str, Any]:
    """Delete a saved root-domain Explore inventory directory cleanly."""
    exp_dir = _safe_explore_dir(explore_id)
    if not exp_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Explore session '{explore_id}' not found")
    shutil.rmtree(exp_dir)
    return {"deleted": True, "explore_id": explore_id}


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

    all_discovered: list[dict[str, Any]] = list(exp_data.get("discovered_endpoints") or [])
    selected_set = set(body.selected_endpoints) if body.selected_endpoints else None
    matched_count = (
        sum(
            1
            for ep in all_discovered
            if str(ep.get("id") or f"{ep.get('method', 'GET')} {ep.get('path', '/')}") in selected_set
            or str(ep.get("path") or "/") in selected_set
        )
        if selected_set is not None
        else len(all_discovered)
    )
    run_name = f"Explore · {root_domain} ({matched_count} APIs)"

    run = Run(
        project_id=project.id,
        target_id=target.id if target else None,
        status="queued",
        trigger_source="web",
        config_snapshot={
            "modules": raw_modules,
            "target_url": base_url or None,
            "promoted_from_explore_id": explore_id,
            "run_name": run_name,
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

    # 4. Carry over ALL accumulated seed HARs, JS bundles/endpoints, and synthesized discovered HAR
    seed_har_dir = exp_dir / "seed_har"
    has_seed_har = False
    if seed_har_dir.is_dir():
        seed_files = sorted(seed_har_dir.glob("*.har"))
        for idx, sh_file in enumerate(seed_files, start=1):
            dest_name = "01_seed.har" if len(seed_files) == 1 else f"01_seed_{idx:02d}.har"
            shutil.copy2(sh_file, har_dir / dest_name)
            has_seed_har = True

    src_js_bundles = exp_dir / "js_bundles"
    if src_js_bundles.is_dir():
        shutil.copytree(src_js_bundles, har_dir / "js_bundles", dirs_exist_ok=True)

    src_js_endpoints = exp_dir / "js_endpoints.json"
    if src_js_endpoints.is_file():
        shutil.copy2(src_js_endpoints, har_dir / "js_endpoints.json")

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
