"""The thin layer between the app and ARQ, the job queue in Redis (Day 11).

Only this module and `ai_service.worker` import ARQ. The work itself is written as plain async
functions, so that changing the queue would not change them.
"""

import asyncio
import uuid

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from ai_service.core.config import Settings

# The app and the worker must use the same name, or jobs wait and nobody takes them.
QUEUE_NAME = "ai-service:jobs"
# the name under which the worker registers the ingestion job
INGEST_JOB = "ingest_document"


def redis_settings(url: str) -> RedisSettings:
    return RedisSettings.from_dsn(url)


async def create_job_queue(settings: Settings) -> ArqRedis:
    """A connection that puts jobs on the queue. The caller closes it with `aclose()`."""
    return await create_pool(redis_settings(settings.REDIS_URL), default_queue_name=QUEUE_NAME)


class JobQueue:
    """The API's way to the queue. It connects when first used, not when the app starts, so
    that the API also runs (and can say what is wrong) while Redis is down."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.REDIS_URL
        self._pool: ArqRedis | None = None
        self._lock = asyncio.Lock()

    async def enqueue_ingestion(self, job_id: uuid.UUID) -> bool:
        async with self._lock:
            if self._pool is None:
                connect = redis_settings(self._url)
                # a user is waiting: fail fast (ARQ's default tries five more times)
                connect.conn_retries = 1
                connect.conn_timeout = 2
                self._pool = await create_pool(connect, default_queue_name=QUEUE_NAME)
        async with asyncio.timeout(5):
            return await enqueue_ingestion(self._pool, job_id)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()


async def enqueue_ingestion(queue: ArqRedis, job_id: uuid.UUID) -> bool:
    """Put the job (a row of ingestion_jobs, already COMMITTED) on the queue. Only its id is
    sent. False if this job id is on the queue already, or finished less than an hour ago."""
    queued = await queue.enqueue_job(INGEST_JOB, str(job_id), _job_id=f"ingest:{job_id}")
    return queued is not None
