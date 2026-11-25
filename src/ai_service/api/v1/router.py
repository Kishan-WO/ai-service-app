"""All v1 routes in one router."""

from fastapi import APIRouter

from ai_service.api.v1 import (
    auth,
    chat,
    documents,
    extract,
    ingest,
    route,
    search,
    stream,
    summarise,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(documents.router)
api_router.include_router(ingest.router)
api_router.include_router(stream.router)
api_router.include_router(chat.router)
api_router.include_router(extract.router)
api_router.include_router(route.router)
api_router.include_router(summarise.router)
api_router.include_router(search.router)
