"""FastAPI application factory."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import AsyncQdrantClient

from ai_service.api.v1.router import api_router
from ai_service.core.config import get_settings
from ai_service.core.logging import setup_logging
from ai_service.core.middlewares import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    register_error_handlers,
)
from ai_service.db.session import async_session_factory, engine
from ai_service.services.llm import SettingsLLMRegistry
from ai_service.services.llm_accounting import DbCallRecorder


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # one shared client for the whole process: connection pooling, one place for timeouts
    app.state.http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    # model clients are built on first use; every call they make is logged and saved to llm_calls
    app.state.llm = SettingsLLMRegistry(get_settings(), DbCallRecorder(async_session_factory))
    # one Qdrant client for the process, only when Qdrant is the chosen vector store
    settings = get_settings()
    app.state.qdrant = (
        AsyncQdrantClient(url=settings.QDRANT_URL, timeout=10)
        if settings.VECTOR_STORE == "qdrant"
        else None
    )
    logging.getLogger("ai_service").info("vector store: %s", settings.VECTOR_STORE)
    try:
        yield
    finally:
        if app.state.qdrant is not None:
            await app.state.qdrant.close()
        await app.state.llm.aclose()
        await app.state.http_client.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.DEBUG)

    app = FastAPI(title=settings.PROJECT_NAME, version="0.1.0", lifespan=lifespan)

    # Middleware added last is outermost, so the request context wraps everything.
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[REQUEST_ID_HEADER, "X-Process-Time"],
        )
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
