"""Separate Explore & Discovery workspace endpoints and Promote-to-Run bridge."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.config import settings
from reconqa.models.db import get_db
from reconqa.models.schemas import (
    AuthProfileOut,
    DiscoverPortalsRequest,
    DiscoverPortalsResponse,
    ExploreAuthConfig,
    ExploreVerifyAuthRequest,
    ExploreVerifyAuthResponse,
    RunOut,
)
from reconqa.models.tables import AuthProfile, Job, Project, Run, Target
from reconqa.modules.discovery.auth_resolver import (
    ResolvedAuth,
    headers_for_url,
    is_unsafe_auth_url,
    load_storage_state_cookies_for_host,
    make_async_scope_request_hook,
    redact_secrets,
    resolve_explore_auth,
    sanitize_url_for_storage,
)
from reconqa.modules.discovery.seed_explorer import (
    _collect_merged_hosts,
    _registrable_root_domain,
    discover_sibling_portals,
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
_MAX_EXPLORE_JOBS = 40
_EXPLORE_JOBS: dict[str, dict[str, Any]] = {}
_EXPLORE_JOB_TASKS: set[asyncio.Task[Any]] = set()


class ExploreRequest(BaseModel):
    seed_curl: str | None = None
    target_url: str | None = None
    target_urls: list[str] | None = None
    mode: Literal["passive", "active"] = "passive"
    probe_openapi_docs: bool = True
    crawl_js: bool = True
    discover_portals: bool = True
    depth: int = 2
    merge: bool = True
    project_id: str | None = None
    credential_id: str | None = None
    auth: ExploreAuthConfig | None = None


class ExploreJobRequest(ExploreRequest):
    target_ids: list[str] | None = None
    explore_all_targets: bool = False


class ExploreJobOut(BaseModel):
    job_id: str
    project_id: str | None = None
    status: Literal["running", "completed", "failed"] = "running"
    progress_pct: int = 0
    stage: str = "init"
    targets_total: int = 1
    targets_done: int = 0
    current_target: str = ""
    endpoints_found: int = 0
    explore_id: str | None = None
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


def _prune_explore_jobs() -> None:
    if len(_EXPLORE_JOBS) <= _MAX_EXPLORE_JOBS:
        return
    ordered = sorted(
        _EXPLORE_JOBS.values(),
        key=lambda j: (1 if j.get("status") == "running" else 0, str(j.get("started_at") or "")),
    )
    excess = len(_EXPLORE_JOBS) - _MAX_EXPLORE_JOBS
    for job in ordered[:excess]:
        _EXPLORE_JOBS.pop(job["job_id"], None)


def _serialize_explore_job(job: dict[str, Any], *, include_result: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job["job_id"],
        "project_id": job.get("project_id"),
        "status": job.get("status", "running"),
        "progress_pct": int(job.get("progress_pct", 0)),
        "stage": str(job.get("stage") or "init"),
        "targets_total": int(job.get("targets_total", 1)),
        "targets_done": int(job.get("targets_done", 0)),
        "current_target": str(job.get("current_target") or ""),
        "endpoints_found": int(job.get("endpoints_found", 0)),
        "explore_id": job.get("explore_id"),
        "started_at": str(job.get("started_at") or ""),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
    }
    if include_result:
        out["result"] = job.get("result")
    return out


class ExploreAllTargetsRequest(BaseModel):
    target_ids: list[str] | None = None
    mode: Literal["passive", "active"] = "passive"
    probe_openapi_docs: bool = True
    crawl_js: bool = True
    discover_portals: bool = True
    depth: int = 2
    merge: bool = True
    credential_id: str | None = None
    auth: ExploreAuthConfig | None = None


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


def _build_auth_meta(resolved: ResolvedAuth | None) -> dict[str, Any] | None:
    if resolved is None:
        return None
    return {
        "auth_mode": resolved.auth_mode,
        "profile_id": resolved.profile_id,
        "profile_name": resolved.profile_name,
        "scope_domains": list(resolved.scope_domains or []),
    }


def _is_env_ref_str(raw: str | None) -> bool:
    val = str(raw or "").strip()
    return val.startswith(("ENV:", "env:")) or (val.startswith("${") and val.endswith("}"))


def _build_profile_headers_for_save(
    auth_cfg: ExploreAuthConfig,
    resolved: ResolvedAuth,
) -> dict[str, str]:
    """Build extra_headers_ref to persist on AuthProfile while preserving ENV:RECONQA_* refs."""
    mode = auth_cfg.auth_type
    if mode == "basic":
        # username & password (including ENV refs) are stored in login_config;
        # avoid persisting resolved plaintext Basic header into extra_headers_ref.
        return {}
    if mode == "bearer":
        raw_tok = str(auth_cfg.token or "").strip()
        if _is_env_ref_str(raw_tok):
            if not (auth_cfg.token_prefix or ""):
                return {auth_cfg.token_header or "Authorization": raw_tok}
            # Prefix + ENV ref stored cleanly in login_config so it resolves dynamically.
            return {}
        return dict(resolved.headers)
    if mode == "cookie":
        raw_ck = str(auth_cfg.cookie or "").strip()
        if _is_env_ref_str(raw_ck):
            return {"Cookie": raw_ck}
        return dict(resolved.headers)
    if mode == "headers":
        out: dict[str, str] = {}
        for k, v in (auth_cfg.headers or {}).items():
            h_name = str(k or "").strip()
            raw_v = str(v or "").strip()
            if not h_name or h_name.startswith(":"):
                continue
            if _is_env_ref_str(raw_v):
                out[h_name] = raw_v
            elif h_name in resolved.headers:
                out[h_name] = resolved.headers[h_name]
        return out
    # login_api / form_login cache exchanged session headers; when form_login saves
    # storage_state.json, keep cookies exclusively in storage_state for per-host scoping.
    if mode == "form_login" and resolved.storage_state_data:
        return {k: v for k, v in resolved.headers.items() if k.lower() != "cookie"}
    return dict(resolved.headers)


def _build_profile_login_config_from_auth(auth_cfg: ExploreAuthConfig) -> dict[str, Any]:
    mode = auth_cfg.auth_type
    cfg: dict[str, Any] = {}
    if auth_cfg.verify_url:
        cfg["verify_url"] = auth_cfg.verify_url
    if auth_cfg.scope_domains:
        cfg["scope_domains"] = list(auth_cfg.scope_domains)
    if mode == "bearer" and _is_env_ref_str(auth_cfg.token):
        cfg.update(
            {
                "token": str(auth_cfg.token or "").strip(),
                "token_header": auth_cfg.token_header,
                "token_prefix": auth_cfg.token_prefix,
            }
        )
    elif mode == "basic":
        cfg.update(
            {
                "username": auth_cfg.username or "",
                "password": auth_cfg.password or "",
            }
        )
    elif mode == "login_api":
        cfg.update(
            {
                "login_url": auth_cfg.login_url or "",
                "login_method": auth_cfg.login_method,
                "login_content_type": auth_cfg.login_content_type,
                "username_field": auth_cfg.username_field,
                "password_field": auth_cfg.password_field,
                "username": auth_cfg.username or "",
                "password": auth_cfg.password or "",
                "extra_fields": dict(auth_cfg.extra_fields or {}),
                "token_jsonpath": auth_cfg.token_jsonpath or "",
                "token_header": auth_cfg.token_header,
                "token_prefix": auth_cfg.token_prefix,
            }
        )
    elif mode == "form_login":
        cfg.update(
            {
                "login_url": auth_cfg.login_url or "",
                "username": auth_cfg.username or "",
                "password": auth_cfg.password or "",
                "username_selector": auth_cfg.username_selector or "",
                "password_selector": auth_cfg.password_selector or "",
                "submit_selector": auth_cfg.submit_selector or "",
                "token_header": auth_cfg.token_header,
                "token_prefix": auth_cfg.token_prefix,
            }
        )
    return cfg


async def _resolve_request_auth(
    db: AsyncSession,
    *,
    project_id: str | None,
    credential_id: str | None,
    auth_cfg: ExploreAuthConfig | None,
    target_hint: str = "",
    allow_save: bool = True,
) -> tuple[ResolvedAuth, dict[str, Any] | None]:
    """Resolve runtime auth headers ONCE per Explore request and optionally save as AuthProfile."""
    eff_cred_id = (credential_id or "").strip() or (
        (auth_cfg.credential_id or "").strip() if auth_cfg else ""
    )
    profile: AuthProfile | None = None
    if eff_cred_id:
        profile = await db.get(AuthProfile, eff_cred_id)
        if profile is None or (project_id and profile.project_id != project_id):
            raise HTTPException(
                status_code=404,
                detail=f"Auth profile '{eff_cred_id}' not found",
            )

    target_host = ""
    if target_hint:
        hint_raw = target_hint.strip()
        if "://" in hint_raw:
            target_host = (urlparse(hint_raw).hostname or "").lower()
        else:
            target_host = (urlparse(f"https://{hint_raw}").hostname or "").lower()

    auth_dict = auth_cfg.model_dump() if auth_cfg is not None else None
    resolved = await asyncio.to_thread(
        resolve_explore_auth,
        auth_dict,
        profile=profile,
        target_host=target_host,
    )

    saved_profile_out: dict[str, Any] | None = None
    if (
        allow_save
        and auth_cfg is not None
        and auth_cfg.save_as_profile
        and project_id
        and auth_cfg.auth_type not in {"none", "profile"}
        and resolved.login_succeeded is not False
        and bool(resolved.headers)
    ):
        login_cfg = _build_profile_login_config_from_auth(auth_cfg)
        headers_to_save = _build_profile_headers_for_save(auth_cfg, resolved)
        if headers_to_save or login_cfg:
            proj = await db.get(Project, project_id)
            if proj is not None:
                profile_name = (
                    (auth_cfg.profile_name or "").strip()
                    or f"Explore Auth ({resolved.auth_mode})"
                )
                role_tag = (auth_cfg.role_tag or "account_a").strip() or "account_a"
                existing_res = await db.execute(
                    select(AuthProfile).where(
                        AuthProfile.project_id == project_id,
                        AuthProfile.name == profile_name,
                    )
                )
                saved_prof = existing_res.scalars().first()
                if saved_prof is not None:
                    saved_prof.role_tag = role_tag
                    saved_prof.auth_type = auth_cfg.auth_type
                    saved_prof.extra_headers_ref = headers_to_save
                    saved_prof.login_config = login_cfg
                else:
                    saved_prof = AuthProfile(
                        project_id=project_id,
                        name=profile_name,
                        role_tag=role_tag,
                        auth_type=auth_cfg.auth_type,
                        storage_state_path=None,
                        extra_headers_ref=headers_to_save,
                        login_config=login_cfg,
                    )
                    db.add(saved_prof)
                    await db.flush()

                if resolved.storage_state_data:
                    auth_dir = ensure_dir(
                        settings.data_dir.resolve() / "projects" / project_id / "auth"
                    )
                    safe_name = (
                        f"{slugify(saved_prof.id)}_{slugify(saved_prof.role_tag)}_storage_state.json"
                    )
                    dest_path = (auth_dir / safe_name).resolve()
                    if auth_dir.resolve() in dest_path.parents:
                        dest_path.write_text(
                            json.dumps(resolved.storage_state_data, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        saved_prof.storage_state_path = str(dest_path)

                await db.commit()
                await db.refresh(saved_prof)
                resolved.profile_id = saved_prof.id
                resolved.profile_name = saved_prof.name
                saved_profile_out = AuthProfileOut.model_validate(saved_prof).model_dump(
                    mode="json"
                )

    return resolved, saved_profile_out


async def _run_batch_explore_urls(
    urls: list[str],
    *,
    mode: str,
    probe_openapi_docs: bool,
    crawl_js: bool,
    discover_portals: bool = True,
    depth: int,
    merge: bool,
    project_id: str | None,
    job_state: dict[str, Any] | None = None,
    resolved_auth: ResolvedAuth | None = None,
) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    """Explore multiple portal URLs with bounded concurrency (2) and merge into root-domain inventory."""
    sem = asyncio.Semaphore(2)
    per_url_out: list[tuple[str, Path | None, dict[str, Any] | None, str | None]] = []
    per_target_pct = [0] * len(urls)
    auth_meta = _build_auth_meta(resolved_auth)
    auth_secrets = list(resolved_auth.secret_values) if resolved_auth else []
    auth_scope = list(resolved_auth.scope_domains) if resolved_auth else []

    # Bind batch auth scope to explicit scope_domains, login_url root domain, or first target root domain
    # so unrelated root domains in a multi-target batch never auto-authorize themselves.
    batch_allowed_root = ""
    if not auth_scope:
        scope_seed_url = (
            (resolved_auth.login_url or "").strip()
            if (resolved_auth and resolved_auth.login_url)
            else (urls[0] if urls else "")
        )
        if scope_seed_url:
            raw_s = scope_seed_url if "://" in scope_seed_url else f"https://{scope_seed_url}"
            s_host = (urlparse(raw_s).hostname or "").lower()
            batch_allowed_root = _registrable_root_domain(s_host) or s_host

    def _headers_for_batch_target(u: str) -> tuple[dict[str, str] | None, list[str]]:
        if resolved_auth is None:
            return None, list(auth_secrets)
        u_raw = u if "://" in u else f"https://{u}"
        u_host = (urlparse(u_raw).hostname or "").lower()
        per_secrets = list(auth_secrets)
        if resolved_auth.storage_state_path:
            # Re-filter storage_state cookies per portal host to avoid leaking Portal A cookies to Portal B
            per_hdrs = {
                k: v
                for k, v in (resolved_auth.non_storage_headers or {}).items()
                if k.lower() != "cookie"
            }
            ck_hdr, ck_secs = load_storage_state_cookies_for_host(
                resolved_auth.storage_state_path, u_host
            )
            if ck_hdr:
                per_hdrs["Cookie"] = ck_hdr
                per_secrets.extend(ck_secs)
            return (per_hdrs or None), per_secrets
        return (dict(resolved_auth.headers) if resolved_auth.headers else None), per_secrets

    def _update_batch_progress(idx: int, stage: str, pct: int, detail: str) -> None:
        if job_state is None:
            return
        per_target_pct[idx] = max(per_target_pct[idx], max(0, min(100, int(pct))))
        overall = 5 + int((sum(per_target_pct) / max(1, len(urls))) * 0.85)
        job_state["progress_pct"] = max(int(job_state.get("progress_pct", 0)), min(92, overall))
        job_state["stage"] = stage
        if detail:
            job_state["current_target"] = sanitize_url_for_storage(detail)

    async def _explore_one(idx: int, u: str) -> tuple[str, Path | None, dict[str, Any] | None, str | None]:
        async with sem:
            sid = uuid.uuid4().hex[:12]
            tdir = ensure_dir(_explores_root() / f"_tmp_{sid}")
            safe_u = sanitize_url_for_storage(u)
            if job_state is not None:
                job_state["current_target"] = safe_u

            def _cb(stage: str, pct: int, detail: str) -> None:
                _update_batch_progress(idx, stage, pct, detail or safe_u)

            per_headers, per_secrets = _headers_for_batch_target(u)
            try:
                res = await asyncio.to_thread(
                    explore_from_seed,
                    tdir,
                    target_url=u,
                    mode=mode,
                    probe_openapi_docs=probe_openapi_docs,
                    crawl_js=crawl_js,
                    discover_portals=discover_portals and idx == 0,
                    depth=depth,
                    explore_id=sid,
                    on_progress=_cb if job_state is not None else None,
                    extra_headers=per_headers,
                    auth_meta=auth_meta,
                    secret_values=per_secrets,
                    scope_domains=auth_scope,
                    allowed_root_domain=batch_allowed_root or None,
                )
                if project_id:
                    res["project_id"] = project_id
                if job_state is not None:
                    job_state["targets_done"] = int(job_state.get("targets_done", 0)) + 1
                    job_state["endpoints_found"] = int(job_state.get("endpoints_found", 0)) + len(
                        res.get("discovered_endpoints") or []
                    )
                    _update_batch_progress(idx, "finalize", 100, safe_u)
                return u, tdir, res, None
            except Exception as exc:
                shutil.rmtree(tdir, ignore_errors=True)
                if job_state is not None:
                    job_state["targets_done"] = int(job_state.get("targets_done", 0)) + 1
                    _update_batch_progress(idx, "finalize", 100, safe_u)
                return u, None, None, redact_secrets(str(exc), per_secrets)

    results = await asyncio.gather(*(_explore_one(i, u) for i, u in enumerate(urls)))
    per_url_out.extend(results)

    succeeded = [(u, tdir, res) for (u, tdir, res, err) in per_url_out if res is not None and tdir is not None]
    failed_warnings = [f"Failed to explore {u}: {err}" for (u, _, res, err) in per_url_out if res is None and err]

    if not succeeded:
        raise HTTPException(
            status_code=400,
            detail="; ".join(failed_warnings) or "All target URLs failed to explore.",
        )

    if job_state is not None:
        job_state["stage"] = "merge"
        job_state["progress_pct"] = max(int(job_state.get("progress_pct", 0)), 94)

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
    auth_warnings = list(resolved_auth.warnings) if resolved_auth else []
    extra_warnings = [*auth_warnings, *failed_warnings]
    if extra_warnings:
        merged_result["warnings"] = [
            redact_secrets(w, auth_secrets)
            for w in list(
                dict.fromkeys([*(merged_result.get("warnings") or []), *extra_warnings])
            )[-30:]
        ]
    merged_hosts = _collect_merged_hosts(merged_result)
    merged_result["merged_hosts"] = merged_hosts
    merged_result["seed_hosts"] = list(merged_hosts)
    write_json(existing_path, merged_result)
    return merged_result, counts_by_url


async def _run_single_explore_seed(
    *,
    seed_curl: str | None,
    target_url: str | None,
    mode: str,
    probe_openapi_docs: bool,
    crawl_js: bool,
    discover_portals: bool,
    depth: int,
    merge: bool,
    project_id: str | None,
    job_state: dict[str, Any] | None = None,
    resolved_auth: ResolvedAuth | None = None,
) -> dict[str, Any]:
    scan_id = uuid.uuid4().hex[:12]
    tmp_dir = ensure_dir(_explores_root() / f"_tmp_{scan_id}")
    auth_headers = dict(resolved_auth.headers) if resolved_auth and resolved_auth.headers else None
    auth_meta = _build_auth_meta(resolved_auth)
    auth_secrets = list(resolved_auth.secret_values) if resolved_auth else []
    auth_scope = list(resolved_auth.scope_domains) if resolved_auth else []

    def _cb(stage: str, pct: int, detail: str) -> None:
        if job_state is None:
            return
        job_state["stage"] = stage
        job_state["progress_pct"] = max(int(job_state.get("progress_pct", 0)), min(92, int(pct)))
        if detail:
            job_state["current_target"] = sanitize_url_for_storage(detail)

    try:
        incoming = await asyncio.to_thread(
            explore_from_seed,
            tmp_dir,
            seed_curl=seed_curl,
            target_url=target_url,
            mode=mode,
            probe_openapi_docs=probe_openapi_docs,
            crawl_js=crawl_js,
            discover_portals=discover_portals,
            depth=depth,
            explore_id=scan_id,
            on_progress=_cb if job_state is not None else None,
            extra_headers=auth_headers,
            auth_meta=auth_meta,
            secret_values=auth_secrets,
            scope_domains=auth_scope,
        )
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=redact_secrets(str(exc), auth_secrets)) from exc

    if job_state is not None:
        job_state["stage"] = "merge"
        job_state["progress_pct"] = max(int(job_state.get("progress_pct", 0)), 95)
        job_state["targets_done"] = 1
        job_state["endpoints_found"] = len(incoming.get("discovered_endpoints") or [])

    if project_id:
        incoming["project_id"] = project_id
    if resolved_auth and resolved_auth.warnings:
        incoming["warnings"] = [
            redact_secrets(w, auth_secrets)
            for w in list(
                dict.fromkeys([*(incoming.get("warnings") or []), *resolved_auth.warnings])
            )[-30:]
        ]

    resolved_domain = (
        str((incoming.get("seed") or {}).get("root_domain") or "").strip().lower()
        or extract_seed_root_domain(seed_curl, target_url)
    )
    final_id = _domain_explore_id(resolved_domain) if (merge and resolved_domain) else scan_id
    dest_dir = ensure_dir(_explores_root() / final_id)
    existing_path = dest_dir / "explore_result.json"

    ts_tag = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "_" + scan_id[:6]
    _copy_explore_supporting_files(tmp_dir, dest_dir, ts_tag)

    if merge and existing_path.is_file():
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


@router.post("/explore/verify-auth", response_model=ExploreVerifyAuthResponse)
async def verify_explore_auth(
    body: ExploreVerifyAuthRequest,
    db: AsyncSession = Depends(get_db),
) -> ExploreVerifyAuthResponse:
    """Burp-style read-only pre-flight session validity check (GET with follow_redirects=False, zero raw secrets)."""
    from reconqa.server.routers.projects import _classify_matrix_verdict, _should_bypass_env_proxy

    explicit_verify = (
        (body.verify_url or "").strip()
        or ((body.auth.verify_url or "").strip() if body.auth else "")
    )
    target_hint = explicit_verify or (body.target_url or "").strip()
    resolved, _ = await _resolve_request_auth(
        db,
        project_id=body.project_id,
        credential_id=body.credential_id,
        auth_cfg=body.auth,
        target_hint=target_hint,
        allow_save=False,
    )
    # Precedence: 1. body.verify_url -> 2. body.auth.verify_url -> 3. profile.login_config.verify_url -> 4. body.target_url
    raw_verify = (
        explicit_verify
        or (resolved.verify_url or "").strip()
        or (body.target_url or "").strip()
    )

    if not raw_verify:
        raise HTTPException(
            status_code=400,
            detail="Either verify_url or target_url must be provided.",
        )
    if "://" not in raw_verify:
        raw_verify = f"https://{raw_verify}"
    if is_unsafe_auth_url(raw_verify):
        raise HTTPException(
            status_code=400,
            detail="Verification URL must use http(s):// and must not target cloud metadata or link-local hosts.",
        )

    if resolved.login_succeeded is False:
        msg = redact_secrets(
            " ".join(resolved.warnings) or "Authentication login exchange failed.",
            resolved.secret_values,
        )
        return ExploreVerifyAuthResponse(
            valid=False,
            verdict="denied",
            auth_mode=resolved.auth_mode,
            header_names=resolved.auth_header_names,
            status_code=int(resolved.login_status_code or 0),
            message=msg,
            warnings=list(resolved.warnings),
        )

    if resolved.auth_mode != "none" and not resolved.headers:
        msg = redact_secrets(
            " ".join(resolved.warnings)
            or "No usable authentication headers were resolved (check token/cookie/ENV:RECONQA_* reference).",
            resolved.secret_values,
        )
        return ExploreVerifyAuthResponse(
            valid=False,
            verdict="denied",
            auth_mode=resolved.auth_mode,
            header_names=[],
            status_code=int(resolved.login_status_code or 0),
            message=msg,
            warnings=list(resolved.warnings),
        )

    v_host = (urlparse(raw_verify).hostname or "").lower()
    scope_doms = list(resolved.scope_domains or [])
    req_headers = (
        headers_for_url(raw_verify, resolved.headers, "", scope_domains=scope_doms)
        if scope_doms
        else dict(resolved.headers)
    )
    if scope_doms and resolved.headers and not req_headers:
        warn_msg = "Verification URL host is outside configured scope_domains; credentials were not sent."
        return ExploreVerifyAuthResponse(
            valid=False,
            verdict="denied",
            auth_mode=resolved.auth_mode,
            header_names=[],
            status_code=0,
            message=warn_msg,
            warnings=[*resolved.warnings, warn_msg],
        )

    scope_hook = make_async_scope_request_hook(
        v_host, scope_doms or None, list(req_headers.keys())
    )
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            verify=False,
            follow_redirects=False,
            timeout=12.0,
            trust_env=not _should_bypass_env_proxy(v_host),
            event_hooks={"request": [scope_hook]},
        ) as client:
            resp = await client.get(raw_verify, headers=req_headers)
    except httpx.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        err_msg = redact_secrets(f"Verification request failed: {exc}", resolved.secret_values)
        return ExploreVerifyAuthResponse(
            valid=False,
            verdict="error",
            auth_mode=resolved.auth_mode,
            header_names=resolved.auth_header_names,
            status_code=0,
            duration_ms=elapsed_ms,
            message=err_msg,
            warnings=list(resolved.warnings),
        )

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    redirected_to: str | None = None
    if 300 <= resp.status_code < 400:
        loc = (resp.headers.get("location") or "").strip()
        if loc:
            redirected_to = redact_secrets(loc, resolved.secret_values)

    body_json: Any = None
    try:
        raw_text = (resp.text or "").strip()
        if raw_text and ("json" in (resp.headers.get("content-type") or "").lower() or raw_text.startswith(("{", "["))):
            body_json = json.loads(raw_text)
    except Exception:
        body_json = None

    biz_code, matrix_verdict = _classify_matrix_verdict(resp.status_code, body_json)
    if matrix_verdict == "allowed":
        verdict: Literal["valid", "denied", "error"] = "valid"
        valid = True
        message = f"Session verified (HTTP {resp.status_code})."
    elif matrix_verdict == "error":
        verdict = "error"
        valid = False
        message = f"Verification endpoint returned server error (HTTP {resp.status_code})."
    else:
        verdict = "denied"
        valid = False
        if redirected_to:
            message = f"Session denied: HTTP {resp.status_code} redirected to {redirected_to}."
        elif biz_code is not None:
            message = f"Session denied (HTTP {resp.status_code}, code={biz_code})."
        else:
            message = f"Session denied (HTTP {resp.status_code})."

    return ExploreVerifyAuthResponse(
        valid=valid,
        verdict=verdict,
        auth_mode=resolved.auth_mode,
        header_names=resolved.auth_header_names,
        status_code=resp.status_code,
        biz_code=biz_code,
        redirected_to=redirected_to,
        duration_ms=elapsed_ms,
        message=redact_secrets(message, resolved.secret_values),
        warnings=list(resolved.warnings),
    )


@router.post("/explore", status_code=status.HTTP_201_CREATED)
@router.post("/explore/seed", status_code=status.HTTP_201_CREATED)
async def run_explore(
    body: ExploreRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run an Explore & Discovery scan (single or multi-URL batch), merging by root domain."""
    has_curl = bool(body.seed_curl and body.seed_curl.strip())
    parsed_urls = _parse_batch_target_urls(body.target_url, body.target_urls)
    if not has_curl and not parsed_urls:
        raise HTTPException(
            status_code=400,
            detail="Either seed_curl, target_url, or target_urls must be provided.",
        )

    target_hint = parsed_urls[0] if parsed_urls else (extract_seed_root_domain(body.seed_curl, None) or "")
    resolved_auth, saved_profile_out = await _resolve_request_auth(
        db,
        project_id=body.project_id,
        credential_id=body.credential_id,
        auth_cfg=body.auth,
        target_hint=target_hint,
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
            discover_portals=body.discover_portals,
            depth=body.depth,
            merge=body.merge,
            project_id=body.project_id,
            resolved_auth=resolved_auth,
        )
        if saved_profile_out is not None:
            return {**merged_result, "saved_auth_profile": saved_profile_out}
        return merged_result

    single_url = parsed_urls[0] if parsed_urls else body.target_url
    final_result = await _run_single_explore_seed(
        seed_curl=body.seed_curl,
        target_url=single_url,
        mode=body.mode,
        probe_openapi_docs=body.probe_openapi_docs,
        crawl_js=body.crawl_js,
        discover_portals=body.discover_portals,
        depth=body.depth,
        merge=body.merge,
        project_id=body.project_id,
        resolved_auth=resolved_auth,
    )
    if saved_profile_out is not None:
        return {**final_result, "saved_auth_profile": saved_profile_out}
    return final_result


