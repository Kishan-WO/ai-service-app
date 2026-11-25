"""Application configuration loaded and validated from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Base directory for ai-service
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


class ModelPrice(BaseModel):
    """USD per 1,000,000 tokens."""

    input: float = Field(default=0.0, ge=0)  # new (uncached) input
    output: float = Field(default=0.0, ge=0)
    cache_read: float = Field(default=0.0, ge=0)  # input served from the provider's cache
    cache_write: float = Field(default=0.0, ge=0)  # input written to the cache (extra on Anthropic)


class Settings(BaseSettings):
    # Application settings with strict environment choices
    ENVIRONMENT: Literal["development", "staging", "production", "testing"] = "development"
    DEBUG: bool = True
    PROJECT_NAME: str = "ai-service"
    API_V1_PREFIX: str = "/api/v1"

    # Strictly required: No defaults. Must come from .env or environment
    SECRET_KEY: str = Field(
        ...,
        min_length=32,
        description="Secret key for JWT generation; must be at least 32 characters.",
    )
    DATABASE_URL: str = Field(
        ...,
        description="Async database connection URL, e.g. postgresql+asyncpg://...",
    )

    # Security settings
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=1440, gt=0)

    # Optional / Test settings
    TEST_DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"

    # Uploads & Limits (bytes, must be positive)
    MAX_UPLOAD_SIZE_BYTES: int = Field(default=20 * 1024 * 1024, gt=0)

    # Browser origins allowed to call the API, as a JSON list in .env, e.g. ["http://localhost:3000"].
    # Empty means no CORS middleware at all.
    CORS_ORIGINS: list[str] = Field(default_factory=list)

    # LLM providers. Both speak the OpenAI chat-completions protocol, so one SDK serves both.
    # "hosted" = Gemini by default; "local" = Ollama. Nothing is required to start the service:
    # a missing hosted key only fails when someone calls /chat or /extract.
    LLM_API_KEY: SecretStr | None = None
    LLM_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    LLM_MODEL: str = ""
    # "hosted_large" = a second, stronger model on the same hosted API (same key and base URL).
    # /route sends unclear requests to it when LLM_LARGE_MODEL is set; empty = use "hosted".
    LLM_LARGE_MODEL: str = ""
    LLM_LARGE_REQUESTS_PER_MINUTE: int = Field(default=0, ge=0)  # its own quota (0 = no limit)
    LOCAL_LLM_BASE_URL: str = "http://localhost:11434/v1"
    LOCAL_LLM_MODEL: str = "gemma3:270m"
    # /summarise refuses a document longer than this (the whole text goes into one model call).
    SUMMARISE_MAX_DOCUMENT_CHARS: int = Field(default=30000, gt=0)
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)
    LLM_MAX_OUTPUT_TOKENS: int = Field(default=2000, gt=0)
    # The /chat system prompt is a registry prompt: prompts/<name>/<version>.md. "chat_basic" is one
    # line; "chat_system" is a long, stable handbook that the provider caches (cheaper, faster).
    CHAT_PROMPT_NAME: str = "chat_basic"
    CHAT_PROMPT_VERSION: str = "v1"
    # How many of the latest stored messages are sent to the model on each turn.
    CHAT_HISTORY_LIMIT: int = Field(default=20, ge=1)

    # Embeddings (Day 10)
    JINA_API_KEY: SecretStr | None = None
    JINA_EMBEDDING_MODEL: str = "jina-embeddings-v3"
    JINA_EMBEDDING_URL: str = "https://api.jina.ai/v1/embeddings"

    # Which store `POST /search` reads and document deletion cleans (Day 10, task 5). PostgreSQL
    # stays the main copy: before switching to qdrant, run scripts/copy_to_qdrant.py.
    VECTOR_STORE: Literal["pgvector", "qdrant"] = "pgvector"

    # Qdrant, the second vector store (Day 10, task 5)
    # 127.0.0.1, not "localhost": Qdrant is published on IPv4 only. On my Windows machine
    # "localhost" tries ::1 first, and that cost about 260 ms on every request (qdrant-client did
    # not reuse connections to localhost). Docker Compose sets http://qdrant:6333 for the API.
    QDRANT_URL: str = "http://127.0.0.1:6333"
    QDRANT_COLLECTION: str = "chunks"

    # Second provider (Day 6). The SDKs' own retries are switched off: our wrapper retries, so
    # the numbers below are the only retry settings and the calls do not multiply.
    ANTHROPIC_API_KEY: SecretStr | None = None
    ANTHROPIC_MODEL: str = ""
    # When a backend keeps failing (rate limit, overload, timeout), answer from this one instead.
    LLM_FALLBACK_BACKEND: Literal["hosted", "local", "anthropic"] | None = None
    LLM_RETRY_ATTEMPTS: int = Field(default=3, ge=1)  # tries per backend, the first one included
    LLM_RETRY_BASE_DELAY_SECONDS: float = Field(default=1.0, ge=0)
    LLM_RETRY_MAX_DELAY_SECONDS: float = Field(default=20.0, ge=0)
    LLM_RETRY_TOTAL_SECONDS: float = Field(default=90.0, gt=0)  # stop retrying after this long
    # Calls in flight at once, per backend, and calls per minute (0 = no limit). The free Gemini
    # tier allows 15 per minute, so a batch of inputs must not be sent all at once.
    LLM_MAX_CONCURRENCY: int = Field(default=4, ge=1)
    LLM_REQUESTS_PER_MINUTE: int = Field(default=0, ge=0)

    # USD per 1,000,000 tokens, by model name. Copied from the providers' price pages (checked
    # 2026-11): they change, so override this with LLM_PRICES='{"model": {...}}' when they do.
    # A model that is not listed is logged with cost = null; it never breaks a request.
    LLM_PRICES: dict[str, ModelPrice] = Field(
        default_factory=lambda: {
            # Standard (paid) price. The free tier does not bill, so this is the cost it would have.
            "gemini-3.1-flash-lite": ModelPrice(input=0.25, output=1.50, cache_read=0.025),
            # the "large" model of the Day 8 router; its output price includes thinking tokens
            "gemini-3-flash-preview": ModelPrice(input=0.50, output=3.00, cache_read=0.05),
            "gemma3:270m": ModelPrice(),  # local: no per-token price
        }
    )

    @field_validator(
        "LLM_API_KEY",
        "ANTHROPIC_API_KEY",
        "JINA_API_KEY",
        "LLM_FALLBACK_BACKEND",
        mode="before",
    )
    @classmethod
    def blank_means_not_set(cls, v: object) -> object:
        # `KEY=` in a .env file arrives as "", which is "not configured", not an empty key
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        valid_prefixes = ("postgresql+asyncpg://", "sqlite+aiosqlite://")
        if not any(v.startswith(prefix) for prefix in valid_prefixes):
            raise ValueError(f"DATABASE_URL must start with one of {valid_prefixes}. Got: {v}")
        return v

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
