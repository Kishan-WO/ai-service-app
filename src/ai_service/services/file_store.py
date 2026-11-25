"""Saving and reading original files (Day 11).

The rest of the app uses only these two functions. If files move to object storage one day,
this module changes and its callers do not.
"""

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_service.core.exceptions import DocumentNotFoundError
from ai_service.db.models import Document, DocumentFile


async def save_file(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    filename: str,
    content_type: str | None,
    data: bytes,
) -> DocumentFile:
    """Store (or replace) the original file of one of this tenant's documents.

    It does not commit: the caller saves the document, the file and whatever else belongs to
    them in one transaction.
    """
    owned = await session.scalar(
        select(Document.id).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    if owned is None:  # missing, or another tenant's: reported the same way
        raise DocumentNotFoundError()
    stored = await session.merge(
        DocumentFile(
            document_id=document_id,
            tenant_id=tenant_id,
            filename=filename[:512],
            content_type=content_type,
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )
    )
    await session.flush()
    return stored


async def get_file(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> DocumentFile | None:
    """The file of this tenant's document, or None if there is none it may see."""
    return await session.scalar(
        select(DocumentFile).where(
            DocumentFile.document_id == document_id, DocumentFile.tenant_id == tenant_id
        )
    )
