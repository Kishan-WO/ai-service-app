"""Shared fixtures: an in-memory SQLite database and an HTTP client wired straight to the app."""

import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable

# The settings need these before ai_service is imported, so tests do not depend on a .env file.
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-at-least-32-characters-long")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5433/ai_service"
)

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection: object, connection_record: object) -> None:
    if hasattr(dbapi_connection, "cursor"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

from ai_service.api.deps import get_llm_registry, get_session_factory  # noqa: E402
from ai_service.core.config import get_settings  # noqa: E402
from ai_service.db.base import Base  # noqa: E402
from ai_service.db.session import get_db_session  # noqa: E402
from ai_service.main import create_app  # noqa: E402

from .fakes import FakeRegistry  # noqa: E402

REGISTER_URL = "/api/v1/auth/register"
PASSWORD = "correct-horse-battery"


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    # StaticPool: every connection shares one in-memory database. Without it each new
    # connection would see its own empty database and the tables would "disappear".
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def llm() -> FakeRegistry:
    """The fake models the app talks to in tests. Change `llm.hosted` / `llm.local` per test."""
    return FakeRegistry()


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession], llm: FakeRegistry) -> FastAPI:
    app = create_app()

    async def override_get_db_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session
    # the chat stream saves its reply in a session of its own, which must also use the test database
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_llm_registry] = lambda: llm
    return app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


Registrar = Callable[[str], Awaitable[dict[str, str]]]


@pytest.fixture
def register(client: httpx.AsyncClient) -> Registrar:
    """register("a@example.com") -> Authorization headers for a brand-new tenant."""

    async def _register(email: str) -> dict[str, str]:
        response = await client.post(
            REGISTER_URL,
            json={"email": email, "password": PASSWORD, "tenant_name": f"Tenant of {email}"},
        )
        assert response.status_code == 201, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _register


@pytest_asyncio.fixture
async def auth_headers(register: Registrar) -> dict[str, str]:
    return await register("user@example.com")


def unique_text(label: str = "doc") -> bytes:
    """Distinct content on every call, so the duplicate check does not trigger by accident."""
    return f"{label} {uuid.uuid4()}".encode()


def make_pdf(text: str) -> bytes:
    """A minimal valid one-page PDF containing `text` (ASCII only)."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 18 Tf 72 720 Td ({safe}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


__all__ = ["get_settings", "make_pdf", "unique_text"]
