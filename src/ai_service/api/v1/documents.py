"""/documents endpoints. Every query is scoped to the authenticated user's tenant."""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from ai_service.api.deps import AppSettings, CurrentUser, DbSession, VectorStoreDep
from ai_service.schemas.document import DocumentDetail, DocumentList, DocumentResponse
from ai_service.services import document_service

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: Annotated[UploadFile, File(description=".txt, .md or .pdf, up to 20 MB")],
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
    title: Annotated[str | None, Form(max_length=255)] = None,
) -> DocumentResponse:
    data = await document_service.read_upload(file, settings.MAX_UPLOAD_SIZE_BYTES)
    document = await document_service.create_document(
        session,
        tenant_id=user.tenant_id,
        filename=file.filename or "upload",
        title=title,
        data=data,
        content_type=file.content_type,
    )
    return DocumentResponse.model_validate(document)


@router.get("", response_model=DocumentList)
async def list_documents(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentList:
    documents, total = await document_service.list_documents(
        session, tenant_id=user.tenant_id, limit=limit, offset=offset
    )
    return DocumentList(
        items=[DocumentResponse.model_validate(d) for d in documents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> DocumentDetail:
    document = await document_service.get_document(
        session, tenant_id=user.tenant_id, document_id=document_id
    )
    return DocumentDetail.model_validate(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession, store: VectorStoreDep
) -> Response:
    await document_service.delete_document(
        session, store, tenant_id=user.tenant_id, document_id=document_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
