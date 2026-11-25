"""Day 10, Task 2: Embed and upsert the corpus into PostgreSQL (pgvector).

    uv run python scripts/upsert_corpus.py
    uv run python scripts/upsert_corpus.py --tenant-name "Corpus Tenant"
    uv run python scripts/upsert_corpus.py --limit 64

Reads:
  - ai-eng-learning/day-9/corpus/data/documents.json
  - ai-eng-learning/day-9/corpus/data/sections.json
  - ai-eng-learning/day-9/corpus/data/embeddings/jina-embeddings-v3/vectors.npy
  - ai-eng-learning/day-9/corpus/data/embeddings/jina-embeddings-v3/meta.json

Writes to ai-service PostgreSQL:
  - tenants: ensures the target tenant exists
  - documents: creates or reuses document records for all 234 documents
  - chunks: batch-upserts all 2,097 chunks with their 1024-dimensional vectors
            into the `chunks` table with ON CONFLICT (document_id, chunk_index) DO UPDATE
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.db.models import Chunk, Document, Tenant
from ai_service.db.session import async_session_factory

# Path to Day 9 corpus data
AI_ENG_ROOT = Path(__file__).resolve().parent.parent.parent
DAY9_DATA = AI_ENG_ROOT / "ai-eng-learning" / "day-9" / "corpus" / "data"
EMBEDDINGS_DIR = DAY9_DATA / "embeddings" / "jina-embeddings-v3"

BATCH_SIZE = 200
DEFAULT_TENANT_NAME = "Corpus Tenant"
DEFAULT_MODEL_NAME = "jina-embeddings-v3"


def make_stable_uuid(namespace: uuid.UUID, key: str) -> uuid.UUID:
    """Deterministic UUID so re-runs always generate identical IDs."""
    return uuid.uuid5(namespace, key)


async def get_or_create_tenant(session: AsyncSession, name: str) -> Tenant:
    """Retrieve existing tenant by name or create a new one with a stable UUID."""
    tenant = await session.scalar(select(Tenant).where(Tenant.name == name))
    if tenant is not None:
        return tenant

    # Deterministic tenant ID based on tenant name
    tenant_id = make_stable_uuid(uuid.NAMESPACE_DNS, f"ai-service.tenant.{name}")
    tenant = Tenant(id=tenant_id, name=name)
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    print(f"Created tenant '{name}' with id: {tenant.id}")
    return tenant


async def upsert_documents(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    docs_metadata: list[dict[str, Any]],
    sections_by_doc: dict[str, list[dict[str, Any]]],
) -> dict[str, uuid.UUID]:
    """Ensure all documents exist, returning {doc_source_id: document_uuid}."""
    doc_id_map: dict[str, uuid.UUID] = {}

    # Query all existing documents for this tenant
    existing_rows = await session.execute(
        select(Document.id, Document.source).where(Document.tenant_id == tenant_id)
    )
    existing_by_source = {source: doc_id for doc_id, source in existing_rows}

    docs_to_create: list[Document] = []
    for d in docs_metadata:
        source = d["id"]
        if source in existing_by_source:
            doc_id_map[source] = existing_by_source[source]
            continue

        doc_sections = sections_by_doc.get(source, [])
        content = "\n\n".join(s["text"] for s in doc_sections)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Deterministic document id
        stable_doc_id = make_stable_uuid(tenant_id, f"doc:{source}")
        new_doc = Document(
            id=stable_doc_id,
            tenant_id=tenant_id,
            title=d.get("title", source)[:255],
            source=source[:512],
            content=content,
            content_hash=content_hash,
            metadata_json={
                "source_kind": d.get("source", ""),
                "kind": d.get("kind", ""),
                "licence": d.get("licence", ""),
                "sections_count": len(doc_sections),
            },
        )
        docs_to_create.append(new_doc)
        doc_id_map[source] = stable_doc_id

    if docs_to_create:
        session.add_all(docs_to_create)
        await session.commit()
        print(f"Created {len(docs_to_create)} new document records in DB.")
    else:
        print(f"All {len(docs_metadata)} documents already present in DB.")

    return doc_id_map


async def upsert_chunks_batch(
    session: AsyncSession,
    chunk_dicts: list[dict[str, Any]],
) -> None:
    """Perform a batch upsert on chunks table."""
    stmt = pg_insert(Chunk).values(chunk_dicts)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_chunks_document_chunk_index",
        set_={
            "heading": stmt.excluded.heading,
            "text": stmt.excluded.text,
            "text_hash": stmt.excluded.text_hash,
            "embedding": stmt.excluded.embedding,
            "embedding_model": stmt.excluded.embedding_model,
            "metadata": stmt.excluded["metadata"],
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.commit()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upsert Day 9 corpus into PostgreSQL chunks table."
    )
    parser.add_argument(
        "--tenant-name", default=DEFAULT_TENANT_NAME, help="Tenant name to own the chunks"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Optional limit on number of chunks to upsert"
    )
    args = parser.parse_args()

    # Verify input files exist
    docs_file = DAY9_DATA / "documents.json"
    sections_file = DAY9_DATA / "sections.json"
    vectors_file = EMBEDDINGS_DIR / "vectors.npy"
    meta_file = EMBEDDINGS_DIR / "meta.json"

    for file_path in (docs_file, sections_file, vectors_file, meta_file):
        if not file_path.exists():
            raise FileNotFoundError(f"Required corpus file missing: {file_path}")

    print("=== Day 10 Task 2: Upsert Corpus into pgvector ===")
    print(f"Loading corpus from {DAY9_DATA.relative_to(AI_ENG_ROOT)} ...")

    with open(docs_file, encoding="utf-8") as df:
        docs_data = json.load(df)

    with open(sections_file, encoding="utf-8") as sf:
        sections_data = json.load(sf)

    print(f"Loading precomputed vectors from {vectors_file.relative_to(AI_ENG_ROOT)} ...")
    vectors = np.load(vectors_file)
    print(f"Loaded {vectors.shape[0]} vectors of dimension {vectors.shape[1]}")

    if len(sections_data) != vectors.shape[0]:
        raise ValueError(
            f"Mismatch: {len(sections_data)} sections vs {vectors.shape[0]} vectors!"
        )

    # Group sections by document id and compute chunk index per document
    sections_by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chunk_index_map: dict[str, int] = {}  # section_id -> chunk_index inside that document

    for sec in sections_data:
        doc_id = sec["doc_id"]
        chunk_index = len(sections_by_doc[doc_id])
        chunk_index_map[sec["id"]] = chunk_index
        sections_by_doc[doc_id].append(sec)

    total_chunks = len(sections_data) if args.limit is None else min(args.limit, len(sections_data))
    print(f"Will upsert {total_chunks} chunks across {len(docs_data)} documents.")

    start_time = time.perf_counter()

    async with async_session_factory() as session:
        # Step 1: Get or create tenant
        tenant = await get_or_create_tenant(session, args.tenant_name)
        tenant_id = tenant.id

        # Step 2: Ensure documents exist
        doc_id_map = await upsert_documents(session, tenant_id, docs_data, sections_by_doc)

        # Step 3: Prepare and batch-upsert chunks
        print(f"Upserting chunks in batches of {BATCH_SIZE} ...")
        batch_chunks: list[dict[str, Any]] = []
        upserted_count = 0

        for i in range(total_chunks):
            sec = sections_data[i]
            doc_source = sec["doc_id"]
            db_doc_id = doc_id_map[doc_source]
            chunk_idx = chunk_index_map[sec["id"]]

            heading = " > ".join(sec.get("heading_path", []))[:512]
            text = sec["text"]
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            stable_chunk_id = make_stable_uuid(tenant_id, f"chunk:{sec['id']}")

            # pgvector vector can be passed as a Python list of floats
            emb_vector = vectors[i].tolist()

            chunk_dict = {
                "id": stable_chunk_id,
                "tenant_id": tenant_id,
                "document_id": db_doc_id,
                "chunk_index": chunk_idx,
                "heading": heading,
                "text": text,
                "text_hash": text_hash,
                "embedding": emb_vector,
                "embedding_model": DEFAULT_MODEL_NAME,
                "metadata": {
                    "section_id": sec["id"],
                    "source": sec.get("source", ""),
                    "title": sec.get("title", ""),
                    "licence": sec.get("licence", ""),
                    "words": sec.get("words", 0),
                },
            }
            batch_chunks.append(chunk_dict)

            if len(batch_chunks) >= BATCH_SIZE or i == total_chunks - 1:
                await upsert_chunks_batch(session, batch_chunks)
                upserted_count += len(batch_chunks)
                print(
                    f"  Upserted {upserted_count}/{total_chunks} chunks...", end="\r", flush=True
                )
                batch_chunks = []

        elapsed = time.perf_counter() - start_time
        print(f"\nSuccessfully upserted {upserted_count} chunks in {elapsed:.2f}s!")

        # Verify final count in DB
        db_count = await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.tenant_id == tenant_id)
        )
        print(f"Total chunks in database for tenant '{args.tenant_name}': {db_count}")


if __name__ == "__main__":
    asyncio.run(main())
