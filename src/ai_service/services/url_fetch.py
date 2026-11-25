"""Downloading a file from a link a user gave (Day 11, task 1), without letting the link reach
anything it should not ("server-side request forgery").

The worker runs inside my network: it can reach the database, Redis, the API, the host and, on
a cloud machine, the metadata service. So:
1. only http and https, ports 80 and 443, no user name or password in the link;
2. the host name is looked up and EVERY address must be a public one;
3. the connection goes to the address that was checked, not to the name, so a name that gives a
   different address a moment later ("DNS rebinding") changes nothing. The Host header and TLS
   (including the certificate check) still use the name;
4. redirects are followed here, and each new link goes through 1 to 3 again;
5. proxy settings from the environment are ignored (a proxy would do its own lookup);
6. one time limit for everything, a size limit while reading, and no compressed answers.

Messages never say what a name resolved to: that would let a user map my network. Logs name the
host only, because a path or query can contain a secret.
"""

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote

import httpx

logger = logging.getLogger("ai_service.url_fetch")

MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 5
TOTAL_SECONDS = 30.0
_PORTS = {"http": 80, "https": 443}
_REDIRECTS = {301, 302, 303, 307, 308}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")  # an IPv4 address written as IPv6
_NOT_ALLOWED = "This address is not allowed."

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchError(Exception):
    """The message is short and safe to show to the user."""


class BlockedURLError(FetchError):
    """The link points somewhere the server must not go. Never retried."""


class PermanentFetchError(FetchError):
    """Fetching again cannot help."""


class TransientFetchError(FetchError):
    """It may work later."""


@dataclass(frozen=True)
class FetchedFile:
    data: bytes
    filename: str  # ends in .pdf, .md or .txt
    content_type: str
    final_url: str  # after any redirects


