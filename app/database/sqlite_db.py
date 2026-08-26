from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


class Base(DeclarativeBase):
    pass


def _db_url() -> str:
    db_path = Path(settings.sqlite_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{db_path}"


engine = create_async_engine(_db_url(), echo=False)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


async def init_db() -> None:
    """Create all tables defined on Base, and make the DB safe for two writers.

    T1: with a second front-end there are two PROCESSES appending turns to this
    file, and sqlite's default rollback journal locks the whole database for the
    duration of a write — so the turn that records the answer fails with
    "database is locked" while the other front-end is mid-write. WAL lets a
    writer and readers proceed together, and `busy_timeout` makes the remaining
    writer-writer overlap wait instead of raising.

    Best-effort: a filesystem that cannot do WAL (some network shares) keeps the
    old journal mode rather than failing startup.
    """
    async with engine.begin() as conn:
        try:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA busy_timeout=5000")
        except Exception:  # pragma: no cover - depends on the filesystem
            pass
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a session: `async with get_session() as s:`."""
    async with AsyncSessionLocal() as session:
        yield session


async def health_check() -> bool:
    """Return True if the DB is reachable."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
