"""Async database engine and session management."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ai_service.core.config import get_settings
from ai_service.db.base import Base

settings = get_settings()

# Engine configuration
engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    # Never write SQL values to logs or error messages: they are document texts, whole files
    # and password hashes. With DEBUG on, the statements are still shown.
    hide_parameters=True,
    pool_pre_ping=True,
)

# Session factory with expire_on_commit=False for async FastAPI compatibility
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency that provides an isolated async database session per request."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables(bind_engine: AsyncEngine | None = None) -> None:
    """Create all database tables (useful for test environments)."""
    target_engine = bind_engine or engine
    async with target_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables(bind_engine: AsyncEngine | None = None) -> None:
    """Drop all database tables (useful for test cleanup)."""
    target_engine = bind_engine or engine
    async with target_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

