"""CRUD endpoints for projects, targets, auth profiles, and project knowledge base."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

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
    ProjectOut,
    TargetCreate,
    TargetOut,
)
from reconqa.models.tables import AuthProfile, KnowledgeDoc, Project, Target
from reconqa.utils.io import ensure_dir, slugify

router = APIRouter(tags=["projects"])

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
