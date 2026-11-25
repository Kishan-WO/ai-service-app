"""The embed stage against a fake Jina (Day 11, task 1). No network, no waiting."""

import asyncio
import json
import logging
from collections.abc import Callable

import httpx
import pytest

from ai_service.core.config import Settings
from ai_service.services.embedding import (
    DIMENSIONS,
    EmbeddingResult,
    Pacer,
    PermanentEmbeddingError,
    TransientEmbeddingError,
    embed_passages,
    make_batches,
)

KEY = "test-key-that-must-not-be-logged"
Reply = Callable[[list[str]], httpx.Response]


class WordCounter:
    """One token for each word."""

    def offsets(self, text: str) -> list[tuple[int, int]]:
        raise NotImplementedError

    def count(self, text: str) -> int:
        return len(text.split())


def vector_for(text: str) -> list[float]:
    """Different for different texts, so that a mix-up is seen."""
    return [float(sum(map(ord, text))), float(len(text))] + [0.5] * (DIMENSIONS - 2)


def good(inputs: list[str]) -> httpx.Response:
    data = [{"index": i, "embedding": vector_for(t)} for i, t in enumerate(inputs)]
    # Jina's order is not promised: send it backwards
    return httpx.Response(200, json={"data": data[::-1], "usage": {"total_tokens": 7}})


class FakeJina:
    """Answers each request with the next reply; after the last one, with a good answer."""

    def __init__(self, *replies: Reply | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else good
        if isinstance(reply, Exception):
            raise reply
        return reply(json.loads(request.content)["input"])

    @property
    def inputs(self) -> list[list[str]]:
        return [json.loads(r.content)["input"] for r in self.requests]


def status(code: int, headers: dict[str, str] | None = None) -> Reply:
    return lambda _: httpx.Response(code, headers=headers, json={"detail": "x"})


def settings(**values: object) -> Settings:
    return Settings(**{"JINA_API_KEY": KEY} | values)  # type: ignore[arg-type]


async def run(
    texts: list[str],
    jina: FakeJina,
    *,
    config: Settings | None = None,
    pauses: list[float] | None = None,
) -> EmbeddingResult:
    async def sleep(seconds: float) -> None:
        if pauses is not None:
            pauses.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(jina)) as http:
        return await embed_passages(
            texts,
            http=http,
            settings=config or settings(),
            counter=WordCounter(),
            pacer=Pacer(10**9, 10**9, sleep=sleep),
            sleep=sleep,
        )


# ── what is sent, and what comes back ───────────────────────────────────


async def test_the_request_is_what_jina_expects() -> None:
    jina = FakeJina()
    await run(["one two"], jina)
    request = jina.requests[0]
    assert str(request.url) == "https://api.jina.ai/v1/embeddings"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "model": "jina-embeddings-v3",
        "task": "retrieval.passage",
        "dimensions": 1024,
        "late_chunking": False,
        "truncate": False,
        "input": ["one two"],
    }


async def test_each_text_gets_its_own_vector_in_the_input_order() -> None:
    texts = ["alpha", "bravo two", "charlie three words"]
    result = await run(texts, FakeJina())
    assert result.vectors == [vector_for(t) for t in texts]
    assert (result.requests, result.retries, result.billed_tokens) == (1, 0, 7)
    assert result.estimated_tokens == (1 + 11) + (2 + 11) + (3 + 11)


async def test_the_same_text_is_sent_only_once() -> None:
    jina = FakeJina()
    result = await run(["a", "b", "a", "a", "b"], jina)
    assert jina.inputs == [["a", "b"]]
    assert result.unique_texts == 2
    assert result.vectors == [vector_for(t) for t in ["a", "b", "a", "a", "b"]]


async def test_no_texts_make_no_request() -> None:
    jina = FakeJina()
    result = await run([], jina)
    assert (result.vectors, result.requests, jina.requests) == ([], 0, [])


# ── batches ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sizes", "expected"),
    [
        ([1, 1, 1, 1, 1], [[0, 1, 2], [3, 4]]),  # at most 3 texts
        ([6, 4, 1, 9, 2], [[0, 1], [2, 3], [4]]),  # at most 10 tokens, exactly 10 fits
        ([30, 1], [[0], [1]]),  # one text over the limit goes alone
        ([], []),
    ],
)
def test_batches(sizes: list[int], expected: list[list[int]]) -> None:
    assert make_batches(sizes, max_texts=3, max_tokens=10) == expected


async def test_batches_follow_the_settings_and_totals_add_up() -> None:
    jina = FakeJina()
    texts = [f"text {i}" for i in range(7)]
    result = await run(texts, jina, config=settings(JINA_BATCH_TEXTS=3))
    assert [len(batch) for batch in jina.inputs] == [3, 3, 1]
    assert (result.requests, result.billed_tokens) == (3, 21)
    assert result.vectors == [vector_for(t) for t in texts]


# ── retries ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "problem",
    [
        status(429),
        status(500),
        status(503),
        httpx.ConnectError("no network"),
        httpx.ReadTimeout("too slow"),
        httpx.RemoteProtocolError("hung up"),
    ],
)
async def test_a_temporary_problem_is_retried(problem: Reply | Exception) -> None:
    pauses: list[float] = []
    result = await run(["a"], FakeJina(problem), pauses=pauses)
    assert (result.retries, result.requests) == (1, 1)
    assert len(pauses) == 1 and 3.75 <= pauses[0] <= 6.25  # 5 s with jitter


