"""The original file of a document (Day 11)."""

import uuid

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from ai_service.db.base import Base, TimestampMixin


class DocumentFile(Base, TimestampMixin):
    """The uploaded bytes of a document, so that the worker can parse them, now and again
    with a better parser.

    A table of its own: reading or listing documents never loads file contents. At most one
    file per document. A document may have none (the textbook, whose PDF is not stored).
    """

    __tablename__ = "document_files"

    # The primary key: one file per document. Deleting the document deletes the file.
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    # Copied from the document so that every read can filter on this table alone. It must
    # equal the document's tenant_id; file_store.save_file checks that.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # bytea in PostgreSQL
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
