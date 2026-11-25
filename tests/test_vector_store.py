"""The VectorStore contract (Day 10, task 5): every test runs against both implementations.

PgVectorStore runs on the SQLite test database (the Python fallback of the search, NOT the
pgvector SQL) and QdrantStore on qdrant-client's in-memory mode (NOT a Qdrant server). These
tests check the contract; scripts/verify_isolation.py and scripts/copy_to_qdrant.py check the
real servers.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.db.models import Document, Tenant
from ai_service.services.qdrant_store import QdrantStore
from ai_service.services.vector_store import PgVectorStore, VectorRecord, VectorStore


def _vector(x: float, y: float) -> list[float]:
    return [x, y] + [0.0] * 1022


QUERY = _vector(1.0, 0.0)
SAME, CLOSE, FAR = _vector(1.0, 0.0), _vector(0.8, 0.6), _vector(0.0, 1.0)  # scores 1.0, 0.8, 0.0


class Fixture:
    """A store with two tenants, each with its own documents in the database."""

    def __init__(self, store: VectorStore, tenants: list[uuid.UUID], docs: list[uuid.UUID]):
        self.store = store
        self.tenant_a, self.tenant_b = tenants
        self.doc_a1, self.doc_a2, self.doc_b = docs

    def record(
        self, tenant: uuid.UUID, document: uuid.UUID, index: int, vector: list[float], text: str
    ) -> VectorRecord:
        return VectorRecord(
            id=uuid.uuid5(document, str(index)),
            tenant_id=tenant,
            document_id=document,
            chunk_index=index,
            heading=f"heading {text}",
            text=text,
            text_hash=f"hash-{text}",
            embedding=vector,
            embedding_model="test-model",
            metadata={"label": text},
        )

    async def load(self) -> None:
        await self.store.upsert(
            [
                self.record(self.tenant_a, self.doc_a1, 0, CLOSE, "a-close"),
                self.record(self.tenant_a, self.doc_a1, 1, FAR, "a-far"),
                self.record(self.tenant_a, self.doc_a2, 0, SAME, "a-same"),
                # the same vector as a-same, owned by the other tenant
                self.record(self.tenant_b, self.doc_b, 0, SAME, "b-same"),
            ]
        )

    async def texts(
        self, tenant: uuid.UUID, limit: int = 10, threshold: float | None = None
    ) -> list[str]:
        found = await self.store.search(
            tenant_id=tenant, query_vector=QUERY, limit=limit, threshold=threshold
        )
        return [r.text for r in found]


@pytest.fixture(params=["pgvector", "qdrant"])
async def vs(
    request: pytest.FixtureRequest, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Fixture]:
    async with session_factory() as session:
        tenants = [Tenant(name="A"), Tenant(name="B")]
        session.add_all(tenants)
        await session.flush()
        owners = [tenants[0], tenants[0], tenants[1]]
        documents = [
            Document(
                tenant_id=owner.id,
                title="t",
                source="s",
                content="c",
                content_hash=f"h{n}",
                metadata_json={},
            )
            for n, owner in enumerate(owners)
        ]
        session.add_all(documents)
        await session.flush()

        client = AsyncQdrantClient(location=":memory:")
        store: VectorStore
        if request.param == "qdrant":
            qdrant = QdrantStore(client, "test_chunks")
            assert await qdrant.ensure_collection() is True
            assert await qdrant.ensure_collection() is False  # running it again changes nothing
            store = qdrant
        else:
            store = PgVectorStore(session)
        fixture = Fixture(store, [t.id for t in tenants], [d.id for d in documents])
        await fixture.load()
        yield fixture
        await client.close()


async def test_a_tenant_gets_only_its_own_chunks(vs: Fixture) -> None:
    assert await vs.texts(vs.tenant_a) == ["a-same", "a-close", "a-far"]
    assert await vs.texts(vs.tenant_b) == ["b-same"]
    assert await vs.texts(uuid.uuid4()) == []


async def test_scores_are_similarity_highest_first_and_limit_applies(vs: Fixture) -> None:
    found = await vs.store.search(tenant_id=vs.tenant_a, query_vector=QUERY, limit=2)
    assert [r.text for r in found] == ["a-same", "a-close"]
    assert [r.score for r in found] == pytest.approx([1.0, 0.8], abs=1e-4)
    first = found[0]
    assert (first.document_id, first.chunk_index) == (vs.doc_a2, 0)
    assert (first.heading, first.metadata) == ("heading a-same", {"label": "a-same"})


async def test_threshold_removes_lower_scores(vs: Fixture) -> None:
    assert await vs.texts(vs.tenant_a, threshold=0.5) == ["a-same", "a-close"]
    assert await vs.texts(vs.tenant_a, threshold=0.9) == ["a-same"]


async def test_upsert_with_the_same_id_replaces_the_chunk(vs: Fixture) -> None:
    await vs.store.upsert([vs.record(vs.tenant_a, vs.doc_a2, 0, SAME, "a-same, new text")])
    assert await vs.texts(vs.tenant_a) == ["a-same, new text", "a-close", "a-far"]


async def test_delete_document_removes_only_that_document(vs: Fixture) -> None:
    await vs.store.delete_document(tenant_id=vs.tenant_a, document_id=vs.doc_a1)
    assert await vs.texts(vs.tenant_a) == ["a-same"]
    assert await vs.texts(vs.tenant_b) == ["b-same"]


async def test_another_tenant_cannot_delete_a_document(vs: Fixture) -> None:
    await vs.store.delete_document(tenant_id=vs.tenant_b, document_id=vs.doc_a1)
    assert await vs.texts(vs.tenant_a) == ["a-same", "a-close", "a-far"]
