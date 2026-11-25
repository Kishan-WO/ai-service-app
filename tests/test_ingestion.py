"""The ingestion job and its ARQ wrapper (Day 11, task 1). SQLite and a fake Jina."""

import asyncio
import dataclasses
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from arq.worker import Function, Retry
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service import worker
from ai_service.core.config import Settings
from ai_service.db.models import Chunk, Document, DocumentFile, IngestionJob, Tenant
from ai_service.services import ingestion
from ai_service.services.embedding import Pacer
from ai_service.services.file_store import save_file
from ai_service.services.ingest_request import accept_link
from ai_service.services.ingestion import (
    GENERIC_FAILURE,
    IngestionDeps,
    RetryableIngestion,
    run_ingestion,
)
from ai_service.services.ingestion_jobs import attach_document, create_job
from ai_service.services.job_queue import INGEST_JOB, enqueue_ingestion
from ai_service.services.url_fetch import (
    BlockedURLError,
    FetchedFile,
    PermanentFetchError,
    TransientFetchError,
    fetch_url,
)

Sessions = async_sessionmaker[AsyncSession]
TEXT = b"# Guide\n\n## A\n\naa bb cc\n\n## B\n\ndd ee ff"


class FakeCounter:
    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [word.span() for word in re.finditer(r"\S+", text)]

    def count(self, text: str) -> int:
        return len(text.split())


class FakeJina:
    """Good vectors, unless a status code or an action is set."""

    def __init__(self) -> None:
        self.inputs: list[list[str]] = []
        self.status = 200
        self.during: Callable[[], Awaitable[None]] | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        self.inputs.append(texts)
        if self.during:
            await self.during()
        if self.status != 200:
            return httpx.Response(self.status)
        data = [
            {"index": i, "embedding": [float(len(t))] + [1.0] * 1023} for i, t in enumerate(texts)
        ]
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 5}})


async def no_sleep(_: float) -> None:
    return None


class World:
    def __init__(self, sessions: Sessions, http: httpx.AsyncClient, jina: FakeJina) -> None:
        self.sessions = sessions
        self.jina = jina
        settings = Settings(JINA_API_KEY="fake", CHUNK_MAX_TOKENS=50, CHUNK_OVERLAP_TOKENS=0)
        self.deps = IngestionDeps(
            sessions=sessions,
            http=http,
            settings=settings,
            counter=FakeCounter(),
            pacer=Pacer(10**9, 10**9, sleep=no_sleep),
            sleep=no_sleep,
        )
        self.tenant = uuid.uuid4()
        self.document = uuid.uuid4()

    async def setup(self) -> None:
        async with self.sessions() as session:
            session.add(Tenant(id=self.tenant, name="t"))
            await session.flush()
            session.add(
                Document(
                    id=self.document,
                    tenant_id=self.tenant,
                    title="upload",
                    source="guide.md",
                    content="",
                    content_hash="none",
                    metadata_json={},
                )
            )
            await session.commit()

    async def store(self, data: bytes, filename: str = "guide.md") -> None:
        async with self.sessions() as session:
            await save_file(
                session,
                tenant_id=self.tenant,
                document_id=self.document,
                filename=filename,
                content_type=None,
                data=data,
            )
            await session.commit()

    async def new_job(self, *, with_document: bool = True) -> uuid.UUID:
        async with self.sessions() as session:
            job = await create_job(
                session,
                tenant_id=self.tenant,
                source_kind="file",
                source="guide.md",
                document_id=self.document if with_document else None,
            )
            await session.commit()
            return job.id

    async def job(self, job_id: uuid.UUID) -> IngestionJob:
        async with self.sessions() as session:
            return await session.get_one(IngestionJob, job_id)

    async def chunks(self) -> list[str]:
        async with self.sessions() as session:
            return list(await session.scalars(select(Chunk.text).order_by(Chunk.chunk_index)))


@pytest.fixture
async def world(session_factory: Sessions) -> Any:
    jina = FakeJina()
    async with httpx.AsyncClient(transport=httpx.MockTransport(jina)) as http:
        made = World(session_factory, http, jina)
        await made.setup()
        await made.store(TEXT)
        yield made


# ── the whole way ───────────────────────────────────────────────────────


async def test_a_job_saves_the_chunks_and_says_so(world: World) -> None:
    job_id = await world.new_job()
    seen: list[tuple[str, str | None]] = []

    async def look() -> None:  # what the status endpoint would show while Jina works
        job = await world.job(job_id)
        seen.append((job.status, job.stage))

    world.jina.during = look
    assert await run_ingestion(world.deps, job_id) == "succeeded"
    assert seen == [("running", "embed")]
    job = await world.job(job_id)
    assert (job.status, job.stage, job.chunks_written, job.attempts) == ("succeeded", None, 2, 1)
    saved = await world.chunks()
    assert len(saved) == 2
    assert saved[0].endswith("aa bb cc") and saved[1] == "## B\n\ndd ee ff"


