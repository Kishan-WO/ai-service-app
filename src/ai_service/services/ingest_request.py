"""What `POST /ingest` does before the worker takes over (Day 11, task 1).

For a file: decide which document it belongs to, and save the document, the file and the job in
ONE transaction. For a link: only create the job; the worker downloads the file and then saves it
here too, with the link as its source.

Which document is it?
- The caller gives `source` (an identifier such as a path): it means "this is THAT document".
  The tenant's document with that source gets the new file; if there is none, one is created.
- No `source`: the same bytes as a document the tenant already has are that document again;
  other bytes are a new document. Two files that merely share a name never replace each other.

Nothing is parsed here. The old chunks stay searchable until the new ones are ready.
"""

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.core.exceptions import (
    AmbiguousSourceError,
    DuplicateDocumentError,
    InvalidDocumentError,
    InvalidLinkError,
)
from ai_service.db.models import Document, IngestionJob
from ai_service.services.file_store import save_file
from ai_service.services.ingestion_jobs import create_job
from ai_service.services.parsing import kind_of
from ai_service.services.url_fetch import BlockedURLError, parse_url

# documents.metadata: the title the user typed when sending the file, if any
GIVEN_TITLE_KEY = "given_title"
_TITLE_LENGTH = 255
_SOURCE_LENGTH = 512  # documents.source


@dataclass(frozen=True)
class Accepted:
    document: Document
    job: IngestionJob
    new_document: bool


async def _lock_source(session: AsyncSession, tenant_id: uuid.UUID, source: str) -> None:
    """Two requests for the same new source must not both create a document. There is no row
    to lock yet, so PostgreSQL gets an advisory lock until the transaction ends."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"ingest:{tenant_id}:{source}"},
        )


async def accept_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str,
    data: bytes,
    content_type: str | None,
    title: str | None = None,
    source: str | None = None,
) -> Accepted:
    """It does not commit. The job must be committed before it is put on the queue."""
    document, new_document = await save_document_file(
        session,
        tenant_id=tenant_id,
        filename=filename,
        data=data,
        content_type=content_type,
        title=title,
        source=source,
    )
    job = await create_job(
        session,
        tenant_id=tenant_id,
        source_kind="file",
        source=document.source,
        document_id=document.id,
    )
    return Accepted(document=document, job=job, new_document=new_document)


def check_link(url: str) -> str:
    """The checks that need no network. Where the name leads is checked by the worker, when it
    downloads. Returns the link as it is kept: it identifies the document, so a "#part" at the
    end, which is never sent to the server, is removed."""
    try:
        parsed = parse_url(url.strip())
    except BlockedURLError as exc:
        raise InvalidLinkError(str(exc)) from exc
    link = str(parsed.copy_with(fragment=None))
    if len(link) > _SOURCE_LENGTH:
        raise InvalidLinkError(f"The link is longer than {_SOURCE_LENGTH} characters.")
    return link


async def accept_link(session: AsyncSession, *, tenant_id: uuid.UUID, url: str) -> IngestionJob:
    """A job without a document: that is made when the file has been downloaded. No commit."""
    return await create_job(session, tenant_id=tenant_id, source_kind="url", source=check_link(url))


async def save_document_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str,
    data: bytes,
    content_type: str | None,
    title: str | None = None,
    source: str | None = None,
) -> tuple[Document, bool]:
    """The document (found or new) with this file as its stored file, and whether it is new.
    No commit."""
    safe_name = Path(filename or "upload").name
    kind_of(safe_name)  # refuses a type the worker could not parse
    if not data:
        raise InvalidDocumentError("The file is empty.")
    title = (title or "").strip()[:_TITLE_LENGTH] or None
    source = (source or "").strip() or None
    if source and len(source) > _SOURCE_LENGTH:
        raise InvalidDocumentError(f"The source is longer than {_SOURCE_LENGTH} characters.")
    sha256 = hashlib.sha256(data).hexdigest()

    mine = select(Document).where(Document.tenant_id == tenant_id)
    same_bytes = await session.scalar(mine.where(Document.content_hash == sha256))
    document: Document | None
    if source:
        await _lock_source(session, tenant_id, source)
        found = list(await session.scalars(mine.where(Document.source == source).limit(2)))
        if len(found) > 1:
            raise AmbiguousSourceError(source)
        document = found[0] if found else None
        # one tenant cannot have the same file twice (a unique constraint)
        same_bytes = await session.scalar(mine.where(Document.content_hash == sha256))
        if same_bytes is not None and (document is None or same_bytes.id != document.id):
            raise DuplicateDocumentError(same_bytes.id)
    else:
        document = same_bytes

    new_document = document is None
    if document is None:
        document = Document(
            tenant_id=tenant_id,
            title=title or Path(safe_name).stem[:_TITLE_LENGTH] or safe_name,
            source=source or safe_name[:_SOURCE_LENGTH],
            content="",  # the worker writes the cleaned text
            content_hash=sha256,
            metadata_json={},
        )
        session.add(document)
    else:
        document.content_hash = sha256
    facts: dict[str, object] = {"byte_size": len(data), "content_type": content_type}
    if title:  # without a new title, one given earlier stays
        facts[GIVEN_TITLE_KEY] = title
    document.metadata_json = {**document.metadata_json, **facts}
    await session.flush()

    await save_file(
        session,
        tenant_id=tenant_id,
        document_id=document.id,
        filename=safe_name,
        content_type=content_type,
        data=data,
    )
    return document, new_document
