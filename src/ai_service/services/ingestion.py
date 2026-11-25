"""One ingestion job from start to end (Day 11, task 1). A plain async function: it knows
nothing about the queue (ai_service.worker does that part).

    file -> parse -> clean -> chunk -> embed -> upsert
    link -> fetch (download and store the file) -> the same stages

A link is fetched once. When the file is stored, a later attempt of the same job uses the stored
file. A new job for the same link downloads it again and updates the same document.

What can happen:
- "succeeded": the chunks are saved and the job says so, in ONE transaction.
- "up_to_date": the file, pipeline and title are as last time. Nothing is done again.
- "failed": a permanent problem (a broken file, a refused request, a bug). The job says why.
- "skipped": the job is finished already, or does not exist. Nothing is touched.
- RetryableIngestion is raised: a temporary problem. The job stays "running"; whoever called
  decides whether to try again or give up.

Every step commits what the status endpoint should see. Logs name the job, the stage and the
kind of error; never the text of a document.
"""

import asyncio
import logging
import traceback
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.core.config import Settings
from ai_service.core.exceptions import AppError
from ai_service.db.models import Document, DocumentFile, IngestionJob
from ai_service.services.chunk_save import (
    is_up_to_date,
    pipeline_for,
    pipeline_version,
    reusable_vectors,
    save_chunks,
)
from ai_service.services.chunk_stage import ChunkedDocument, ChunkOptions, chunk_document
from ai_service.services.cleaning import clean_document
from ai_service.services.embedding import (
    Pacer,
    PermanentEmbeddingError,
    Sleep,
    TransientEmbeddingError,
    embed_passages,
)
from ai_service.services.file_store import get_file
from ai_service.services.ingest_request import GIVEN_TITLE_KEY, save_document_file
from ai_service.services.ingestion_jobs import (
    attach_document,
    fail_job,
    set_stage,
    start_job,
    succeed_job,
)
from ai_service.services.parsing import parse_file
from ai_service.services.tokenizer import TokenCounter
from ai_service.services.url_fetch import (
    FetchedFile,
    FetchError,
    TransientFetchError,
    fetch_url,
)

logger = logging.getLogger("ai_service.ingestion")

GENERIC_FAILURE = "The document could not be processed."

Outcome = Literal["succeeded", "up_to_date", "failed", "skipped"]


class RetryableIngestion(Exception):
    """A temporary problem. The message is safe to show to the user."""


class _FileChanged(Exception):
    """The stored file was replaced while this job was working on the old one."""


class Fetcher(Protocol):
    async def __call__(self, url: str, *, max_bytes: int) -> FetchedFile: ...


@dataclass(frozen=True)
class IngestionDeps:
    """What a job needs from the process it runs in. The pacer is shared by all its jobs."""

    sessions: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    settings: Settings
    counter: TokenCounter
    pacer: Pacer
    sleep: Sleep = asyncio.sleep
    fetch: Fetcher = fetch_url  # downloads a user's link, with the checks of url_fetch


def _is_temporary(exc: BaseException) -> bool:
    """Database connection trouble. A statement the database refuses is not temporary."""
    if isinstance(exc, OperationalError | InterfaceError | ConnectionError | TimeoutError):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


async def give_up(deps: IngestionDeps, job_id: uuid.UUID, message: str) -> None:
    """Mark a job as failed (nothing happens if it is finished already)."""
    async with deps.sessions() as session:
        await fail_job(session, job_id, message)
        await session.commit()


async def _enter(deps: IngestionDeps, job_id: uuid.UUID, stage: str) -> None:
    async with deps.sessions() as session:
        await set_stage(session, job_id, stage)
        await session.commit()


async def _fetch(
    deps: IngestionDeps, job_id: uuid.UUID, tenant_id: uuid.UUID, link: str
) -> uuid.UUID | None:
    """Downloads the link and stores the file; the document and the job are linked in ONE
    transaction. None: the job was finished or given its document by someone else."""
    await _enter(deps, job_id, "fetch")
    # no database session is open while downloading
    fetched = await deps.fetch(link, max_bytes=deps.settings.MAX_UPLOAD_SIZE_BYTES)
    async with deps.sessions() as session:
        try:
            document, _ = await save_document_file(
                session,
                tenant_id=tenant_id,
                filename=fetched.filename,
                data=fetched.data,
                content_type=fetched.content_type[:255] or None,
                source=link,  # the link as given, also when it redirects somewhere else
            )
            if not await attach_document(session, job_id, document.id):
                return None  # leaving without a commit undoes the save
            await session.commit()
        except IntegrityError:  # another job saved the same new file at the same moment
            raise RetryableIngestion("Another job saved the same file. Trying again.") from None
        return document.id