@router.post("/explore/jobs", status_code=status.HTTP_202_ACCEPTED)
async def start_explore_job(
    body: ExploreJobRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Launch an async Explore scan job with real-time stage & percentage progress tracking."""
    has_curl = bool(body.seed_curl and body.seed_curl.strip())
    parsed_urls = _parse_batch_target_urls(body.target_url, body.target_urls)
    target_meta_rows: list[tuple[str, str, str]] = []  # (target_id, portal_label, url)

    if body.explore_all_targets:
        if not body.project_id:
            raise HTTPException(
                status_code=400,
                detail="project_id is required when explore_all_targets is true.",
            )
        project = await db.get(Project, body.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project '{body.project_id}' not found")
        t_res = await db.execute(select(Target).where(Target.project_id == body.project_id))
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

        for tgt in selected_targets:
            u = (tgt.base_url or "").strip()
            if not u and tgt.root_domain:
                u = f"https://{tgt.root_domain.strip()}"
            if u:
                label = str((tgt.scope_rules or {}).get("portal_label") or "")
                target_meta_rows.append((tgt.id, label, u))
        if not target_meta_rows:
            raise HTTPException(status_code=400, detail="Selected targets have no valid URLs.")
        targets_total = len(target_meta_rows)
        initial_target = target_meta_rows[0][2]
    else:
        if not has_curl and not parsed_urls:
            raise HTTPException(
                status_code=400,
                detail="Either seed_curl, target_url, target_urls, or explore_all_targets must be provided.",
            )
        targets_total = 1 if has_curl else len(parsed_urls)
        initial_target = (
            parsed_urls[0]
            if parsed_urls
            else (extract_seed_root_domain(body.seed_curl, None) or "seed_curl")
        )

    # Resolve auth ONCE here (never persist raw auth/headers/secrets inside _EXPLORE_JOBS!)
    resolved_auth, _ = await _resolve_request_auth(
        db,
        project_id=body.project_id,
        credential_id=body.credential_id,
        auth_cfg=body.auth,
        target_hint=initial_target,
    )

    job_id = f"exp_{uuid.uuid4().hex[:10]}"
    now_iso = datetime.now(UTC).isoformat()
    job_state: dict[str, Any] = {
        "job_id": job_id,
        "project_id": body.project_id,
        "status": "running",
        "progress_pct": 5,
        "stage": "init",
        "targets_total": targets_total,
        "targets_done": 0,
        "current_target": sanitize_url_for_storage(initial_target),
        "endpoints_found": 0,
        "explore_id": None,
        "started_at": now_iso,
        "finished_at": None,
        "error": None,
        "result": None,
    }
    _EXPLORE_JOBS[job_id] = job_state
    _prune_explore_jobs()

    async def _runner() -> None:
        try:
            if body.merge:
                await asyncio.to_thread(_consolidate_legacy_explores)

            if body.explore_all_targets:
                urls = [u for _, _, u in target_meta_rows]
                merged_result, counts_by_url = await _run_batch_explore_urls(
                    urls,
                    mode=body.mode,
                    probe_openapi_docs=body.probe_openapi_docs,
                    crawl_js=body.crawl_js,
                    discover_portals=body.discover_portals,
                    depth=body.depth,
                    merge=body.merge,
                    project_id=body.project_id,
                    job_state=job_state,
                    resolved_auth=resolved_auth,
                )
                count_map = dict(counts_by_url)
                merged_result["explored_targets"] = [
                    {
                        "id": tid,
                        "portal_label": label,
                        "base_url": sanitize_url_for_storage(u),
                        "endpoints_found": int(count_map.get(u, 0)),
                    }
                    for tid, label, u in target_meta_rows
                ]
                dest_path = _explores_root() / str(merged_result["explore_id"]) / "explore_result.json"
                if dest_path.parent.is_dir():
                    write_json(dest_path, merged_result)
                final_result = merged_result
            elif not has_curl and len(parsed_urls) >= 2:
                final_result, _ = await _run_batch_explore_urls(
                    parsed_urls,
                    mode=body.mode,
                    probe_openapi_docs=body.probe_openapi_docs,
                    crawl_js=body.crawl_js,
                    discover_portals=body.discover_portals,
                    depth=body.depth,
                    merge=body.merge,
                    project_id=body.project_id,
                    job_state=job_state,
                    resolved_auth=resolved_auth,
                )
            else:
                single_url = parsed_urls[0] if parsed_urls else body.target_url
                final_result = await _run_single_explore_seed(
                    seed_curl=body.seed_curl,
                    target_url=single_url,
                    mode=body.mode,
                    probe_openapi_docs=body.probe_openapi_docs,
                    crawl_js=body.crawl_js,
                    discover_portals=body.discover_portals,
                    depth=body.depth,
                    merge=body.merge,
                    project_id=body.project_id,
                    job_state=job_state,
                    resolved_auth=resolved_auth,
                )

            job_state["status"] = "completed"
            job_state["stage"] = "completed"
            job_state["progress_pct"] = 100
            job_state["targets_done"] = targets_total
            job_state["endpoints_found"] = len(final_result.get("discovered_endpoints") or [])
            job_state["explore_id"] = str(final_result.get("explore_id") or "")
            job_state["result"] = final_result
            job_state["finished_at"] = datetime.now(UTC).isoformat()
        except HTTPException as exc:
            job_state["status"] = "failed"
            job_state["stage"] = "failed"
            job_state["error"] = redact_secrets(str(exc.detail), resolved_auth.secret_values)
            job_state["finished_at"] = datetime.now(UTC).isoformat()
        except Exception as exc:
            job_state["status"] = "failed"
            job_state["stage"] = "failed"
            job_state["error"] = redact_secrets(str(exc), resolved_auth.secret_values)
            job_state["finished_at"] = datetime.now(UTC).isoformat()

    task = asyncio.create_task(_runner())
    _EXPLORE_JOB_TASKS.add(task)
    task.add_done_callback(_EXPLORE_JOB_TASKS.discard)

    return _serialize_explore_job(job_state, include_result=False)


@router.get("/explore/jobs")
async def list_explore_jobs(
    project_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
) -> list[dict[str, Any]]:
    """List recent async Explore jobs sorted newest first (without heavy result payload)."""
    jobs = list(_EXPLORE_JOBS.values())
    if project_id:
        jobs = [j for j in jobs if j.get("project_id") == project_id]
    if active_only:
        jobs = [j for j in jobs if j.get("status") == "running"]
    jobs.sort(key=lambda j: str(j.get("started_at") or ""), reverse=True)
    return [_serialize_explore_job(j, include_result=False) for j in jobs]


@router.get("/explore/jobs/{job_id}")
async def get_explore_job(job_id: str) -> dict[str, Any]:
    """Get status & progress of an async Explore job (includes result once completed)."""
    job = _EXPLORE_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Explore job '{job_id}' not found")
    return _serialize_explore_job(job, include_result=(job.get("status") == "completed"))


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

    resolved_auth, saved_profile_out = await _resolve_request_auth(
        db,
        project_id=project_id,
        credential_id=body.credential_id,
        auth_cfg=body.auth,
        target_hint=urls[0],
    )

    merged_result, counts_by_url = await _run_batch_explore_urls(
        urls,
        mode=body.mode,
        probe_openapi_docs=body.probe_openapi_docs,
        crawl_js=body.crawl_js,
        discover_portals=body.discover_portals,
        depth=body.depth,
        merge=body.merge,
        project_id=project_id,
        resolved_auth=resolved_auth,
    )
    count_map = dict(counts_by_url)
    explored_targets = [
        {
            "id": tgt.id,
            "portal_label": str((tgt.scope_rules or {}).get("portal_label") or ""),
            "base_url": sanitize_url_for_storage(u),
            "endpoints_found": int(count_map.get(u, 0)),
        }
        for tgt, u in target_url_pairs
    ]
    merged_result["explored_targets"] = explored_targets
    dest_path = _explores_root() / str(merged_result["explore_id"]) / "explore_result.json"
    if dest_path.parent.is_dir():
        write_json(dest_path, merged_result)
    if saved_profile_out is not None:
        return {**merged_result, "saved_auth_profile": saved_profile_out}
    return merged_result


@router.post(
    "/projects/{project_id}/discover-portals",
    response_model=DiscoverPortalsResponse,
)
async def discover_project_portals(
    project_id: str,
    body: DiscoverPortalsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Discover live sibling environment portals from a seed URL or the project's first Target."""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    seed_url = (body.seed_url or "").strip()
    if not seed_url:
        t_res = await db.execute(select(Target).where(Target.project_id == project_id))
        first_target = t_res.scalars().first()
        if first_target is not None:
            seed_url = (first_target.base_url or "").strip()
            if not seed_url and first_target.root_domain:
                seed_url = f"https://{first_target.root_domain.strip()}"

    if not seed_url:
        raise HTTPException(
            status_code=400,
            detail="Provide seed_url or configure at least one Target on this project.",
        )

    return await asyncio.to_thread(discover_sibling_portals, seed_url)


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
