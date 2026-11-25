"""Qdrant behind the `VectorStore` protocol (Day 10, task 5).

One collection holds every tenant's chunks; `tenant_id` is a payload field with a tenant index,
and every search and delete filters on it inside the Qdrant query. The payload also carries the
chunk text and metadata, so a search needs no second query. PostgreSQL stays the main copy:
this collection can be rebuilt from the `chunks` table with scripts/copy_to_qdrant.py.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ApiException

from ai_service.core.exceptions import VectorStoreError
from ai_service.db.models.chunk import EMBEDDING_DIMENSIONS
from ai_service.schemas.search import SearchResultItem
from ai_service.services.vector_store import VectorRecord

logger = logging.getLogger("ai_service.qdrant")

_BATCH_SIZE = 256

# The same graph as the pgvector index (migration dc013ced71fa).
HNSW_M = 16
HNSW_EF_CONSTRUCT = 64
# Search time, the same value as pgvector's hnsw.ef_search in search_service.
HNSW_EF = 100
# Both limits are in KB of vectors. Qdrant's defaults (10,000) are higher than my whole corpus,
# so it built no index and compared every vector. They are lowered so that this port really uses
# HNSW, like pgvector; for data this small, exact search would be a reasonable choice too.
INDEXING_THRESHOLD_KB = 100  # a segment larger than this gets an HNSW index (0 would disable it)
FULL_SCAN_THRESHOLD_KB = 10  # 10 is the smallest allowed; below it, a search scans instead


def _match(field: str, value: uuid.UUID) -> models.FieldCondition:
    return models.FieldCondition(key=field, match=models.MatchValue(value=str(value)))


@asynccontextmanager
async def _reported(operation: str) -> AsyncIterator[None]:
    """Log what Qdrant said and raise an error that is safe to show to the user."""
    try:
        yield
    except ApiException as exc:  # unreachable, timed out, or an error response
        logger.warning("Qdrant %s failed: %s", operation, exc)
        raise VectorStoreError() from exc


class QdrantStore:
    def __init__(self, client: AsyncQdrantClient, collection: str) -> None:
        self._client = client
        self._collection = collection

    async def ensure_collection(self) -> bool:
        """Create the collection and its payload indexes if missing. True if it was created.

        An existing collection keeps its points and gets the current index settings.
        """
        hnsw = models.HnswConfigDiff(
            m=HNSW_M,
            ef_construct=HNSW_EF_CONSTRUCT,
            full_scan_threshold=FULL_SCAN_THRESHOLD_KB,
        )
        optimizers = models.OptimizersConfigDiff(indexing_threshold=INDEXING_THRESHOLD_KB)
        if await self._client.collection_exists(self._collection):
            await self._client.update_collection(
                self._collection, hnsw_config=hnsw, optimizers_config=optimizers
            )
            return False
        await self._client.create_collection(
            self._collection,
            vectors_config=models.VectorParams(
                size=EMBEDDING_DIMENSIONS, distance=models.Distance.COSINE
            ),
            hnsw_config=hnsw,
            optimizers_config=optimizers,
        )
        # is_tenant tells Qdrant to keep each tenant's points together.
        await self._client.create_payload_index(
            self._collection,
            "tenant_id",
            field_schema=models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True
            ),
        )
        await self._client.create_payload_index(
            self._collection, "document_id", field_schema=models.PayloadSchemaType.KEYWORD
        )
        return True

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        query_vector: list[float],
        limit: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResultItem]:
        async with _reported("search"):
            response = await self._client.query_points(
                self._collection,
                query=query_vector,
                query_filter=models.Filter(must=[_match("tenant_id", tenant_id)]),
                limit=limit,
                score_threshold=threshold,
                search_params=models.SearchParams(hnsw_ef=HNSW_EF),
                with_payload=True,
            )
        results = []
        for point in response.points:
            payload = point.payload or {}
            results.append(
                SearchResultItem(
                    id=uuid.UUID(str(point.id)),
                    document_id=uuid.UUID(payload["document_id"]),
                    chunk_index=payload["chunk_index"],
                    heading=payload["heading"],
                    text=payload["text"],
                    # with cosine distance, Qdrant's score is already the similarity
                    score=round(point.score, 4),
                    metadata=payload["metadata"],
                )
            )
        return results

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        for start in range(0, len(records), _BATCH_SIZE):
            points = [
                models.PointStruct(
                    id=str(r.id),
                    vector=r.embedding,
                    payload={
                        "tenant_id": str(r.tenant_id),
                        "document_id": str(r.document_id),
                        "chunk_index": r.chunk_index,
                        "heading": r.heading,
                        "text": r.text,
                        "text_hash": r.text_hash,
                        "embedding_model": r.embedding_model,
                        "metadata": r.metadata,
                    },
                )
                for r in records[start : start + _BATCH_SIZE]
            ]
            # wait=True: return only when the points are applied and searchable
            async with _reported("upsert"):
                await self._client.upsert(self._collection, points=points, wait=True)

    async def delete_document(self, *, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        async with _reported("delete"):
            await self._client.delete(
                self._collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[_match("tenant_id", tenant_id), _match("document_id", document_id)]
                    )
                ),
                wait=True,
            )
