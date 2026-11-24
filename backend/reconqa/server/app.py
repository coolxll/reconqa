"""ReconQA FastAPI application entrypoint with lifespan and CORS."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from reconqa.config import settings
from reconqa.models.db import init_db
from reconqa.server.routers import artifacts, projects, reports, runs
from reconqa.server.worker import InProcessTaskRunner


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.ensure_dirs()
    await init_db()
    runner: InProcessTaskRunner = app.state.task_runner
    await runner.start()
    try:
        yield
    finally:
        await runner.stop()


def create_app() -> FastAPI:
    application = FastAPI(
        title="ReconQA API",
        version="0.1.0",
        description="One-recording, multi-use Web/API testing platform (HAR 1.2 + OpenAPI 3.0.3).",
        lifespan=lifespan,
    )
    application.state.task_runner = InProcessTaskRunner()

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "reconqa"}

    application.include_router(projects.router, prefix="/api/v1")
    application.include_router(runs.router, prefix="/api/v1")
    application.include_router(artifacts.router, prefix="/api/v1")
    application.include_router(reports.router, prefix="/api/v1")

    return application


app = create_app()
