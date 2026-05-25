from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from configs.settings import get_settings
from core.memory.models import Base


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            echo=False,
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Run Alembic migrations to bring the schema to head. Safe to run
    repeatedly (Alembic tracks applied revisions in the ``alembic_version``
    table).

    Production-grade replacement for ``Base.metadata.create_all`` — schema
    evolution now goes through versioned migrations under ``alembic/versions/``.
    """
    import asyncio
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parent.parent.parent
    cfg_path = repo_root / "alembic.ini"
    if not cfg_path.is_file():
        # Fallback to create_all only when running outside the packaged repo
        # (e.g., a developer building objects in-memory). Production paths
        # always have alembic.ini present.
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return

    # Alembic's command.upgrade is sync. Run it in a thread so we don't
    # block the event loop and so it can manage its own DB connections
    # without competing with our async pool.
    cfg = Config(str(cfg_path))
    await asyncio.to_thread(command.upgrade, cfg, "head")


async def ping() -> bool:
    from sqlalchemy import text
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
