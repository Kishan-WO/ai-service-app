"""POST /ingest for files (Day 11, task 1). SQLite and a fake queue."""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.api.deps import get_job_queue
from ai_service.core.config import get_settings
from ai_service.db.models import Chunk, Document, DocumentFile, IngestionJob
from ai_service.db.session import engine as app_engine
from ai_service.services.ingestion_jobs import fail_job, set_stage, start_job, succeed_job
from ai_service.services.job_queue import JobQueue

URL = "/api/v1/ingest"
Sessions = async_sessionmaker[AsyncSession]
Register = Callable[[str], Awaitable[dict[str, str]]]


class FakeQueue:
    """Records what is queued, and whether the job row was committed by then."""

    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions
        self.queued: list[uuid.UUID] = []
        self.status_when_queued: list[str | None] = []
        self.down = False

    async def enqueue_ingestion(self, job_id: uuid.UUID) -> bool:
        async with self.sessions() as session:
            job = await session.get(IngestionJob, job_id)
        self.status_when_queued.append(job.status if job else None)
        if self.down:
            raise ConnectionError("redis is down: secret-host:6379")
        self.queued.append(job_id)
        return True


@pytest.fixture
def queue(app: FastAPI, session_factory: Sessions) -> FakeQueue:
    fake = FakeQueue(session_factory)
    app.dependency_overrides[get_job_queue] = lambda: fake
    return fake


async def send(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    data: bytes = b"# Guide\n\nhello",
    filename: str = "guide.md",
    **form: str,
) -> httpx.Response:
    return await client.post(
        URL, headers=headers, files={"file": (filename, data, "text/markdown")}, data=form
    )


