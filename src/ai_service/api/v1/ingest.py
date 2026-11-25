"""Ingestion (Day 11, task 1): POST /ingest accepts a file or a link and lets the worker make
it searchable; GET /ingest/{job_id} says how far that job is."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.api.deps import AppSettings, CurrentUser, DbSession, JobQueueDep
from ai_service.core.exceptions import InvalidIngestRequestError, QueueUnavailableError
from ai_service.schemas.ingest import IngestAccepted, IngestJobStatus
from ai_service.services import document_service
from ai_service.services.ingest_request import Accepted, accept_file, accept_link
from ai_service.services.ingestion_jobs import fail_job, get_job
from ai_service.services.job_queue import JobQueue

router = APIRouter(prefix="/ingest", tags=["ingestion"])
logger = logging.getLogger("ai_service.ingest")


async def _enqueue(session: AsyncSession, queue: JobQueue, job_id: uuid.UUID, what: str) -> None:
    """Call only after the job is committed: the worker must be able to see its row."""
    try:
        await queue.enqueue_ingestion(job_id)
    except Exception as exc:  # Redis is down or unreachable
        logger.warning("job %s could not be queued: %s", job_id, type(exc).__name__)
        # without this, the job would say "queued" for ever
        await fail_job(
            session, job_id, f"The job could not be queued. Please send the {what} again."
        )
        await session.commit()
        raise QueueUnavailableError(job_id, what) from exc


@router.post("", response_model=IngestAccepted, status_code=status.HTTP_202_ACCEPTED)
async def ingest(
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
    queue: JobQueueDep,
    file: Annotated[
        UploadFile | None, File(description=".txt, .md or .pdf, up to 20 MB. Or send `url`.")
    ] = None,
    url: Annotated[
        str | None,
        Form(
            max_length=2048,
            description="A public http or https link to a .txt, .md or .pdf file, up to 512 "
            "characters. Or send `file`.",
        ),
    ] = None,
    title: Annotated[str | None, Form(max_length=255, description="For a file only.")] = None,
    source: Annotated[
        str | None,
        Form(
            max_length=512,
            description="For a file only. An identifier such as a path. Sending a file with the "
            "source of an existing document replaces that document's file.",
        ),
    ] = None,
) -> IngestAccepted:
    """Queues a job. 202: accepted, not yet searchable."""
    url = (url or "").strip() or None
    if (file is None) == (url is None):
        raise InvalidIngestRequestError("Send either a file or a url.")
    tenant_id = user.tenant_id  # always from the login, never from the request body

    if file is None:
        assert url is not None
        if (title or "").strip() or (source or "").strip():
            raise InvalidIngestRequestError(
                "title and source cannot be used with a url. The url is the document's source."
            )
        # nothing is downloaded here: a slow link must not hold up the request
        job = await accept_link(session, tenant_id=tenant_id, url=url)
        await session.commit()
        job_id, link = job.id, job.source
        await _enqueue(session, queue, job_id, "link")
        return IngestAccepted(job_id=job_id, document_id=None, source=link, new_document=None)

    data = await document_service.read_upload(file, settings.MAX_UPLOAD_SIZE_BYTES)
    filename, content_type = file.filename or "upload", file.content_type

    async def accept() -> Accepted:
        return await accept_file(
            session,
            tenant_id=tenant_id,
            filename=filename,
            data=data,
            content_type=content_type,
            title=title,
            source=source,
        )

    try:
        accepted = await accept()
        await session.commit()
    except IntegrityError:  # the same new file arrived twice at the same moment
        await session.rollback()
        accepted = await accept()  # now it finds the document the other request made
        await session.commit()

    job_id = accepted.job.id
    await _enqueue(session, queue, job_id, "file")
    return IngestAccepted(
        job_id=job_id,
        document_id=accepted.document.id,
        source=accepted.document.source,
        new_document=accepted.new_document,
    )


@router.get("/{job_id}", response_model=IngestJobStatus)
async def job_status(job_id: uuid.UUID, user: CurrentUser, session: DbSession) -> IngestJobStatus:
    """The job's row in the database. Another tenant's job is "not found", like a missing one."""
    job = await get_job(session, tenant_id=user.tenant_id, job_id=job_id)
    return IngestJobStatus.model_validate(job)