async def test_the_same_file_again_does_no_work(world: World) -> None:
    await run_ingestion(world.deps, await world.new_job())
    second = await world.new_job()
    assert await run_ingestion(world.deps, second) == "up_to_date"
    job = await world.job(second)
    assert (job.status, job.chunks_written) == ("succeeded", 0)
    assert len(world.jina.inputs) == 1  # only the first job asked for vectors


async def test_the_title_the_user_gave_is_used(world: World) -> None:
    async with world.sessions() as session:
        document = await session.get_one(Document, world.document)
        document.metadata_json = {"given_title": "My title"}
        await session.commit()
    await run_ingestion(world.deps, await world.new_job())
    async with world.sessions() as session:
        assert (await session.get_one(Document, world.document)).title == "My title"


async def test_a_finished_or_unknown_job_is_left_alone(world: World) -> None:
    job_id = await world.new_job()
    await run_ingestion(world.deps, job_id)
    before = (await world.job(job_id)).updated_at
    assert await run_ingestion(world.deps, job_id) == "skipped"  # delivered twice
    assert await run_ingestion(world.deps, uuid.uuid4()) == "skipped"
    assert (await world.job(job_id)).updated_at == before
    assert len(world.jina.inputs) == 1


# ── permanent failures ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("broken.pdf", b"not a pdf", "The PDF could not be read."),
        ("a.md", b"\xff\xfe\x00", "The file is not valid UTF-8 text."),
        ("a.md", b"   \n  ", "The file contains no text."),
    ],
)
async def test_a_bad_file_fails_once_with_a_readable_reason(
    world: World, filename: str, data: bytes, message: str
) -> None:
    await world.store(data, filename)
    job_id = await world.new_job()
    assert await run_ingestion(world.deps, job_id) == "failed"
    job = await world.job(job_id)
    assert (job.status, job.error, job.attempts) == ("failed", message, 1)
    assert job.stage in ("parse", "clean") and world.jina.inputs == []


async def test_an_unsupported_type_fails(world: World) -> None:
    await world.store(b"x", "a.docx")
    job_id = await world.new_job()
    assert await run_ingestion(world.deps, job_id) == "failed"
    assert (await world.job(job_id)).stage == "parse"


async def test_a_job_without_a_stored_file_fails(world: World) -> None:
    job_id = await world.new_job(with_document=False)
    assert await run_ingestion(world.deps, job_id) == "failed"
    assert (await world.job(job_id)).error == "There is no stored file for this job."


async def test_a_refused_embedding_request_fails_and_keeps_the_old_version(world: World) -> None:
    await run_ingestion(world.deps, await world.new_job())
    before = await world.chunks()
    await world.store(TEXT + b" changed")
    world.jina.status = 401
    job_id = await world.new_job()
    assert await run_ingestion(world.deps, job_id) == "failed"
    job = await world.job(job_id)
    assert (job.stage, job.error) == (
        "embed",
        "The embedding service refused the request (HTTP 401).",
    )
    assert await world.chunks() == before


async def test_an_unexpected_error_says_nothing_private(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="ai_service")

    # Put together at run time, as a document's text would be: the log shows lines of SOURCE
    # code (where the error happened), so a literal in the raising line would appear there.
    private = " ".join(["PRIVATE", "TEXT", "of", "a", "document"])

    async def boom() -> None:
        raise RuntimeError(private)

    world.jina.during = boom
    job_id = await world.new_job()
    assert await run_ingestion(world.deps, job_id) == "failed"
    assert (await world.job(job_id)).error == GENERIC_FAILURE
    ours = "".join(r.getMessage() for r in caplog.records if r.name.startswith("ai_service"))
    assert "RuntimeError" in ours and private not in ours


async def test_the_chunks_and_succeeded_are_saved_together(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: object, **__: object) -> bool:
        raise RuntimeError("fails after the chunks were written")

    monkeypatch.setattr(ingestion, "succeed_job", broken)
    job_id = await world.new_job()
    assert await run_ingestion(world.deps, job_id) == "failed"
    assert await world.chunks() == []
    assert (await world.job(job_id)).stage == "upsert"


# ── temporary problems ──────────────────────────────────────────────────


