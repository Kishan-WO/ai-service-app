"""Schemas for POST /search (Day 10)."""

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SearchRequest(BaseModel):
    query: str = Field(..., max_length=1000, description="Search query string")
    limit: int = Field(default=5, ge=1, le=50, description="Max number of chunks to return")
    threshold: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Optional minimum cosine similarity score"
    )

    @field_validator("query")
    @classmethod
    def not_blank(cls, value: str) -> str:
        v = value.strip()
        if not v:
            raise ValueError("Query must not be empty.")
        return v


class SearchResultItem(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    heading: str
    text: str
    score: float = Field(description="Cosine similarity score between 0 and 1")
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultItem]
    total: int
