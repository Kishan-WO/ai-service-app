"""The "upsert" stage of the ingestion job (Day 11, task 1): saving chunks and vectors, so that
doing it again changes nothing.

- The PIPELINE VERSION names everything that decides the result apart from the file itself.
- A CHUNK ID comes from the document, the pipeline version, the position and the embedded text.
  The same input gives the same ids.
- A document whose file, pipeline version and title are as last time is up to date: nothing has
  to be parsed, embedded or written.
- Otherwise only what differs is written. Old rows are deleted BEFORE new ones are inserted,
  because (document_id, chunk_index) is unique, all in the caller's one transaction.
- A vector that the document already has for exactly the same text and model is reused.

PostgreSQL only: nothing here writes to Qdrant.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.core.config import Settings
from ai_service.core.exceptions import DocumentNotFoundError
from ai_service.db.models import Chunk, Document
from ai_service.services.chunk_stage import (
    CHUNKER_VERSION,
    ChunkedDocument,
    PreparedChunk,
    choose_chunker,
)
from ai_service.services.cleaning import CLEANER_VERSION
from ai_service.services.embedding import DIMENSIONS
from ai_service.services.parsing import PDF_PARSER, kind_of

_NAMESPACE = uuid.UUID("5b0c9a52-2f57-4a55-9a4b-6f0d1c3e8d21")
_HEADING_LENGTH = 512  # chunks.heading


def pipeline_for(filename: str, settings: Settings) -> dict[str, Any]:
    """Everything apart from the file that decides its chunks and vectors. It needs only the
    file name, so "is this document up to date?" can be answered before parsing."""
    kind = kind_of(filename)
    return {
        "parser": PDF_PARSER if kind == "pdf" else "utf-8",
        "kind": kind,
        "cleaner": CLEANER_VERSION,
        "chunker": choose_chunker(settings.CHUNK_STRATEGY, kind),
        "chunker_version": CHUNKER_VERSION,
        "max_tokens": settings.CHUNK_MAX_TOKENS,
        "overlap_tokens": settings.CHUNK_OVERLAP_TOKENS,
        "context_headers": settings.CHUNK_CONTEXT_HEADERS,
        "embedding_model": settings.JINA_EMBEDDING_MODEL,
        "dimensions": DIMENSIONS,
    }


def pipeline_version(pipeline: dict[str, Any]) -> str:
    canonical = json.dumps(pipeline, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def text_hash(embedding_text: str) -> str:
    """Of the text that is EMBEDDED (with its header, if any): the same hash and model mean
    the same vector."""
    return hashlib.sha256(embedding_text.encode()).hexdigest()


def chunk_id(document_id: uuid.UUID, version: str, index: int, embedding_text: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{document_id}|{version}|{index}|{text_hash(embedding_text)}")


async def _owned_document(
    session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, *, lock: bool = False
) -> Document:
    query = select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    if lock:  # two jobs for one document write one after the other, not mixed
        query = query.with_for_update()
    document = await session.scalar(query.execution_options(populate_existing=True))
    if document is None:
        raise DocumentNotFoundError()
    return document


async def is_up_to_date(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    file_sha256: str,
    version: str,
    given_title: str | None = None,
) -> bool:
    """True when this file was already saved with this pipeline and title, and all the chunks
    saved then are still there."""
    document = await _owned_document(session, tenant_id, document_id)
    record = document.metadata_json.get("ingestion")
    if not record:
        return False
    if (record["file_sha256"], record["pipeline_version"], record["given_title"]) != (
        file_sha256,
        version,
        given_title,
    ):
        return False
    stored = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
    )
    return bool(stored == record["chunks"])


async def reusable_vectors(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    texts: Sequence[str],
    embedding_model: str,
) -> dict[str, list[float]]:
    """text -> vector, for the texts this document already has a vector for, made by the same
    model. Only chunks saved by this pipeline are used: their hash is of the embedded text."""
    wanted = {text_hash(text): text for text in texts}
    rows = await session.execute(
        select(Chunk.text_hash, Chunk.embedding, Chunk.metadata_json).where(
            Chunk.tenant_id == tenant_id,
            Chunk.document_id == document_id,
            Chunk.embedding_model == embedding_model,
            Chunk.text_hash.in_(list(wanted)),
        )
    )
    return {
        wanted[digest]: [float(x) for x in vector]
        for digest, vector, metadata in rows
        if metadata.get("pipeline_version")
    }


@dataclass(frozen=True)
class SaveResult:
    inserted: int
    deleted: int
    kept: int  # rows with the same id: their text and vector were not written again


def _metadata(title: str, prepared: PreparedChunk, chunker: str, version: str) -> dict[str, Any]:
    chunk = prepared.chunk
    return {
        "title": title,
        "headings": list(chunk.headings),
        "header": chunk.header,
        "token_count": chunk.token_count,
        "page_start": prepared.page_start,
        "page_end": prepared.page_end,
        "chunker": chunker,
        "pipeline_version": version,
    }


async def save_chunks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    chunked: ChunkedDocument,
    vectors: Sequence[list[float]],
    content: str,
    file_sha256: str,
    pipeline: dict[str, Any],
    given_title: str | None = None,
) -> SaveResult:
    """Make the document's rows equal to `chunked`. It does not commit: until the caller does,
    everyone else still sees the old version, and a failure leaves it as it was."""
    if len(vectors) != len(chunked.chunks):
        raise ValueError("one vector is needed for every chunk")
    document = await _owned_document(session, tenant_id, document_id, lock=True)
    version = pipeline_version(pipeline)
    model = pipeline["embedding_model"]

    new: dict[uuid.UUID, tuple[PreparedChunk, list[float]]] = {
        chunk_id(document_id, version, p.chunk.index, p.chunk.embedding_text): (p, vector)
        for p, vector in zip(chunked.chunks, vectors, strict=True)
    }
    rows = await session.scalars(
        select(Chunk)
        .where(Chunk.document_id == document_id)
        .execution_options(populate_existing=True)
    )
    old = {row.id: row for row in rows}

    gone = [row_id for row_id in old if row_id not in new]
    if gone:  # first, to free their positions
        await session.execute(
            delete(Chunk).where(Chunk.document_id == document_id, Chunk.id.in_(gone))
        )
    inserted = 0
    for row_id, (prepared, vector) in new.items():
        chunk = prepared.chunk
        metadata = _metadata(chunked.title, prepared, chunked.chunker, version)
        heading = " > ".join(chunk.headings)[:_HEADING_LENGTH]
        row = old.get(row_id)
        if row is None:
            session.add(
                Chunk(
                    id=row_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    chunk_index=chunk.index,
                    heading=heading,
                    text=chunk.text,
                    text_hash=text_hash(chunk.embedding_text),
                    embedding=vector,
                    embedding_model=model,
                    metadata_json=metadata,
                )
            )
            inserted += 1
            continue
        # the same id means the same embedded text; what can still differ is written only
        # when it really does, so that an unchanged row is not touched
        if row.metadata_json != metadata:
            row.metadata_json = metadata
        if row.heading != heading:
            row.heading = heading
        if row.text != chunk.text:
            row.text = chunk.text

    document.title = chunked.title
    document.content = content
    document.content_hash = file_sha256
    record = {
        "file_sha256": file_sha256,
        "pipeline_version": version,
        "pipeline": pipeline,
        "given_title": given_title,
        "chunks": len(new),
    }
    if document.metadata_json.get("ingestion") != record:
        document.metadata_json = {**document.metadata_json, "ingestion": record}
    await session.flush()
    return SaveResult(inserted=inserted, deleted=len(gone), kept=len(new) - inserted)
