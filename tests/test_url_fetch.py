"""The safe downloader (Day 11, task 1). A fake server and fake name lookup: no network."""

import asyncio
import ipaddress
import logging
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from ai_service.services.url_fetch import (
    MAX_REDIRECTS,
    BlockedURLError,
    FetchedFile,
    PermanentFetchError,
    TransientFetchError,
    fetch_url,
    is_public_address,
    parse_url,
)

PUBLIC = "93.184.216.34"
NAMES = {
    "files.example": [PUBLIC],
    "other.example": ["8.8.8.8"],
    "inside.example": ["10.0.0.5"],
    "mixed.example": [PUBLIC, "127.0.0.1"],
    "metadata.example": ["169.254.169.254"],
    "sixes.example": ["::1"],
}


async def lookup(host: str, port: int) -> list[str]:
    if host not in NAMES:
        # like the real resolver, a literal address "resolves" to itself
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise OSError("unknown host") from None
        return [host]
    return NAMES[host]


Reply = Callable[[httpx.Request], httpx.Response]


def ok(body: bytes = b"# Title", content_type: str = "text/markdown", **headers: str) -> Reply:
    sent = {"content-type": content_type, "content-length": str(len(body)), **headers}
    # `stream=`, not `content=`: like a real server, the body has not been read yet
    return lambda _: httpx.Response(200, stream=httpx.ByteStream(body), headers=sent)


def redirect(to: str, code: int = 302) -> Reply:
    return lambda _: httpx.Response(code, headers={"location": to})


