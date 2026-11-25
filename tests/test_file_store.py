"""Original files of documents (Day 11). These run on SQLite; the PostgreSQL check is live."""

import hashlib
import uuid
from collections.abc import Awaitable, Callable

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_service.core.exceptions import DocumentNotFoundError
from ai_service.db.models import DocumentFile
from ai_service.services.file_store import get_file, save_file

DOCUMENTS_URL = "/api/v1/documents"
DATA = bytes(range(256)) * 40  # every byte value, including NUL


async def _upload(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[uuid.UUID, uuid.UUID]:
    """(tenant id, document id) of a document made through the API."""
    response = await client.post(
        f"{DOCUMENTS_URL}/upload",
        headers=headers,
        files={"file": ("a.txt", b"some text", "text/plain")},
    )
    assert response.status_code == 201
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return uuid.UUID(me["tenant"]["id"]), uuid.UUID(response.json()["id"])


async def _count(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(DocumentFile)) or 0


async def _save(
    sessions: async_sessionmaker[AsyncSession], tenant: uuid.UUID, document: uuid.UUID, data: bytes
) -> None:
    async with sessions() as session:
        await save_file(
            session,
            tenant_id=tenant,
            document_id=document,
            filename="paper.pdf",
            content_type="application/pdf",
            data=data,
        )
        await session.commit()


async def test_a_saved_file_comes_back_byte_for_byte(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant, document = await _upload(client, auth_headers)
    await _save(session_factory, tenant, document, DATA)
    async with session_factory() as session:
        found = await get_file(session, tenant_id=tenant, document_id=document)
    assert found is not None and found.data == DATA
    assert (found.byte_size, found.sha256) == (len(DATA), hashlib.sha256(DATA).hexdigest())
    assert (found.filename, found.content_type) == ("paper.pdf", "application/pdf")


async def test_saving_again_replaces_the_file(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant, document = await _upload(client, auth_headers)
    await _save(session_factory, tenant, document, b"first")
    await _save(session_factory, tenant, document, b"second version")
    async with session_factory() as session:
        found = await get_file(session, tenant_id=tenant, document_id=document)
    assert found is not None and (found.data, found.byte_size) == (b"second version", 14)
    assert await _count(session_factory) == 1


async def test_another_tenant_can_neither_read_nor_write_the_file(
    client: httpx.AsyncClient,
    register: Callable[[str], Awaitable[dict[str, str]]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, other = await register("owner@corp.com"), await register("other@corp.com")
    tenant, document = await _upload(client, owner)
    other_tenant, _ = await _upload(client, other)
    await _save(session_factory, tenant, document, DATA)

    async with session_factory() as session:
        assert await get_file(session, tenant_id=other_tenant, document_id=document) is None
    with pytest.raises(DocumentNotFoundError):
        await _save(session_factory, other_tenant, document, b"overwritten")
    async with session_factory() as session:
        found = await get_file(session, tenant_id=tenant, document_id=document)
    assert found is not None and found.data == DATA


async def test_nothing_is_kept_if_the_caller_does_not_commit(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant, document = await _upload(client, auth_headers)
    async with session_factory() as session:
        await save_file(
            session,
            tenant_id=tenant,
            document_id=document,
            filename="a",
            content_type=None,
            data=DATA,
        )
        await session.rollback()
    assert await _count(session_factory) == 0


async def test_deleting_the_document_through_the_api_deletes_the_file(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant, document = await _upload(client, auth_headers)
    await _save(session_factory, tenant, document, DATA)
    assert await _count(session_factory) == 1

    response = await client.delete(f"{DOCUMENTS_URL}/{document}", headers=auth_headers)

    assert response.status_code == 204
    assert await _count(session_factory) == 0
