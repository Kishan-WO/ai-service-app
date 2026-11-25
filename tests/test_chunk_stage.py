"""The chunk stage (Day 11, task 1). A small fake tokenizer, so these run anywhere."""

import re

import pytest

from ai_service.core.config import Settings
from ai_service.services.chunk_stage import (
    ChunkedDocument,
    ChunkOptions,
    choose_chunker,
    chunk_document,
    document_title,
    pages_of,
)
from ai_service.services.cleaning import PageSpan, clean_document
from ai_service.services.parsing import ParsedDocument


class FakeCounter:
    """Every word is cut into pieces of up to 4 characters, one token each."""

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [
            (start, min(start + 4, word.end()))
            for word in re.finditer(r"\S+", text)
            for start in range(word.start(), word.end(), 4)
        ]

    def count(self, text: str) -> int:
        return len(self.offsets(text))


def stage(
    *pages: str,
    kind: str = "markdown",
    filename: str = "guide.md",
    title: str | None = None,
    **options: object,
) -> ChunkedDocument:
    cleaned = clean_document(ParsedDocument(kind, pages, "test"))
    return chunk_document(
        cleaned,
        kind=kind,
        filename=filename,
        counter=FakeCounter(),
        options=ChunkOptions(**options),  # type: ignore[arg-type]
        given_title=title,
    )


# ── which chunker ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("strategy", "kind", "expected"),
    [
        ("auto", "markdown", "markdown"),
        ("auto", "pdf", "recursive"),
        ("auto", "text", "recursive"),
        ("fixed", "markdown", "fixed"),
        ("recursive", "markdown", "recursive"),
        ("markdown", "pdf", "markdown"),
    ],
)
def test_which_chunker_is_used(strategy: str, kind: str, expected: str) -> None:
    assert choose_chunker(strategy, kind) == expected  # type: ignore[arg-type]
    assert stage("aa bb", kind=kind, strategy=strategy).chunker == expected


def test_the_size_options_reach_the_chunker() -> None:
    found = stage("aa bb cc dd ee ff gg hh", kind="text", max_tokens=4, overlap_tokens=0)
    assert [p.chunk.text for p in found.chunks] == ["aa bb cc dd", "ee ff gg hh"]


# ── titles ──────────────────────────────────────────────────────────────


def test_a_comment_in_code_is_never_the_title() -> None:
    text = "```python\n# returns a dictionary:\n```\n\n## Sub\n\n# Serializing data { #s }\n\nx"
    assert document_title(text, "markdown", "serialization.md") == "Serializing data"


@pytest.mark.parametrize(
    ("text", "kind", "given", "expected"),
    [
        ("# Heading\n\nx", "markdown", "  My title ", "My title"),  # the user's title wins
        ("# Heading\n\nx", "markdown", "   ", "Heading"),  # a blank one does not count
        ("## Only level two\n\nx", "markdown", None, "My File"),
        ("# not a heading in a pdf\nx", "pdf", None, "My File"),
        ("# nor in a text file\nx", "text", None, "My File"),
    ],
)
def test_where_the_title_comes_from(text: str, kind: str, given: str | None, expected: str) -> None:
    assert document_title(text, kind, "my_file.md", given) == expected


def test_a_title_fits_the_database_column() -> None:
    assert len(document_title("x", "text", "a.txt", "t" * 900)) == 255


# ── pages ───────────────────────────────────────────────────────────────

SPANS = (PageSpan(1, 0, 10), PageSpan(3, 12, 20), PageSpan(4, 22, 30))


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (0, 10, (1, 1)),
        (5, 15, (1, 3)),  # page 2 had no text, so no characters
        (0, 30, (1, 4)),
        (12, 13, (3, 3)),
        (9, 12, (1, 1)),  # reaches the start of page 3 but has none of its text
        (10, 12, (None, None)),  # only the gap between two pages
    ],
)
def test_the_pages_a_piece_overlaps(
    start: int, end: int, expected: tuple[int | None, int | None]
) -> None:
    assert pages_of(start, end, SPANS) == expected


def test_each_pdf_chunk_knows_its_pages() -> None:
    found = stage(
        "aa bb cc",
        "dd ee ff",
        "gg hh ii",
        kind="pdf",
        filename="p.pdf",
        max_tokens=5,
        overlap_tokens=0,
    )
    assert [(p.chunk.text, p.page_start, p.page_end) for p in found.chunks] == [
        ("aa bb cc", 1, 1),
        ("dd ee ff", 2, 2),
        ("gg hh ii", 3, 3),
    ]
    spanning = stage("aa bb", "cc dd", kind="pdf", max_tokens=9, overlap_tokens=0)
    assert [(p.page_start, p.page_end) for p in spanning.chunks] == [(1, 2)]


def test_a_text_file_has_no_pages() -> None:
    assert [(p.page_start, p.page_end) for p in stage("aa bb").chunks] == [(None, None)]


# ── headings and headers ────────────────────────────────────────────────


def test_headers_are_off_unless_asked_for() -> None:
    chunk = stage("# Guide\n\n## Fees\n\naa bb").chunks[0].chunk
    assert chunk.header == "" and chunk.embedding_text == chunk.text


@pytest.mark.parametrize("strategy", ["auto", "fixed", "recursive", "markdown"])
def test_markdown_headers_name_the_title_and_section(strategy: str) -> None:
    text = "# Guide\n\n## Fees\n\n" + "aa bb cc. " * 6 + "\n\n## Dates\n\n" + "dd ee ff. " * 6
    found = stage(text, strategy=strategy, context_headers=True, max_tokens=12, overlap_tokens=0)
    assert found.title == "Guide"
    assert all(p.chunk.header.startswith("Guide") for p in found.chunks)
    assert found.chunks[-1].chunk.header == "Guide > Dates"


@pytest.mark.parametrize("strategy", ["auto", "markdown"])
def test_a_hash_line_in_a_pdf_is_not_a_section(strategy: str) -> None:
    found = stage(
        "intro words\n# Synth.\nmore words",
        kind="pdf",
        filename="paper.pdf",
        strategy=strategy,
        context_headers=True,
    )
    assert {p.chunk.header for p in found.chunks} == {"Paper"}
    assert all(p.chunk.headings == () for p in found.chunks)


def test_the_default_strategy_setting() -> None:
    assert Settings.model_fields["CHUNK_STRATEGY"].default == "auto"
    assert ChunkOptions() == ChunkOptions("auto", 400, 60, False)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("strict_mode.md", "Strict Mode"),
        ("enterprise-support.md", "Enterprise Support"),
        ("API_reference.md", "API reference"),  # existing capitals are respected
        ("My yearly - report.pdf", "My yearly - report"),  # written with spaces: kept
        ("2504.08748.pdf", "2504.08748"),
        ("___.md", "___"),
    ],
)
def test_a_title_made_from_the_file_name_is_readable(filename: str, expected: str) -> None:
    assert document_title("x", "text", filename) == expected
