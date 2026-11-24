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
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.config import settings
from reconqa.models.db import get_db
from reconqa.models.schemas import RunOut
from reconqa.models.tables import Job, Project, Run, Target
from reconqa.modules.discovery.seed_explorer import (
    _collect_merged_hosts,
    _registrable_root_domain,
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
    target_urls: list[str] | None = None
    mode: Literal["passive", "active"] = "passive"
    probe_openapi_docs: bool = True
    crawl_js: bool = True
    depth: int = 2
    merge: bool = True
    project_id: str | None = None


class ExploreAllTargetsRequest(BaseModel):
    target_ids: list[str] | None = None
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


async def _project_domain_set(db: AsyncSession, project_id: str) -> set[str]:
    """Return registrable root domains & raw hostnames associated with a project's targets/runs."""
    domains: set[str] = set()
    t_res = await db.execute(select(Target).where(Target.project_id == project_id))
    for tgt in t_res.scalars().all():
        for raw_dom in (tgt.root_domain, urlparse(tgt.base_url or "").hostname or ""):
            dom = raw_dom.strip().lower()
            if dom:
                domains.add(dom)
                reg = _registrable_root_domain(dom)
                if reg:
                    domains.add(reg)
    r_res = await db.execute(select(Run).where(Run.project_id == project_id))
    for run in r_res.scalars().all():
        cfg = run.config_snapshot or {}
        t_url = str(cfg.get("target_url") or "")
        if t_url:
            host = (urlparse(t_url).hostname or "").lower()
            if host:
                domains.add(host)
                reg = _registrable_root_domain(host)
                if reg:
                    domains.add(reg)
    return domains


def _explore_matches_project(
    data: dict[str, Any],
    project_id: str,
    project_domains: set[str],
) -> bool:
    if str(data.get("project_id") or "") == project_id:
        return True
    if not project_domains:
        return False
    seed = data.get("seed") or {}
    candidates = [
        str(seed.get("root_domain") or "").strip().lower(),
        (urlparse(str(seed.get("base_url") or "")).hostname or "").strip().lower(),
    ]
    for cand in candidates:
        if not cand:
            continue
        if cand in project_domains or _registrable_root_domain(cand) in project_domains:
            return True
    return False


async def find_project_explores(db: AsyncSession, project_id: str) -> list[dict[str, Any]]:
    """Return full ExploreResult dicts belonging to or matching a project's target domains."""
    _consolidate_legacy_explores()
    project_domains = await _project_domain_set(db, project_id)
    root = _explores_root()
    matched: list[dict[str, Any]] = []
    for res_file in root.glob("*/explore_result.json"):
        if res_file.parent.name.startswith("_tmp_"):
            continue
        try:
            data = read_json(res_file)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if _explore_matches_project(data, project_id, project_domains):
            matched.append(data)
    matched.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return matched


def _parse_batch_target_urls(
    target_url: str | None,
    target_urls: list[str] | None,
) -> list[str]:
    """Extract deduplicated URLs from target_urls and newline/comma-separated target_url."""
    raw_items: list[str] = []
    if target_urls:
        for item in target_urls:
            if item and str(item).strip():
                raw_items.extend(re.split(r"[\r\n,]+", str(item)))
    if target_url and target_url.strip():
        raw_items.extend(re.split(r"[\r\n,]+", target_url))

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        u = raw.strip()
        if not u:
            continue
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
    return cleaned


async def _run_batch_explore_urls(
    urls: list[str],
    *,
    mode: str,
    probe_openapi_docs: bool,
    crawl_js: bool,
    depth: int,
    merge: bool,
    project_id: str | None,
) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    """Explore multiple portal URLs with bounded concurrency (2) and merge into root-domain inventory."""
    sem = asyncio.Semaphore(2)
    per_url_out: list[tuple[str, Path | None, dict[str, Any] | None, str | None]] = []

    async def _explore_one(u: str) -> tuple[str, Path | None, dict[str, Any] | None, str | None]:
        async with sem:
            sid = uuid.uuid4().hex[:12]
            tdir = ensure_dir(_explores_root() / f"_tmp_{sid}")
            try:
                res = await asyncio.to_thread(
                    explore_from_seed,
                    tdir,
                    target_url=u,
                    mode=mode,
                    probe_openapi_docs=probe_openapi_docs,
                    crawl_js=crawl_js,
                    depth=depth,
                    explore_id=sid,
                )
                if project_id:
                    res["project_id"] = project_id
                return u, tdir, res, None
            except Exception as exc:
                shutil.rmtree(tdir, ignore_errors=True)
                return u, None, None, str(exc)

    results = await asyncio.gather(*(_explore_one(u) for u in urls))
    per_url_out.extend(results)

    succeeded = [(u, tdir, res) for (u, tdir, res, err) in per_url_out if res is not None and tdir is not None]
    failed_warnings = [f"Failed to explore {u}: {err}" for (u, _, res, err) in per_url_out if res is None and err]

    if not succeeded:
        raise HTTPException(
            status_code=400,
            detail="; ".join(failed_warnings) or "All target URLs failed to explore.",
        )

    # Determine canonical explore_id from first successful root domain
    first_url, _, first_res = succeeded[0]
    primary_domain = (
        str((first_res.get("seed") or {}).get("root_domain") or "").strip().lower()
        or extract_seed_root_domain(None, first_url)
    )
    batch_scan_id = uuid.uuid4().hex[:12]
    final_id = _domain_explore_id(primary_domain) if (merge and primary_domain) else batch_scan_id
    dest_dir = ensure_dir(_explores_root() / final_id)
    existing_path = dest_dir / "explore_result.json"

    merged_result: dict[str, Any] | None = None
    if merge and existing_path.is_file():
        try:
            merged_result = read_json(existing_path)
        except Exception:
            merged_result = None

    counts_by_url: list[tuple[str, int]] = []
    for idx, (u, tdir, incoming) in enumerate(succeeded):
        ep_count = len(incoming.get("discovered_endpoints") or [])
        counts_by_url.append((u, ep_count))
        ts_tag = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + f"_{idx:02d}_" + str(incoming.get("explore_id") or "")[:6]
        _copy_explore_supporting_files(tdir, dest_dir, ts_tag)
        if merged_result is None:
            merged_result = dict(incoming)
            merged_result["explore_id"] = final_id
        else:
            merged_result = merge_explore_results(merged_result, incoming, explore_id=final_id)
        shutil.rmtree(tdir, ignore_errors=True)

    # Append zero counts for failed URLs in counts_by_url
    succeeded_urls = {u for u, _, _ in succeeded}
    for u in urls:
        if u not in succeeded_urls:
            counts_by_url.append((u, 0))

    assert merged_result is not None
    if project_id:
        merged_result["project_id"] = project_id
    if failed_warnings:
        merged_result["warnings"] = list(
            dict.fromkeys([*(merged_result.get("warnings") or []), *failed_warnings])
        )[-30:]
    merged_hosts = _collect_merged_hosts(merged_result)
    merged_result["merged_hosts"] = merged_hosts
    merged_result["seed_hosts"] = list(merged_hosts)
    write_json(existing_path, merged_result)
    return merged_result, counts_by_url


@router.post("/explore", status_code=status.HTTP_201_CREATED)
@router.post("/explore/seed", status_code=status.HTTP_201_CREATED)
async def run_explore(body: ExploreRequest) -> dict[str, Any]:
    """Run an Explore & Discovery scan (single or multi-URL batch), merging by root domain."""
    has_curl = bool(body.seed_curl and body.seed_curl.strip())
    parsed_urls = _parse_batch_target_urls(body.target_url, body.target_urls)
    if not has_curl and not parsed_urls:
        raise HTTPException(
            status_code=400,
            detail="Either seed_curl, target_url, or target_urls must be provided.",
        )

    if body.merge:
        _consolidate_legacy_explores()

    # Multi-URL batch explore path (when >= 2 URLs provided via target_urls or multiline target_url)
    if not has_curl and len(parsed_urls) >= 2:
        merged_result, _ = await _run_batch_explore_urls(
            parsed_urls,
            mode=body.mode,
            probe_openapi_docs=body.probe_openapi_docs,
            crawl_js=body.crawl_js,
            depth=body.depth,
            merge=body.merge,
            project_id=body.project_id,
        )
        return merged_result

    single_url = parsed_urls[0] if parsed_urls else body.target_url
    scan_id = uuid.uuid4().hex[:12]
    tmp_dir = ensure_dir(_explores_root() / f"_tmp_{scan_id}")
    try:
        incoming = await asyncio.to_thread(
            explore_from_seed,
            tmp_dir,
            seed_curl=body.seed_curl,
            target_url=single_url,
            mode=body.mode,
            probe_openapi_docs=body.probe_openapi_docs,
            crawl_js=body.crawl_js,
            depth=body.depth,
            explore_id=scan_id,
        )
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.project_id:
        incoming["project_id"] = body.project_id

    resolved_domain = (
        str((incoming.get("seed") or {}).get("root_domain") or "").strip().lower()
        or extract_seed_root_domain(body.seed_curl, single_url)
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

    merged_hosts = _collect_merged_hosts(final_result)
    final_result["merged_hosts"] = merged_hosts
    final_result["seed_hosts"] = list(merged_hosts)
    write_json(existing_path, final_result)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return final_result


@router.post("/projects/{project_id}/explore-all", status_code=status.HTTP_201_CREATED)
async def explore_all_project_targets(
    project_id: str,
    body: ExploreAllTargetsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run multi-portal batch explore across all (or selected) registered Targets of a project."""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    t_res = await db.execute(select(Target).where(Target.project_id == project_id))
    all_targets = list(t_res.scalars().all())
    if not all_targets:
        raise HTTPException(
            status_code=400,
            detail="No targets configured for this project. Add at least one portal Target first.",
        )

    if body.target_ids:
        by_id = {t.id: t for t in all_targets}
        selected_targets = [by_id[tid] for tid in body.target_ids if tid in by_id]
        if not selected_targets:
            raise HTTPException(
                status_code=400,
                detail="None of the requested target_ids belong to this project.",
            )
    else:
        selected_targets = all_targets

    if body.merge:
        _consolidate_legacy_explores()

    target_url_pairs: list[tuple[Target, str]] = []
    urls: list[str] = []
    for tgt in selected_targets:
        u = (tgt.base_url or "").strip()
        if not u and tgt.root_domain:
            u = f"https://{tgt.root_domain.strip()}"
        if u:
            target_url_pairs.append((tgt, u))
            urls.append(u)

    if not urls:
        raise HTTPException(status_code=400, detail="Selected targets have no valid URLs.")

    merged_result, counts_by_url = await _run_batch_explore_urls(
        urls,
        mode=body.mode,
        probe_openapi_docs=body.probe_openapi_docs,
        crawl_js=body.crawl_js,
        depth=body.depth,
        merge=body.merge,
        project_id=project_id,
    )
    count_map = dict(counts_by_url)
    explored_targets = [
        {
            "id": tgt.id,
            "portal_label": str((tgt.scope_rules or {}).get("portal_label") or ""),
            "base_url": u,
            "endpoints_found": int(count_map.get(u, 0)),
        }
        for tgt, u in target_url_pairs
    ]
    merged_result["explored_targets"] = explored_targets
    dest_path = _explores_root() / str(merged_result["explore_id"]) / "explore_result.json"
    if dest_path.parent.is_dir():
        write_json(dest_path, merged_result)
    return merged_result


@router.get("/explore")
async def list_explores(
    project_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """List consolidated root-domain Explore inventories (optionally filtered to a project)."""
    _consolidate_legacy_explores()
    project_domains = await _project_domain_set(db, project_id) if project_id else set()
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
        if project_id and not _explore_matches_project(data, project_id, project_domains):
            continue
        scans = data.get("scans") or []
        summary = dict(data.get("summary") or {})
        scans_count = int(summary.get("scans_count") or len(scans) or 1)
        summary["scans_count"] = scans_count
        created_at = str(data.get("created_at") or "")
        updated_at = str(data.get("updated_at") or created_at)
        seed_info = data.get("seed") or {}
        merged_hosts = _collect_merged_hosts(data)
        items.append(
            {
                "explore_id": data.get("explore_id", res_file.parent.name),
                "project_id": data.get("project_id"),
                "root_domain": str(seed_info.get("root_domain") or ""),
                "merged_hosts": merged_hosts,
                "seed_hosts": list(merged_hosts),
                "created_at": created_at,
                "updated_at": updated_at,
                "mode": data.get("mode", "passive"),
                "seed": seed_info,
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
    merged_hosts = _collect_merged_hosts(data)
    data.setdefault("merged_hosts", merged_hosts)
    data.setdefault("seed_hosts", list(merged_hosts))
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
