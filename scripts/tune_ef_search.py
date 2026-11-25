"""Day 10, task 4, step 3: what hnsw.ef_search changes.

ef_search is the number of candidates HNSW keeps while it walks the graph. The HNSW index is
forced (as in compare_hnsw.py) and only ef_search changes. For each value:
    - how many of the exact top 5 chunks come back, over the 20 queries
    - recall@5 (the Day 9 rule)
    - the time of one search
    - how many results come back when 50 are asked for (the API allows limit up to 50)

Run after eval_search.py:
    uv run python scripts/tune_ef_search.py
"""

import asyncio
import json
import statistics
import sys

from compare_hnsw import MODES, run_mode, scan_counters
from eval_search import (
    CORPUS_TENANT_ID,
    QUERIES_FILE,
    RESULTS_FILE,
    SCRIPTS,
    load_query_vectors,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ai_service.core.config import get_settings
from ai_service.services.search_service import search_chunks

TUNING_FILE = SCRIPTS / "results" / "day10_ef_search_tuning.json"
VALUES = [5, 10, 20, 40, 100, 200, 400]
LARGE_LIMIT = 50


async def rows_at_large_limit(mode: str, vectors: list[list[float]], ef_search: int) -> list[int]:
    """How many chunks each query gets back when it asks for LARGE_LIMIT."""
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    async with AsyncSession(engine) as session:
        for setting in MODES[mode]:
            await session.execute(text(setting))
        counts = [
            len(
                await search_chunks(
                    session,
                    tenant_id=CORPUS_TENANT_ID,
                    query_vector=v,
                    limit=LARGE_LIMIT,
                    ef_search=ef_search,
                    iterative_scan="off",
                )
            )
            for v in vectors
        ]
        await session.rollback()
    await engine.dispose()
    return counts


async def main() -> None:
    queries = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    vectors = await load_query_vectors(queries)
    baseline = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))["queries"]
    baseline_ids = [[item["chunk_id"] for item in q["top5"]] for q in baseline]

    report = []
    print(
        "ef_search  exact top-5 chunks  recall@5  median ms  p95 ms  "
        "rows for limit=50  HNSW used  queries that lost chunks"
    )
    for value in VALUES:
        mode = f"ef_{value}"
        MODES[mode] = MODES["hnsw"]  # ef_search itself is passed to search_chunks

        before = await scan_counters()
        ids, documents, times, unstable = await run_mode(mode, queries, vectors, ef_search=value)
        large = await rows_at_large_limit(mode, [vectors[q["query"]] for q in queries], value)
        await asyncio.sleep(2)
        after = await scan_counters()
        used = {key: after[key] - before[key] for key in after if after[key] != before[key]}

        shared = [len(set(a) & set(b)) for a, b in zip(ids, baseline_ids, strict=True)]
        hits = sum(
            any(d in q["correct_docs"] for d in docs)
            for q, docs in zip(queries, documents, strict=True)
        )
        lost = {
            q["id"]: len(b) - s
            for q, b, s in zip(queries, baseline_ids, shared, strict=True)
            if s < len(b)
        }
        ordered = sorted(times)
        row = {
            "ef_search": value,
            "chunks_shared_with_exact": sum(shared),
            "chunks_in_exact": sum(len(b) for b in baseline_ids),
            "hits": hits,
            "recall_at_5": hits / len(queries),
            "median_ms": round(statistics.median(times), 2),
            "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1], 2),
            "rows_for_limit_50": {"min": min(large), "max": max(large)},
            "index_use": used,
            "lost_chunks_by_query": lost,
            "unstable_queries": unstable,
        }
        report.append(row)
        rows_text = f"{min(large)}-{max(large)}"
        print(
            f"{value:>9}  {row['chunks_shared_with_exact']:>10}/{row['chunks_in_exact']:<7}  "
            f"{hits:>2}/{len(queries):<5}  {row['median_ms']:>9}  {row['p95_ms']:>6}  "
            f"{rows_text:^17}  {used.get('ix_chunks_embedding_hnsw', 0):>9}  {lost or '-'}"
        )
        if unstable or set(used) != {"ix_chunks_embedding_hnsw"}:
            print(f"           check: unstable queries = {unstable}, index use = {used}")

    TUNING_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nsaved: scripts/results/{TUNING_FILE.name}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
