"""Day 10, tasks 6 and 7, on the real PostgreSQL and the running API.

Task 6: a tenant can never get another tenant's chunks, also with zero documents.
Task 7: deleting a document removes its vectors.

The script signs up a TEMPORARY organisation through the API (random password, never printed or
saved). It gives it exact copies of 5 chunks of the Corpus Tenant: same text, same vectors. That
is the hardest case, because the same vector then exists in both tenants. At the end the temporary
organisation is removed, also when a check fails. The corpus is only read.

Needs: the API on localhost:8000, the local Postgres with the corpus, .env.corpus-test, and the
saved query vectors of eval_search.py.
    uv run python scripts/verify_isolation.py              the API runs with VECTOR_STORE=pgvector
    uv run python scripts/verify_isolation.py --qdrant     the API runs with VECTOR_STORE=qdrant

With --qdrant the temporary chunks are also written to Qdrant, the direct searches use
QdrantStore, and the deletion is checked in Qdrant too. The script cannot see which store the
API uses, so start the API with the matching setting.
"""

import asyncio
import hashlib
import json
import secrets
import sys
import uuid
from pathlib import Path

import httpx
from compare_hnsw import MODES
from eval_search import CORPUS_TENANT_ID, QUERIES_FILE, load_query_vectors
from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from ai_service.core.config import get_settings
from ai_service.db.models import Chunk
from ai_service.services.qdrant_store import QdrantStore
from ai_service.services.search_service import search_chunks
from ai_service.services.vector_store import VectorRecord

API = "http://localhost:8000/api/v1"
CREDENTIALS = Path(".env.corpus-test")
HTTP_QUERIES = [
    "How do I define a path parameter in FastAPI?",
    "FastAPI में CORS कैसे enable करें?",
    "What is the solubility product constant (Ksp) of silver chloride?",
]
COPIES = 5
USE_QDRANT = "--qdrant" in sys.argv
qdrant = AsyncQdrantClient(url=get_settings().QDRANT_URL) if USE_QDRANT else None


def qdrant_store() -> QdrantStore:
    assert qdrant is not None
    return QdrantStore(qdrant, get_settings().QDRANT_COLLECTION)


async def points_of(document: uuid.UUID) -> int:
    assert qdrant is not None
    only = models.Filter(
        must=[
            models.FieldCondition(key="document_id", match=models.MatchValue(value=str(document)))
        ]
    )
    collection = get_settings().QDRANT_COLLECTION
    return (await qdrant.count(collection, count_filter=only, exact=True)).count


async def copy_document_to_qdrant(engine: AsyncEngine, document: uuid.UUID) -> None:
    async with AsyncSession(engine) as session:
        rows = await session.execute(select(Chunk).where(Chunk.document_id == document))
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
            for c in rows.scalars()
        ]
    await qdrant_store().upsert(records)


failures: list[str] = []


def check(label: str, ok: bool, detail: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<58} {detail}")
    if not ok:
        failures.append(label)


async def count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as connection:  # a new transaction: sees only committed data
        return (await connection.execute(text(sql), params)).scalar_one()


async def totals(engine: AsyncEngine) -> tuple[int, ...]:
    return tuple(
        [await count(engine, f"select count(*) from {t}") for t in ("tenants", "users")]
        + [await count(engine, f"select count(*) from {t}") for t in ("documents", "chunks")]
    )


async def search_all_plans(
    engine: AsyncEngine, tenant: uuid.UUID, vectors: list[list[float]], limit: int
) -> dict[str, list]:
    """Run every vector for one tenant: pgvector with the exact, planner-chosen and forced-HNSW
    plans, or (with --qdrant) QdrantStore."""
    if USE_QDRANT:
        store = qdrant_store()
        return {
            "qdrant": [
                await store.search(tenant_id=tenant, query_vector=v, limit=limit) for v in vectors
            ]
        }
    found: dict[str, list] = {}
    for mode, settings in MODES.items():
        async with AsyncSession(engine) as session:
            for setting in settings:
                await session.execute(text(setting))
            found[mode] = [
                await search_chunks(session, tenant_id=tenant, query_vector=v, limit=limit)
                for v in vectors
            ]
            await session.rollback()
    return found


