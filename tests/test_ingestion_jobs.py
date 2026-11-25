"""The job status table and its allowed changes (Day 11). These run on SQLite."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.core.exceptions import DocumentNotFoundError, IngestionJobNotFoundError
from ai_service.db.models import Document, IngestionJob, Tenant
from ai_service.services.ingestion_jobs import (
    create_job,
    fail_job,
    get_job,
    set_stage,
    start_job,
    succeed_job,
)

Sessions = async_sessionmaker[AsyncSession]


async def _tenant(session: AsyncSession) -> uuid.UUID:
    tenant = Tenant(name="t")
    session.add(tenant)
    await session.flush()
    return tenant.id


async def _document(session: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    document = Document(
        tenant_id=tenant,
        title="d",
        source="d.md",
        content="c",
        content_hash=uuid.uuid4().hex,
        metadata_json={},
    )
    session.add(document)
    await session.flush()
    return document.id


@pytest.fixture
async def new_job(session_factory: Sessions) -> tuple[uuid.UUID, uuid.UUID]:
    """(tenant id, job id) of a committed queued job."""
    async with session_factory() as session:
        tenant = await _tenant(session)
        job = await create_job(session, tenant_id=tenant, source_kind="file", source="a.pdf")
        await session.commit()
        return tenant, job.id


async def _read(sessions: Sessions, tenant: uuid.UUID, job: uuid.UUID) -> IngestionJob:
    async with sessions() as session:
        return await get_job(session, tenant_id=tenant, job_id=job)


async def test_a_new_job_is_queued(
    session_factory: Sessions, new_job: tuple[uuid.UUID, uuid.UUID]
) -> None:
    job = await _read(session_factory, *new_job)
    assert (job.status, job.stage, job.error, job.attempts) == ("queued", None, None, 0)
    assert (job.started_at, job.finished_at, job.chunks_written) == (None, None, None)


async def test_the_normal_way_from_queued_to_succeeded(
    session_factory: Sessions, new_job: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with session_factory() as session:
        assert await start_job(session, new_job[1])
        assert await set_stage(session, new_job[1], "parse")
        await session.commit()
    job = await _read(session_factory, *new_job)
    assert (job.status, job.stage, job.attempts) == ("running", "parse", 1)
    assert job.started_at is not None

    async with session_factory() as session:
        assert await succeed_job(session, new_job[1], chunks_written=12)
        await session.commit()
    job = await _read(session_factory, *new_job)
    assert (job.status, job.stage, job.chunks_written) == ("succeeded", None, 12)
    assert job.finished_at is not None and job.error is None


async def test_a_retry_counts_attempts_and_keeps_the_first_start_time(
    session_factory: Sessions, new_job: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with session_factory() as session:
        await start_job(session, new_job[1])
        await set_stage(session, new_job[1], "embed")
        await session.commit()
    first = await _read(session_factory, *new_job)
    async with session_factory() as session:
        assert await start_job(session, new_job[1])
        await session.commit()
    again = await _read(session_factory, *new_job)
    assert (again.status, again.stage, again.attempts) == ("running", None, 2)
    assert again.started_at == first.started_at


async def test_a_failed_job_keeps_the_stage_and_a_short_one_line_error(
    session_factory: Sessions, new_job: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with session_factory() as session:
        await start_job(session, new_job[1])
        await set_stage(session, new_job[1], "parse")
        assert await fail_job(session, new_job[1], "The PDF\n could not be read. " + "x" * 900)
        await session.commit()
    job = await _read(session_factory, *new_job)
    assert (job.status, job.stage) == ("failed", "parse")
    assert job.error is not None and len(job.error) == 500
    assert job.error.startswith("The PDF could not be read. x")


@pytest.mark.parametrize("end", ["succeeded", "failed"])
async def test_a_finished_job_cannot_change_any_more(
    session_factory: Sessions, new_job: tuple[uuid.UUID, uuid.UUID], end: str
) -> None:
    job_id = new_job[1]
    async with session_factory() as session:
        await start_job(session, job_id)
        if end == "succeeded":
            await succeed_job(session, job_id, chunks_written=1)
        else:
            await fail_job(session, job_id, "no")
        assert not await start_job(session, job_id)
        assert not await set_stage(session, job_id, "embed")
        assert not await succeed_job(session, job_id, chunks_written=99)
        assert not await fail_job(session, job_id, "again")
        await session.commit()
    job = await _read(session_factory, *new_job)
    assert (job.status, job.attempts) == (end, 1)


async def test_a_queued_job_cannot_succeed_or_have_a_stage_but_can_fail(
    session_factory: Sessions, new_job: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with session_factory() as session:
        assert not await succeed_job(session, new_job[1], chunks_written=1)
        assert not await set_stage(session, new_job[1], "parse")
        assert await fail_job(session, new_job[1], "The job could not be queued.")
        await session.commit()
    assert (await _read(session_factory, *new_job)).status == "failed"


async def test_unknown_jobs_and_stages(
    session_factory: Sessions, new_job: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with session_factory() as session:
        assert not await start_job(session, uuid.uuid4())
        with pytest.raises(ValueError, match="unknown stage"):
            await set_stage(session, new_job[1], "dancing")


async def test_another_tenant_cannot_see_the_job_or_use_the_document(
    session_factory: Sessions, new_job: tuple[uuid.UUID, uuid.UUID]
) -> None:
    async with session_factory() as session:
        other = await _tenant(session)
        mine = await _document(session, new_job[0])
        with pytest.raises(IngestionJobNotFoundError):
            await get_job(session, tenant_id=other, job_id=new_job[1])
        with pytest.raises(DocumentNotFoundError):
            await create_job(
                session, tenant_id=other, source_kind="file", source="x", document_id=mine
            )


async def test_the_job_stays_when_its_document_is_deleted(session_factory: Sessions) -> None:
    async with session_factory() as session:
        tenant = await _tenant(session)
        document = await _document(session, tenant)
        job = await create_job(
            session, tenant_id=tenant, source_kind="file", source="d.md", document_id=document
        )
        await session.commit()
        await session.delete(await session.get(Document, document))
        await session.commit()
    kept = await _read(session_factory, tenant, job.id)
    assert (kept.document_id, kept.source) == (None, "d.md")


@pytest.mark.parametrize(
    "values",
    [
        {"status": "done"},
        {"stage": "dancing"},
        {"source_kind": "ftp"},
        {"status": "failed"},  # without an error
        {"error": "an error on a job that has not failed"},
    ],
)
async def test_the_database_itself_refuses_wrong_values(
    session_factory: Sessions, values: dict[str, str]
) -> None:
    async with session_factory() as session:
        tenant = await _tenant(session)
        session.add(
            IngestionJob(**{"tenant_id": tenant, "source_kind": "file", "source": "a"} | values)
        )
        with pytest.raises(IntegrityError):
            await session.flush()