async def accepted(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def count(sessions: Sessions, model: type) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def document(sessions: Sessions, document_id: str) -> Document:
    async with sessions() as session:
        return await session.get_one(Document, uuid.UUID(document_id))


async def test_a_file_is_saved_and_a_job_is_queued(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    data = b"# Guide\n\nhello"
    body = await accepted(await send(client, auth_headers, data, title="My guide"))

    assert body["status"] == "queued" and body["new_document"] is True
    assert body["source"] == "guide.md"
    assert queue.queued == [uuid.UUID(body["job_id"])]
    assert queue.status_when_queued == ["queued"]  # committed BEFORE it was queued
    found = await document(session_factory, body["document_id"])
    assert (found.title, found.content, found.content_hash) == (
        "My guide",
        "",  # nothing is parsed in the request
        hashlib.sha256(data).hexdigest(),
    )
    assert found.metadata_json["given_title"] == "My guide"
    async with session_factory() as session:
        stored = await session.get_one(DocumentFile, found.id)
        job = await session.get_one(IngestionJob, uuid.UUID(body["job_id"]))
    assert (stored.data, stored.filename, stored.tenant_id) == (data, "guide.md", found.tenant_id)
    assert (job.tenant_id, job.document_id, job.source_kind) == (found.tenant_id, found.id, "file")
    assert await count(session_factory, Chunk) == 0


async def test_the_same_bytes_again_are_the_same_document_with_a_new_job(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    first = await accepted(await send(client, auth_headers, title="Kept"))
    again = await accepted(await send(client, auth_headers, filename="copy.md"))
    assert (again["document_id"], again["new_document"]) == (first["document_id"], False)
    assert again["job_id"] != first["job_id"] and again["source"] == "guide.md"
    assert await count(session_factory, Document) == 1
    assert await count(session_factory, DocumentFile) == 1
    kept = await document(session_factory, first["document_id"])
    assert kept.metadata_json["given_title"] == "Kept"  # no new title: the old one stays


async def test_two_files_with_one_name_do_not_replace_each_other(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    one = await accepted(await send(client, auth_headers, b"one", "index.md"))
    two = await accepted(await send(client, auth_headers, b"two", "index.md"))
    assert one["document_id"] != two["document_id"] and two["new_document"] is True


async def test_the_same_source_replaces_the_documents_file(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    path = "docs/tutorial/cors.md"
    first = await accepted(await send(client, auth_headers, b"version 1", source=path))
    second = await accepted(await send(client, auth_headers, b"version 2", source=path))
    assert (second["document_id"], second["new_document"], second["source"]) == (
        first["document_id"],
        False,
        path,  # the full path is kept
    )
    found = await document(session_factory, first["document_id"])
    assert found.content_hash == hashlib.sha256(b"version 2").hexdigest()
    async with session_factory() as session:
        assert (await session.get_one(DocumentFile, found.id)).data == b"version 2"


async def test_a_file_that_another_document_has_is_refused(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    first = await accepted(await send(client, auth_headers, b"same", source="a.md"))
    other = await send(client, auth_headers, b"same", source="b.md")
    assert other.status_code == 409
    assert other.json()["error"]["details"] == {"document_id": first["document_id"]}
    assert await count(session_factory, Document) == 1 and len(queue.queued) == 1


async def test_a_source_that_two_documents_have_is_not_guessed(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    await send(client, auth_headers, b"one", "index.md")
    await send(client, auth_headers, b"two", "index.md")
    refused = await send(client, auth_headers, b"three", source="index.md")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "AMBIGUOUS_SOURCE"
    assert await count(session_factory, IngestionJob) == 2


async def test_tenants_never_share_or_replace_documents(
    client: httpx.AsyncClient, register: Register, queue: FakeQueue, session_factory: Sessions
) -> None:
    owner, other = await register("owner@corp.com"), await register("other@corp.com")
    mine = await accepted(await send(client, owner, b"same bytes", source="a.md"))
    theirs = await accepted(await send(client, other, b"same bytes", source="a.md"))
    assert theirs["new_document"] is True and theirs["document_id"] != mine["document_id"]
    async with session_factory() as session:
        rows = (await session.execute(select(Document.id, Document.tenant_id))).all()
    assert len({tenant for _, tenant in rows}) == 2


async def test_a_tenant_in_the_request_body_is_ignored(
    client: httpx.AsyncClient,
    register: Register,
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    owner, victim = await register("a@corp.com"), await register("b@corp.com")
    victim_id = (await client.get("/api/v1/auth/me", headers=victim)).json()["tenant"]["id"]
    body = await accepted(await send(client, owner, tenant_id=victim_id))
    found = await document(session_factory, body["document_id"])
    assert str(found.tenant_id) != victim_id


@pytest.mark.parametrize(
    ("filename", "data", "status", "code"),
    [
        ("a.docx", b"x", 415, "UNSUPPORTED_FILE_TYPE"),
        ("a.md", b"", 422, "INVALID_DOCUMENT"),
        ("noextension", b"x", 415, "UNSUPPORTED_FILE_TYPE"),
    ],
)
async def test_a_refused_file_saves_and_queues_nothing(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
    filename: str,
    data: bytes,
    status: int,
    code: str,
) -> None:
    response = await send(client, auth_headers, data, filename)
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    for model in (Document, DocumentFile, IngestionJob):
        assert await count(session_factory, model) == 0
    assert queue.queued == []


async def test_the_size_limit(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    small = get_settings().model_copy(update={"MAX_UPLOAD_SIZE_BYTES": 100})
    app.dependency_overrides[get_settings] = lambda: small
    assert (await send(client, auth_headers, b"x" * 101)).status_code == 413
    assert (await send(client, auth_headers, b"x" * 100)).status_code == 202
    assert await count(session_factory, Document) == 1


async def test_no_login_no_ingestion(client: httpx.AsyncClient, queue: FakeQueue) -> None:
    assert (await send(client, {})).status_code == 401
    assert queue.queued == []


async def test_a_binary_pdf_is_accepted_without_being_parsed(
    client: httpx.AsyncClient, auth_headers: dict[str, str], queue: FakeQueue
) -> None:
    """The worker finds out that it is broken, and the job says so."""
    assert (await send(client, auth_headers, b"\x00not a pdf", "x.pdf")).status_code == 202


async def test_when_the_queue_is_down_the_job_is_marked_failed(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    queue.down = True
    response = await send(client, auth_headers)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "QUEUE_UNAVAILABLE" and "secret-host" not in response.text
    async with session_factory() as session:
        job = await session.get_one(IngestionJob, uuid.UUID(error["details"]["job_id"]))
    assert job.status == "failed" and job.error is not None and "send the file again" in job.error

    queue.down = False  # sending it again works, on the same document
    again = await accepted(await send(client, auth_headers))
    assert again["new_document"] is False
    assert await count(session_factory, Document) == 1


async def test_the_real_queue_gives_up_quickly_when_redis_is_not_there() -> None:
    queue = JobQueue(get_settings().model_copy(update={"REDIS_URL": "redis://127.0.0.1:1/0"}))
    with pytest.raises(Exception):  # noqa: B017  any connection error
        await queue.enqueue_ingestion(uuid.uuid4())
    await queue.aclose()


def test_the_apps_database_engine_never_writes_sql_values() -> None:
    assert app_engine.sync_engine.hide_parameters is True


# ── links ───────────────────────────────────────────────────────────────


async def send_link(
    client: httpx.AsyncClient, headers: dict[str, str], url: str, **form: str
) -> httpx.Response:
    return await client.post(URL, headers=headers, data={"url": url, **form})


async def test_a_link_only_creates_a_queued_job(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    link = "https://files.example/docs/cors.md?v=2"
    body = await accepted(await send_link(client, auth_headers, f"  {link}#part-3 "))

    assert (body["document_id"], body["new_document"], body["source"]) == (None, None, link)
    assert queue.queued == [uuid.UUID(body["job_id"])]
    assert queue.status_when_queued == ["queued"]
    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    async with session_factory() as session:
        job = await session.get_one(IngestionJob, uuid.UUID(body["job_id"]))
    assert (job.source_kind, job.source, job.document_id) == ("url", link, None)
    assert str(job.tenant_id) == me["tenant"]["id"]
    for model in (Document, DocumentFile):  # nothing was downloaded in the request
        assert await count(session_factory, model) == 0


@pytest.mark.parametrize(
    "link",
    [
        "file:///etc/passwd",
        "ftp://files.example/a.md",
        "http://files.example:6379/",
        "http://user:pass@files.example/a.md",
        "files.example/a.md",
        "http://files.example/a b.md",
        "https://files.example/" + "a" * 500,  # does not fit in documents.source
    ],
)
async def test_a_refused_link_saves_and_queues_nothing(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
    link: str,
) -> None:
    response = await send_link(client, auth_headers, link)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "INVALID_LINK")
    assert await count(session_factory, IngestionJob) == 0 and queue.queued == []


@pytest.mark.parametrize("extra", [{"title": "T"}, {"source": "docs/a.md"}])
async def test_title_and_source_are_not_silently_ignored_for_a_link(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
    extra: dict[str, str],
) -> None:
    response = await send_link(client, auth_headers, "https://files.example/a.md", **extra)
    assert response.json()["error"]["code"] == "INVALID_INGEST_REQUEST"
    assert response.status_code == 422 and await count(session_factory, IngestionJob) == 0


async def test_exactly_one_of_file_and_url(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    both = await send(client, auth_headers, url="https://files.example/a.md")
    neither = await client.post(URL, headers=auth_headers, data={"url": "   "})
    for response in (both, neither):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["message"] == "Send either a file or a url."
    assert await count(session_factory, IngestionJob) == 0 and queue.queued == []


async def test_no_login_no_link(client: httpx.AsyncClient, queue: FakeQueue) -> None:
    assert (await send_link(client, {}, "https://files.example/a.md")).status_code == 401
    assert queue.queued == []


async def test_a_link_when_the_queue_is_down(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    queue.down = True
    response = await send_link(client, auth_headers, "https://files.example/a.md")
    assert response.status_code == 503
    error = response.json()["error"]
    assert "The link could not be queued" in error["message"]
    async with session_factory() as session:
        job = await session.get_one(IngestionJob, uuid.UUID(error["details"]["job_id"]))
    assert job.status == "failed" and job.error is not None and "send the link again" in job.error


# ── the status of a job ─────────────────────────────────────────────────

STATUS_FIELDS = {
    "job_id",
    "status",
    "stage",
    "error",
    "source_kind",
    "source",
    "document_id",
    "attempts",
    "chunks_written",
    "created_at",
    "started_at",
    "finished_at",
}


async def status_of(
    client: httpx.AsyncClient, headers: dict[str, str], job_id: object
) -> httpx.Response:
    return await client.get(f"{URL}/{job_id}", headers=headers)


async def test_the_status_of_a_new_job(
    client: httpx.AsyncClient, auth_headers: dict[str, str], queue: FakeQueue
) -> None:
    sent = await accepted(await send(client, auth_headers))
    response = await status_of(client, auth_headers, sent["job_id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == STATUS_FIELDS  # and so no tenant id
    assert body["created_at"]
    assert {k: v for k, v in body.items() if k != "created_at"} == {
        "job_id": sent["job_id"],
        "status": "queued",
        "stage": None,
        "error": None,
        "source_kind": "file",
        "source": "guide.md",
        "document_id": sent["document_id"],
        "attempts": 0,
        "chunks_written": None,
        "started_at": None,
        "finished_at": None,
    }


async def test_the_status_follows_the_job(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    sent = await accepted(await send_link(client, auth_headers, "https://files.example/a.md"))
    job_id = uuid.UUID(sent["job_id"])

    async def change(step: Callable[[AsyncSession], Awaitable[bool]]) -> dict[str, Any]:
        async with session_factory() as session:
            assert await step(session)
            await session.commit()
        body: dict[str, Any] = (await status_of(client, auth_headers, job_id)).json()
        return body

    body = await change(lambda s: start_job(s, job_id))
    assert (body["status"], body["attempts"], body["document_id"]) == ("running", 1, None)
    assert body["started_at"] and body["finished_at"] is None
    body = await change(lambda s: set_stage(s, job_id, "fetch"))
    assert (body["status"], body["stage"], body["error"]) == ("running", "fetch", None)
    body = await change(lambda s: fail_job(s, job_id, "This address is not allowed."))
    assert (body["status"], body["stage"], body["error"]) == (
        "failed",
        "fetch",
        "This address is not allowed.",
    )
    assert body["finished_at"]


async def test_the_status_of_a_succeeded_job(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    queue: FakeQueue,
    session_factory: Sessions,
) -> None:
    sent = await accepted(await send(client, auth_headers))
    job_id = uuid.UUID(sent["job_id"])
    async with session_factory() as session:
        await start_job(session, job_id)
        await set_stage(session, job_id, "upsert")
        await succeed_job(session, job_id, chunks_written=7)
        await session.commit()
    body = (await status_of(client, auth_headers, job_id)).json()
    assert (body["status"], body["stage"], body["chunks_written"]) == ("succeeded", None, 7)


async def test_another_tenants_job_looks_like_a_missing_one(
    client: httpx.AsyncClient, register: Register, queue: FakeQueue
) -> None:
    owner, other = await register("owner@corp.com"), await register("other@corp.com")
    sent = await accepted(await send(client, owner, filename="salaries.md"))
    theirs = await status_of(client, other, sent["job_id"])
    missing = await status_of(client, other, uuid.uuid4())
    assert theirs.status_code == missing.status_code == 404
    for response in (theirs, missing):
        error = response.json()["error"]
        assert (error["code"], error["message"]) == (
            "INGESTION_JOB_NOT_FOUND",
            "Ingestion job not found.",
        )
    assert "salaries" not in theirs.text and sent["document_id"] not in theirs.text
    assert (await status_of(client, owner, sent["job_id"])).status_code == 200


async def test_the_status_needs_a_login_and_a_real_id(
    client: httpx.AsyncClient, auth_headers: dict[str, str], queue: FakeQueue
) -> None:
    sent = await accepted(await send(client, auth_headers))
    assert (await status_of(client, {}, sent["job_id"])).status_code == 401
    assert (await status_of(client, auth_headers, "not-an-id")).status_code == 422
