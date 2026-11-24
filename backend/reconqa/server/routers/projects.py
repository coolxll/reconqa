"""CRUD endpoints for projects, targets, auth profiles, and project knowledge base."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.config import settings
from reconqa.models.db import get_db
from reconqa.models.schemas import (
    AuthProfileCreate,
    AuthProfileOut,
    KnowledgeDocCreate,
    KnowledgeDocOut,
    KnowledgeDocUpdate,
    ProjectCreate,
    ProjectInventoryResponse,
    ProjectOut,
    ProjectUpdate,
    ReplayMatrixEntry,
    ReplayMatrixRequest,
    ReplayMatrixResponse,
    ReplayRequest,
    ReplayResponse,
    TargetCreate,
    TargetOut,
)
from reconqa.models.tables import AuthProfile, KnowledgeDoc, Project, Run, Target
from reconqa.modules.hub.inventory import build_project_inventory
from reconqa.server.routers.explore import find_project_explores
from reconqa.utils.io import ensure_dir, slugify

router = APIRouter(tags=["projects"])

_ALLOWED_REPLAY_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_BLOCKED_METADATA_HOSTS = {
    "169.254.169.254",
    "169.254.170.2",
    "metadata.google.internal",
    "metadata.goog",
}
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_LINK_LOCAL_NET = ipaddress.ip_network("169.254.0.0/16")
_MAX_REPLAY_BODY_BYTES = 200 * 1024
_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_VALID_DOC_TYPES = {"prd", "api_spec", "business_rule", "test_notes", "architecture", "other"}
_TEXT_EXTRACTABLE_EXTS = {
    ".md",
    ".markdown",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".csv",
    ".http",
    ".xml",
    ".sql",
    ".graphql",
}
_MAX_EXTRACTED_TEXT_BYTES = 512 * 1024


async def _get_project_or_404(db: AsyncSession, project_id: str) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
) -> Project:
    base_slug = slugify(body.slug or body.name)
    slug = base_slug
    existing = await db.execute(select(Project).where(Project.slug == slug))
    if existing.scalar_one_or_none() is not None:
        if body.slug:
            raise HTTPException(status_code=409, detail=f"Project slug '{slug}' already exists")
        suffix = 2
        while True:
            candidate = f"{base_slug}-{suffix}"
            check = await db.execute(select(Project).where(Project.slug == candidate))
            if check.scalar_one_or_none() is None:
                slug = candidate
                break
            suffix += 1

    project = Project(name=body.name, slug=slug, description=body.description)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)) -> list[Project]:
    res = await db.execute(select(Project).order_by(Project.created_at.desc()))
    return list(res.scalars().all())


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)) -> Project:
    return await _get_project_or_404(db, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
) -> Project:
    project = await _get_project_or_404(db, project_id)
    if body.name is not None:
        trimmed = body.name.strip()
        if not trimmed:
            raise HTTPException(status_code=422, detail="Project name must not be blank")
        project.name = trimmed
    if body.description is not None:
        project.description = body.description
    await db.commit()
    await db.refresh(project)
    return project


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    project = await _get_project_or_404(db, project_id)
    await db.delete(project)
    await db.commit()
    return {"deleted": True, "id": project_id}


# --- Targets ---


@router.post(
    "/projects/{project_id}/targets",
    response_model=TargetOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_target(
    project_id: str,
    body: TargetCreate,
    db: AsyncSession = Depends(get_db),
) -> Target:
    await _get_project_or_404(db, project_id)
    target = Target(
        project_id=project_id,
        root_domain=body.root_domain,
        base_url=body.base_url,
        scope_rules=body.scope_rules,
    )
    db.add(target)
    await db.commit()
    await db.refresh(target)
    return target


@router.get("/projects/{project_id}/targets", response_model=list[TargetOut])
async def list_targets(project_id: str, db: AsyncSession = Depends(get_db)) -> list[Target]:
    await _get_project_or_404(db, project_id)
    res = await db.execute(select(Target).where(Target.project_id == project_id))
    return list(res.scalars().all())


@router.delete("/projects/{project_id}/targets/{target_id}")
async def delete_target(
    project_id: str,
    target_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    await _get_project_or_404(db, project_id)
    target = await db.get(Target, target_id)
    if target is None or target.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"Target '{target_id}' not found")
    await db.delete(target)
    await db.commit()
    return {"deleted": True, "id": target_id}


# --- Auth Profiles ---


@router.post(
    "/projects/{project_id}/auth-profiles",
    response_model=AuthProfileOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_auth_profile(
    project_id: str,
    body: AuthProfileCreate,
    db: AsyncSession = Depends(get_db),
) -> AuthProfile:
    await _get_project_or_404(db, project_id)
    profile = AuthProfile(
        project_id=project_id,
        name=body.name,
        role_tag=body.role_tag,
        storage_state_path=body.storage_state_path,
        extra_headers_ref=body.extra_headers_ref,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


@router.get("/projects/{project_id}/auth-profiles", response_model=list[AuthProfileOut])
async def list_auth_profiles(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[AuthProfile]:
    await _get_project_or_404(db, project_id)
    res = await db.execute(select(AuthProfile).where(AuthProfile.project_id == project_id))
    return list(res.scalars().all())


@router.post(
    "/projects/{project_id}/auth-profiles/{profile_id}/storage-state",
    response_model=AuthProfileOut,
)
async def upload_storage_state(
    project_id: str,
    profile_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> AuthProfile:
    await _get_project_or_404(db, project_id)
    profile = await db.get(AuthProfile, profile_id)
    if profile is None or profile.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"Auth profile '{profile_id}' not found")

    raw_bytes = await file.read()
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("storage_state must be a JSON object")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid storage_state JSON: {exc}") from exc

    auth_dir = ensure_dir(settings.data_dir.resolve() / "projects" / project_id / "auth")
    safe_name = f"{slugify(profile_id)}_{slugify(profile.role_tag)}_storage_state.json"
    dest_path = (auth_dir / safe_name).resolve()
    if auth_dir.resolve() not in dest_path.parents:
        raise HTTPException(status_code=400, detail="Unsafe storage path")

    dest_path.write_bytes(raw_bytes)
    profile.storage_state_path = str(dest_path)
    await db.commit()
    await db.refresh(profile)
    return profile


@router.delete("/projects/{project_id}/auth-profiles/{profile_id}")
async def delete_auth_profile(
    project_id: str,
    profile_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    await _get_project_or_404(db, project_id)
    profile = await db.get(AuthProfile, profile_id)
    if profile is None or profile.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"Auth profile '{profile_id}' not found")
    if profile.storage_state_path:
        p = Path(profile.storage_state_path)
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    await db.delete(profile)
    await db.commit()
    return {"deleted": True, "id": profile_id}


# --- Project Knowledge Base ---


def _split_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


async def _get_knowledge_or_404(
    db: AsyncSession,
    project_id: str,
    doc_id: str,
) -> KnowledgeDoc:
    await _get_project_or_404(db, project_id)
    doc = await db.get(KnowledgeDoc, doc_id)
    if doc is None or doc.project_id != project_id:
        raise HTTPException(status_code=404, detail=f"Knowledge doc '{doc_id}' not found")
    return doc


@router.get("/projects/{project_id}/knowledge", response_model=list[KnowledgeDocOut])
async def list_knowledge_docs(
    project_id: str,
    doc_type: str | None = Query(default=None),
    q: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeDoc]:
    await _get_project_or_404(db, project_id)
    stmt = (
        select(KnowledgeDoc)
        .where(KnowledgeDoc.project_id == project_id)
        .order_by(KnowledgeDoc.updated_at.desc(), KnowledgeDoc.created_at.desc())
    )
    if doc_type:
        stmt = stmt.where(KnowledgeDoc.doc_type == doc_type.strip().lower())
    res = await db.execute(stmt)
    docs = list(res.scalars().all())

    if q and q.strip():
        needle = q.strip().lower()
        filtered: list[KnowledgeDoc] = []
        for doc in docs:
            haystack = " ".join(
                [
                    doc.title or "",
                    doc.content or "",
                    doc.original_filename or "",
                    doc.external_url or "",
                    " ".join(doc.tags or []),
                    " ".join(doc.related_endpoints or []),
                ]
            ).lower()
            if needle in haystack:
                filtered.append(doc)
        return filtered

    return docs


@router.post(
    "/projects/{project_id}/knowledge",
    response_model=KnowledgeDocOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_doc(
    project_id: str,
    body: KnowledgeDocCreate,
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDoc:
    await _get_project_or_404(db, project_id)
    now = datetime.now(UTC)
    payload_text = body.content or body.external_url or ""
    doc = KnowledgeDoc(
        project_id=project_id,
        title=body.title.strip(),
        doc_type=body.doc_type,
        source_kind=body.source_kind,
        content=body.content,
        external_url=body.external_url,
        tags=[t.strip() for t in body.tags if t.strip()],
        related_endpoints=[e.strip() for e in body.related_endpoints if e.strip()],
        size_bytes=len(payload_text.encode("utf-8")),
        created_at=now,
        updated_at=now,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post(
    "/projects/{project_id}/knowledge/upload",
    response_model=KnowledgeDocOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_knowledge_doc(
    project_id: str,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    doc_type: str = Form(default="prd"),
    tags: str | None = Form(default=None),
    related_endpoints: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDoc:
    await _get_project_or_404(db, project_id)
    norm_type = (doc_type or "prd").strip().lower()
    if norm_type not in _VALID_DOC_TYPES:
        norm_type = "other"

    raw_bytes = await file.read()
    orig_name = Path(file.filename or "document.md").name
    ext = Path(orig_name).suffix.lower()
    stem = Path(orig_name).stem or "document"
    safe_filename = f"{uuid.uuid4().hex[:8]}_{slugify(stem)}{ext}"

    kb_dir = ensure_dir(settings.data_dir.resolve() / "projects" / project_id / "knowledge")
    dest_path = (kb_dir / safe_filename).resolve()
    if kb_dir.resolve() not in dest_path.parents:
        raise HTTPException(status_code=400, detail="Unsafe knowledge storage path")
    dest_path.write_bytes(raw_bytes)

    extracted_content = ""
    if ext in _TEXT_EXTRACTABLE_EXTS:
        try:
            extracted_content = raw_bytes[:_MAX_EXTRACTED_TEXT_BYTES].decode(
                "utf-8", errors="replace"
            )
        except Exception:
            extracted_content = ""

    now = datetime.now(UTC)
    doc = KnowledgeDoc(
        project_id=project_id,
        title=(title.strip() if title and title.strip() else orig_name),
        doc_type=norm_type,
        source_kind="file",
        content=extracted_content,
        file_path=str(dest_path),
        original_filename=orig_name,
        tags=_split_csv_list(tags),
        related_endpoints=_split_csv_list(related_endpoints),
        size_bytes=len(raw_bytes),
        created_at=now,
        updated_at=now,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.put("/projects/{project_id}/knowledge/{doc_id}", response_model=KnowledgeDocOut)
async def update_knowledge_doc(
    project_id: str,
    doc_id: str,
    body: KnowledgeDocUpdate,
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDoc:
    doc = await _get_knowledge_or_404(db, project_id, doc_id)
    if body.title is not None:
        doc.title = body.title.strip()
    if body.doc_type is not None:
        doc.doc_type = body.doc_type
    if body.content is not None:
        doc.content = body.content
        if doc.source_kind != "file":
            doc.size_bytes = len(body.content.encode("utf-8"))
    if body.external_url is not None:
        doc.external_url = body.external_url
    if body.tags is not None:
        doc.tags = [t.strip() for t in body.tags if t.strip()]
    if body.related_endpoints is not None:
        doc.related_endpoints = [e.strip() for e in body.related_endpoints if e.strip()]
    doc.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(doc)
    return doc


@router.get("/projects/{project_id}/knowledge/{doc_id}/download")
async def download_knowledge_doc(
    project_id: str,
    doc_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    doc = await _get_knowledge_or_404(db, project_id, doc_id)
    if doc.source_kind == "file" and doc.file_path:
        kb_root = (settings.data_dir.resolve() / "projects" / project_id / "knowledge").resolve()
        target = Path(doc.file_path).resolve()
        if kb_root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="Uploaded knowledge file not found")
        return FileResponse(path=target, filename=doc.original_filename or target.name)

    md_lines = [f"# {doc.title}", ""]
    if doc.external_url:
        md_lines.extend([f"URL: {doc.external_url}", ""])
    if doc.tags:
        md_lines.append(f"Tags: {', '.join(doc.tags)}")
    if doc.related_endpoints:
        md_lines.append(f"Related Endpoints: {', '.join(doc.related_endpoints)}")
    if doc.tags or doc.related_endpoints:
        md_lines.append("")
    md_lines.append(doc.content or "")
    md_text = "\n".join(md_lines)
    fname = f"{slugify(doc.title)}.md"
    return Response(
        content=md_text.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.delete("/projects/{project_id}/knowledge/{doc_id}")
async def delete_knowledge_doc(
    project_id: str,
    doc_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    doc = await _get_knowledge_or_404(db, project_id, doc_id)
    if doc.file_path:
        p = Path(doc.file_path)
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    await db.delete(doc)
    await db.commit()
    return {"deleted": True, "id": doc_id}


# --- Project Master API Inventory & Safe Inline Replay ---


async def _compute_project_inventory(
    db: AsyncSession,
    project_id: str,
) -> tuple[Project, dict[str, Any], dict[str, Any]]:
    from sqlalchemy.orm import selectinload

    from reconqa.server.routers.scenarios import build_project_scenario_meta

    project = await _get_project_or_404(db, project_id)
    runs_res = await db.execute(
        select(Run)
        .options(selectinload(Run.jobs))
        .where(Run.project_id == project_id, Run.status == "completed")
        .order_by(Run.started_at.asc(), Run.finished_at.asc())
    )
    completed_runs = list(runs_res.scalars().all())
    explores = await find_project_explores(db, project_id)
    inventory, openapi_doc = build_project_inventory(
        project_id=project.id,
        project_name=project.name,
        project_slug=project.slug,
        runs=completed_runs,
        explore_results=explores,
    )
    inventory["scenario_meta"] = await build_project_scenario_meta(
        db,
        project.id,
        inventory=inventory,
        completed_runs=completed_runs,
    )
    return project, inventory, openapi_doc


@router.get("/projects/{project_id}/inventory", response_model=ProjectInventoryResponse)
async def get_project_inventory(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return cumulative Master API Inventory across Explore sessions and Completed Runs."""
    _, inventory, _ = await _compute_project_inventory(db, project_id)
    return inventory


