"""The background worker (Day 11): a separate process that runs the jobs the API queues.

    arq ai_service.worker.WorkerSettings            run the worker (Docker Compose does this)
    arq ai_service.worker.WorkerSettings --check    is a worker alive?

This module is the thin ARQ layer: what the worker needs at start, and what happens around a
job (the time limit, retries, giving up). The ingestion itself is in services/ingestion.py.

It runs in a Linux container. On Windows, ARQ 0.28.0 failed while closing a worker
(notes/day-11, Step 10).
"""

import asyncio
import logging
import socket
import uuid
from typing import Any

import httpx
from arq import func
from arq.worker import Retry
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ai_service.core.config import get_settings
from ai_service.core.logging import setup_logging
from ai_service.services.embedding import Pacer
from ai_service.services.ingestion import IngestionDeps, RetryableIngestion, give_up, run_ingestion
from ai_service.services.job_queue import INGEST_JOB, QUEUE_NAME, redis_settings
from ai_service.services.tokenizer import find_tokenizer_file, load_token_counter

logger = logging.getLogger("ai_service.worker")

# Pause before the next attempt: this many seconds times the number of attempts so far.
_RETRY_PAUSE_SECONDS = 30
# ARQ's own time limit is this much longer than mine, so that mine comes first.
_ARQ_MARGIN_SECONDS = 60


async def ping(ctx: dict[str, Any], message: str = "ping") -> dict[str, Any]:
    """A job that does no work. It shows that jobs travel from the app to a worker, and says
    which machine ran it."""
    return {
        "message": message,
        "job_id": ctx["job_id"],
        "job_try": ctx["job_try"],
        "worker_host": socket.gethostname(),
    }


async def ingest_document(ctx: dict[str, Any], job_id: str) -> str:
    """The queue message is only the id of a row in ingestion_jobs. ARQ logs arguments and
    results, so neither may contain anything private: an id goes in, one word comes out.

    My time limit is inside the job: if ARQ's own limit stopped it, nobody would write
    "failed", and the job would say "running" for ever.
    """
    deps: IngestionDeps = ctx["deps"]
    ingestion_id = uuid.UUID(job_id)
    limit = deps.settings.INGEST_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(limit) as timer:
            return await run_ingestion(deps, ingestion_id)
    except RetryableIngestion as exc:
        if ctx["job_try"] >= deps.settings.INGEST_MAX_TRIES:
            await give_up(deps, ingestion_id, f"{exc} Gave up after {ctx['job_try']} attempts.")
            return "failed"
        pause = _RETRY_PAUSE_SECONDS * ctx["job_try"]
        logger.warning("job %s: attempt %d failed, next in %d s", job_id, ctx["job_try"], pause)
        raise Retry(defer=pause) from None
    except TimeoutError:
        if not timer.expired():
            raise
        await give_up(deps, ingestion_id, "Processing took too long and was stopped.")
        return "failed"


def quiet_libraries() -> None:
    """What the libraries may write to the worker's log, also with DEBUG on."""
    # pypdf's warnings quote bytes of the file it is reading, which is a tenant's document
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    # connection-by-connection detail that buries the job lines
    for name in ("httpcore", "httpx", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # arq prints its own lines; passing them on to my handler printed each one twice
    logging.getLogger("arq").propagate = False


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.DEBUG)
    quiet_libraries()
    # The worker's own engine: never echo SQL, and keep the values out of error messages. They
    # are document texts and whole files.
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True, hide_parameters=True)
    ctx["engine"] = engine
    ctx["deps"] = IngestionDeps(
        sessions=async_sessionmaker(engine, expire_on_commit=False),
        http=httpx.AsyncClient(),
        settings=settings,
        counter=load_token_counter(find_tokenizer_file(settings.TOKENIZER_PATH)),
        # one pacer for all jobs of this worker; a second worker would have its own
        pacer=Pacer(settings.JINA_TOKENS_PER_MINUTE, settings.JINA_REQUESTS_PER_MINUTE),
    )
    if settings.VECTOR_STORE == "qdrant":
        logger.warning("ingestion saves to PostgreSQL only: run scripts/copy_to_qdrant.py")


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["deps"].http.aclose()
    await ctx["engine"].dispose()


class WorkerSettings:
    """What `arq` reads to start the worker."""

    functions = [
        ping,
        func(
            ingest_document,
            name=INGEST_JOB,
            max_tries=get_settings().INGEST_MAX_TRIES,
            timeout=get_settings().INGEST_TIMEOUT_SECONDS + _ARQ_MARGIN_SECONDS,
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = QUEUE_NAME
    redis_settings = redis_settings(get_settings().REDIS_URL)
    # each job can hold a 20 MB file and its text in memory
    max_jobs = get_settings().WORKER_MAX_JOBS
    # How often the worker writes its "I am alive" key, which `--check` reads. ARQ's default
    # is one hour, too long for a container health check.
    health_check_interval = 10
