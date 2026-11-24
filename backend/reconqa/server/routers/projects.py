"""CRUD endpoints for projects, targets, and dual-account auth profiles."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.config import settings
from reconqa.models.db import get_db
from reconqa.models.schemas import (
    AuthProfileCreate,
    AuthProfileOut,
    ProjectCreate,
    ProjectOut,
    TargetCreate,
    TargetOut,
)
from reconqa.models.tables import AuthProfile, Project, Target
from reconqa.utils.io import ensure_dir, slugify

router = APIRouter(tags=["projects"])


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
