"""Day 10: pgvector against Qdrant, both searching through HNSW, with the same query vectors.

Both are called through `VectorStore.search` with the app's settings (ef 100 on both). PostgreSQL
is forced to use its HNSW index, because for a limit of 50 its planner would choose an exact scan,
and that would not compare the two indexes. For limit 5 and 50:
    - how many of the exact top results each store returns
    - the time of one search seen from Python (after one warm-up round)
Each store's own counters show that HNSW really ran. Nothing is written to either store.

Run (needs Postgres and Qdrant with the same chunks, and the saved query vectors):
    uv run python scripts/compare_stores.py
"""

import asyncio
import json
import statistics
import sys
import time
import urllib.request
from collections import Counter
from collections.abc import Awaitable, Callable

from compare_hnsw import MODES, scan_counters
from eval_search import CORPUS_TENANT_ID, QUERIES_FILE, SCRIPTS, load_query_vectors
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ai_service.core.config import get_settings
from ai_service.schemas.search import SearchResultItem
from ai_service.services.qdrant_store import QdrantStore
from ai_service.services.vector_store import PgVectorStore, VectorStore

RESULTS_FILE = SCRIPTS / "results" / "day10_store_comparison.json"
LIMITS = [5, 50]
TIMED_ROUNDS = 10

Search = Callable[[list[float], int], Awaitable[list[SearchResultItem]]]


def qdrant_counters(collection: str) -> Counter[str]:
    """Qdrant's own count, over all segments, of how searches were answered."""
    url = f"{get_settings().QDRANT_URL}/telemetry?details_level=10"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)["result"]
    total: Counter[str] = Counter()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for entry in node.get("vector_index_searches", []):
                for key, value in entry.items():
                    if isinstance(value, dict) and value.get("count"):
                        total[key] += value["count"]
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk([c for c in data["collections"]["collections"] if c.get("id") == collection])
    return total


async def measure(search: Search, vectors: list[list[float]], limit: int) -> tuple[list, list]:
    """(ids per query from the warm-up round, time in ms of every timed search)"""
    ids = [[str(r.id) for r in await search(v, limit)] for v in vectors]
    times = []
    for _ in range(TIMED_ROUNDS):
        for vector in vectors:
            started = time.perf_counter()
            await search(vector, limit)
            times.append((time.perf_counter() - started) * 1000)
    return ids, times


async def main() -> None:
    settings = get_settings()
    queries = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    saved = await load_query_vectors(queries)
    vectors = [saved[q["query"]] for q in queries]
    engine = create_async_engine(str(settings.DATABASE_URL))
    client = AsyncQdrantClient(url=settings.QDRANT_URL)
    collection = settings.QDRANT_COLLECTION

    async with engine.connect() as connection:
        in_postgres = (await connection.execute(text("select count(*) from chunks"))).scalar_one()
    in_qdrant = (await client.count(collection, exact=True)).count
    print(f"PostgreSQL chunks: {in_postgres} | Qdrant points: {in_qdrant}")
    if in_postgres != in_qdrant:
        sys.exit("the two stores differ: run scripts/copy_to_qdrant.py first")

    report = []
    for limit in LIMITS:
        async with AsyncSession(engine) as session:  # the truth: PostgreSQL, no index
            await session.execute(text("set local enable_indexscan = off"))
            exact = [
                [
                    str(r.id)
                    for r in await PgVectorStore(session).search(
                        tenant_id=CORPUS_TENANT_ID, query_vector=v, limit=limit
                    )
                ]
                for v in vectors
            ]
            await session.rollback()

        for name in ("pgvector", "qdrant"):
            await asyncio.sleep(2)
            pg_before, qdrant_before = await scan_counters(), qdrant_counters(collection)
            async with AsyncSession(engine) as session:
                store: VectorStore
                if name == "pgvector":
                    for setting in MODES["hnsw"]:
                        await session.execute(text(setting))
                    store = PgVectorStore(session)
                else:
                    store = QdrantStore(client, collection)

                async def search(
                    vector: list[float], k: int, store: VectorStore = store
                ) -> list[SearchResultItem]:
                    return await store.search(
                        tenant_id=CORPUS_TENANT_ID, query_vector=vector, limit=k
                    )

                ids, times = await measure(search, vectors, limit)
                await session.rollback()
            await asyncio.sleep(2)
            pg_after, qdrant_after = await scan_counters(), qdrant_counters(collection)
            used = (
                {k: pg_after[k] - pg_before[k] for k in pg_after if pg_after[k] != pg_before[k]}
                if name == "pgvector"
                else dict(qdrant_after - qdrant_before)
            )
            missed = {
                query["id"]: len(set(truth) - set(got))
                for query, truth, got in zip(queries, exact, ids, strict=True)
                if set(truth) - set(got)
            }
            total = sum(map(len, exact))
            ordered = sorted(times)
            row = {
                "store": name,
                "limit": limit,
                "exact_results_returned": total - sum(missed.values()),
                "exact_results": total,
                "missed_by_query": missed,
                "median_ms": round(statistics.median(times), 2),
                "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1], 2),
                "timed_searches": len(times),
                "searches_answered_by": used,
            }
            report.append(row)
            print(
                f"limit {limit:>2}  {name:<8}  exact results {row['exact_results_returned']:>4}/"
                f"{total:<4}  median {row['median_ms']:>5} ms  p95 {row['p95_ms']:>5} ms  "
                f"missed {missed or '-'}\n{'':19}counters: {used}"
            )

    RESULTS_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"saved: scripts/results/{RESULTS_FILE.name}")
    await client.close()
    await engine.dispose()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
