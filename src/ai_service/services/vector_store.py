"""The vector store behind semantic search (Day 10, task 5).

The search endpoint depends on `VectorStore`, not on one database. `PgVectorStore` is pgvector
behind that interface; `QdrantStore` (qdrant_store.py) is the second implementation.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.db.models import Chunk
from ai_service.schemas.search import SearchResultItem
from ai_service.services.search_service import search_chunks


@dataclass(frozen=True)
class VectorRecord:
    """One chunk with its vector, as a store receives it."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    heading: str
    text: str
    text_hash: str
    embedding: list[float]
    embedding_model: str
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
    """Chunks with vectors, always read and deleted for ONE tenant.

    What every implementation must keep:
    - `search` returns only chunks of `tenant_id`, filtered inside the store's own query (not
      afterwards in Python), and `tenant_id` is a required argument;
    - results are ordered by `score`, highest first;
    - `score` is cosine similarity (1.0 = the same direction);
    - with a `threshold`, no result scores below it;
    - `upsert` with an id that exists replaces that chunk;
    - `delete_document` removes the chunks of that document only if it belongs to `tenant_id`.

    One difference between stores: pgvector writes belong to the caller's database transaction
    (the caller commits). A separate store applies them at once and cannot be rolled back.
    """

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        query_vector: list[float],
        limit: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResultItem]: ...

    async def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    async def delete_document(self, *, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None: ...


class PgVectorStore:
    """Chunks and vectors in the PostgreSQL `chunks` table (pgvector, HNSW index)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        query_vector: list[float],
        limit: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResultItem]:
        # `search_chunks` stays a function of its own: the measurement scripts call it with
        # pgvector-only settings (ef_search, iterative_scan) that are not part of the protocol.
        return await search_chunks(
            self._session,
            tenant_id=tenant_id,
            query_vector=query_vector,
            limit=limit,
            threshold=threshold,
        )

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        # One row at a time, by primary key: fine for one document. The corpus is loaded in
        # batches by scripts/upsert_corpus.py.
        for r in records:
            await self._session.merge(
                Chunk(
                    id=r.id,
                    tenant_id=r.tenant_id,
                    document_id=r.document_id,
                    chunk_index=r.chunk_index,
                    heading=r.heading,
                    text=r.text,
                    text_hash=r.text_hash,
                    embedding=r.embedding,
                    embedding_model=r.embedding_model,
                    metadata_json=r.metadata,
                )
            )
        await self._session.flush()

    async def delete_document(self, *, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.document_id == document_id)
        )
