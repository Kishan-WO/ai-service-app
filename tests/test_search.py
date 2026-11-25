"""Tests for semantic search endpoint and chunk isolation (Day 10, Tasks 3, 6, 7)."""

import uuid
from collections.abc import Awaitable, Callable
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.api.deps import get_vector_store
from ai_service.db.models import Chunk, Document, Tenant, User
from ai_service.schemas.search import SearchResultItem

SEARCH_URL = "/api/v1/search"


def _fake_vector(seed: float = 1.0, dim: int = 1024) -> list[float]:
    """Generate a normalized 1024-dim vector for testing."""
    vec = [0.0] * dim
    vec[0] = seed
    return vec


@pytest.mark.asyncio
async def test_search_requires_auth(client: httpx.AsyncClient) -> None:
    """Searching without auth returns 401 Unauthorized."""
    response = await client.post(SEARCH_URL, json={"query": "hello world"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_search_validation_errors(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Searching with empty query or invalid limits returns 422."""
    # Blank query
    res = await client.post(SEARCH_URL, json={"query": "   "}, headers=auth_headers)
    assert res.status_code == 422

    # Limit too small
    res = await client.post(
        SEARCH_URL, json={"query": "test", "limit": 0}, headers=auth_headers
    )
    assert res.status_code == 422

    # Limit too large
    res = await client.post(
        SEARCH_URL, json={"query": "test", "limit": 100}, headers=auth_headers
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_search_empty_tenant(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A tenant with no documents/chunks returns an empty list."""
    with patch(
        "ai_service.api.v1.search.embed_query", return_value=_fake_vector(1.0)
    ):
        response = await client.post(
            SEARCH_URL, json={"query": "What is AI?"}, headers=auth_headers
        )
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "What is AI?"
    assert data["total"] == 0
    assert data["results"] == []


@pytest.mark.asyncio
async def test_search_matches_and_threshold(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Semantic search returns matching chunks and honors score threshold."""
    async with session_factory() as session:
        user_res = await session.execute(select(User).limit(1))
        user = user_res.scalar_one()
        tenant_id = user.tenant_id

        doc = Document(
            tenant_id=tenant_id,
            title="AI Architecture",
            source="arch.txt",
            content="Architecture notes",
            content_hash="hash123",
            metadata_json={"byte_size": 18},
        )
        session.add(doc)
        await session.flush()

        v1 = _fake_vector(1.0)
        v2 = [0.0] * 1024
        v2[1] = 1.0  # Orthogonal vector (cosine sim = 0)

        chunk1 = Chunk(
            tenant_id=tenant_id,
            document_id=doc.id,
            chunk_index=0,
            heading="Section 1",
            text="High similarity text",
            text_hash="h1",
            embedding=v1,
            embedding_model="jina-embeddings-v3",
            metadata_json={"page": 1},
        )
        chunk2 = Chunk(
            tenant_id=tenant_id,
            document_id=doc.id,
            chunk_index=1,
            heading="Section 2",
            text="Orthogonal text",
            text_hash="h2",
            embedding=v2,
            embedding_model="jina-embeddings-v3",
            metadata_json={"page": 2},
        )
        session.add_all([chunk1, chunk2])
        await session.commit()

    # Search with vector close to v1
    with patch("ai_service.api.v1.search.embed_query", return_value=v1):
        res = await client.post(
            SEARCH_URL,
            json={"query": "High similarity", "limit": 5},
            headers=auth_headers,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 2
        assert body["results"][0]["heading"] == "Section 1"
        assert body["results"][0]["score"] == 1.0

        # Search with threshold 0.5 (filters out v2)
        res_threshold = await client.post(
            SEARCH_URL,
            json={"query": "High similarity", "threshold": 0.5},
            headers=auth_headers,
        )
        assert res_threshold.status_code == 200
        body_th = res_threshold.json()
        assert body_th["total"] == 1
        assert body_th["results"][0]["heading"] == "Section 1"


@pytest.mark.asyncio
async def test_tenant_isolation(
    client: httpx.AsyncClient,
    register: Callable[[str], Awaitable[dict[str, str]]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Task 6: Tenant B must never see Tenant A's chunks, even with identical vectors."""
    headers_a = await register("alice@corp.com")
    headers_b = await register("bob@other.com")

    target_vector = _fake_vector(1.0)

    async with session_factory() as session:
        users = (await session.execute(select(User))).scalars().all()
        user_a = next(u for u in users if u.email == "alice@corp.com")

        # Tenant A doc + chunk
        doc_a = Document(
            tenant_id=user_a.tenant_id,
            title="Secret A",
            source="secret_a.txt",
            content="Top secret for tenant A",
            content_hash="hash_a",
            metadata_json={"byte_size": 20},
        )
        session.add(doc_a)
        await session.flush()

        chunk_a = Chunk(
            tenant_id=user_a.tenant_id,
            document_id=doc_a.id,
            chunk_index=0,
            heading="Secret Clause",
            text="Tenant A private financial data",
            text_hash="ch_hash_a",
            embedding=target_vector,
            embedding_model="jina-embeddings-v3",
            metadata_json={"confidential": True},
        )
        session.add(chunk_a)
        await session.commit()

    # User B queries with identical target vector
    with patch("ai_service.api.v1.search.embed_query", return_value=target_vector):
        res_b = await client.post(
            SEARCH_URL,
            json={"query": "financial data", "limit": 10},
            headers=headers_b,
        )
        assert res_b.status_code == 200
        body_b = res_b.json()
        # Tenant B MUST see 0 results
        assert body_b["total"] == 0
        assert body_b["results"] == []

        # User A queries and sees their chunk
        res_a = await client.post(
            SEARCH_URL,
            json={"query": "financial data", "limit": 10},
            headers=headers_a,
        )
        assert res_a.status_code == 200
        body_a = res_a.json()
        assert body_a["total"] == 1
        assert body_a["results"][0]["text"] == "Tenant A private financial data"


@pytest.mark.asyncio
async def test_document_cascade_deletion(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Task 7: Deleting a document must cascade-delete all of its chunks."""
    async with session_factory() as session:
        t = Tenant(name="Cascade Tenant")
        session.add(t)
        await session.flush()

        doc = Document(
            tenant_id=t.id,
            title="Parent Doc",
            source="parent.txt",
            content="Parent text",
            content_hash="parent_hash",
            metadata_json={"byte_size": 11},
        )
        session.add(doc)
        await session.flush()

        c1 = Chunk(
            tenant_id=t.id,
            document_id=doc.id,
            chunk_index=0,
            heading="C1",
            text="chunk 1",
            text_hash="h_c1",
            embedding=_fake_vector(1.0),
            embedding_model="jina-embeddings-v3",
        )
        c2 = Chunk(
            tenant_id=t.id,
            document_id=doc.id,
            chunk_index=1,
            heading="C2",
            text="chunk 2",
            text_hash="h_c2",
            embedding=_fake_vector(2.0),
            embedding_model="jina-embeddings-v3",
        )
        session.add_all([c1, c2])
        await session.commit()

        # Verify chunks exist
        res = (
            await session.execute(
                select(Chunk).where(Chunk.document_id == doc.id)
            )
        ).scalars().all()
        assert len(res) == 2

        # Delete document
        await session.delete(doc)
        await session.commit()

        # Verify chunks are cascade deleted
        res_after = (
            await session.execute(
                select(Chunk).where(Chunk.document_id == doc.id)
            )
        ).scalars().all()
        assert len(res_after) == 0


class _RecordingStore:
    """A fake VectorStore: remembers how it was called and returns one fixed result."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = SearchResultItem(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            chunk_index=0,
            heading="from the fake store",
            text="fake",
            score=0.5,
            metadata={},
        )

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        query_vector: list[float],
        limit: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResultItem]:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "query_vector": query_vector,
                "limit": limit,
                "threshold": threshold,
            }
        )
        return [self.result]


@pytest.mark.asyncio
async def test_search_goes_through_the_vector_store(
    app: FastAPI,
    client: httpx.AsyncClient,
    register: Callable[[str], Awaitable[dict[str, str]]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Task 5: the endpoint uses the VectorStore, and the tenant comes from the logged-in user."""
    headers = await register("carol@corp.com")
    async with session_factory() as session:
        user = (
            await session.execute(select(User).where(User.email == "carol@corp.com"))
        ).scalar_one()

    store = _RecordingStore()
    app.dependency_overrides[get_vector_store] = lambda: store
    with patch("ai_service.api.v1.search.embed_query", return_value=_fake_vector(1.0)):
        response = await client.post(
            SEARCH_URL,
            json={"query": "hello", "limit": 7, "threshold": 0.25, "tenant_id": str(uuid.uuid4())},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["results"][0]["heading"] == "from the fake store"
    assert store.calls == [
        {
            "tenant_id": user.tenant_id,
            "query_vector": _fake_vector(1.0),
            "limit": 7,
            "threshold": 0.25,
        }
    ]

