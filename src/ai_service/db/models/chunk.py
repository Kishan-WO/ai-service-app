"""Chunk database model: a piece of a document with its embedding vector (Day 10)."""

import uuid
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from ai_service.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from ai_service.db.models.document import Document

# The size of one embedding. It is part of the column type, so a model with another size
# (for example 3,072 numbers) needs a new column or table, not a change of this number.
EMBEDDING_DIMENSIONS = 1024


class Chunk(Base, TimestampMixin):
    """One searchable piece of a document, owned by a tenant, with the vector that stands for it."""

    __tablename__ = "chunks"

    # A stable id (derived from the document and the chunk number by the loader), so loading the
    # same document twice replaces its chunks and does not duplicate them.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # tenant_id is copied from the document so a search can filter on this table alone.
    # It must always equal the document's tenant_id; the code that writes chunks sets it.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The vector. pgvector's type on PostgreSQL; plain JSON on SQLite, which only the fast unit
    # tests use (they never search by vector).
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS).with_variant(JSON(), "sqlite"), nullable=False
    )
    # Which model made the vector. Vectors of different models must never be searched together.
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON().with_variant(JSONB, "postgresql"),
        default=dict,
        nullable=False,
    )

    document: Mapped[Document] = relationship("Document")

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk_index"),
        # The approximate-nearest-neighbour index (HNSW). vector_cosine_ops means it is built for
        # cosine distance, so a query must use the <=> operator or the index is not used.
        # m = links per point, ef_construction = how carefully it is built (the defaults);
        # the search-time setting hnsw.ef_search is not part of the index. On SQLite (the unit
        # tests) these postgresql_* options are ignored and a plain index is made.
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
