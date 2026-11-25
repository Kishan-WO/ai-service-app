"""Search endpoint (Day 10, Task 3)."""

from fastapi import APIRouter

from ai_service.api.deps import AppSettings, CurrentUser, HttpClient, VectorStoreDep
from ai_service.schemas.search import SearchRequest, SearchResponse
from ai_service.services.search_service import embed_query

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search_documents(
    body: SearchRequest,
    user: CurrentUser,
    store: VectorStoreDep,
    http_client: HttpClient,
    settings: AppSettings,
) -> SearchResponse:
    """Semantic vector search over document chunks.

    Tenant isolation is enforced inside the vector store's query:
    only chunks where tenant_id == user.tenant_id can ever be returned.
    """
    query_vector = await embed_query(body.query, http_client, settings)
    results = await store.search(
        tenant_id=user.tenant_id,
        query_vector=query_vector,
        limit=body.limit,
        threshold=body.threshold,
    )
    return SearchResponse(query=body.query, results=results, total=len(results))
