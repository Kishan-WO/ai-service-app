"""The VECTOR_STORE switch and document deletion through the chosen store (Day 10, task 5)."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.api.deps import get_vector_store
from ai_service.core.config import Settings, get_settings
from ai_service.core.exceptions import VectorStoreError
from ai_service.db.models import Document
from ai_service.services.qdrant_store import QdrantStore
from ai_service.services.vector_store import PgVectorStore

DOCUMENTS_URL = "/api/v1/documents"


def _store_for(backend: str | None) -> object:
    settings = get_settings()
    if backend is not None:
        settings = settings.model_copy(update={"VECTOR_STORE": backend})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(qdrant=object())))
    return get_vector_store(request, object(), settings)  # type: ignore[arg-type]


def test_pgvector_is_the_default_store() -> None:
    assert Settings.model_fields["VECTOR_STORE"].default == "pgvector"


def test_the_setting_chooses_the_store() -> None:
    assert isinstance(_store_for("pgvector"), PgVectorStore)
    assert isinstance(_store_for("qdrant"), QdrantStore)


def test_an_unknown_store_name_is_refused() -> None:
    with pytest.raises(ValueError, match="VECTOR_STORE"):
        Settings(VECTOR_STORE="chroma")


class _DeletingStore:
    """Records deletes, notes whether the document row still existed, and can fail."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], fail: bool = False) -> None:
        self._sessions = sessions
        self.fail = fail
        self.deleted: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.document_existed: list[bool] = []

    async def delete_document(self, *, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        async with self._sessions() as session:
            row = await session.scalar(select(Document.id).where(Document.id == document_id))
        self.document_existed.append(row is not None)
        if self.fail:
            raise VectorStoreError()
        self.deleted.append((tenant_id, document_id))


async def _upload(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    response = await client.post(
        f"{DOCUMENTS_URL}/upload",
        headers=headers,
        files={"file": ("a.txt", b"some text", "text/plain")},
    )
    assert response.status_code == 201
    body: dict[str, str] = response.json()
    return body


async def test_delete_cleans_the_vector_store_before_the_database(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document_id = (await _upload(client, auth_headers))["id"]
    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    store = _DeletingStore(session_factory)
    app.dependency_overrides[get_vector_store] = lambda: store

    response = await client.delete(f"{DOCUMENTS_URL}/{document_id}", headers=auth_headers)

    assert response.status_code == 204
    assert store.deleted == [(uuid.UUID(me["tenant"]["id"]), uuid.UUID(document_id))]
    assert store.document_existed == [True]


async def test_if_the_store_fails_nothing_is_deleted(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    url = f"{DOCUMENTS_URL}/{(await _upload(client, auth_headers))['id']}"
    store = _DeletingStore(session_factory, fail=True)
    app.dependency_overrides[get_vector_store] = lambda: store

    response = await client.delete(url, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "VECTOR_STORE_UNAVAILABLE"
    assert (await client.get(url, headers=auth_headers)).status_code == 200  # still there


async def test_another_tenants_delete_never_reaches_the_store(
    app: FastAPI,
    client: httpx.AsyncClient,
    register: Callable[[str], Awaitable[dict[str, str]]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, other = await register("owner@corp.com"), await register("other@corp.com")
    url = f"{DOCUMENTS_URL}/{(await _upload(client, owner))['id']}"
    store = _DeletingStore(session_factory)
    app.dependency_overrides[get_vector_store] = lambda: store

    assert (await client.delete(url, headers=other)).status_code == 404
    assert store.document_existed == []


@pytest.fixture
async def unreachable_store() -> AsyncIterator[QdrantStore]:
    client = AsyncQdrantClient(url="http://127.0.0.1:1", timeout=2, check_compatibility=False)
    yield QdrantStore(client, "chunks")
    await client.close()


async def test_an_unreachable_qdrant_is_reported_as_unavailable(
    unreachable_store: QdrantStore,
) -> None:
    with pytest.raises(VectorStoreError):
        await unreachable_store.search(tenant_id=uuid.uuid4(), query_vector=[1.0] * 1024)
    with pytest.raises(VectorStoreError):
        await unreachable_store.delete_document(tenant_id=uuid.uuid4(), document_id=uuid.uuid4())