@router.get("/projects/{project_id}/inventory/openapi.json")
async def download_project_inventory_openapi(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Download merged Project Master API Inventory as an OpenAPI 3.0.3 JSON file."""
    project, _, openapi_doc = await _compute_project_inventory(db, project_id)
    fname = f"{slugify(project.slug or project.name)}-inventory-openapi.json"
    return Response(
        content=json.dumps(openapi_doc, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _is_blocked_metadata_host(host: str) -> bool:
    h = host.strip().lower().rstrip(".")
    if h in _BLOCKED_METADATA_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(h)
        if isinstance(ip, ipaddress.IPv4Address) and ip in _LINK_LOCAL_NET:
            return True
    except ValueError:
        pass
    return False


def _should_bypass_env_proxy(host: str) -> bool:
    h = host.strip().lower().rstrip(".")
    if h in {"localhost", "127.0.0.1", "::1"} or h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return True
        if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
            return True
    except ValueError:
        pass
    return False


def _resolve_header_value(raw_val: str) -> str:
    val = str(raw_val).strip()
    m = _ENV_REF_RE.match(val)
    if m:
        return os.environ.get(m.group(1), val)
    if val.lower().startswith("env:"):
        env_key = val[4:].strip()
        return os.environ.get(env_key, val)
    return val


def _load_credential_headers(profile: AuthProfile, target_host: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    cookies: list[str] = []

    if profile.storage_state_path:
        state_path = Path(profile.storage_state_path)
        if state_path.is_file():
            try:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                for ck in state_data.get("cookies") or []:
                    if not isinstance(ck, dict):
                        continue
                    c_name = str(ck.get("name") or "").strip()
                    c_val = str(ck.get("value") or "")
                    c_dom = str(ck.get("domain") or "").lstrip(".").lower()
                    if not c_name:
                        continue
                    if not c_dom or target_host == c_dom or target_host.endswith("." + c_dom):
                        cookies.append(f"{c_name}={c_val}")
            except Exception:
                pass

    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    for k, v in (profile.extra_headers_ref or {}).items():
        h_name = str(k).strip()
        if h_name:
            headers[h_name] = _resolve_header_value(str(v))

    return headers


_DENIED_BIZ_CODES = {401, 403, 10001, 40001, 40100, 40300}
_DENIED_MSG_KEYWORDS = ("无权限", "未登录", "unauthorized", "forbidden", "token", "登录过期", "失效")


def _validate_replay_target(method_raw: str, url_raw: str) -> tuple[str, str, str]:
    method = (method_raw or "GET").strip().upper()
    if method not in _ALLOWED_REPLAY_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported HTTP method '{method_raw}'. Allowed: {sorted(_ALLOWED_REPLAY_METHODS)}",
        )

    raw_url = (url_raw or "").strip()
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(
            status_code=400,
            detail="Replay URL must use http:// or https:// with a valid host.",
        )

    target_host = parsed.hostname.lower()
    if _is_blocked_metadata_host(target_host):
        raise HTTPException(
            status_code=400,
            detail="Requests to cloud metadata or link-local addresses are blocked.",
        )
    return method, raw_url, target_host


async def _execute_single_replay(
    *,
    method: str,
    raw_url: str,
    target_host: str,
    user_headers: dict[str, str],
    query_params: dict[str, str],
    body: Any,
    profile: AuthProfile | None,
    timeout_sec: float,
) -> tuple[ReplayResponse, int]:
    merged_headers: dict[str, str] = {}
    if profile is not None:
        merged_headers.update(_load_credential_headers(profile, target_host))

    lower_to_key = {k.lower(): k for k in merged_headers}
    for k, v in (user_headers or {}).items():
        clean_k = str(k).strip()
        if not clean_k or clean_k.startswith(":"):
            continue
        existing_k = lower_to_key.get(clean_k.lower())
        if existing_k and existing_k != clean_k:
            merged_headers.pop(existing_k, None)
        merged_headers[clean_k] = str(v)
        lower_to_key[clean_k.lower()] = clean_k

    req_kwargs: dict[str, Any] = {
        "headers": merged_headers,
        "params": query_params or None,
    }
    if body is not None and method not in {"GET", "HEAD"}:
        if isinstance(body, (dict, list)):
            req_kwargs["json"] = body
        elif isinstance(body, str):
            req_kwargs["content"] = body.encode("utf-8")
        else:
            req_kwargs["json"] = body

    trust_env = not _should_bypass_env_proxy(target_host)
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            verify=False,
            follow_redirects=False,
            timeout=timeout_sec,
            trust_env=trust_env,
        ) as client:
            resp = await client.request(method, raw_url, **req_kwargs)
    except httpx.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return (
            ReplayResponse(
                status_code=0,
                status_text="Network Error",
                duration_ms=elapsed_ms,
                response_headers={},
                body_text="",
                body_json=None,
                content_type="",
                truncated=False,
                error=str(exc),
            ),
            0,
        )

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    raw_content = resp.content or b""
    truncated = len(raw_content) > _MAX_REPLAY_BODY_BYTES
    sliced = raw_content[:_MAX_REPLAY_BODY_BYTES]
    body_text = sliced.decode("utf-8", errors="replace")
    content_type = resp.headers.get("content-type", "")

    body_json: Any = None
    if not truncated and body_text.strip():
        if "json" in content_type.lower() or body_text.lstrip().startswith(("{", "[")):
            try:
                body_json = json.loads(body_text)
            except Exception:
                body_json = None

    resp_headers = {k: v for k, v in resp.headers.items()}
    return (
        ReplayResponse(
            status_code=resp.status_code,
            status_text=resp.reason_phrase or "",
            duration_ms=elapsed_ms,
            response_headers=resp_headers,
            body_text=body_text,
            body_json=body_json,
            content_type=content_type,
            truncated=truncated,
            error=None,
        ),
        len(raw_content),
    )


def _classify_matrix_verdict(
    status_code: int,
    body_json: Any,
) -> tuple[int | str | None, str]:
    biz_code: int | str | None = None
    if isinstance(body_json, dict) and "code" in body_json:
        raw_code = body_json.get("code")
        if isinstance(raw_code, (int, str)) and not isinstance(raw_code, bool):
            biz_code = raw_code

    if status_code == 0 or status_code >= 500:
        return biz_code, "error"

    if status_code in (401, 403):
        return biz_code, "denied"

    if isinstance(biz_code, int) and biz_code in _DENIED_BIZ_CODES:
        return biz_code, "denied"
    if isinstance(biz_code, str) and biz_code.isdigit() and int(biz_code) in _DENIED_BIZ_CODES:
        return biz_code, "denied"

    if isinstance(body_json, dict):
        msg = str(body_json.get("message") or body_json.get("msg") or "").lower()
        if any(kw in msg for kw in _DENIED_MSG_KEYWORDS):
            return biz_code, "denied"

    if 200 <= status_code < 300:
        return biz_code, "allowed"
    return biz_code, "denied"


@router.post("/projects/{project_id}/replay", response_model=ReplayResponse)
async def replay_project_endpoint(
    project_id: str,
    body: ReplayRequest,
    db: AsyncSession = Depends(get_db),
) -> ReplayResponse:
    """Safely replay a single HTTP request inline with optional server-side credential injection."""
    await _get_project_or_404(db, project_id)
    method, raw_url, target_host = _validate_replay_target(body.method, body.url)
    clamped_timeout = max(1.0, min(30.0, float(body.timeout_sec or 15.0)))

    profile: AuthProfile | None = None
    if body.credential_id:
        profile = await db.get(AuthProfile, body.credential_id)
        if profile is None or profile.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail=f"Credential '{body.credential_id}' not found in project '{project_id}'",
            )

    resp, _ = await _execute_single_replay(
        method=method,
        raw_url=raw_url,
        target_host=target_host,
        user_headers=body.headers,
        query_params=body.query_params,
        body=body.body,
        profile=profile,
        timeout_sec=clamped_timeout,
    )
    return resp


@router.post("/projects/{project_id}/replay-matrix", response_model=ReplayMatrixResponse)
async def replay_project_endpoint_matrix(
    project_id: str,
    body: ReplayMatrixRequest,
    db: AsyncSession = Depends(get_db),
) -> ReplayMatrixResponse:
    """Concurrently replay an endpoint across Anonymous + multiple project role credentials."""
    await _get_project_or_404(db, project_id)
    method, raw_url, target_host = _validate_replay_target(body.method, body.url)
    clamped_timeout = max(1.0, min(30.0, float(body.timeout_sec or 15.0)))

    res = await db.execute(select(AuthProfile).where(AuthProfile.project_id == project_id))
    all_profiles = list(res.scalars().all())
    if body.credential_ids:
        by_id = {p.id: p for p in all_profiles}
        selected_profiles: list[AuthProfile] = []
        for cid in body.credential_ids:
            prof = by_id.get(cid)
            if prof is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Credential '{cid}' not found in project '{project_id}'",
                )
            selected_profiles.append(prof)
    else:
        selected_profiles = all_profiles

    targets: list[tuple[str | None, str, str, AuthProfile | None]] = []
    if body.include_anonymous:
        targets.append((None, "Anonymous", "anonymous", None))
    for prof in selected_profiles:
        targets.append((prof.id, prof.name, prof.role_tag, prof))

    async def _run_entry(
        cred_id: str | None,
        cred_name: str,
        role_tag: str,
        prof: AuthProfile | None,
    ) -> ReplayMatrixEntry:
        resp, raw_size = await _execute_single_replay(
            method=method,
            raw_url=raw_url,
            target_host=target_host,
            user_headers=body.headers,
            query_params=body.query_params,
            body=body.body,
            profile=prof,
            timeout_sec=clamped_timeout,
        )
        biz_code, verdict = _classify_matrix_verdict(resp.status_code, resp.body_json)
        return ReplayMatrixEntry(
            credential_id=cred_id,
            credential_name=cred_name,
            role_tag=role_tag,
            status_code=resp.status_code,
            biz_code=biz_code,
            duration_ms=resp.duration_ms,
            body_size=raw_size,
            body_preview=(resp.body_text or "")[:500],
            verdict=verdict,  # type: ignore[arg-type]
            error=resp.error,
        )

    entries = list(await asyncio.gather(*(_run_entry(*t) for t in targets)))
    allowed_count = sum(1 for e in entries if e.verdict == "allowed")
    denied_count = sum(1 for e in entries if e.verdict == "denied")

    anon_allowed = any(e.role_tag == "anonymous" and e.verdict == "allowed" for e in entries)
    distinct_allowed_roles = {
        e.role_tag.strip().lower() for e in entries if e.verdict == "allowed"
    }
    potential_bfla = anon_allowed or len(distinct_allowed_roles) >= 2

    return ReplayMatrixResponse(
        method=method,
        url=raw_url,
        results=entries,
        allowed_count=allowed_count,
        denied_count=denied_count,
        potential_bfla_warning=potential_bfla,
    )
