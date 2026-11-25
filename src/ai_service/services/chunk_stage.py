"""The "chunk" stage of the ingestion job (Day 11, task 1): from cleaned text to the pieces that
will be embedded and saved, each with its heading path, PDF pages and optional header.
"""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from ai_service.services.chunking import (
    TextChunk,
    add_contextual_headers,
    contextual_header,
    fixed_size_chunks,
    markdown_blocks,
    markdown_chunks,
    recursive_chunks,
)
from ai_service.services.cleaning import CleanDocument, PageSpan
from ai_service.services.tokenizer import TokenCounter

# Part of the pipeline version. Change it when a chunker cuts differently.
CHUNKER_VERSION = "1"

Strategy = Literal["auto", "fixed", "recursive", "markdown"]
_CHUNKERS = {
    "fixed": fixed_size_chunks,
    "recursive": recursive_chunks,
    "markdown": markdown_chunks,
}
_TITLE_LENGTH = 255  # documents.title


@dataclass(frozen=True)
class ChunkOptions:
    strategy: Strategy = "auto"
    max_tokens: int = 400
    overlap_tokens: int = 60
    context_headers: bool = False


@dataclass(frozen=True)
class PreparedChunk:
    chunk: TextChunk
    # the first and last PDF page the chunk has text from; None for a file without pages
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True)
class ChunkedDocument:
    title: str
    chunker: str  # the one that was really used (never "auto")
    chunks: tuple[PreparedChunk, ...]


def choose_chunker(strategy: Strategy, kind: str) -> str:
    """ "auto": the Markdown chunker for Markdown, the recursive one for PDF and plain text,
    which have no Markdown headings (a "#" line in them is not a heading)."""
    if strategy != "auto":
        return strategy
    return "markdown" if kind == "markdown" else "recursive"


def _title_from_filename(filename: str) -> str:
    """ "strict_mode.md" -> "Strict Mode". A name that has spaces is left as it is."""
    stem = Path(filename).stem or filename
    if " " not in stem:
        stem = " ".join(part for part in stem.replace("_", "-").split("-") if part) or stem
        if stem == stem.lower():
            stem = stem.title()
    return stem


def document_title(text: str, kind: str, filename: str, given: str | None = None) -> str:
    """The title the user gave; else, in Markdown, the first level-1 heading that is NOT inside
    a code block (a "# comment" in code is not a title); else the file name."""
    title = (given or "").strip()
    if not title and kind == "markdown":
        title = next(
            (b.title for b in markdown_blocks(text) if b.kind == "heading" and b.level == 1), ""
        ).strip()
    return (title or _title_from_filename(filename))[:_TITLE_LENGTH]


def pages_of(start: int, end: int, pages: tuple[PageSpan, ...]) -> tuple[int | None, int | None]:
    """The numbers of the first and last page that text[start:end] overlaps."""
    touched = [p.number for p in pages if p.start < end and p.end > start]
    return (touched[0], touched[-1]) if touched else (None, None)


def chunk_document(
    cleaned: CleanDocument,
    *,
    kind: str,
    filename: str,
    counter: TokenCounter,
    options: ChunkOptions,
    given_title: str | None = None,
) -> ChunkedDocument:
    """Blocking and CPU-bound, so async callers run it in a thread."""
    title = document_title(cleaned.text, kind, filename, given_title)
    name = choose_chunker(options.strategy, kind)
    chunks = _CHUNKERS[name](
        cleaned.text,
        counter,
        max_tokens=options.max_tokens,
        overlap_tokens=options.overlap_tokens,
    )
    if kind != "markdown":
        # only the "markdown" chunker would set headings here, from lines that merely start
        # with "#"
        chunks = [replace(c, headings=()) for c in chunks]
    if options.context_headers:
        if kind == "markdown":
            chunks = add_contextual_headers(cleaned.text, chunks, title)
        else:
            chunks = [replace(c, header=contextual_header(title, ())) for c in chunks]
    return ChunkedDocument(
        title=title,
        chunker=name,
        chunks=tuple(PreparedChunk(c, *pages_of(c.start, c.end, cleaned.pages)) for c in chunks),
    )