class Server:
    """Answers each request with the next reply; remembers what was asked for."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return (self.replies.pop(0) if self.replies else ok())(request)


async def get(url: str, server: Server, max_bytes: int = 1024) -> FetchedFile:
    return await fetch_url(
        url, max_bytes=max_bytes, resolve=lookup, transport=httpx.MockTransport(server)
    )


# ── which addresses are public ──────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.9.9.9",
        "0.0.0.0",
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # the cloud metadata service
        "100.64.0.1",  # carrier-grade NAT
        "192.0.0.1",
        "198.18.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4 inside IPv6, four ways
        "::ffff:10.0.0.1",
        "2002:7f00:0001::1",
        "64:ff9b::7f00:1",
        "64:ff9b::a9fe:a9fe",
        "not an address",
        "",
    ],
)
def test_addresses_that_are_not_public(address: str) -> None:
    assert is_public_address(address) is False


@pytest.mark.parametrize(
    "address", [PUBLIC, "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"]
)
def test_public_addresses(address: str) -> None:
    assert is_public_address(address) is True


# ── rule 1: the link itself ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://files.example/a.pdf",
        "gopher://files.example/",
        "data:text/plain,hello",
        "//files.example/a.md",
        "files.example/a.md",
        "http://",
        "http://user:pass@files.example/a.md",
        "http://files.example@inside.example/a.md",
        "http://files.example:8000/a.md",
        "http://files.example:5432/",
        "https://files.example:80/a.md",
        "http://files.example:99999/",
        "http://files.example\\@inside.example/",
        "http://files.example/a b.md",
        "http://files.example/a\r\nHost: inside.example",
        "http://files.example/" + "a" * 2100,
    ],
)
async def test_links_that_are_refused_before_any_lookup(url: str) -> None:
    server = Server()
    with pytest.raises(BlockedURLError):
        await get(url, server)
    assert server.seen == []


def test_the_standard_ports_may_be_written_out() -> None:
    assert parse_url("http://files.example:80/a.md").host == "files.example"
    assert parse_url("https://files.example:443/a.md").host == "files.example"


# ── rule 2: where the name leads ────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://inside.example/a.md",
        "http://mixed.example/a.md",  # one public and one private address
        "http://metadata.example/latest/meta-data/",
        "http://sixes.example/a.md",
        "http://127.0.0.1/a.md",
        "http://[::1]/a.md",
        "http://[::ffff:127.0.0.1]/a.md",
        "http://169.254.169.254/",
        "http://0.0.0.0/",
    ],
)
async def test_a_name_or_address_that_is_not_public_is_never_requested(url: str) -> None:
    server = Server()
    with pytest.raises(BlockedURLError, match="This address is not allowed."):
        await get(url, server)
    assert server.seen == []


async def test_an_unknown_name() -> None:
    with pytest.raises(PermanentFetchError, match="could not be found"):
        await get("http://nowhere.example/a.md", Server())


# ── rule 3: connect to the checked address ──────────────────────────────


async def test_the_connection_goes_to_the_checked_address_with_the_real_name() -> None:
    server = Server()
    await get("https://files.example/docs/a.md?x=1", server)
    request = server.seen[0]
    assert (request.url.host, request.url.path, request.url.query) == (PUBLIC, "/docs/a.md", b"x=1")
    assert request.headers["host"] == "files.example"
    assert request.extensions["sni_hostname"] == "files.example"  # also checks the certificate
    assert request.headers["accept-encoding"] == "identity"
    assert "cookie" not in request.headers and "authorization" not in request.headers


async def test_a_name_that_changes_its_address_is_looked_up_only_once_per_request() -> None:
    answers = [[PUBLIC], ["127.0.0.1"]]  # public when checked, private a moment later
    asked: list[str] = []

    async def rebinding(host: str, port: int) -> list[str]:
        asked.append(host)
        return answers.pop(0)

    server = Server()
    await fetch_url(
        "http://rebind.example/a.md",
        max_bytes=100,
        resolve=rebinding,
        transport=httpx.MockTransport(server),
    )
    assert asked == ["rebind.example"]
    assert server.seen[0].url.host == PUBLIC


# ── rule 4: redirects ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target",
    [
        "http://inside.example/secret",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "http://files.example:6379/",
        "//inside.example/x",
        "http://user:pass@other.example/",
    ],
)
@pytest.mark.parametrize("code", [301, 302, 307, 308])
async def test_a_redirect_to_somewhere_forbidden_is_not_followed(target: str, code: int) -> None:
    server = Server(redirect(target, code))
    with pytest.raises(BlockedURLError):
        await get("http://files.example/a.md", server)
    assert len(server.seen) == 1  # only the first, public, request was made


async def test_allowed_redirects_are_followed_and_checked_each_time() -> None:
    server = Server(redirect("/moved/b.md"), redirect("https://other.example/final/c.md"), ok())
    found = await get("http://files.example/a.md", server)
    assert [(r.headers["host"], r.url.host, r.url.path) for r in server.seen] == [
        ("files.example", PUBLIC, "/a.md"),
        ("files.example", PUBLIC, "/moved/b.md"),
        ("other.example", "8.8.8.8", "/final/c.md"),
    ]
    assert (found.final_url, found.filename) == ("https://other.example/final/c.md", "c.md")


async def test_too_many_redirects() -> None:
    server = Server(*[redirect("/again")] * 20)
    with pytest.raises(PermanentFetchError, match="too many"):
        await get("http://files.example/a.md", server)
    assert len(server.seen) == MAX_REDIRECTS + 1


# ── rule 5: no proxy ────────────────────────────────────────────────────


async def test_proxy_settings_of_the_environment_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[dict[str, object]] = []
    real = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        made.append(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    await get("http://files.example/a.md", Server())
    assert made[0]["trust_env"] is False


# ── rule 6: size, compression, time ─────────────────────────────────────


async def test_a_declared_size_over_the_limit() -> None:
    with pytest.raises(PermanentFetchError, match="larger than"):
        await get("http://files.example/a.md", Server(ok(b"x" * 11)), max_bytes=10)
    assert (await get("http://files.example/a.md", Server(ok(b"x" * 10)), max_bytes=10)).data


async def test_the_limit_holds_when_the_length_is_not_declared() -> None:
    produced: list[int] = []

    async def endless() -> AsyncIterator[bytes]:
        while True:
            produced.append(1)
            yield b"x" * 100

    def reply(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless(), headers={"content-type": "text/plain"})

    with pytest.raises(PermanentFetchError, match="larger than"):
        await get("http://files.example/a.txt", Server(reply), max_bytes=1000)
    assert len(produced) == 11  # it stopped as soon as the limit was passed


async def test_a_compressed_answer_is_refused() -> None:
    server = Server(ok(b"\x1f\x8b...", **{"content-encoding": "gzip"}))
    with pytest.raises(PermanentFetchError, match="compressed"):
        await get("http://files.example/a.md", server)


async def test_one_time_limit_covers_everything() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    with pytest.raises(TransientFetchError, match="took too long"):
        await fetch_url(
            "http://files.example/a.md",
            max_bytes=10,
            resolve=lookup,
            transport=httpx.MockTransport(slow),
            total_seconds=0.1,
        )


# ── what comes back ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("content_type", "path", "body", "filename"),
    [
        ("application/pdf", "/p/paper", b"%PDF-1.7 x", "paper.pdf"),
        ("application/pdf", "/2504.08748.pdf", b"%PDF-1.7 x", "2504.08748.pdf"),
        ("text/markdown; charset=utf-8", "/a/README", b"# t", "README.md"),
        ("text/plain; charset=utf-8", "/a/cors.md", b"# t", "cors.md"),
        ("text/plain", "/notes", b"t", "notes.txt"),
        ("application/octet-stream", "/x/y.pdf", b"%PDF-1.7", "y.pdf"),
        ("text/plain", "/", b"t", "download.txt"),
        ("text/plain", "/a%2F..%2F..%2Fetc%2Fpasswd", b"t", "passwd.txt"),
        ("text/plain", "/my%20file%3C%3E.txt", b"t", "my file_.txt"),
    ],
)
async def test_the_file_name_and_kind(
    content_type: str, path: str, body: bytes, filename: str
) -> None:
    found = await get(f"http://files.example{path}", Server(ok(body, content_type)))
    assert (found.filename, found.data) == (filename, body)
    assert "/" not in found.filename


@pytest.mark.parametrize(
    ("content_type", "path", "body", "message"),
    [
        ("text/html; charset=utf-8", "/page", b"<html>", "web page"),
        ("text/html", "/looks-like.pdf", b"<html>", "web page"),
        ("image/png", "/a.png", b"x", "Only links to"),
        ("application/octet-stream", "/a.exe", b"x", "Only links to"),
        ("application/pdf", "/a.pdf", b"<html>not a pdf", "does not give a PDF"),
    ],
)
async def test_other_kinds_of_content_are_refused(
    content_type: str, path: str, body: bytes, message: str
) -> None:
    with pytest.raises(PermanentFetchError, match=message):
        await get(f"http://files.example{path}", Server(ok(body, content_type)))


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (404, PermanentFetchError),
        (401, PermanentFetchError),
        (204, PermanentFetchError),
        (429, TransientFetchError),
        (500, TransientFetchError),
        (503, TransientFetchError),
    ],
)
async def test_other_status_codes(code: int, error: type[Exception]) -> None:
    with pytest.raises(error, match=f"HTTP {code}"):
        await get("http://files.example/a.md", Server(lambda _: httpx.Response(code)))


async def test_a_network_problem_is_temporary() -> None:
    def broken(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused: 10.9.8.7")

    with pytest.raises(TransientFetchError) as failed:
        await get("http://files.example/a.md", Server(broken))
    assert "10.9.8.7" not in str(failed.value)


async def test_neither_the_message_nor_the_log_gives_away_addresses_or_the_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="ai_service")
    with pytest.raises(BlockedURLError) as blocked:
        await get("http://inside.example/a.md?token=SECRET-TOKEN", Server())
    logged = "".join(r.getMessage() for r in caplog.records if r.name.startswith("ai_service"))
    assert "10.0.0.5" not in str(blocked.value) + logged
    assert "SECRET-TOKEN" not in str(blocked.value) + logged
    assert "inside.example" in logged
