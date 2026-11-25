"""Parsing and cleaning (Day 11, task 1)."""

import io

import pytest
from pypdf import PdfWriter

from ai_service.core.exceptions import InvalidDocumentError, UnsupportedFileTypeError
from ai_service.services.cleaning import CleanDocument, clean_document
from ai_service.services.parsing import ParsedDocument, parse_file


def pdf(*pages: str) -> CleanDocument:
    return clean_document(ParsedDocument("pdf", pages, "test"))


def text_file(text: str, kind: str = "markdown") -> CleanDocument:
    return clean_document(ParsedDocument(kind, (text,), "test"))


# ── parsing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("name", "kind"), [("a.md", "markdown"), ("a.TXT", "text")])
def test_a_text_file_is_one_page_without_its_byte_order_mark(name: str, kind: str) -> None:
    parsed = parse_file(name, "﻿नमस्ते".encode())
    assert (parsed.kind, parsed.pages) == (kind, ("नमस्ते",))


def test_unsupported_and_broken_files() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        parse_file("a.docx", b"x")
    with pytest.raises(InvalidDocumentError, match="UTF-8"):
        parse_file("a.md", b"\xff\xfe\x00bad")
    with pytest.raises(InvalidDocumentError, match="could not be read"):
        parse_file("a.pdf", b"this is not a pdf")


def _blank_pdf(password: str | None = None) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_a_pdf_gives_one_string_per_page_and_encrypted_is_refused() -> None:
    parsed = parse_file("a.pdf", _blank_pdf())
    assert (parsed.kind, parsed.pages) == ("pdf", ("",))
    assert parsed.parser.startswith("pypdf-")
    with pytest.raises(InvalidDocumentError, match="Encrypted"):
        parse_file("a.pdf", _blank_pdf("secret"))


def test_a_file_without_text_is_refused() -> None:
    with pytest.raises(InvalidDocumentError, match="no text"):
        clean_document(parse_file("scan.pdf", _blank_pdf()))


# ── characters and lines (all files) ────────────────────────────────────


def test_ligatures_and_unreadable_characters() -> None:
    found = text_file("eﬃcient​ te\x10xt soft­word\x00")
    assert found.text == "efficient text softword"
    assert found.removed == {"ligatures expanded": 1, "unreadable characters removed": 5}


def test_hindi_joiners_tabs_and_indentation_are_kept() -> None:
    source = "क्‍ष and क्‌ष\n\n    code\tindented\n| ² x½ |"
    assert text_file(source).text == source


def test_the_same_letter_written_two_ways_becomes_one() -> None:
    assert text_file("café").text == "café"


def test_line_ends_and_extra_blank_lines() -> None:
    assert text_file("a  \r\n\r\n\r\n\r\nb\t\n").text == "a\n\nb"


def test_markdown_is_not_treated_like_a_pdf() -> None:
    source = "Title\n\n1\n\nan inter-\nesting word\n\nTitle\n\n2"
    found = text_file(source)
    assert found.text == source and found.pages == () and found.removed == {}


# ── PDF headers, footers, page numbers ──────────────────────────────────


def _pages(n: int) -> list[str]:
    return [
        f"Journal of Tests, Vol. 3\nbody of page {chr(97 + i)} one\nmiddle {chr(97 + i)}\n"
        f"body {chr(97 + i)} two\n{i + 1}"
        for i in range(n)
    ]


def test_running_header_and_page_numbers_are_removed() -> None:
    found = pdf(*_pages(6))
    assert "Journal of Tests" not in found.text
    assert found.removed == {"header or footer lines removed": 6, "page numbers removed": 6}
    assert found.removed_lines == ("Journal of Tests, Vol. #",)
    assert found.text.startswith("body of page a one\nmiddle a\nbody a two\n\nbody of page b")


def test_page_numbers_that_do_not_start_at_one_are_still_found() -> None:
    pages = [f"words {chr(97 + i)}\n{i + 41}" for i in range(5)]
    assert pdf(*pages).removed == {"page numbers removed": 5}


def test_numbers_at_the_page_edge_that_do_not_count_up_are_kept() -> None:
    """Table cells can end a page. They are data, not page numbers."""
    pages = [f"intro {chr(97 + i)}\nvalue\n{v}" for i, v in enumerate([7, 93, 12, 58, 4])]
    found = pdf(*pages)
    assert all(str(v) in found.text for v in (7, 93, 12, 58, 4))
    decimals = pdf(*[f"row {chr(97 + i)}\n0.{i}1" for i in range(5)])
    assert "0.41" in decimals.text


def test_a_repeated_line_in_the_middle_of_pages_is_content() -> None:
    pages = [f"a{i}x\nb{i}x\nSee the table.\nc{i}x\nd{i}x" for i in "pqrst"]
    assert pdf(*pages).text.count("See the table.") == 5


def test_a_line_on_too_few_pages_is_kept() -> None:
    pages = [f"{'Notice' if i < 2 else 'text'} {chr(97 + i)}\nbody {chr(97 + i)}" for i in range(8)]
    assert pdf(*pages).text.count("Notice") == 2


def test_two_pages_are_too_few_to_call_anything_a_header() -> None:
    assert pdf("Same\nfirst", "Same\nsecond").text.count("Same") == 2


# ── PDF hyphenation ─────────────────────────────────────────────────────


def test_a_split_word_is_joined() -> None:
    found = pdf("the curricu-\nlum of the course")
    assert found.text == "the curriculum of the course"
    assert found.removed == {"words joined": 1}


def test_a_hyphen_that_the_document_uses_elsewhere_is_kept() -> None:
    found = pdf("a well-known fact. This is well-\nknown too")
    assert found.text == "a well-known fact. This is well-known too"
    assert found.removed == {"hyphens kept, line break removed": 1}


def test_the_joined_word_wins_when_the_document_writes_it_both_ways() -> None:
    found = pdf("online and on-line. Then on-\nline again")
    assert found.text == "online and on-line. Then online again"
    assert found.removed == {"words joined": 1}


@pytest.mark.parametrize("source", ["COVID-\n19 cases", "see A-\nB here", "Jean-\nPaul spoke"])
def test_numbers_single_letters_and_capitals_are_left_alone(source: str) -> None:
    assert pdf(source).text == source


# ── pages ───────────────────────────────────────────────────────────────


def test_page_spans_point_at_each_page_and_empty_pages_are_reported() -> None:
    found = pdf("first page", "   ", "third page")
    assert [(p.number, found.text[p.start : p.end]) for p in found.pages] == [
        (1, "first page"),
        (3, "third page"),
    ]
    assert found.empty_pages == (2,)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("human-in-\nthe loop", "human-in-the loop"),  # a hyphen before the break
        ("state-\nof-the-art", "state-of-the-art"),  # a hyphen after it
        ("a state-\nof-\nthe-art model", "a state-of-the-art model"),  # broken twice
        ("it is prompt-\nbased", "it is prompt-based"),
        ("a non-\ncompliant case", "a non-compliant case"),
        ("self-\nattention", "self-attention"),
        # what the document writes elsewhere wins over the list
        ("nonlinear maps. A non-\nlinear one", "nonlinear maps. A nonlinear one"),
        ("they pre-\nsented it", "they presented it"),
    ],
)
def test_compounds_keep_their_hyphen(source: str, expected: str) -> None:
    assert pdf(source).text == expected
