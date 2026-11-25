"""Day 10, task 5: copy the `chunks` table into Qdrant, check the copy, and compare the search.

The vectors are the ones already in PostgreSQL, so there is no embedding API call. PostgreSQL is
the main copy: points that no longer exist there are removed from Qdrant, so running the script
again makes Qdrant a mirror of the table.

Run (needs the local Postgres and Qdrant, and the files saved by eval_search.py):
    uv run python scripts/copy_to_qdrant.py
"""

import asyncio
import json
import math
import sys
import time
from collections import Counter
from importlib.metadata import version

from eval_search import (
    CORPUS_TENANT_ID,
    QUERIES_FILE,
    RESULTS_FILE,
    SCRIPTS,
    TOP_K,
    load_query_vectors,
)
from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ai_service.core.config import get_settings
from ai_service.db.models import Chunk, Document
from ai_service.services.qdrant_store import QdrantStore
from ai_service.services.vector_store import VectorRecord

COMPARISON_FILE = SCRIPTS / "results" / "day10_qdrant_comparison.json"


async def read_chunks() -> tuple[list[VectorRecord], dict]:
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    async with AsyncSession(engine) as session:
        chunks = (await session.execute(select(Chunk).order_by(Chunk.id))).scalars().all()
        records = [
            VectorRecord(
                id=c.id,
                tenant_id=c.tenant_id,
                document_id=c.document_id,
                chunk_index=c.chunk_index,
                heading=c.heading,
                text=c.text,
                text_hash=c.text_hash,
                embedding=[float(x) for x in c.embedding],
                embedding_model=c.embedding_model,
                metadata=c.metadata_json or {},
            )
            for c in chunks
        ]
        rows = await session.execute(select(Document.id, Document.source))
        source_of = {document_id: source for document_id, source in rows}
    await engine.dispose()
    return records, source_of


async def all_points(client: AsyncQdrantClient, collection: str) -> dict:
    """Every point of the collection: id -> (payload, vector)."""
    points: dict = {}
    offset = None
    while True:
        batch, offset = await client.scroll(
            collection, limit=500, offset=offset, with_payload=True, with_vectors=True
        )
        points.update({str(p.id): (p.payload, p.vector) for p in batch})
        if offset is None:
            return points


async def wait_until_indexed(client: AsyncQdrantClient, collection: str) -> None:
    """Qdrant builds the index in the background. Wait until it is idle, so that the search
    below is not measured halfway through. "green" must hold for 5 checks in a row, because
    optimisation can start a moment after a write."""
    green = 0
    for _ in range(240):
        info = await client.get_collection(collection)
        green = green + 1 if info.status == models.CollectionStatus.GREEN else 0
        if green == 5:
            return
        await asyncio.sleep(0.5)
    sys.exit("Qdrant did not finish indexing in 2 minutes")


async def main() -> None:
    settings = get_settings()
    collection = settings.QDRANT_COLLECTION
    records, source_of = await read_chunks()
    print(f"PostgreSQL: {len(records)} chunks")

    client = AsyncQdrantClient(url=settings.QDRANT_URL)
    store = QdrantStore(client, collection)
    created = await store.ensure_collection()
    print(f"collection '{collection}': {'created' if created else 'already there'}")

    started = time.perf_counter()
    await store.upsert(records)
    print(f"upsert of {len(records)} points: {time.perf_counter() - started:.1f} s")

    points = await all_points(client, collection)
    wanted = {str(r.id): r for r in records}
    extra = sorted(set(points) - set(wanted))
    if extra:
        await client.delete(collection, points_selector=extra, wait=True)
        for point_id in extra:
            points.pop(point_id)
    print(f"removed from Qdrant (not in PostgreSQL): {len(extra)}")

    # ---- is it a faithful copy? ----
    missing = set(wanted) - set(points)
    wrong_payload = 0
    largest_difference = 0.0
    for point_id, record in wanted.items():
        if point_id in missing:
            continue
        payload, vector = points[point_id]
        expected = (
            str(record.tenant_id),
            str(record.document_id),
            record.text,
            record.heading,
            record.metadata,
        )
        got = tuple(payload[k] for k in ("tenant_id", "document_id", "text", "heading", "metadata"))
        wrong_payload += got != expected
        # Qdrant stores a cosine vector scaled to length 1, so compare with the same scaling.
        length = math.sqrt(sum(x * x for x in record.embedding))
        largest_difference = max(
            largest_difference,
            max(abs(a / length - b) for a, b in zip(record.embedding, vector, strict=True)),
        )
    per_tenant_pg = Counter(str(r.tenant_id) for r in records)
    per_tenant_qdrant = Counter(payload["tenant_id"] for payload, _ in points.values())
    print(f"points in Qdrant: {len(points)} | missing: {len(missing)}")
    print(f"points whose tenant, document, text, heading or metadata differ: {wrong_payload}")
    print(f"largest difference in any vector number: {largest_difference:.1e}")
    print(f"points per tenant equal to PostgreSQL: {per_tenant_pg == per_tenant_qdrant}")

    await wait_until_indexed(client, collection)
    info = await client.get_collection(collection)
    print(
        f"collection: status {info.status.value}, {info.points_count} points, "
        f"{info.indexed_vectors_count} vectors in an HNSW index, "
        f"indexing_threshold {info.config.optimizer_config.indexing_threshold} KB, "
        f"payload indexes {sorted(info.payload_schema)}"
    )

    # ---- the same 20 queries, against the exact search of PostgreSQL ----
    queries = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    vectors = await load_query_vectors(queries)
    baseline = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))["queries"]
    hits = shared = same_order = 0
    largest_score_gap = 0.0
    different = []
    for query, exact in zip(queries, baseline, strict=True):
        found = await store.search(
            tenant_id=CORPUS_TENANT_ID, query_vector=vectors[query["query"]], limit=TOP_K
        )
        ids = [str(r.id) for r in found]
        exact_ids = [item["chunk_id"] for item in exact["top5"]]
        hits += any(source_of[r.document_id] in query["correct_docs"] for r in found)
        shared += len(set(ids) & set(exact_ids))
        same_order += ids == exact_ids
        if ids != exact_ids:
            different.append(query["id"])
        exact_score = {item["chunk_id"]: item["score"] for item in exact["top5"]}
        for r in found:
            if str(r.id) in exact_score:
                largest_score_gap = max(largest_score_gap, abs(r.score - exact_score[str(r.id)]))
    total = sum(len(q["top5"]) for q in baseline)
    print(f"\nQdrant search, {len(queries)} queries, top {TOP_K}")
    print(f"  recall@{TOP_K}                       : {hits}/{len(queries)}")
    print(f"  chunks shared with exact       : {shared}/{total}")
    print(f"  same 5 in the same order       : {same_order}/{len(queries)}")
    print(f"  queries that differ            : {different or 'none'}")
    print(f"  largest score gap, same chunk  : {largest_score_gap:.4f}")

    COMPARISON_FILE.write_text(
        json.dumps(
            {
                "qdrant_server": (await client.info()).version,
                "qdrant_client": version("qdrant-client"),
                "points": len(points),
                "indexed_vectors": info.indexed_vectors_count,
                "recall_at_5": hits / len(queries),
                "chunks_shared_with_exact": shared,
                "chunks_in_exact": total,
                "queries_identical_to_exact": same_order,
                "different_queries": different,
                "largest_score_gap": largest_score_gap,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    await client.close()
    faithful = not missing and not wrong_payload and per_tenant_pg == per_tenant_qdrant
    sys.exit(0 if faithful else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