async def http_search(client: httpx.AsyncClient, token: str, query: str) -> list[dict]:
    response = await client.post(
        f"{API}/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "limit": 50},
    )
    response.raise_for_status()
    return response.json()["results"]


async def main() -> None:
    queries = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    saved = await load_query_vectors(queries)
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    before = await totals(engine)
    print(f"store: {'qdrant' if USE_QDRANT else 'pgvector'}")
    print(f"before: tenants, users, documents, chunks = {before}")
    if qdrant is not None:
        collection = get_settings().QDRANT_COLLECTION
        points_before = (await qdrant.count(collection, exact=True)).count
        print(f"before: Qdrant points = {points_before}")

    # The vectors to attack with: the 20 queries and the vectors of 200 corpus chunks themselves.
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("select embedding::text from chunks where tenant_id = :c order by id limit 200"),
            {"c": CORPUS_TENANT_ID},
        )
        corpus_vectors = [json.loads(value) for value in rows.scalars()]
        copies = (
            await connection.execute(
                text(
                    "select id, embedding::text from chunks where tenant_id = :c and "
                    "metadata->>'section_id' like 'fastapi-docs/tutorial/cors.md#%' "
                    "order by chunk_index limit :n"
                ),
                {"c": CORPUS_TENANT_ID, "n": COPIES},
            )
        ).all()
    copied_vectors = [json.loads(row[1]) for row in copies]
    attack = [saved[q["query"]] for q in queries] + corpus_vectors + copied_vectors

    credentials = dict(
        line.split("=", 1)
        for line in CREDENTIALS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    email = f"tmp-isolation-{secrets.token_hex(4)}@example.com"
    password = secrets.token_urlsafe(18)
    tenant_two: uuid.UUID | None = None
    document: uuid.UUID | None = None

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{API}/auth/register",
                json={"email": email, "password": password, "tenant_name": "tmp-isolation"},
            )
            response.raise_for_status()
            token_two = (
                await client.post(f"{API}/auth/login", json={"email": email, "password": password})
            ).json()["access_token"]
            token_corpus = (
                await client.post(
                    f"{API}/auth/login",
                    json={
                        "email": credentials["CORPUS_TEST_EMAIL"],
                        "password": credentials["CORPUS_TEST_PASSWORD"],
                    },
                )
            ).json()["access_token"]
            me = await client.get(
                f"{API}/auth/me", headers={"Authorization": f"Bearer {token_two}"}
            )
            tenant_two = uuid.UUID(me.json()["tenant"]["id"])

            print(f"\n[A] Task 6: tenant 2 has ZERO documents ({len(attack)} vectors)")
            for mode, results in (await search_all_plans(engine, tenant_two, attack, 50)).items():
                total = sum(len(r) for r in results)
                check(f"{mode}: rows returned to tenant 2", total == 0, f"{total} rows")
            for query in HTTP_QUERIES:
                n = len(await http_search(client, token_two, query))
                check(f"HTTP, tenant 2: {query[:40]}", n == 0, f"{n} results")

            print(f"\n[B] Task 6: tenant 2 now owns copies of {COPIES} corpus chunks")
            response = await client.post(
                f"{API}/documents/upload",
                headers={"Authorization": f"Bearer {token_two}"},
                files={"file": ("tmp-isolation.txt", b"temporary isolation test", "text/plain")},
            )
            response.raise_for_status()
            document = uuid.UUID(response.json()["id"])
            async with engine.begin() as connection:  # there is no ingestion yet (Day 11)
                for position, (chunk_id, _) in enumerate(copies):
                    await connection.execute(
                        text(
                            "insert into chunks (id, tenant_id, document_id, chunk_index, heading,"
                            " text, text_hash, embedding, embedding_model, metadata, created_at,"
                            " updated_at) select :new, :t, :d, :i, heading, text, :h, embedding,"
                            " embedding_model, metadata, now(), now() from chunks where id = :src"
                        ),
                        {
                            "new": uuid.uuid4(),
                            "t": tenant_two,
                            "d": document,
                            "i": position,
                            "h": hashlib.sha256(str(position).encode()).hexdigest(),
                            "src": chunk_id,
                        },
                    )
            if USE_QDRANT:
                await copy_document_to_qdrant(engine, document)

            for mode, results in (await search_all_plans(engine, tenant_two, attack, 50)).items():
                foreign = sum(x.document_id != document for r in results for x in r)
                sizes = {len(r) for r in results}
                check(
                    f"{mode}: tenant 2 gets only its own chunks",
                    foreign == 0 and sizes == {COPIES},
                    f"{foreign} foreign rows; rows per search {sorted(sizes)}",
                )
            reverse = await search_all_plans(engine, CORPUS_TENANT_ID, attack, 50)
            for mode, results in reverse.items():
                leaked = sum(x.document_id == document for r in results for x in r)
                check(
                    f"{mode}: the Corpus Tenant never gets tenant 2's copies",
                    leaked == 0,
                    f"{leaked} leaked rows in {sum(len(r) for r in results)}",
                )
            for query in HTTP_QUERIES:
                own = await http_search(client, token_two, query)
                other = await http_search(client, token_corpus, query)
                ids_two = {r["document_id"] for r in own}
                leaked = sum(r["document_id"] == str(document) for r in other)
                check(
                    f"HTTP, both tenants: {query[:36]}",
                    ids_two == {str(document)} and leaked == 0,
                    f"tenant 2: {len(own)} own; corpus: {len(other)}, {leaked} of tenant 2",
                )

            print("\n[C] Task 7: delete the document")
            by_document = "select count(*) from chunks where document_id = :d"
            n = await count(engine, by_document, d=document)
            check("chunks of the document before the delete", n == COPIES, f"{n} rows")
            if USE_QDRANT:
                n = await points_of(document)
                check("Qdrant points of the document before the delete", n == COPIES, f"{n} points")

            response = await client.delete(
                f"{API}/documents/{document}",
                headers={"Authorization": f"Bearer {token_corpus}"},
            )
            n = await count(engine, by_document, d=document)
            check(
                "DELETE by another tenant is refused and removes nothing",
                response.status_code == 404 and n == COPIES,
                f"HTTP {response.status_code}; {n} rows left",
            )

            response = await client.delete(
                f"{API}/documents/{document}",
                headers={"Authorization": f"Bearer {token_two}"},
            )
            check(
                "DELETE by the owner", response.status_code == 204, f"HTTP {response.status_code}"
            )
            n = await count(engine, by_document, d=document)
            check("chunk rows (and vectors) of the document", n == 0, f"{n} rows")
            if USE_QDRANT:
                n = await points_of(document)
                check("Qdrant points of the document after the delete", n == 0, f"{n} points")
            for mode, results in (
                await search_all_plans(engine, tenant_two, copied_vectors, 50)
            ).items():
                total = sum(len(r) for r in results)
                check(f"{mode}: searching with the deleted vectors", total == 0, f"{total} rows")
            for query in HTTP_QUERIES:
                n = len(await http_search(client, token_two, query))
                check(f"HTTP, tenant 2: {query[:40]}", n == 0, f"{n} results")
            n = await count(
                engine, "select count(*) from chunks where tenant_id = :c", c=CORPUS_TENANT_ID
            )
            check("the Corpus Tenant's chunks are untouched", n == before[3], f"{n} rows")
    finally:
        if qdrant is not None and tenant_two is not None and document is not None:
            await qdrant_store().delete_document(tenant_id=tenant_two, document_id=document)
            collection = get_settings().QDRANT_COLLECTION
            points_after = (await qdrant.count(collection, exact=True)).count
            check(
                "Qdrant has the same points as before",
                points_after == points_before,
                f"{points_after}",
            )
        if tenant_two is not None:  # remove only what this run created
            async with engine.begin() as connection:
                for statement in (
                    "delete from documents where tenant_id = :t",
                    "delete from users where tenant_id = :t",
                    "delete from tenants where id = :t and name = 'tmp-isolation'",
                ):
                    await connection.execute(text(statement), {"t": tenant_two})
        after = await totals(engine)
        print(f"\nafter clean-up: tenants, users, documents, chunks = {after}")
        await engine.dispose()

    check("the database counts are the same as before", after == before, f"{before} -> {after}")
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
