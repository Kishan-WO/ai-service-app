"""Day 10, task 4: recall@5 of the vector search on the 20 labelled queries of Day 9.

Step 1 (this version): EXACT search, which compares the query with every chunk. It is the
baseline that the HNSW index is compared with afterwards.

A query is a hit when at least one of its top 5 chunks belongs to one of its `correct_docs`
(the Day 9 rule). recall@5 = hits / number of queries.

The query embeddings are saved in a file, so every later run searches with the same vectors and
does not call the embedding API again.

Run (needs the local Postgres with the corpus, and JINA_API_KEY in .env on the first run):
    uv run python scripts/eval_search.py
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ai_service.core.config import get_settings
from ai_service.db.models import Document
from ai_service.services.search_service import embed_query, search_chunks

SCRIPTS = Path(__file__).parent
QUERIES_FILE = SCRIPTS / "data" / "search_queries.json"
VECTORS_FILE = SCRIPTS / "results" / "day10_query_vectors.json"
RESULTS_FILE = SCRIPTS / "results" / "day10_exact_search.json"
CORPUS_TENANT_ID = uuid.UUID("c69cf31f-3e72-567f-bf70-f833be7a5ca9")
TOP_K = 5


async def load_query_vectors(queries: list[dict]) -> dict[str, list[float]]:
    """Embed each query once; reuse the saved vector while the query text and model are the same."""
    settings = get_settings()
    saved: dict = {}
    if VECTORS_FILE.exists():
        data = json.loads(VECTORS_FILE.read_text(encoding="utf-8"))
        if data["model"] == settings.JINA_EMBEDDING_MODEL:
            saved = data["vectors"]
    missing = [q["query"] for q in queries if q["query"] not in saved]
    if missing:
        async with httpx.AsyncClient() as http:
            for query_text in missing:
                saved[query_text] = await embed_query(query_text, http, settings)
        VECTORS_FILE.write_text(
            json.dumps(
                {"model": settings.JINA_EMBEDDING_MODEL, "vectors": saved}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    print(
        f"query vectors: {len(missing)} from the API, {len(queries) - len(missing)} from the file"
    )
    return saved


async def exact_plan(session: AsyncSession, vector: list[float]) -> str:
    """The scans PostgreSQL would use, to prove that the HNSW index is not one of them."""
    literal = "[" + ",".join(map(str, vector)) + "]"
    rows = await session.execute(
        text(
            "explain (costs off) select id from chunks where tenant_id = :tenant "
            "order by embedding <=> cast(:vector as vector) limit 5"
        ),
        {"tenant": CORPUS_TENANT_ID, "vector": literal},
    )
    return " | ".join(line.strip(" ->") for line in rows.scalars() if "Scan" in line)


async def main() -> None:
    queries = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    vectors = await load_query_vectors(queries)

    engine = create_async_engine(str(get_settings().DATABASE_URL))
    results = []
    async with AsyncSession(engine) as session:
        source_of = dict(
            (
                await session.execute(
                    select(Document.id, Document.source).where(
                        Document.tenant_id == CORPUS_TENANT_ID
                    )
                )
            ).all()
        )
        unknown = {d for q in queries for d in q["correct_docs"]} - set(source_of.values())
        if unknown:
            sys.exit(f"labelled documents that are not in the database: {sorted(unknown)}")

        # An HNSW index can only be read by an index scan, so switching index scans off for this
        # transaction makes PostgreSQL compare the query with every chunk of the tenant.
        await session.execute(text("set local enable_indexscan = off"))
        plan = await exact_plan(session, vectors[queries[0]["query"]])
        if "hnsw" in plan:
            sys.exit(f"the HNSW index is still used, so this would not be exact: {plan}")
        print(f"plan: {plan}\n")

        print(" id  language  hit@5  first correct rank  top score  query")
        for q in queries:
            found = await search_chunks(
                session,
                tenant_id=CORPUS_TENANT_ID,
                query_vector=vectors[q["query"]],
                limit=TOP_K,
                ef_search=40,  # as measured in Step 8 (an exact search does not use it)
                iterative_scan="off",
            )
            top = [
                {
                    "chunk_id": str(r.id),
                    "document": source_of[r.document_id],
                    "heading": r.heading,
                    "score": round(r.score, 4),
                }
                for r in found
            ]
            ranks = [i for i, item in enumerate(top, 1) if item["document"] in q["correct_docs"]]
            first = ranks[0] if ranks else None
            results.append(
                {
                    "id": q["id"],
                    "language": q["language"],
                    "query": q["query"],
                    "hit_at_5": first is not None,
                    "first_correct_rank": first,
                    "top5": top,
                }
            )
            print(
                f"{q['id']:>3}  {q['language']:<8}  {'yes' if first else 'NO':<5}  "
                f"{first if first else '-':^18}  {top[0]['score']:.4f}     {q['query']}"
            )
        await session.rollback()
    await engine.dispose()

    hits = sum(r["hit_at_5"] for r in results)
    recall = hits / len(results)
    print(f"\nexact search recall@{TOP_K}: {hits}/{len(results)} = {recall:.0%}")
    RESULTS_FILE.write_text(
        json.dumps(
            {"search": "exact", "k": TOP_K, "hits": hits, "recall": recall, "queries": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"saved: scripts/results/{RESULTS_FILE.name}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
