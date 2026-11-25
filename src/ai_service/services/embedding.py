"""The "embed" stage of the ingestion job (Day 11, task 1): chunk texts to vectors with Jina.

- The same text is sent only once within one call.
- Texts go in batches, limited by number and by tokens.
- Requests are paced to stay under the per-minute limits of my key.
- Temporary problems (429, 5xx, network, timeout) are retried with growing pauses. If they do
  not stop, TransientEmbeddingError tells the job to try again later.
- Anything else raises PermanentEmbeddingError: sending the same request again cannot help.

Texts and the API key are never logged.
"""

import asyncio
import logging
import math
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import httpx

from ai_service.core.config import Settings
from ai_service.services.tokenizer import TokenCounter

logger = logging.getLogger("ai_service.embedding")

DIMENSIONS = 1024  # chunks.embedding is vector(1024)
# What Jina billed for each text on my corpus, above the tokenizer's content tokens: <s> </s>
# and 9 more (notes/day-11, Step 2). Used to estimate, never to report what was billed.
EXTRA_TOKENS_PER_TEXT = 11
_MAX_ATTEMPTS = 4
_FIRST_PAUSE = 5.0
_LONGEST_PAUSE = 60.0
_TIMEOUT_SECONDS = 120.0

Sleep = Callable[[float], Awaitable[None]]


class EmbeddingError(Exception):
    """The message is short and safe to show to the user."""


class TransientEmbeddingError(EmbeddingError):
    """It may work later: let the job be retried."""


class PermanentEmbeddingError(EmbeddingError):
    """Trying again cannot help."""


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]  # one for each input text, in the same order
    model: str
    billed_tokens: int  # as Jina reported
    estimated_tokens: int  # what I expected before sending
    requests: int  # successful requests
    retries: int
    unique_texts: int


