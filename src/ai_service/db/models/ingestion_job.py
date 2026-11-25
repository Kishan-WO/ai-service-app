"""The status of one ingestion job (Day 11)."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ai_service.db.base import Base, TimestampMixin

#     queued -> running -> succeeded
#        |         |
#        +---------+----> failed
JOB_STATUSES = ("queued", "running", "succeeded", "failed")
# Where a running job is, and for a failed one, where it stopped. "fetch" is for a URL; the
# others are the plan's: parse, clean, chunk, embed, upsert.
JOB_STAGES = ("fetch", "parse", "clean", "chunk", "embed", "upsert")
SOURCE_KINDS = ("file", "url")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(v) for v in values)})"


class IngestionJob(Base, TimestampMixin):
    """What the status endpoint reports. This table, not ARQ, is the record of a job: ARQ
    forgets a result after a while and calls a failed job "complete" too.

    The queue message carries only `id`; the worker reads everything else, the tenant
    included, from this row.
    """

    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Empty while a URL has not been fetched yet, and after the document is deleted: the job
    # stays as history.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(2048), nullable=False)  # file name or URL
    status: Mapped[str] = mapped_column(String(10), default="queued", nullable=False)
    stage: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # A short message that is safe to show to the user: never a stack trace.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks_written: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_one_of("status", JOB_STATUSES), name="status"),
        CheckConstraint(_one_of("stage", JOB_STAGES), name="stage"),
        CheckConstraint(_one_of("source_kind", SOURCE_KINDS), name="source_kind"),
        # a failed job always says why, and no other job carries an error
        CheckConstraint("(status = 'failed') = (error is not null)", name="error_when_failed"),
    )
