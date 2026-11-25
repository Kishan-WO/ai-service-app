"""The upsert stage (Day 11, task 1). On SQLite; the PostgreSQL check is live."""

import hashlib
import re
import uuid
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.core.config import Settings
from ai_service.core.exceptions import DocumentNotFoundError, UnsupportedFileTypeError
from ai_service.db.models import Chunk, Document, Tenant
from ai_service.services.chunk_save import (
    SaveResult,
    chunk_id,
    is_up_to_date,
    pipeline_for,
    pipeline_version,
    reusable_vectors,
    save_chunks,
)
from ai_service.services.chunk_stage import ChunkedDocument, ChunkOptions, chunk_document
from ai_service.services.cleaning import clean_document
from ai_service.services.parsing import ParsedDocument

Sessions = async_sessionmaker[AsyncSession]
MODEL = "jina-embeddings-v3"
TEXT = "# Guide\n\n## A\n\naa bb cc\n\n## B\n\ndd ee ff\n\n## C\n\ngg hh ii"


class FakeCounter:
    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [word.span() for word in re.finditer(r"\S+", text)]

    def count(self, text: str) -> int:
        return len(text.split())


def vector(text: str) -> list[float]:
    return [float(sum(map(ord, text))), 1.0] + [0.0] * 1022


class Prepared:
    """What the stages before the upsert produce for a Markdown file."""

    def __init__(self, text: str = TEXT, given_title: str | None = None, **settings: object):
        self.settings = Settings(**{"CHUNK_MAX_TOKENS": 50, "CHUNK_OVERLAP_TOKENS": 0} | settings)  # type: ignore[arg-type]
        self.sha = hashlib.sha256(text.encode()).hexdigest()
        self.given_title = given_title
        self.pipeline = pipeline_for("guide.md", self.settings)
        self.version = pipeline_version(self.pipeline)
        cleaned = clean_document(ParsedDocument("markdown", (text,), "utf-8"))
        self.content = cleaned.text
        self.chunked: ChunkedDocument = chunk_document(
            cleaned,
            kind="markdown",
            filename="guide.md",
            counter=FakeCounter(),
            options=ChunkOptions(
                "auto",
                self.settings.CHUNK_MAX_TOKENS,
                self.settings.CHUNK_OVERLAP_TOKENS,
                self.settings.CHUNK_CONTEXT_HEADERS,
            ),
            given_title=given_title,
        )
        self.texts = [p.chunk.embedding_text for p in self.chunked.chunks]
        self.vectors = [vector(t) for t in self.texts]


@pytest.fixture
async def doc(session_factory: Sessions) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        tenant = Tenant(name="t")
        session.add(tenant)
        await session.flush()
        document = Document(
            tenant_id=tenant.id,
            title="new upload",
            source="guide.md",
            content="",
            content_hash="not yet",
            metadata_json={"byte_size": 1},
        )
        session.add(document)
        await session.commit()
        return tenant.id, document.id


async def save(sessions: Sessions, doc: tuple[uuid.UUID, uuid.UUID], p: Prepared) -> SaveResult:
    async with sessions() as session:
        result = await save_chunks(
            session,
            tenant_id=doc[0],
            document_id=doc[1],
            chunked=p.chunked,
            vectors=p.vectors,
            content=p.content,
            file_sha256=p.sha,
            pipeline=p.pipeline,
            given_title=p.given_title,
        )
        await session.commit()
        return result


async def rows(sessions: Sessions) -> list[tuple[Any, ...]]:
    """Every column of every chunk, in order, and the document row."""
    async with sessions() as session:
        chunks = await session.scalars(select(Chunk).order_by(Chunk.chunk_index))
        columns = [c.key for c in Chunk.__mapper__.column_attrs]
        found = [tuple(repr(getattr(row, c)) for c in columns) for row in chunks]
        documents = await session.scalars(select(Document))
        columns = [c.key for c in Document.__mapper__.column_attrs]
        return found + [tuple(repr(getattr(d, c)) for c in columns) for d in documents]


async def texts(sessions: Sessions) -> list[str]:
    async with sessions() as session:
        return list(await session.scalars(select(Chunk.text).order_by(Chunk.chunk_index)))


async def up_to_date(
    sessions: Sessions, doc: tuple[uuid.UUID, uuid.UUID], p: Prepared, **changed: object
) -> bool:
    values: dict[str, Any] = {
        "file_sha256": p.sha,
        "version": p.version,
        "given_title": p.given_title,
    } | changed
    async with sessions() as session:
        return await is_up_to_date(session, tenant_id=doc[0], document_id=doc[1], **values)