async def _work(deps: IngestionDeps, job_id: uuid.UUID) -> Outcome:
    settings = deps.settings
    async with deps.sessions() as session:
        # where the last attempt stopped; starting a new one clears it
        last_stage = await session.scalar(
            select(IngestionJob.stage).where(IngestionJob.id == job_id)
        )
        if not await start_job(session, job_id):
            return "skipped"
        await session.commit()
        # the tenant and the document come from the job's row, never from the queue message
        job = await session.get_one(IngestionJob, job_id)
        tenant_id, document_id = job.tenant_id, job.document_id
        source_kind, source = job.source_kind, job.source

    if source_kind == "url" and document_id is None:
        if last_stage not in (None, "fetch"):  # it had a document: do not make it again
            await give_up(deps, job_id, "The document of this job was deleted.")
            return "failed"
        document_id = await _fetch(deps, job_id, tenant_id, source)
        if document_id is None:
            return "skipped"

    async with deps.sessions() as session:
        stored = (
            None
            if document_id is None
            else await get_file(session, tenant_id=tenant_id, document_id=document_id)
        )
        if document_id is None or stored is None:
            await fail_job(session, job_id, "There is no stored file for this job.")
            await session.commit()
            return "failed"
        metadata = await session.scalar(
            select(Document.metadata_json).where(Document.id == document_id)
        )
        given_title = (metadata or {}).get(GIVEN_TITLE_KEY)
        filename, data, file_sha256 = stored.filename, stored.data, stored.sha256

    await _enter(deps, job_id, "parse")
    pipeline = pipeline_for(filename, settings)  # refuses an unsupported type
    async with deps.sessions() as session:
        if await is_up_to_date(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            file_sha256=file_sha256,
            version=pipeline_version(pipeline),
            given_title=given_title,
        ):
            await succeed_job(session, job_id, chunks_written=0)
            await session.commit()
            return "up_to_date"

    # CPU-bound work runs in a thread, so that the worker keeps answering
    parsed = await asyncio.to_thread(parse_file, filename, data)
    await _enter(deps, job_id, "clean")
    cleaned = await asyncio.to_thread(clean_document, parsed)
    await _enter(deps, job_id, "chunk")
    options = ChunkOptions(
        settings.CHUNK_STRATEGY,
        settings.CHUNK_MAX_TOKENS,
        settings.CHUNK_OVERLAP_TOKENS,
        settings.CHUNK_CONTEXT_HEADERS,
    )
    chunked: ChunkedDocument = await asyncio.to_thread(
        chunk_document,
        cleaned,
        kind=parsed.kind,
        filename=filename,
        counter=deps.counter,
        options=options,
        given_title=given_title,
    )

    await _enter(deps, job_id, "embed")
    vectors = await _vectors(
        deps, tenant_id, document_id, [p.chunk.embedding_text for p in chunked.chunks]
    )

    await _enter(deps, job_id, "upsert")
    async with deps.sessions() as session:
        result = await save_chunks(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            chunked=chunked,
            vectors=vectors,
            content=cleaned.text,
            file_sha256=file_sha256,
            pipeline=pipeline,
            given_title=given_title,
        )
        # save_chunks holds the document's lock: is this still the file that is stored?
        current = await session.scalar(
            select(DocumentFile.sha256).where(DocumentFile.document_id == document_id)
        )
        if current != file_sha256:
            raise _FileChanged  # leaving the block without a commit undoes the save
        if not await succeed_job(session, job_id, chunks_written=result.inserted):
            return "skipped"  # someone else finished the job: keep what they saved
        await session.commit()
    logger.info(
        "job %s: saved (%d new, %d removed, %d unchanged)",
        job_id,
        result.inserted,
        result.deleted,
        result.kept,
    )
    return "succeeded"


async def _vectors(
    deps: IngestionDeps, tenant_id: uuid.UUID, document_id: uuid.UUID, texts: Sequence[str]
) -> list[list[float]]:
    async with deps.sessions() as session:
        known = await reusable_vectors(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            texts=texts,
            embedding_model=deps.settings.JINA_EMBEDDING_MODEL,
        )
    # no database session is open while waiting for the embedding service
    missing = [text for text in texts if text not in known]
    if missing:
        result = await embed_passages(
            missing,
            http=deps.http,
            settings=deps.settings,
            counter=deps.counter,
            pacer=deps.pacer,
            sleep=deps.sleep,
        )
        known |= dict(zip(missing, result.vectors, strict=True))
        logger.info(
            "embedded %d texts, %d reused, %d tokens billed",
            len(missing),
            len(texts) - len(missing),
            result.billed_tokens,
        )
    return [known[text] for text in texts]


async def run_ingestion(deps: IngestionDeps, job_id: uuid.UUID) -> Outcome:
    try:
        return await _work(deps, job_id)
    except TransientEmbeddingError as exc:
        raise RetryableIngestion(str(exc)) from exc
    except _FileChanged as exc:
        raise RetryableIngestion("The file changed while it was being processed.") from exc
    except TransientFetchError as exc:
        raise RetryableIngestion(str(exc)) from exc
    except RetryableIngestion:
        raise
    except (AppError, FetchError, PermanentEmbeddingError) as exc:
        # these messages are written to be shown to the user
        logger.warning("job %s failed: %s", job_id, type(exc).__name__)
        await give_up(deps, job_id, str(exc))
        return "failed"
    except Exception as exc:
        # The message of an unexpected error can contain parts of a document (a database error
        # shows the values it was given): log where it happened, not what it said.
        where = "".join(traceback.format_tb(exc.__traceback__)[-6:])
        logger.error("job %s: %s\n%s", job_id, type(exc).__name__, where)
        if _is_temporary(exc):
            raise RetryableIngestion("A temporary problem. Please try again.") from None
        await give_up(deps, job_id, GENERIC_FAILURE)
        return "failed"
