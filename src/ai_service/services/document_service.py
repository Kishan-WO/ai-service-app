"""Document parsing, hashing and storage. Every query is scoped to one tenant."""

import asyncio
import hashlib
import io
import uuid
from pathlib import Path

from fastapi import UploadFile
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.core.exceptions import (
    DocumentNotFoundError,
    DuplicateDocumentError,
    InvalidDocumentError,
    PayloadTooLargeError,
    UnsupportedFileTypeError,
)
from ai_service.db.models import Document
from ai_service.services.vector_store import VectorStore

ALLOWED_SUFFIXES = {".txt", ".md", ".pdf"}
_CHUNK_SIZE = 1024 * 1024


async def read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in chunks and stop as soon as it is over the limit."""
    if file.size is not None and file.size > max_bytes:
        raise PayloadTooLargeError(max_bytes)
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def extract_text(suffix: str, data: bytes) -> tuple[str, int | None]:
    """Return (text, page_count). Blocking and CPU-bound, so callers use a thread."""
    if suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise InvalidDocumentError("Encrypted PDFs are not supported.")
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
        except InvalidDocumentError:
            raise
        except Exception as exc:  # pypdf raises many different errors for broken files
            raise InvalidDocumentError("The PDF could not be read.") from exc
        return "\n\n".join(page for page in pages if page), len(pages)
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        raise InvalidDocumentError("The file is not valid UTF-8 text.") from exc


async def create_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    filename: str,
    title: str | None,
    data: bytes,
    content_type: str | None,
) -> Document:
    safe_name = Path(filename or "upload").name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise UnsupportedFileTypeError()

    text, page_count = await asyncio.to_thread(extract_text, suffix, data)
    text = text.replace("\x00", "")  # PostgreSQL text columns cannot hold NUL characters
    if not text.strip():
        raise InvalidDocumentError("The file contains no text.")

    content_hash = hashlib.sha256(data).hexdigest()
    existing_id = await _find_by_hash(session, tenant_id, content_hash)
    if existing_id:
        raise DuplicateDocumentError(existing_id)

    metadata: dict[str, object] = {
        "file_type": suffix.lstrip("."),
        "byte_size": len(data),
        "content_type": content_type,
    }
    if page_count is not None:
        metadata["page_count"] = page_count

    document = Document(
        tenant_id=tenant_id,
        title=(title or Path(safe_name).stem)[:255],
        source=safe_name[:512],
        content=text,
        content_hash=content_hash,
        metadata_json=metadata,
    )
    session.add(document)
    try:
        await session.commit()
    except IntegrityError:  # the same file was uploaded twice at the same moment
        await session.rollback()
        existing_id = await _find_by_hash(session, tenant_id, content_hash)
        raise DuplicateDocumentError(existing_id) from None
    return document


async def _find_by_hash(
    session: AsyncSession, tenant_id: uuid.UUID, content_hash: str
) -> uuid.UUID | None:
    return await session.scalar(
        select(Document.id).where(
            Document.tenant_id == tenant_id, Document.content_hash == content_hash
        )
    )


async def list_documents(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[Document], int]:
    total = await session.scalar(
        select(func.count()).select_from(Document).where(Document.tenant_id == tenant_id)
    )
    rows = await session.scalars(
        select(Document)
        .where(Document.tenant_id == tenant_id)
        .order_by(Document.created_at.desc(), Document.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows), total or 0


async def get_document(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """A document of another tenant is reported exactly like a missing one."""
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    if document is None:
        raise DocumentNotFoundError()
    return document


async def delete_document(
    session: AsyncSession, store: VectorStore, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Remove a document and its vectors.

    The order matters when the store is not the database: the vectors go first. If the store
    fails, nothing is deleted and the request can be repeated. The other order could leave the
    text of a deleted document searchable.
    """
    document = await get_document(session, tenant_id=tenant_id, document_id=document_id)
    await store.delete_document(tenant_id=tenant_id, document_id=document_id)
    await session.delete(document)
    await session.commit()