async def test_the_pauses_grow_and_then_the_job_is_asked_to_retry() -> None:
    jina = FakeJina(*[status(503)] * 9)
    pauses: list[float] = []
    with pytest.raises(TransientEmbeddingError, match="not available"):
        await run(["a"], jina, pauses=pauses)
    assert len(jina.requests) == 4 and len(pauses) == 3
    for pause, base in zip(pauses, (5, 10, 20), strict=True):
        assert 0.75 * base <= pause <= 1.25 * base


@pytest.mark.parametrize(("header", "expected"), [("7", 7.0), ("999", 60.0)])
async def test_retry_after_is_followed_up_to_a_minute(header: str, expected: float) -> None:
    pauses: list[float] = []
    await run(["a"], FakeJina(status(429, {"retry-after": header})), pauses=pauses)
    assert pauses == [expected]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
async def test_other_refusals_are_not_retried(code: int) -> None:
    jina = FakeJina(status(code))
    with pytest.raises(PermanentEmbeddingError, match=f"HTTP {code}"):
        await run(["a"], jina)
    assert len(jina.requests) == 1


def _answer(data: object, usage: object = None) -> Reply:
    body = {"data": data, "usage": usage if usage is not None else {"total_tokens": 1}}
    return lambda _: httpx.Response(200, content=json.dumps(body))


_OK = [1.0] * DIMENSIONS


@pytest.mark.parametrize(
    "reply",
    [
        _answer([{"index": 0, "embedding": _OK}]),  # one vector for two texts
        _answer([{"index": 0, "embedding": _OK}, {"index": 0, "embedding": _OK}]),
        _answer([{"index": 0, "embedding": _OK}, {"index": 1, "embedding": [1.0] * 8}]),
        _answer([{"index": 0, "embedding": _OK}, {"index": 1, "embedding": [0.0] * DIMENSIONS}]),
        _answer([{"index": 0, "embedding": _OK}, {"index": 1, "embedding": [float("nan")] * 1024}]),
        _answer([{"index": 0, "embedding": _OK}, {"index": 1, "embedding": _OK}], usage={}),
        lambda _: httpx.Response(200, content=b"<html>not json</html>"),
    ],
)
async def test_an_answer_with_a_wrong_shape_is_never_accepted(reply: Reply) -> None:
    jina = FakeJina(reply)
    with pytest.raises(PermanentEmbeddingError, match="cannot be used"):
        await run(["a", "b"], jina)
    assert len(jina.requests) == 1


async def test_a_failing_second_batch_returns_nothing() -> None:
    jina = FakeJina(good, status(400))
    with pytest.raises(PermanentEmbeddingError):
        await run(["a", "b"], jina, config=settings(JINA_BATCH_TEXTS=1))


# ── before sending ──────────────────────────────────────────────────────


async def test_without_a_key_or_with_an_empty_text_nothing_is_sent() -> None:
    jina = FakeJina()
    with pytest.raises(PermanentEmbeddingError, match="not configured"):
        await run(["a"], jina, config=settings(JINA_API_KEY=None))
    with pytest.raises(PermanentEmbeddingError, match="empty text"):
        await run(["a", "  "], jina)
    assert jina.requests == []


async def test_neither_the_key_nor_the_texts_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="ai_service")
    secret_text = "a private sentence of one tenant"
    await run([secret_text], FakeJina(status(503)))
    with pytest.raises(PermanentEmbeddingError) as refused:
        await run([secret_text], FakeJina(status(401)))
    logged = caplog.text + str(refused.value)
    assert "embedding attempt 1 of 4 failed (HTTP 503)" in logged
    assert KEY not in logged and secret_text not in logged


# ── pacing ──────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def test_the_token_limit_makes_a_request_wait_a_minute() -> None:
    clock = Clock()
    pacer = Pacer(100, 10, clock=clock, sleep=clock.sleep)
    assert await pacer.wait_for(60) == 0
    clock.now += 20
    assert await pacer.wait_for(40) == 0  # exactly 100 in the last minute
    assert await pacer.wait_for(10) == 40  # until the first one is 60 s old


async def test_the_request_limit_and_a_request_larger_than_the_limit() -> None:
    clock = Clock()
    pacer = Pacer(100, 2, clock=clock, sleep=clock.sleep)
    assert await pacer.wait_for(1) == 0
    assert await pacer.wait_for(1) == 0
    assert await pacer.wait_for(1) == 60
    clock.now += 61
    assert await pacer.wait_for(500) == 0  # nothing else counts: it is allowed to go alone


async def test_requests_made_at_the_same_time_share_the_limit() -> None:
    clock = Clock()
    pacer = Pacer(100, 10, clock=clock, sleep=clock.sleep)
    waited = await asyncio.gather(*[pacer.wait_for(50) for _ in range(5)])
    assert waited == [0, 0, 60, 0, 60]


async def test_a_retry_is_counted_by_the_pacer_too() -> None:
    clock = Clock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeJina(status(500)))) as http:
        await embed_passages(
            ["a"],
            http=http,
            settings=settings(),
            counter=WordCounter(),
            pacer=Pacer(10**6, 1, clock=clock, sleep=clock.sleep),
            sleep=clock.sleep,
        )
    assert clock.now - 1000 >= 60  # one request a minute: the retry had to wait


def test_the_default_limits_are_the_ones_of_day_9() -> None:
    fields = Settings.model_fields
    names = ("JINA_BATCH_TEXTS", "JINA_BATCH_TOKENS")
    assert [fields[n].default for n in names] == [32, 12_000]
    names = ("JINA_TOKENS_PER_MINUTE", "JINA_REQUESTS_PER_MINUTE")
    assert [fields[n].default for n in names] == [90_000, 90]
