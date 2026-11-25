"""Chunkers (Day 11, task 2). A chunk is the piece of a document that gets one vector.

1. fixed_size_chunks: fill each chunk with words up to N tokens, whatever they mean.
2. recursive_chunks: cut at the biggest boundary that fits: paragraph, then sentence, then word.
3. markdown_chunks: structure-aware. One Markdown section (a heading and its text) at a time;
   code blocks and tables stay whole when they fit; each chunk knows its heading path.

All three give chunks of at most N tokens with some overlap, packed by the same code. They
differ in the PIECES that are packed and in what may share a chunk.
Every chunk is an exact slice of the original text (`text[start:end]`), so nothing is rewritten.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace

from ai_service.services.tokenizer import TokenCounter

DEFAULT_MAX_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 60  # 15% of 400


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    start: int  # character positions in the original text
    end: int
    token_count: int  # counted on this chunk's own text
    headings: tuple[str, ...] = ()  # the heading path of the chunk's section
    header: str = ""  # the contextual header, if one was added (add_contextual_headers)

    @property
    def embedding_text(self) -> str:
        """What is given to the embedding model. `text` stays the exact slice of the document:
        it is what is stored, shown and cited."""
        return f"{self.header}\n\n{self.text}" if self.header else self.text


@dataclass(frozen=True)
class _Unit:
    """A piece that is packed whole into chunks: a word, a sentence or a paragraph."""

    start: int
    end: int
    tokens: int


def _units(text: str, counter: TokenCounter, max_tokens: int) -> list[_Unit]:
    """Words (runs of non-space characters) with their token counts, from ONE pass over the text.

    A token belongs to the word that holds its last character. A word with more than
    `max_tokens` tokens (a long URL, an encoded blob) cannot stay whole, so it is cut between
    tokens.
    """
    token_ends = [end for _, end in counter.offsets(text)]
    units: list[_Unit] = []
    token = 0
    for word in re.finditer(r"\S+", text):
        inside: list[int] = []
        while token < len(token_ends) and token_ends[token] <= word.end():
            inside.append(token_ends[token])
            token += 1
        if len(inside) <= max_tokens:
            units.append(_Unit(word.start(), word.end(), len(inside)))
            continue
        start = word.start()
        for i in range(0, len(inside), max_tokens):
            part = inside[i : i + max_tokens]
            end = word.end() if i + max_tokens >= len(inside) else part[-1]
            if end > start:
                units.append(_Unit(start, end, len(part)))
                start = end
    return units


def _split_unit(unit: _Unit, text: str, counter: TokenCounter) -> tuple[_Unit, _Unit]:
    middle = (unit.start + unit.end) // 2
    return (
        _Unit(unit.start, middle, counter.count(text[unit.start : middle])),
        _Unit(middle, unit.end, counter.count(text[middle : unit.end])),
    )


def _check_sizes(max_tokens: int, overlap_tokens: int) -> None:
    if max_tokens < 1:
        raise ValueError("max_tokens must be at least 1")
    if not 0 <= overlap_tokens < max_tokens:
        raise ValueError("overlap_tokens must be at least 0 and smaller than max_tokens")


def _pack(
    text: str,
    units: list[_Unit],
    counter: TokenCounter,
    max_tokens: int,
    overlap_tokens: int,
) -> list[TextChunk]:
    """Put whole units, in order, into chunks of at most `max_tokens`, with whole units as
    overlap. `units` may be changed (a unit that cannot fit is split)."""
    chunks: list[TextChunk] = []
    first = 0
    done = -1  # the last unit that is already in a chunk
    while done < len(units) - 1:
        # take whole units while their counts fit
        last, total = first, units[first].tokens
        while last + 1 < len(units) and total + units[last + 1].tokens <= max_tokens:
            last += 1
            total += units[last].tokens
        # Every chunk must add something new, even if some overlap has to go for it.
        last = max(last, done + 1)
        # A piece counted on its own can differ from the sum, so count it again and shrink.
        while True:
            piece = text[units[first].start : units[last].end]
            count = counter.count(piece)
            if count <= max_tokens:
                break
            if last > done + 1:
                last -= 1  # fewer new words
            elif first <= done:
                first += 1  # less overlap
            elif units[first].end - units[first].start > 1:
                units[first : first + 1] = _split_unit(units[first], text, counter)
                last = first
            else:
                raise ValueError(f"one character needs more than {max_tokens} tokens")
        chunks.append(TextChunk(len(chunks), piece, units[first].start, units[last].end, count))
        done = last
        # step back over whole units for the overlap, but always move forward by one at least
        next_first, repeated = last + 1, 0
        while next_first - 1 > first and repeated + units[next_first - 1].tokens <= overlap_tokens:
            next_first -= 1
            repeated += units[next_first].tokens
        first = next_first
    return chunks


def fixed_size_chunks(
    text: str,
    counter: TokenCounter,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[TextChunk]:
    """Cut `text` into chunks of at most `max_tokens`, cutting between words where possible.

    Each chunk after the first starts with up to `overlap_tokens` of whole words from the end
    of the one before, so a sentence cut at a boundary is more likely to be whole in one of
    them. The cut ignores meaning: sentences, headings and tables can be split.
    """
    _check_sizes(max_tokens, overlap_tokens)
    units = _units(text, counter, max_tokens)
    return _pack(text, units, counter, max_tokens, overlap_tokens)


# Where to cut, from the most meaningful boundary to the least. The match itself is the gap
# between two pieces.
_SEPARATORS = (
    re.compile(r"\n[ \t\r]*\n\s*"),  # a blank line: between paragraphs
    # after . ! ? or the Hindi danda (। and ॥), also when a quote or bracket closes there
    re.compile(r"(?:(?<=[.!?।॥])|(?<=[.!?।॥][\"'”’)\]]))\s+"),
    re.compile(r"\s+"),  # between words
)


def _pieces(
    text: str, start: int, end: int, counter: TokenCounter, max_tokens: int, level: int = 0
) -> list[_Unit]:
    """text[start:end] as one unit if it fits; otherwise split at the separator of `level` and
    treat every part the same way with the next separator."""
    tokens = counter.count(text[start:end])
    if tokens <= max_tokens:
        return [_Unit(start, end, tokens)]
    if level == len(_SEPARATORS):  # one word that is too long: cut it between tokens
        return [
            _Unit(start + unit.start, start + unit.end, unit.tokens)
            for unit in _units(text[start:end], counter, max_tokens)
        ]
    parts: list[tuple[int, int]] = []
    begin = start
    for gap in _SEPARATORS[level].finditer(text, start, end):
        parts.append((begin, gap.start()))
        begin = gap.end()
    parts.append((begin, end))
    return [
        unit
        for a, b in parts
        if b > a
        for unit in _pieces(text, a, b, counter, max_tokens, level + 1)
    ]


def recursive_chunks(
    text: str,
    counter: TokenCounter,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[TextChunk]:
    """Cut `text` into chunks of at most `max_tokens` at the biggest boundaries that fit.

    The order is the plan's: paragraph, sentence, word. A paragraph that fits is never cut; a
    longer one is cut at sentence ends, a longer sentence between words. A single line break is
    not a boundary of its own, because text taken from PDFs breaks lines inside sentences.
    Neighbouring pieces are then packed together up to the limit. Overlap repeats whole pieces
    only, so there is none where the last piece of a chunk is larger than `overlap_tokens`.
    Markdown headings, tables and code get no special treatment.
    """
    _check_sizes(max_tokens, overlap_tokens)
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    if end <= start:
        return []
    units = _pieces(text, start, end, counter, max_tokens)
    return _pack(text, units, counter, max_tokens, overlap_tokens)


# ── structure-aware: Markdown ───────────────────────────────────────────

_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")
_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.*)")
_HEADING_ANCHOR = re.compile(r"[ \t]*\{[ \t]*#[^}]*\}[ \t]*$")  # "## Title { #anchor }"
_HTML_ANCHOR = re.compile(r"<a\b[^<>]*>[ \t]*</a>")  # an empty "<a name=...></a>"


@dataclass(frozen=True)
class _Block:
    kind: str  # "heading", "code", "table" or "text"
    start: int
    end: int
    level: int = 0  # headings only
    title: str = ""  # headings only


def _lines(text: str) -> Iterator[tuple[int, int, str]]:
    """(start, end without the line break, line) for every line."""
    position = 0
    for line in text.split("\n"):
        yield position, position + len(line.rstrip()), line.rstrip()
        position += len(line) + 1


def markdown_blocks(text: str) -> list[_Block]:
    """The headings, fenced code blocks, pipe tables and paragraphs of a Markdown text.

    A "#" line inside a code block is code, not a heading. A fence that is never closed runs to
    the end. Headings underlined with === or --- are not recognised.
    """
    blocks: list[_Block] = []
    fence = ""  # the opening mark while inside a code block
    open_kind, open_start, open_end = "", 0, 0  # the block being collected

    def close() -> None:
        nonlocal open_kind
        if open_kind:
            blocks.append(_Block(open_kind, open_start, open_end))
            open_kind = ""

    for start, end, line in _lines(text):
        stripped = line.strip()
        if fence:
            if stripped:  # blank lines at the end of an unclosed block are not part of it
                open_end = end
            if stripped.startswith(fence) and set(stripped) == {fence[0]}:
                fence = ""
                close()
            continue
        if not stripped:
            close()
            continue
        first = start + len(line) - len(line.lstrip())
        if opening := _FENCE.match(line):
            close()
            fence = opening.group(1)
            open_kind, open_start, open_end = "code", first, end
        elif heading := _HEADING.match(line):
            close()
            title = _HTML_ANCHOR.sub("", heading.group(2))
            title = _HEADING_ANCHOR.sub("", title.rstrip()).rstrip(" #")
            blocks.append(_Block("heading", first, end, len(heading.group(1)), title))
        else:
            kind = "table" if stripped.startswith("|") else "text"
            if open_kind != kind:
                close()
                open_kind, open_start = kind, first
            open_end = end
    close()
    return blocks


def _sections(blocks: list[_Block]) -> list[tuple[tuple[str, ...], list[_Block]]]:
    """(heading path, blocks) for each heading with its text. A heading with no text of its own
    stays with the section that follows it."""
    sections: list[tuple[tuple[str, ...], list[_Block]]] = []
    path: list[tuple[int, str]] = []
    current: list[_Block] = []
    titles: tuple[str, ...] = ()
    for block in blocks:
        if block.kind == "heading":
            if any(b.kind != "heading" for b in current):
                sections.append((titles, current))
                current = []
            path = [(level, title) for level, title in path if level < block.level]
            path.append((block.level, block.title))
            titles = tuple(title for _, title in path)
        current.append(block)
    if current:
        sections.append((titles, current))
    return sections


def _section_units(
    text: str, blocks: list[_Block], counter: TokenCounter, max_tokens: int
) -> list[_Unit]:
    """The pieces of one section: the whole section if it fits, else its blocks. A block that
    is too long is split: code and tables between lines, text between sentences."""
    whole = counter.count(text[blocks[0].start : blocks[-1].end])
    if whole <= max_tokens:
        return [_Unit(blocks[0].start, blocks[-1].end, whole)]
    sentence_level, word_level = 1, 2  # positions in _SEPARATORS
    units: list[_Unit] = []
    for block in blocks:
        tokens = counter.count(text[block.start : block.end])
        if tokens <= max_tokens:
            units.append(_Unit(block.start, block.end, tokens))
        elif block.kind in ("code", "table"):
            for start, end, line in _lines(text[block.start : block.end]):
                if line.strip():
                    indent = len(line) - len(line.lstrip())
                    units += _pieces(
                        text,
                        block.start + start + indent,
                        block.start + end,
                        counter,
                        max_tokens,
                        word_level,
                    )
        else:
            units += _pieces(text, block.start, block.end, counter, max_tokens, sentence_level)
    # keep the headings with the first piece of text below them, if they fit together
    headings = sum(b.kind == "heading" for b in blocks)  # they all come first in a section
    if 0 < headings < len(units):
        together = counter.count(text[units[0].start : units[headings].end])
        if together <= max_tokens:
            units[: headings + 1] = [_Unit(units[0].start, units[headings].end, together)]
    return units


def markdown_chunks(
    text: str,
    counter: TokenCounter,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[TextChunk]:
    """Cut Markdown along its structure.

    A chunk never holds text of two sections. A section that fits is one chunk. In a longer one,
    a code block or table that fits is never cut; one that is too long is cut between lines (a
    table's header row is NOT repeated, because every chunk is an exact slice). Overlap stays
    inside a section. Text without Markdown headings counts as one section.
    """
    _check_sizes(max_tokens, overlap_tokens)
    chunks: list[TextChunk] = []
    for headings, blocks in _sections(markdown_blocks(text)):
        units = _section_units(text, blocks, counter, max_tokens)
        for chunk in _pack(text, units, counter, max_tokens, overlap_tokens):
            chunks.append(replace(chunk, index=len(chunks), headings=headings))
    return chunks


# ── contextual headers (task 3) ─────────────────────────────────────────


def _comparable(title: str) -> str:
    return " ".join(re.sub(r"[`*_]", "", title).split()).casefold()


def contextual_header(title: str, headings: tuple[str, ...]) -> str:
    """The title and the section path, joined by " > ". The title is not repeated when the
    first heading says the same."""
    if headings and _comparable(headings[0]) == _comparable(title):
        headings = headings[1:]
    parts = (" ".join(part.split()) for part in (title, *headings))
    return " > ".join(part for part in parts if part)


def add_contextual_headers(text: str, chunks: list[TextChunk], title: str) -> list[TextChunk]:
    """The same chunks, each with a header made of the document title and its section path.

    Works with every chunker. The section of a chunk is the one it STARTS in: a fixed-size or
    recursive chunk can run on into the next section, and its header does not say so. Chunk
    boundaries, `text` and `token_count` do not change, so search with and without headers
    compares the same pieces. The header's tokens come on top of the chunk size.
    """
    paths: list[tuple[int, tuple[str, ...]]] = [
        (blocks[0].start, titles) for titles, blocks in _sections(markdown_blocks(text))
    ]
    result = []
    for chunk in chunks:
        headings = chunk.headings or next(
            (titles for start, titles in reversed(paths) if start <= chunk.start), ()
        )
        result.append(replace(chunk, headings=headings, header=contextual_header(title, headings)))
    return result