# ── ids and the pipeline version ────────────────────────────────────────


def test_a_chunk_id_depends_on_document_pipeline_position_and_text() -> None:
    document = uuid.uuid4()
    base = chunk_id(document, "v1", 0, "text")
    assert base == chunk_id(document, "v1", 0, "text")
    others = [
        chunk_id(uuid.uuid4(), "v1", 0, "text"),
        chunk_id(document, "v2", 0, "text"),
        chunk_id(document, "v1", 1, "text"),
        chunk_id(document, "v1", 0, "text!"),
    ]
    assert len({base, *others}) == 5


@pytest.mark.parametrize(
    "setting",
    [
        {"CHUNK_MAX_TOKENS": 300},
        {"CHUNK_OVERLAP_TOKENS": 5},
        {"CHUNK_STRATEGY": "fixed"},
        {"CHUNK_CONTEXT_HEADERS": True},
        {"JINA_EMBEDDING_MODEL": "another-model"},
    ],
)
def test_every_setting_that_matters_changes_the_pipeline_version(setting: dict[str, Any]) -> None:
    base = pipeline_version(pipeline_for("a.md", Settings()))
    assert pipeline_version(pipeline_for("a.md", Settings(**setting))) != base
    assert pipeline_version(pipeline_for("b.md", Settings())) == base  # the name itself is not


def test_the_pipeline_depends_on_the_kind_of_file() -> None:
    found = {pipeline_version(pipeline_for(n, Settings())) for n in ("a.md", "a.pdf", "a.txt")}
    assert len(found) == 3
    assert pipeline_for("a.PDF", Settings())["parser"].startswith("pypdf-")
    with pytest.raises(UnsupportedFileTypeError):
        pipeline_for("a.docx", Settings())


# ── saving, and saving again ────────────────────────────────────────────


async def test_the_first_save(session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]) -> None:
    p = Prepared()
    assert await save(session_factory, doc, p) == SaveResult(inserted=3, deleted=0, kept=0)
    async with session_factory() as session:
        saved = list(await session.scalars(select(Chunk).order_by(Chunk.chunk_index)))
        document = await session.get(Document, doc[1])
    assert [c.text for c in saved] == [x.chunk.text for x in p.chunked.chunks]
    assert [c.embedding for c in saved] == p.vectors
    assert saved[-1].heading == "Guide > C" and saved[0].tenant_id == doc[0]
    assert saved[0].metadata_json["pipeline_version"] == p.version
    assert document is not None
    assert (document.title, document.content, document.content_hash) == ("Guide", p.content, p.sha)
    assert document.metadata_json["byte_size"] == 1  # what was there is kept
    assert document.metadata_json["ingestion"]["chunks"] == 3


