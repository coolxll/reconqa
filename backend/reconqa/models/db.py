"""Async SQLAlchemy engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from reconqa.config import settings
from reconqa.models.tables import Base, ScenarioModel

__all__ = [
    "Base",
    "ScenarioModel",
    "get_db",
    "get_engine",
    "get_session_factory",
    "init_db",
]

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(database_url: str | None = None) -> AsyncEngine:
    global _engine, _session_factory
    url = database_url or settings.database_url
    if _engine is None or (database_url is not None and str(_engine.url) != url):
        if url.startswith("sqlite"):
            # Ensure parent directory exists for SQLite file paths
            db_part = url.split("///", 1)[-1]
            if db_part and db_part != ":memory:":
                Path(db_part).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url, echo=False, future=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory(database_url: str | None = None) -> async_sessionmaker[AsyncSession]:
    get_engine(database_url)
    assert _session_factory is not None
    return _session_factory


def _migrate_schema(sync_conn: Any) -> None:
    inspector = inspect(sync_conn)
    if "auth_profiles" in inspector.get_table_names():
        existing_cols = {col["name"] for col in inspector.get_columns("auth_profiles")}
        if "auth_type" not in existing_cols:
            sync_conn.execute(
                text(
                    "ALTER TABLE auth_profiles ADD COLUMN auth_type VARCHAR(32) DEFAULT 'headers'"
                )
            )
        if "login_config" not in existing_cols:
            sync_conn.execute(
                text("ALTER TABLE auth_profiles ADD COLUMN login_config JSON DEFAULT '{}'")
            )


async def init_db(database_url: str | None = None) -> None:
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)


async def get_db() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
