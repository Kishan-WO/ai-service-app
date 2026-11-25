"""Creating ingestion jobs and moving them from one status to the next (Day 11).

Every change is one UPDATE that names the statuses it may start from, so the database decides:
a finished job cannot start again, and of two attempts to finish a job only one succeeds.
Each function returns False when the change was not allowed.

None of them commits. The worker commits a stage so that the status endpoint can see it, and
can commit "succeeded" in the same transaction as the chunks it saved.

This does NOT stop two workers from running the same job at the same time ("running" may
start again, because ARQ can run a job twice and a retry starts it again). Saving the chunks
has to be safe to repeat.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.core.exceptions import DocumentNotFoundError, IngestionJobNotFoundError
from ai_service.db.models import Document, IngestionJob
from ai_service.db.models.ingestion_job import JOB_STAGES

_ERROR_LENGTH = 500


async def create_job(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_kind: str,
    source: str,
    document_id: uuid.UUID | None = None,
) -> IngestionJob:
    """A new job with status "queued"."""
    if document_id is not None:
        owned = await session.scalar(
            select(Document.id).where(Document.id == document_id, Document.tenant_id == tenant_id)
        )
        if owned is None:
            raise DocumentNotFoundError()
    job = IngestionJob(
        tenant_id=tenant_id,
        document_id=document_id,
        source_kind=source_kind,
        source=source[:2048],
    )
    session.add(job)
    await session.flush()
    return job


async def get_job(
    session: AsyncSession, *, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> IngestionJob:
    """For the user's side: a job of another tenant is reported like a missing one."""
    job = await session.scalar(
        select(IngestionJob)
        .where(IngestionJob.id == job_id, IngestionJob.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if job is None:
        raise IngestionJobNotFoundError()
    return job


async def _change(
    session: AsyncSession, job_id: uuid.UUID, allowed_from: Sequence[str], **values: Any
) -> bool:
    result = await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id, IngestionJob.status.in_(allowed_from))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return cast("CursorResult[Any]", result).rowcount == 1


async def start_job(session: AsyncSession, job_id: uuid.UUID) -> bool:
    """An attempt begins. False means there is nothing to do: finished, failed or missing."""
    return await _change(
        session,
        job_id,
        ("queued", "running"),
        status="running",
        stage=None,
        attempts=IngestionJob.attempts + 1,
        started_at=func.coalesce(IngestionJob.started_at, datetime.now(UTC)),
    )


async def attach_document(session: AsyncSession, job_id: uuid.UUID, document_id: uuid.UUID) -> bool:
    """A link job gets its document, once, when the file has been downloaded."""
    result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job_id,
            IngestionJob.status == "running",
            IngestionJob.document_id.is_(None),
        )
        .values(document_id=document_id)
        .execution_options(synchronize_session=False)
    )
    return cast("CursorResult[Any]", result).rowcount == 1


async def set_stage(session: AsyncSession, job_id: uuid.UUID, stage: str) -> bool:
    if stage not in JOB_STAGES:
        raise ValueError(f"unknown stage: {stage}")
    return await _change(session, job_id, ("running",), stage=stage)


async def succeed_job(session: AsyncSession, job_id: uuid.UUID, *, chunks_written: int) -> bool:
    return await _change(
        session,
        job_id,
        ("running",),
        status="succeeded",
        stage=None,
        chunks_written=chunks_written,
        finished_at=datetime.now(UTC),
    )


async def fail_job(session: AsyncSession, job_id: uuid.UUID, error: str) -> bool:
    """For a job that will not be tried again. `stage` stays, to show where it stopped.
    "queued" is allowed for a job that could not be put on the queue."""
    message = " ".join(error.split())[:_ERROR_LENGTH] or "The document could not be processed."
    return await _change(
        session,
        job_id,
        ("queued", "running"),
        status="failed",
        error=message,
        finished_at=datetime.now(UTC),
    )