def is_public_address(address: str) -> bool:
    """True only for an address on the public internet."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # some IPv6 addresses carry an IPv4 address inside: judge that one
        if ip.ipv4_mapped:
            return is_public_address(str(ip.ipv4_mapped))
        if ip.sixtofour:
            return is_public_address(str(ip.sixtofour))
        if ip.teredo:
            return all(is_public_address(str(part)) for part in ip.teredo)
        if ip in _NAT64:
            return is_public_address(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    return ip.is_global and not (
        ip.is_multicast
        or ip.is_reserved
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
    )


def parse_url(url: str) -> httpx.URL:
    """The checks that need no network (rule 1)."""
    if len(url) > MAX_URL_LENGTH:
        raise BlockedURLError("The link is too long.")
    if any(ord(c) < 33 for c in url) or "\\" in url:
        raise BlockedURLError("The link contains characters that are not allowed.")
    try:
        parsed = httpx.URL(url)
        port = parsed.port
    except (httpx.InvalidURL, ValueError) as exc:
        raise BlockedURLError("This is not a valid link.") from exc
    if parsed.scheme not in _PORTS:
        raise BlockedURLError("Only http and https links are allowed.")
    if not parsed.host:
        raise BlockedURLError("This is not a valid link.")
    if parsed.userinfo:
        raise BlockedURLError("A link with a user name or password is not allowed.")
    if port not in (None, _PORTS[parsed.scheme]):
        raise BlockedURLError("Only the standard web ports are allowed.")
    return parsed


async def system_resolver(host: str, port: int) -> list[str]:
    found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in found))


async def checked_address(url: httpx.URL, resolve: Resolver) -> str:
    """Rule 2: the address to connect to, only if everything the name gives is public."""
    host = url.raw_host.decode("ascii")  # the encoded form of an international name
    try:
        addresses = await resolve(host, url.port or _PORTS[url.scheme])
    except (OSError, UnicodeError) as exc:
        raise PermanentFetchError("The host name of the link could not be found.") from exc
    if not addresses:
        raise PermanentFetchError("The host name of the link could not be found.")
    if not all(is_public_address(address) for address in addresses):
        logger.warning("blocked a link: host %s has an address that is not public", host)
        raise BlockedURLError(_NOT_ALLOWED)
    return addresses[0]


def _pinned_request(url: httpx.URL, address: str) -> httpx.Request:
    """Rule 3: the connection goes to `address`; Host and TLS still use the name."""
    host = url.raw_host.decode("ascii")
    return httpx.Request(
        "GET",
        url.copy_with(host=address),
        headers={
            "Host": url.netloc.decode("ascii"),
            "Accept": "application/pdf, text/markdown, text/plain",
            "Accept-Encoding": "identity",
            "User-Agent": "ai-service-ingest",
        },
        extensions={"sni_hostname": host},
    )


def _file_kind(content_type: str, path: str, data: bytes) -> str:
    """The suffix the parser will use, from the declared type and the path."""
    media = content_type.split(";")[0].strip().lower()
    suffix = PurePosixPath(path).suffix.lower()
    if media == "application/pdf":
        found = ".pdf"
    elif media in ("text/markdown", "text/x-markdown"):
        found = ".md"
    elif media == "text/plain":
        found = ".md" if suffix == ".md" else ".txt"
    elif media in ("application/octet-stream", "") and suffix in (".pdf", ".md", ".txt"):
        found = suffix
    elif media in ("text/html", "application/xhtml+xml"):
        raise PermanentFetchError(
            "The link is a web page. Only links to PDF, Markdown or text files are supported."
        )
    else:
        raise PermanentFetchError("Only links to PDF, Markdown or text files are supported.")
    if found == ".pdf" and not data.startswith(b"%PDF-"):
        raise PermanentFetchError("The link does not give a PDF file.")
    return found


def _filename(path: str, suffix: str) -> str:
    name = PurePosixPath(unquote(path)).name
    stem = re.sub(r"[^\w.\- ]+", "_", name).strip(" .")
    if stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    return f"{stem[:200] or 'download'}{suffix}"


async def _read(response: httpx.Response, max_bytes: int) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        # a small compressed answer can unpack into gigabytes
        raise PermanentFetchError("The server sent a compressed answer, which is not accepted.")
    too_big = PermanentFetchError(
        f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit."
    )
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise too_big
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_raw():  # the declared length may be wrong or missing
        size += len(chunk)
        if size > max_bytes:
            raise too_big
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_url(
    url: str,
    *,
    max_bytes: int,
    resolve: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    total_seconds: float = TOTAL_SECONDS,
) -> FetchedFile:
    """Download the file behind a user's link, following the six rules above."""
    current = parse_url(url)
    # a client of its own: no cookies or logins from anywhere else; rule 5: trust_env=False
    client = httpx.AsyncClient(
        transport=transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0),
        trust_env=False,
        timeout=httpx.Timeout(10.0),
    )
    try:
        async with asyncio.timeout(total_seconds), client:
            for _ in range(MAX_REDIRECTS + 1):
                address = await checked_address(current, resolve)
                response = await client.send(
                    _pinned_request(current, address), stream=True, follow_redirects=False
                )
                try:
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            raise PermanentFetchError("The server sent a broken redirect.")
                        # relative to the link with the NAME, then checked like a new link
                        current = parse_url(str(current.join(location)))
                        continue
                    if response.status_code == 429 or response.status_code >= 500:
                        raise TransientFetchError(
                            f"The server of the link answered HTTP {response.status_code}."
                        )
                    if response.status_code != 200:
                        raise PermanentFetchError(
                            f"The server of the link answered HTTP {response.status_code}."
                        )
                    data = await _read(response, max_bytes)
                    content_type = response.headers.get("content-type", "")
                    suffix = _file_kind(content_type, current.path, data)
                    return FetchedFile(
                        data=data,
                        filename=_filename(current.path, suffix),
                        content_type=content_type,
                        final_url=str(current.copy_with(fragment=None)),
                    )
                finally:
                    await response.aclose()
            raise PermanentFetchError("The link redirects too many times.")
    except TimeoutError as exc:
        raise TransientFetchError("Downloading the link took too long.") from exc
    except httpx.TransportError as exc:
        logger.warning("download from %s failed: %s", current.host, type(exc).__name__)
        raise TransientFetchError("The link could not be downloaded.") from exc