async def test_a_temporary_problem_leaves_the_job_running_for_another_try(world: World) -> None:
    job_id = await world.new_job()
    world.jina.status = 503
    with pytest.raises(RetryableIngestion, match="not available"):
        await run_ingestion(world.deps, job_id)
    job = await world.job(job_id)
    assert (job.status, job.stage, job.error) == ("running", "embed", None)

    world.jina.status = 200
    assert await run_ingestion(world.deps, job_id) == "succeeded"
    assert (await world.job(job_id)).attempts == 2


async def test_a_lost_database_connection_is_temporary(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def lost(*_: object, **__: object) -> None:
        raise OperationalError("select", {}, ConnectionError("gone"))

    monkeypatch.setattr(ingestion, "save_chunks", lost)
    job_id = await world.new_job()
    with pytest.raises(RetryableIngestion):
        await run_ingestion(world.deps, job_id)
    assert (await world.job(job_id)).status == "running"


async def test_a_file_replaced_during_the_job_is_not_overwritten_by_the_old_one(
    world: World,
) -> None:
    async def replace_file() -> None:
        await world.store(TEXT + b"\n\nnewer")

    world.jina.during = replace_file
    job_id = await world.new_job()
    with pytest.raises(RetryableIngestion, match="file changed"):
        await run_ingestion(world.deps, job_id)
    assert await world.chunks() == []


# ── the ARQ wrapper ─────────────────────────────────────────────────────


def ctx(world: World, job_try: int = 1, **settings: object) -> dict[str, Any]:
    deps = world.deps
    if settings:
        deps = IngestionDeps(
            **{**vars(deps), "settings": deps.settings.model_copy(update=settings)}
        )
    return {"deps": deps, "job_try": job_try}


async def test_the_wrapper_returns_one_word(world: World) -> None:
    assert await worker.ingest_document(ctx(world), str(await world.new_job())) == "succeeded"


@pytest.mark.parametrize(("job_try", "seconds"), [(1, 30), (2, 60), (3, 90)])
async def test_the_wrapper_asks_for_a_later_retry(world: World, job_try: int, seconds: int) -> None:
    world.jina.status = 429
    with pytest.raises(Retry) as asked:
        await worker.ingest_document(ctx(world, job_try), str(await world.new_job()))
    assert asked.value.defer_score == seconds * 1000


async def test_on_the_last_attempt_the_job_is_marked_failed(world: World) -> None:
    world.jina.status = 503
    job_id = await world.new_job()
    assert await worker.ingest_document(ctx(world, job_try=4), str(job_id)) == "failed"
    job = await world.job(job_id)
    assert job.status == "failed"
    assert job.error is not None and job.error.endswith("Gave up after 4 attempts.")


async def test_a_job_over_the_time_limit_is_marked_failed(world: World) -> None:
    async def slow() -> None:
        await asyncio.sleep(5)

    world.jina.during = slow
    job_id = await world.new_job()
    context = ctx(world, INGEST_TIMEOUT_SECONDS=0.3)
    assert await worker.ingest_document(context, str(job_id)) == "failed"
    job = await world.job(job_id)
    assert (job.error, job.stage) == ("Processing took too long and was stopped.", "embed")


def test_arq_is_given_more_time_than_the_job_gives_itself() -> None:
    ingest = next(
        f for f in worker.WorkerSettings.functions if getattr(f, "name", "") == INGEST_JOB
    )
    assert isinstance(ingest, Function)
    assert ingest.max_tries == Settings().INGEST_MAX_TRIES == 4
    assert ingest.timeout_s is not None
    assert ingest.timeout_s > Settings().INGEST_TIMEOUT_SECONDS > 300


async def test_only_the_job_id_goes_on_the_queue() -> None:
    sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Queue:
        async def enqueue_job(self, *args: object, **kwargs: object) -> object:
            sent.append((args, kwargs))
            return object() if len(sent) == 1 else None

    job_id = uuid.uuid4()
    assert await enqueue_ingestion(Queue(), job_id) is True  # type: ignore[arg-type]
    assert await enqueue_ingestion(Queue(), job_id) is False  # type: ignore[arg-type]
    assert sent[0] == ((INGEST_JOB, str(job_id)), {"_job_id": f"ingest:{job_id}"})


def test_the_libraries_cannot_quote_a_file_in_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arq_logger = logging.getLogger("arq")
    monkeypatch.setattr(arq_logger, "propagate", True)
    for name in ("pypdf", "httpcore", "httpx", "aiosqlite"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.DEBUG)
    worker.quiet_libraries()
    assert not logging.getLogger("pypdf._reader").isEnabledFor(logging.ERROR)
    assert not logging.getLogger("httpcore.http11").isEnabledFor(logging.DEBUG)
    assert arq_logger.propagate is False


# ── links ───────────────────────────────────────────────────────────────

LINK = "https://files.example/docs/web.md"
WEB = b"# Web page\n\n## C\n\ngg hh ii\n\n## D\n\njj kk ll"


class FakeFetch:
    """Gives `body`, unless an error or an action is set."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []
        self.body = WEB
        self.error: Exception | None = None
        self.during: Callable[[], Awaitable[None]] | None = None

    async def __call__(self, url: str, *, max_bytes: int) -> FetchedFile:
        self.asked.append((url, max_bytes))
        if self.during:
            await self.during()
        if self.error:
            raise self.error
        return FetchedFile(self.body, "web.md", "text/markdown", url)


@pytest.fixture
def fetch(world: World) -> FakeFetch:
    fake = FakeFetch()
    world.deps = dataclasses.replace(world.deps, fetch=fake)
    return fake


async def link_job(world: World, link: str = LINK) -> uuid.UUID:
    async with world.sessions() as session:
        job = await accept_link(session, tenant_id=world.tenant, url=link)
        await session.commit()
        return job.id


async def documents_of(world: World, source: str) -> list[Document]:
    async with world.sessions() as session:
        return list(await session.scalars(select(Document).where(Document.source == source)))


async def all_documents(world: World) -> int:
    async with world.sessions() as session:
        return len(list(await session.scalars(select(Document.id))))


async def test_a_link_is_downloaded_stored_and_processed(world: World, fetch: FakeFetch) -> None:
    job_id = await link_job(world)
    during: list[tuple[str, str | None, uuid.UUID | None]] = []

    async def look() -> None:
        job = await world.job(job_id)
        during.append((job.status, job.stage, job.document_id))

    fetch.during = look
    assert await run_ingestion(world.deps, job_id) == "succeeded"
    assert during == [("running", "fetch", None)]
    assert fetch.asked == [(LINK, world.deps.settings.MAX_UPLOAD_SIZE_BYTES)]
    [document] = await documents_of(world, LINK)
    assert (document.tenant_id, document.title) == (world.tenant, "Web page")
    async with world.sessions() as session:
        stored = await session.get_one(DocumentFile, document.id)
    assert (stored.data, stored.filename, stored.content_type) == (WEB, "web.md", "text/markdown")
    job = await world.job(job_id)
    assert (job.status, job.document_id, job.chunks_written) == ("succeeded", document.id, 2)
    assert len(await world.chunks()) == 2


async def test_the_same_link_again_changes_nothing(world: World, fetch: FakeFetch) -> None:
    await run_ingestion(world.deps, await link_job(world))
    before = await world.chunks()
    assert await run_ingestion(world.deps, await link_job(world)) == "up_to_date"
    assert len(fetch.asked) == 2  # downloaded again, to see whether it changed
    assert len(world.jina.inputs) == 1  # but not embedded again
    assert await world.chunks() == before and len(await documents_of(world, LINK)) == 1


async def test_a_changed_file_behind_a_link_updates_the_same_document(
    world: World, fetch: FakeFetch
) -> None:
    await run_ingestion(world.deps, await link_job(world))
    [first] = await documents_of(world, LINK)
    fetch.body = WEB + b" mm"
    assert await run_ingestion(world.deps, await link_job(world)) == "succeeded"
    [second] = await documents_of(world, LINK)
    assert second.id == first.id and (await world.chunks())[-1].endswith("jj kk ll mm")


async def test_the_real_downloader_is_the_default_and_its_block_fails_the_job(
    world: World,
) -> None:
    assert world.deps.fetch is fetch_url
    job_id = await link_job(world, "http://127.0.0.1/secret.md")  # refused before connecting
    assert await run_ingestion(world.deps, job_id) == "failed"
    job = await world.job(job_id)
    assert (job.stage, job.error, job.document_id) == (
        "fetch",
        "This address is not allowed.",
        None,
    )
    assert await all_documents(world) == 1 and world.jina.inputs == []


@pytest.mark.parametrize(
    "error",
    [
        BlockedURLError("This address is not allowed."),
        PermanentFetchError("The server of the link answered HTTP 404."),
    ],
)
async def test_a_link_that_cannot_work_is_tried_once(
    world: World, fetch: FakeFetch, error: Exception
) -> None:
    fetch.error = error
    job_id = await link_job(world)
    assert await run_ingestion(world.deps, job_id) == "failed"
    job = await world.job(job_id)
    assert (job.stage, job.error, job.attempts) == ("fetch", str(error), 1)
    assert await all_documents(world) == 1


async def test_a_temporary_download_problem_is_tried_again(world: World, fetch: FakeFetch) -> None:
    fetch.error = TransientFetchError("The link could not be downloaded.")
    job_id = await link_job(world)
    with pytest.raises(RetryableIngestion, match="could not be downloaded"):
        await run_ingestion(world.deps, job_id)
    job = await world.job(job_id)
    assert (job.status, job.stage, job.document_id) == ("running", "fetch", None)

    fetch.error = None
    assert await run_ingestion(world.deps, job_id) == "succeeded"
    assert (await world.job(job_id)).attempts == 2


async def test_the_wrapper_retries_a_download_and_gives_up_in_the_end(
    world: World, fetch: FakeFetch
) -> None:
    fetch.error = TransientFetchError("Downloading the link took too long.")
    job_id = await link_job(world)
    with pytest.raises(Retry):
        await worker.ingest_document(ctx(world, job_try=1), str(job_id))
    assert await worker.ingest_document(ctx(world, job_try=4), str(job_id)) == "failed"
    job = await world.job(job_id)
    assert job.error == "Downloading the link took too long. Gave up after 4 attempts."


async def test_a_later_attempt_uses_the_stored_file(world: World, fetch: FakeFetch) -> None:
    world.jina.status = 503
    job_id = await link_job(world)
    with pytest.raises(RetryableIngestion):
        await run_ingestion(world.deps, job_id)
    assert (await world.job(job_id)).stage == "embed"
    world.jina.status = 200
    fetch.error = PermanentFetchError("the link is gone now")
    assert await run_ingestion(world.deps, job_id) == "succeeded"
    assert len(fetch.asked) == 1


async def test_a_document_deleted_between_attempts_is_not_made_again(
    world: World, fetch: FakeFetch
) -> None:
    world.jina.status = 503
    job_id = await link_job(world)
    with pytest.raises(RetryableIngestion):
        await run_ingestion(world.deps, job_id)
    async with world.sessions() as session:  # what ON DELETE SET NULL does to the job
        await session.execute(
            update(IngestionJob).where(IngestionJob.id == job_id).values(document_id=None)
        )
        await session.commit()
    world.jina.status = 200
    assert await run_ingestion(world.deps, job_id) == "failed"
    assert (await world.job(job_id)).error == "The document of this job was deleted."
    assert len(fetch.asked) == 1


async def test_a_link_with_the_same_file_as_another_document_fails(
    world: World, fetch: FakeFetch
) -> None:
    await run_ingestion(world.deps, await link_job(world))
    job_id = await link_job(world, "https://other.example/copy.md")
    assert await run_ingestion(world.deps, job_id) == "failed"
    job = await world.job(job_id)
    assert job.error is not None and job.error.startswith("This document was already uploaded")
    assert job.stage == "fetch" and await documents_of(world, "https://other.example/copy.md") == []


async def test_an_empty_download_fails(world: World, fetch: FakeFetch) -> None:
    fetch.body = b""
    job_id = await link_job(world)
    assert await run_ingestion(world.deps, job_id) == "failed"
    assert (await world.job(job_id)).error == "The file is empty."


async def test_a_job_given_a_document_meanwhile_leaves_nothing_behind(
    world: World, fetch: FakeFetch
) -> None:
    job_id = await link_job(world)

    async def another_worker_is_first() -> None:
        async with world.sessions() as session:
            assert await attach_document(session, job_id, world.document)
            await session.commit()

    fetch.during = another_worker_is_first
    assert await run_ingestion(world.deps, job_id) == "skipped"
    assert await all_documents(world) == 1
    assert (await world.job(job_id)).document_id == world.document


async def test_two_jobs_saving_the_same_file_at_once_try_again(
    world: World, fetch: FakeFetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def collide(*_: object, **__: object) -> None:
        raise IntegrityError("insert", {}, Exception("unique"))

    monkeypatch.setattr(ingestion, "save_document_file", collide)
    job_id = await link_job(world)
    with pytest.raises(RetryableIngestion, match="same file"):
        await run_ingestion(world.deps, job_id)
    assert (await world.job(job_id)).status == "running"


async def test_the_log_never_shows_the_link_or_the_downloaded_text(
    world: World, fetch: FakeFetch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="ai_service")
    await run_ingestion(world.deps, await link_job(world, LINK + "?token=SECRET-TOKEN"))
    fetch.error = PermanentFetchError("The server of the link answered HTTP 403.")
    await run_ingestion(world.deps, await link_job(world, LINK + "?token=SECRET-TOKEN"))
    ours = "".join(r.getMessage() for r in caplog.records if r.name.startswith("ai_service"))
    assert "job " in ours
    assert "SECRET-TOKEN" not in ours and "gg hh ii" not in ours
