"""Day 10, task 4, step 2: HNSW against exact search on the same 20 queries.

Three modes, all through the app's `search_chunks`:
    exact     index scans off, so every chunk is compared (the baseline of step 1)
    planner   no settings changed: what PostgreSQL does for the API today
    hnsw      the other ways to run the query are switched off, so the HNSW index must be used
              (hnsw.ef_search stays at its default, 40)

For each mode: recall@5, how many of the exact top 5 chunks come back, and the time of one search.
PostgreSQL's usage counters show which index each mode really used.

Run after eval_search.py (it needs the saved query vectors and exact results):
    uv run python scripts/compare_hnsw.py
"""

import asyncio
import json
import statistics
import sys
import time

from eval_search import (
    CORPUS_TENANT_ID,
    QUERIES_FILE,
    RESULTS_FILE,
    SCRIPTS,
    TOP_K,
    load_query_vectors,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ai_service.core.config import get_settings
from ai_service.db.models import Document
from ai_service.services.search_service import search_chunks

COMPARISON_FILE = SCRIPTS / "results" / "day10_hnsw_comparison.json"
TIMED_ROUNDS = 10

MODES = {
    "exact": ["set local enable_indexscan = off"],
    "planner": [],
    "hnsw": [
        "set local enable_seqscan = off",
        "set local enable_bitmapscan = off",
        "set local enable_sort = off",
    ],
}


async def scan_counters() -> dict[str, int]:
    """How often each index of `chunks` (and a full table scan) has been used so far."""
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "select indexrelname, idx_scan from pg_stat_user_indexes where relname = 'chunks' "
                "union all "
                "select 'full table scan', seq_scan from pg_stat_user_tables "
                "where relname = 'chunks'"
            )
        )
        counters = {name: count for name, count in rows}
    await engine.dispose()
    return counters


async def run_mode(
    name: str, queries: list[dict], vectors: dict[str, list[float]], ef_search: int = 40
) -> tuple[list[list[str]], list[list[str]], list[float], int]:
    """Return (chunk ids, documents) per query, the search times in ms, and unstable queries."""
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    ids: list[list[str]] = []
    documents: list[list[str]] = []
    times: list[float] = []
    unstable: set[int] = set()
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            select(Document.id, Document.source).where(Document.tenant_id == CORPUS_TENANT_ID)
        )
        source_of = {document_id: source for document_id, source in rows}
        for setting in MODES[name]:
            await session.execute(text(setting))

        async def search(query: dict) -> list:
            return await search_chunks(
                session,
                tenant_id=CORPUS_TENANT_ID,
                query_vector=vectors[query["query"]],
                limit=TOP_K,
                ef_search=ef_search,
                iterative_scan="off",
            )

        for query in queries:  # first round: the results (and a warm-up, not timed)
            found = await search(query)
            ids.append([str(r.id) for r in found])
            documents.append([source_of[r.document_id] for r in found])
        for _ in range(TIMED_ROUNDS):
            for number, query in enumerate(queries):
                started = time.perf_counter()
                found = await search(query)
                times.append((time.perf_counter() - started) * 1000)
                if [str(r.id) for r in found] != ids[number]:
                    unstable.add(number)
        await session.rollback()
    await engine.dispose()
    return ids, documents, times, len(unstable)


async def main() -> None:
    queries = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    vectors = await load_query_vectors(queries)
    baseline = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))["queries"]
    baseline_ids = [[item["chunk_id"] for item in q["top5"]] for q in baseline]

    report: dict[str, dict] = {}
    for name in MODES:
        before = await scan_counters()
        ids, documents, times, unstable = await run_mode(name, queries, vectors)
        await asyncio.sleep(2)  # PostgreSQL publishes its counters with a short delay
        after = await scan_counters()
        used = {key: after[key] - before[key] for key in after if after[key] != before[key]}

        hits = [
            any(d in q["correct_docs"] for d in docs)
            for q, docs in zip(queries, documents, strict=True)
        ]
        shared = [len(set(a) & set(b)) for a, b in zip(ids, baseline_ids, strict=True)]
        different = [
            {"id": q["id"], "shared_with_exact": s, "returned": len(a)}
            for q, a, b, s in zip(queries, ids, baseline_ids, shared, strict=True)
            if a != b
        ]
        ordered = sorted(times)
        report[name] = {
            "settings": MODES[name],
            "searches": len(queries) * (TIMED_ROUNDS + 1),
            "index_use": used,
            "recall_at_5": sum(hits) / len(hits),
            "hits": sum(hits),
            "chunks_shared_with_exact": sum(shared),
            "chunks_in_exact": sum(len(b) for b in baseline_ids),
            "queries_identical_to_exact": len(queries) - len(different),
            "different_queries": different,
            "unstable_queries": unstable,
            "median_ms": round(statistics.median(times), 2),
            "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1], 2),
            "timed_searches": len(times),
        }

    for name, r in report.items():
        print(f"\n[{name}]  {r['searches']} searches")
        print(f"  index use         : {r['index_use']}")
        print(f"  recall@{TOP_K}          : {r['hits']}/{len(queries)} = {r['recall_at_5']:.0%}")
        print(
            f"  same as exact     : {r['chunks_shared_with_exact']}/{r['chunks_in_exact']} chunks; "
            f"{r['queries_identical_to_exact']}/{len(queries)} queries with the same 5 in the "
            "same order"
        )
        print(f"  differing queries : {r['different_queries'] or 'none'}")
        print(f"  unstable queries  : {r['unstable_queries']}")
        print(
            f"  one search        : median {r['median_ms']} ms, p95 {r['p95_ms']} ms "
            f"({r['timed_searches']} timed)"
        )

    COMPARISON_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nsaved: scripts/results/{COMPARISON_FILE.name}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
