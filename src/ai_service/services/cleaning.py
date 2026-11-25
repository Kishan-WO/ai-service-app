"""Removing from parsed text what is not content (Day 11, task 1: "clean").

The rules are conservative: leaving a stray footer is better than deleting real text. They were
chosen after looking at my PDFs (notes/day-11). Change CLEANER_VERSION when a rule changes.

All files:    Unicode NFC, ligatures expanded, invisible and unreadable characters removed,
              spaces at line ends and extra blank lines removed.
PDFs only:    repeated header and footer lines, page numbers, words split by a hyphen.
Markdown only: website markup (ai_service.services.markdown_cleaning).
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from ai_service.core.exceptions import InvalidDocumentError
from ai_service.services.markdown_cleaning import clean_markdown
from ai_service.services.parsing import ParsedDocument

CLEANER_VERSION = "2"

_EDGE_LINES = 2  # how many lines at the top and bottom of a page may be a header or footer
_REPEATED_SHARE = 0.4  # on this share of the pages (headers that alternate reach about half)
_MIN_REPEATS = 3

_LIGATURES = {code: unicodedata.normalize("NFKC", chr(code)) for code in range(0xFB00, 0xFB07)}
# Removed: zero-width space, byte-order mark and soft hyphen. NOT removed: the zero-width
# joiner and non-joiner (U+200D, U+200C), which change how Hindi and other scripts read.
_INVISIBLE = {"​", "﻿", "­"}
# the second half is only looked at, so that it can also be the first half of the next break
_HYPHEN_BREAK = re.compile(r"([A-Za-z]{2,})-\n(?=([a-z]{2,}))")
# Parts that normally keep their hyphen, used only when the document itself gives no answer.
_HYPHEN_PREFIXES = {"non", "self"}
_HYPHEN_SUFFIXES = {"aware", "based", "free", "level", "specific", "trained"}


@dataclass(frozen=True)
class PageSpan:
    number: int  # the page number in the file, from 1
    start: int  # character positions in the cleaned text
    end: int


@dataclass(frozen=True)
class CleanDocument:
    text: str
    pages: tuple[PageSpan, ...]  # PDFs only: where each page is, for citations
    removed: dict[str, int]  # what each rule did
    removed_lines: tuple[str, ...]  # the different header and footer lines that were dropped
    empty_pages: tuple[int, ...]  # PDF pages that gave no text (a picture or a scan?)


def _clean_characters(text: str, done: Counter[str]) -> str:
    done["ligatures expanded"] += sum(ord(c) in _LIGATURES for c in text)
    text = text.translate(_LIGATURES).replace("\r\n", "\n").replace("\r", "\n")
    kept = []
    for char in text:
        category = unicodedata.category(char)
        # control characters, and "private use" glyphs that have no meaning outside their font
        if char in _INVISIBLE or category == "Co" or (category == "Cc" and char not in "\n\t"):
            done["unreadable characters removed"] += 1
        else:
            kept.append(char)
    return unicodedata.normalize("NFC", "".join(kept))


def _tidy_lines(text: str) -> str:
    text = re.sub(r"[ \t]+(?=\n|$)", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def _edge_positions(lines: list[str]) -> list[int]:
    """Indexes of the first and last non-empty lines of a page."""
    filled = [i for i, line in enumerate(lines) if line.strip()]
    return sorted({*filled[:_EDGE_LINES], *filled[-_EDGE_LINES:]})


def _line_key(line: str) -> str:
    """Lines that differ only in their numbers are the same running header."""
    return re.sub(r"\d+", "#", " ".join(line.split()))


def _remove_running_lines(
    pages: list[list[str]], done: Counter[str]
) -> tuple[list[list[str]], set[str]]:
    needed = max(_MIN_REPEATS, _REPEATED_SHARE * sum(any(p) for p in pages))
    seen: Counter[str] = Counter()
    # a bare number is a page number only if it counts up with the pages: number - index
    offsets: Counter[int] = Counter()
    for index, lines in enumerate(pages):
        edges = [lines[i].strip() for i in _edge_positions(lines)]
        seen.update({_line_key(e) for e in edges if any(c.isalpha() for c in e)})
        offsets.update({int(e) - index for e in edges if re.fullmatch(r"\d{1,4}", e)})
    repeated = {key for key, count in seen.items() if count >= needed}
    page_offsets = {offset for offset, count in offsets.items() if count >= needed}

    dropped: set[str] = set()
    result = []
    for index, lines in enumerate(pages):
        drop = set()
        for i in _edge_positions(lines):
            line = lines[i].strip()
            if _line_key(line) in repeated:
                done["header or footer lines removed"] += 1
                dropped.add(_line_key(line))
                drop.add(i)
            elif re.fullmatch(r"\d{1,4}", line) and int(line) - index in page_offsets:
                done["page numbers removed"] += 1
                drop.add(i)
        result.append([line for i, line in enumerate(lines) if i not in drop])
    return result, dropped


def _join_hyphenated(pages: list[str], done: Counter[str]) -> list[str]:
    """Words broken at a line end: "curricu-\nlum" -> "curriculum". The hyphen is kept when
    the word is part of a longer hyphenated one ("state-of-\nthe-art"), when the document
    writes it elsewhere only with the hyphen, or for a few prefixes and endings. Otherwise
    the halves are joined, which can still be wrong for a compound I cannot recognise.
    """
    vocabulary = set(re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*", "\n".join(pages).lower()))

    def join(match: re.Match[str]) -> str:
        left, right = match.group(1), match.group(2)
        text = match.string
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end() + len(right) : match.end() + len(right) + 1]
        joined, hyphenated = f"{left}{right}".lower(), f"{left}-{right}".lower()
        if before == "-" or after == "-":
            keep = True
        elif joined in vocabulary:
            keep = False
        else:
            keep = (
                hyphenated in vocabulary
                or left.lower() in _HYPHEN_PREFIXES
                or right in _HYPHEN_SUFFIXES
            )
        done["hyphens kept, line break removed" if keep else "words joined"] += 1
        return f"{left}-" if keep else left

    return [_HYPHEN_BREAK.sub(join, page) for page in pages]


def clean_document(parsed: ParsedDocument) -> CleanDocument:
    done: Counter[str] = Counter()
    pages = [_clean_characters(page, done) for page in parsed.pages]
    dropped: set[str] = set()
    if parsed.kind == "pdf":
        split = [_tidy_lines(page).split("\n") for page in pages]
        split, dropped = _remove_running_lines(split, done)
        pages = _join_hyphenated(["\n".join(lines) for lines in split], done)
    elif parsed.kind == "markdown":
        pages = [clean_markdown(page, done) for page in pages]
    pages = [_tidy_lines(page) for page in pages]

    text, spans, empty = "", [], []
    for number, page in enumerate(pages, 1):
        if not page.strip():
            empty.append(number)
            continue
        if text:
            text += "\n\n"
        spans.append(PageSpan(number, len(text), len(text) + len(page)))
        text += page
    if not text.strip():
        raise InvalidDocumentError("The file contains no text.")
    is_pdf = parsed.kind == "pdf"
    return CleanDocument(
        text=text,
        pages=tuple(spans) if is_pdf else (),
        removed={rule: count for rule, count in done.items() if count},
        removed_lines=tuple(sorted(dropped)),
        empty_pages=tuple(empty) if is_pdf else (),
    )