class Pacer:
    """Keeps the requests and tokens of the last 60 seconds within the limits. One instance
    must be shared by everything in a process that uses the same API key. It cannot know what
    other processes send."""

    def __init__(
        self,
        tokens_per_minute: int,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._tokens = tokens_per_minute
        self._requests = requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[tuple[float, int]] = deque()  # (when, tokens)
        self._lock = asyncio.Lock()

    async def wait_for(self, tokens: int) -> float:
        """Wait until a request of this size fits, record it, and return the seconds waited."""
        waited = 0.0
        async with self._lock:  # one waiter at a time, in the order they came
            while True:
                now = self._clock()
                while self._sent and now - self._sent[0][0] >= 60:
                    self._sent.popleft()
                used = sum(size for _, size in self._sent)
                fits = used + tokens <= self._tokens and len(self._sent) < self._requests
                # a request larger than the whole limit goes alone, when nothing else counts
                if fits or not self._sent:
                    self._sent.append((now, tokens))
                    return waited
                pause = max(60 - (now - self._sent[0][0]), 0.5)
                await self._sleep(pause)
                waited += pause


def make_batches(sizes: Sequence[int], max_texts: int, max_tokens: int) -> list[list[int]]:
    """Positions grouped in order. A single text larger than max_tokens goes alone."""
    batches: list[list[int]] = []
    current: list[int] = []
    total = 0
    for position, size in enumerate(sizes):
        if current and (len(current) >= max_texts or total + size > max_tokens):
            batches.append(current)
            current, total = [], 0
        current.append(position)
        total += size
    if current:
        batches.append(current)
    return batches


def _retry_after(response: httpx.Response) -> float | None:
    try:
        seconds = float(response.headers["retry-after"])
    except KeyError, ValueError:
        return None
    return seconds if 0 <= seconds < math.inf else None


def _read_answer(response: httpx.Response, expected: int) -> tuple[list[list[float]], int]:
    """The vectors in input order and the billed tokens. A wrong shape is never accepted: a
    vector saved next to the wrong text would be silently wrong for ever."""
    try:
        body = response.json()
        items = sorted(body["data"], key=lambda item: item["index"])
        if [item["index"] for item in items] != list(range(expected)):
            raise ValueError("wrong indexes")
        vectors = [[float(x) for x in item["embedding"]] for item in items]
        if any(
            len(v) != DIMENSIONS or not all(map(math.isfinite, v)) or not any(v) for v in vectors
        ):
            raise ValueError("wrong vector")
        return vectors, int(body["usage"]["total_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentEmbeddingError(
            "The embedding service sent an answer that cannot be used."
        ) from exc


async def _send(
    texts: list[str],
    *,
    estimate: int,
    http: httpx.AsyncClient,
    settings: Settings,
    pacer: Pacer,
    sleep: Sleep,
) -> tuple[list[list[float]], int, int]:
    """One batch. Returns (vectors, billed tokens, retries)."""
    assert settings.JINA_API_KEY is not None  # checked by embed_passages
    payload = {
        "model": settings.JINA_EMBEDDING_MODEL,
        "task": "retrieval.passage",  # search queries use retrieval.query
        "dimensions": DIMENSIONS,
        "late_chunking": False,
        "truncate": False,  # a text that is too long is an error, not a silently cut vector
        "input": texts,
    }
    headers = {"Authorization": f"Bearer {settings.JINA_API_KEY.get_secret_value()}"}
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        await pacer.wait_for(estimate)  # a retry counts against the limits too
        pause = None
        try:
            response = await http.post(
                settings.JINA_EMBEDDING_URL, json=payload, headers=headers, timeout=_TIMEOUT_SECONDS
            )
        except httpx.TransportError as exc:  # network errors and timeouts
            problem = type(exc).__name__
        else:
            if response.status_code == 200:
                vectors, billed = _read_answer(response, len(texts))
                return vectors, billed, attempt - 1
            if response.status_code != 429 and response.status_code < 500:
                logger.warning("embedding request refused: HTTP %d", response.status_code)
                raise PermanentEmbeddingError(
                    f"The embedding service refused the request (HTTP {response.status_code})."
                )
            problem = f"HTTP {response.status_code}"
            pause = _retry_after(response)
        if attempt == _MAX_ATTEMPTS:
            break
        if pause is None:  # 5, 10, 20 s, with jitter so that retries do not come together
            pause = _FIRST_PAUSE * 2 ** (attempt - 1) * random.uniform(0.75, 1.25)
        pause = min(pause, _LONGEST_PAUSE)
        logger.warning(
            "embedding attempt %d of %d failed (%s); next in %.0f s",
            attempt,
            _MAX_ATTEMPTS,
            problem,
            pause,
        )
        await sleep(pause)
    raise TransientEmbeddingError("The embedding service is not available. Please try again.")


async def embed_passages(
    texts: Sequence[str],
    *,
    http: httpx.AsyncClient,
    settings: Settings,
    counter: TokenCounter,
    pacer: Pacer,
    sleep: Sleep = asyncio.sleep,
) -> EmbeddingResult:
    """Vectors for the texts of one document. All or nothing: it raises rather than return a
    list with holes."""
    if not settings.JINA_API_KEY:
        raise PermanentEmbeddingError("The embedding API key is not configured.")
    if any(not text.strip() for text in texts):
        raise PermanentEmbeddingError("An empty text cannot be embedded.")

    unique = list(dict.fromkeys(texts))  # keeps the first-seen order
    sizes = [counter.count(text) + EXTRA_TOKENS_PER_TEXT for text in unique]
    vectors: dict[str, list[float]] = {}
    billed = retries = requests = 0
    for batch in make_batches(sizes, settings.JINA_BATCH_TEXTS, settings.JINA_BATCH_TOKENS):
        batch_texts = [unique[i] for i in batch]
        found, tokens, tries = await _send(
            batch_texts,
            estimate=sum(sizes[i] for i in batch),
            http=http,
            settings=settings,
            pacer=pacer,
            sleep=sleep,
        )
        vectors.update(zip(batch_texts, found, strict=True))
        billed += tokens
        retries += tries
        requests += 1
    return EmbeddingResult(
        vectors=[vectors[text] for text in texts],
        model=settings.JINA_EMBEDDING_MODEL,
        billed_tokens=billed,
        estimated_tokens=sum(sizes),
        requests=requests,
        retries=retries,
        unique_texts=len(unique),
    )
