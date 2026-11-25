"""Schemas for ingestion (Day 11)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IngestJobStatus(BaseModel):
    """One ingestion job, as its tenant may see it (no tenant id, nothing internal)."""

    model_config = ConfigDict(from_attributes=True)

    job_id: uuid.UUID = Field(validation_alias="id")
    status: Literal["queued", "running", "succeeded", "failed"] = Field(
        description="succeeded and failed are final"
    )
    stage: Literal["fetch", "parse", "clean", "chunk", "embed", "upsert"] | None = Field(
        description="Where a running job is, or where a failed one stopped"
    )
    error: str | None = Field(description="Why a failed job failed")
    source_kind: Literal["file", "url"]
    source: str
    document_id: uuid.UUID | None = Field(
        description="Null until a url has been downloaded, or after the document is deleted"
    )
    attempts: int
    chunks_written: int | None = Field(
        description="New chunks saved by a succeeded job. 0: nothing needed to change"
    )
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class IngestAccepted(BaseModel):
    job_id: uuid.UUID
    document_id: uuid.UUID | None = Field(
        description="Null for a url: the document is known after the worker downloads the file"
    )
    status: Literal["queued"] = "queued"
    source: str
    new_document: bool | None = Field(
        description="False when the file went to a document that already existed. Null for a url"
    )