async def test_saving_the_same_again_changes_no_column_of_any_row(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    await save(session_factory, doc, Prepared())
    before = await rows(session_factory)
    assert await save(session_factory, doc, Prepared()) == SaveResult(0, 0, 3)
    assert await rows(session_factory) == before


async def test_only_the_changed_chunk_is_replaced(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    await save(session_factory, doc, Prepared())
    before = await rows(session_factory)
    changed = Prepared(TEXT.replace("dd ee ff", "dd ee NEW"))
    assert await save(session_factory, doc, changed) == SaveResult(inserted=1, deleted=1, kept=2)
    after = await rows(session_factory)
    assert (after[0], after[2]) == (before[0], before[2])  # the two others were not touched
    assert (await texts(session_factory))[1] == "## B\n\ndd ee NEW"


async def test_fewer_chunks_leave_no_old_rows_behind(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    await save(session_factory, doc, Prepared())
    shorter = Prepared(TEXT.split("\n\n## C")[0])
    assert await save(session_factory, doc, shorter) == SaveResult(0, 1, 2)
    assert await texts(session_factory) == [p.chunk.text for p in shorter.chunked.chunks]


async def test_a_new_pipeline_replaces_every_row(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    await save(session_factory, doc, Prepared())
    result = await save(session_factory, doc, Prepared(CHUNK_STRATEGY="recursive"))
    assert (result.kept, result.deleted) == (0, 3)
    async with session_factory() as session:
        versions = {
            c.metadata_json["pipeline_version"] for c in await session.scalars(select(Chunk))
        }
    assert len(versions) == 1  # never two versions of one document


async def test_a_new_title_without_headers_changes_metadata_but_keeps_the_rows(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    await save(session_factory, doc, Prepared())
    assert await save(session_factory, doc, Prepared(given_title="Renamed")) == SaveResult(0, 0, 3)
    async with session_factory() as session:
        assert {c.metadata_json["title"] for c in await session.scalars(select(Chunk))} == {
            "Renamed"
        }


# ── all or nothing ──────────────────────────────────────────────────────


async def test_a_failure_in_the_middle_keeps_the_old_version(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    await save(session_factory, doc, Prepared())
    before = await rows(session_factory)
    broken = Prepared(TEXT.replace("aa bb cc", "new a").replace("gg hh ii", "new c"))
    first, second, third = broken.chunked.chunks
    # two new rows for one position: the database refuses the second (unique constraint)
    third = replace(third, chunk=replace(third.chunk, index=first.chunk.index))
    broken.chunked = replace(broken.chunked, chunks=(first, second, third))
    with pytest.raises(IntegrityError):
        await save(session_factory, doc, broken)  # the deletes had already run
    assert await rows(session_factory) == before


async def test_nothing_is_saved_without_a_commit(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    p = Prepared()
    before = await rows(session_factory)
    async with session_factory() as session:
        await save_chunks(
            session,
            tenant_id=doc[0],
            document_id=doc[1],
            chunked=p.chunked,
            vectors=p.vectors,
            content=p.content,
            file_sha256=p.sha,
            pipeline=p.pipeline,
        )
        await session.rollback()
    assert await rows(session_factory) == before


async def test_a_wrong_number_of_vectors_or_another_tenant_writes_nothing(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    p = Prepared()
    before = await rows(session_factory)
    p.vectors = p.vectors[:-1]
    with pytest.raises(ValueError, match="one vector"):
        await save(session_factory, doc, p)
    with pytest.raises(DocumentNotFoundError):
        await save(session_factory, (uuid.uuid4(), doc[1]), Prepared())
    assert await rows(session_factory) == before


# ── skipping, and reusing vectors ───────────────────────────────────────


async def test_up_to_date_only_when_file_pipeline_title_and_rows_are_the_same(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    p = Prepared()
    assert not await up_to_date(session_factory, doc, p)  # never ingested
    await save(session_factory, doc, p)
    assert await up_to_date(session_factory, doc, p)
    assert not await up_to_date(session_factory, doc, p, file_sha256="other")
    assert not await up_to_date(session_factory, doc, p, version="other")
    assert not await up_to_date(session_factory, doc, p, given_title="other")
    async with session_factory() as session:  # a chunk goes missing
        await session.delete((await session.scalars(select(Chunk))).first())
        await session.commit()
    assert not await up_to_date(session_factory, doc, p)
    with pytest.raises(DocumentNotFoundError):
        await up_to_date(session_factory, (uuid.uuid4(), doc[1]), p)


async def test_vectors_are_reused_only_for_the_same_text_model_and_tenant(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    p = Prepared()
    await save(session_factory, doc, p)
    asked = [p.texts[0], p.texts[2], "a text never seen"]

    async def found(tenant: uuid.UUID, model: str) -> dict[str, list[float]]:
        async with session_factory() as session:
            return await reusable_vectors(
                session, tenant_id=tenant, document_id=doc[1], texts=asked, embedding_model=model
            )

    assert await found(doc[0], MODEL) == {p.texts[0]: p.vectors[0], p.texts[2]: p.vectors[2]}
    assert await found(doc[0], "another-model") == {}
    assert await found(uuid.uuid4(), MODEL) == {}


async def test_chunks_saved_before_this_pipeline_are_not_reused(
    session_factory: Sessions, doc: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The Day 10 loader hashed the text without the heading it embedded."""
    async with session_factory() as session:
        session.add(
            Chunk(
                tenant_id=doc[0],
                document_id=doc[1],
                chunk_index=0,
                heading="h",
                text="old",
                text_hash=hashlib.sha256(b"old").hexdigest(),
                embedding=vector("old"),
                embedding_model=MODEL,
                metadata_json={"section_id": "x"},
            )
        )
        await session.commit()
        assert (
            await reusable_vectors(
                session, tenant_id=doc[0], document_id=doc[1], texts=["old"], embedding_model=MODEL
            )
            == {}
        )
