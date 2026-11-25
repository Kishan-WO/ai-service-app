"""Shared FastAPI dependencies. The tenant always comes from the authenticated user."""

import uuid
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.core.config import Settings, get_settings
from ai_service.core.exceptions import NotAuthenticatedError
from ai_service.core.security import decode_access_token
from ai_service.db.models import User
from ai_service.db.session import async_session_factory, get_db_session
from ai_service.services.job_queue import JobQueue
from ai_service.services.llm import LLMRegistry
from ai_service.services.qdrant_store import QdrantStore
from ai_service.services.vector_store import PgVectorStore, VectorStore

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]

# auto_error=False so a missing token produces our own error envelope.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{get_settings().API_V1_PREFIX}/auth/token", auto_error=False
)


async def get_current_user(
    request: Request,
    token: Annotated[str | None, Depends(oauth2_scheme)],
    session: DbSession,
) -> User:
    if token is None:
        raise NotAuthenticatedError()
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
        token_tenant_id = uuid.UUID(payload["tenant_id"])
    except jwt.PyJWTError, KeyError, ValueError, TypeError:
        raise NotAuthenticatedError("Invalid or expired token.") from None

    user = await session.get(User, user_id)
    # The tenant in the token must match the user's real tenant.
    if user is None or user.tenant_id != token_tenant_id:
        raise NotAuthenticatedError("Invalid or expired token.")

    # picked up by the request-logging middleware
    request.state.tenant_id = str(user.tenant_id)
    request.state.user_id = str(user.id)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_http_client(request: Request) -> httpx.AsyncClient:
    """The shared client created once in the app lifespan."""
    client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    return client


HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


def get_vector_store(request: Request, session: DbSession, settings: AppSettings) -> VectorStore:
    """The store chosen by VECTOR_STORE: search reads it, document deletion cleans it."""
    if settings.VECTOR_STORE == "qdrant":
        return QdrantStore(request.app.state.qdrant, settings.QDRANT_COLLECTION)
    return PgVectorStore(session)


VectorStoreDep = Annotated[VectorStore, Depends(get_vector_store)]


def get_llm_registry(request: Request) -> LLMRegistry:
    """The model providers created once in the app lifespan. Tests override this with a fake."""
    registry: LLMRegistry = request.app.state.llm
    return registry


Llm = Annotated[LLMRegistry, Depends(get_llm_registry)]


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessions that outlive the request (the chat stream saves its reply after the handler)."""
    return async_session_factory


SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def get_job_queue(request: Request) -> JobQueue:
    """The queue of the background worker. Tests override this with a fake."""
    queue: JobQueue = request.app.state.job_queue
    return queue


JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]
