"""Semantic vector search service over document chunks (Day 10, Task 3)."""

from __future__ import annotations

import logging
import math
import uuid

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.core.config import Settings
from ai_service.core.exceptions import AppError
from ai_service.db.models import Chunk
from ai_service.schemas.search import SearchResultItem

logger = logging.getLogger("ai_service.search")

# How pgvector walks the HNSW index; measured in notes/day-10 (Steps 6, 10 and 11).
# ef_search: candidates kept during the walk. pgvector's default is 40, and a plain HNSW scan
#     cannot return more rows than this, so it must be at least the largest `limit` (50).
# iterative_scan: the tenant filter is applied after the index returns candidates. With this on,
#     pgvector keeps scanning until enough rows pass the filter (up to hnsw.max_scan_tuples).
HNSW_EF_SEARCH = 100
HNSW_ITERATIVE_SCAN = "relaxed_order"


class EmbeddingServiceError(AppError):
    status_code = 502
    code = "EMBEDDING_SERVICE_ERROR"
    message = "The embedding service failed. Please try again."


async def embed_query(
    query: str,
    http_client: httpx.AsyncClient,
    settings: Settings,
) -> list[float]:
    """Embed search query with Jina embeddings API (task: retrieval.query)."""
    if not settings.JINA_API_KEY:
        raise EmbeddingServiceError("Embedding API key is not configured.")

    payload = {
        "model": settings.JINA_EMBEDDING_MODEL,
        "task": "retrieval.query",
        "dimensions": 1024,
        "late_chunking": False,
        "truncate": False,
        "input": [query],
    }
    headers = {
        "Authorization": f"Bearer {settings.JINA_API_KEY.get_secret_value()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        resp = await http_client.post(
            settings.JINA_EMBEDDING_URL,
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        vector: list[float] = data["data"][0]["embedding"]
        return vector
    except Exception as exc:
        logger.warning("Failed to embed query via Jina: %s", exc)
        raise EmbeddingServiceError("Could not generate query embedding.") from exc


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Cosine similarity helper for in-memory / SQLite test fallback."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2, strict=False))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


async def search_chunks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query_vector: list[float],
    limit: int = 5,
    threshold: float | None = None,
    ef_search: int = HNSW_EF_SEARCH,
    iterative_scan: str = HNSW_ITERATIVE_SCAN,
) -> list[SearchResultItem]:
    """Search chunks strictly isolated to tenant_id using cosine distance.

    Uses PostgreSQL pgvector cosine distance (<=>) in production.
    Falls back to Python-computed cosine similarity when running on SQLite in tests.
    """
    dialect_name = ""
    if session.bind is not None and hasattr(session.bind, "dialect"):
        dialect_name = session.bind.dialect.name
    elif session.sync_session.bind is not None and hasattr(session.sync_session.bind, "dialect"):
        dialect_name = session.sync_session.bind.dialect.name

    if dialect_name == "sqlite":
        # SQLite test fallback
        stmt = select(Chunk).where(Chunk.tenant_id == tenant_id)
        raw_chunks = (await session.execute(stmt)).scalars().all()
        scored: list[tuple[Chunk, float]] = []
        for ch in raw_chunks:
            vec = ch.embedding if isinstance(ch.embedding, list) else []
            s = _cosine_similarity(vec, query_vector)
            if threshold is None or s >= threshold:
                scored.append((ch, s))
        # Sort by similarity descending (highest score first)
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            SearchResultItem(
                id=chunk.id,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                heading=chunk.heading,
                text=chunk.text,
                score=round(score, 4),
                metadata=chunk.metadata_json or {},
            )
            for chunk, score in scored[:limit]
        ]

    # PostgreSQL + pgvector
    # Distance: embedding <=> query_vector. Cosine similarity score = 1 - distance
    distance_expr = Chunk.embedding.cosine_distance(query_vector)
    score_expr = (1.0 - distance_expr).label("score")

    stmt = (
        select(Chunk, score_expr)
        .where(Chunk.tenant_id == tenant_id)
        .order_by(distance_expr)
        .limit(limit)
    )

    if threshold is not None:
        stmt = stmt.where(score_expr >= threshold)

    # `true` = only for this transaction, so a pooled connection does not keep the values.
    await session.execute(
        text(
            "select set_config('hnsw.ef_search', :ef_search, true), "
            "set_config('hnsw.iterative_scan', :iterative_scan, true)"
        ),
        {"ef_search": str(ef_search), "iterative_scan": iterative_scan},
    )
    raw_results = (await session.execute(stmt)).all()

    results: list[SearchResultItem] = []
    for r in raw_results:
        chunk: Chunk = r._mapping[Chunk]
        score_val: float = float(r._mapping["score"])
        results.append(
            SearchResultItem(
                id=chunk.id,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                heading=chunk.heading,
                text=chunk.text,
                score=round(score_val, 4),
                metadata=chunk.metadata_json or {},
            )
        )
    # relaxed_order may return rows slightly out of distance order, so sort them here.
    results.sort(key=lambda item: item.score, reverse=True)
    return results
