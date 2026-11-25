"""Day 11, task 2: run the chunkers over every document of the Corpus Tenant, side by side.

Reads `documents.content` from the local database and writes nothing to it. A document's content
is its Day 9 sections joined by blank lines.

    uv run python scripts/chunk_report.py
"""

import asyncio
import re
import statistics
import sys
import time
from collections import Counter

from eval_search import CORPUS_TENANT_ID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ai_service.core.config import get_settings
from ai_service.db.models import Document
from ai_service.services.chunking import (
    TextChunk,
    add_contextual_headers,
    fixed_size_chunks,
    markdown_blocks,
    markdown_chunks,
    recursive_chunks,
)
from ai_service.services.tokenizer import TokenCounter, find_tokenizer_file, load_token_counter

CHUNKERS = {
    "fixed size": fixed_size_chunks,
    "recursive": recursive_chunks,
    "markdown": markdown_chunks,
}
EXAMPLE = "fastapi-docs/tutorial/cors.md"


async def read_documents() -> list[tuple[str, str]]:
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            select(Document.source, Document.content)
            .where(Document.tenant_id == CORPUS_TENANT_ID)
            .order_by(Document.source)
        )
        documents = [(source, content) for source, content in rows]
    await engine.dispose()
    return documents


async def read_titles() -> dict[str, str]:
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            select(Document.source, Document.title).where(Document.tenant_id == CORPUS_TENANT_ID)
        )
        titles = {source: title for source, title in rows}
    await engine.dispose()
    return titles


def header_rows(
    results: list[tuple[str, str, list[TextChunk]]],
    titles: dict[str, str],
    counter: TokenCounter,
    max_tokens: int,
) -> tuple[dict[str, str], dict[str, list[TextChunk]]]:
    """What contextual headers add. The chunks themselves are the same with and without."""
    headed = {
        source: add_contextual_headers(text, found, titles[source])
        for source, text, found in results
    }
    chunks = [c for found in headed.values() for c in found]
    added = [counter.count(c.embedding_text) - c.token_count for c in chunks]
    sizes = [c.token_count + extra for c, extra in zip(chunks, added, strict=True)]
    plain = sum(c.token_count for c in chunks)
    row = {
        "headers on: tokens added, median / max": (
            f"{statistics.median(added):.0f} / {max(added)}"
        ),
        "  tokens to embed": f"{sum(sizes):,} ({sum(sizes) / plain - 1:+.1%})",
        "  largest embedded text, tokens": f"{max(sizes)}",
        f"  embedded texts over {max_tokens} tokens": f"{sum(s > max_tokens for s in sizes):,}",
    }
    return row, headed


def paragraphs(text: str) -> list[tuple[int, int]]:
    """Runs of text with no blank line inside."""
    found, begin = [], 0
    for gap in [*re.finditer(r"\n[ \t\r]*\n\s*", text), None]:
        end = gap.start() if gap else len(text)
        part = text[begin:end]
        if part.strip():
            lead = len(part) - len(part.lstrip())
            found.append((begin + lead, begin + len(part.rstrip())))
        begin = gap.end() if gap else end
    return found


def table_blocks(text: str) -> list[tuple[int, int]]:
    """Markdown tables: runs of lines that start with a pipe, at least 3 lines."""
    pattern = r"(?m)(?:^[ \t]*\|[^\n]*(?:\n|$)){3,}"
    return [(m.start(), len(text[: m.end()].rstrip())) for m in re.finditer(pattern, text)]


def where_it_ends(text: str, end: int) -> str:
    """What kind of boundary a chunk ends at, by what follows it in the document."""
    gap = re.match(r"\s*", text[end:]).group()  # type: ignore[union-attr]
    if not gap:
        return "inside a word"
    if re.match(r"[ \t\r]*\n[ \t\r]*\n", gap):
        return "end of a paragraph"
    if "\n" in gap:
        return "end of a line"
    if re.search(r"[.!?।॥][\"'”’)\]]?$", text[:end]):
        return "end of a sentence"
    return "between two words of a sentence"


def whole_in_one_chunk(span: tuple[int, int], chunks: list[TextChunk]) -> bool:
    return any(c.start <= span[0] and c.end >= span[1] for c in chunks)


def report(
    name: str,
    documents: list[tuple[str, str]],
    counter: TokenCounter,
    max_tokens: int,
    overlap: int,
    titles: dict[str, str],
) -> tuple[dict[str, str], list[TextChunk]]:
    started = time.perf_counter()
    results = [
        (
            source,
            text,
            CHUNKERS[name](text, counter, max_tokens=max_tokens, overlap_tokens=overlap),
        )
        for source, text in documents
    ]
    seconds = time.perf_counter() - started

    chunks = [c for _, _, found in results for c in found]
    sizes = [c.token_count for c in chunks]
    ends: Counter[str] = Counter()
    overlaps: list[int] = []
    lost = fitting = cut_fitting = tables = cut_tables = 0
    code = cut_code = mixed = markdown_total = 0
    for source, text, found in results:
        if source.endswith(".md"):  # sections and code blocks: only where the text is Markdown
            blocks = markdown_blocks(text)
            headings = [b.start for b in blocks if b.kind == "heading"]
            text_ends = [b.end for b in blocks if b.kind != "heading"]
            markdown_total += len(found)
            # mixed: some text of the chunk comes before a heading that is also in the chunk
            # (a text block never crosses a heading, so "ends before it" is enough)
            mixed += sum(
                any(c.start < end <= h < c.end for h in headings for end in text_ends)
                for c in found
            )
            for block in blocks:
                if block.kind != "code":
                    continue
                if counter.count(text[block.start : block.end]) <= max_tokens:
                    code += 1
                    cut_code += not whole_in_one_chunk((block.start, block.end), found)
        covered = bytearray(len(text))
        for c in found:
            covered[c.start : c.end] = b"\x01" * (c.end - c.start)
        lost += sum(1 for i, ch in enumerate(text) if not covered[i] and not ch.isspace())
        for before, after in zip(found, found[1:], strict=False):
            ends[where_it_ends(text, before.end)] += 1
            shared = text[after.start : before.end] if after.start < before.end else ""
            overlaps.append(counter.count(shared) if shared else 0)
        for start, end in paragraphs(text):
            if counter.count(text[start:end]) <= max_tokens:
                fitting += 1
                cut_fitting += not whole_in_one_chunk((start, end), found)
        for table in table_blocks(text):
            tables += 1
            cut_tables += not whole_in_one_chunk(table, found)

    boundaries = len(overlaps)
    in_documents = sum(counter.count(text) for _, text in documents)

    def share(kind: str) -> str:
        return f"{ends[kind]:,} ({ends[kind] / boundaries:.0%})"

    repeated = [o for o in overlaps if o]
    row = {
        "chunks": f"{len(chunks):,}",
        "time to chunk": f"{seconds:.1f} s",
        "tokens per chunk, median": f"{statistics.median(sizes):.0f}",
        "tokens per chunk, min / max": f"{min(sizes)} / {max(sizes)}",
        "chunks under 50 tokens": f"{sum(s < 50 for s in sizes):,}",
        f"chunks over {max_tokens} tokens": str(sum(s > max_tokens for s in sizes)),
        "visible characters in no chunk": str(lost),
        "tokens to embed": f"{sum(sizes):,}",
        "  more than the documents have": f"{sum(sizes) / in_documents - 1:+.0%}",
        "boundaries between chunks": f"{boundaries:,}",
        "  with no overlap": f"{overlaps.count(0):,} ({overlaps.count(0) / boundaries:.0%})",
        "  median overlap where there is one": f"{statistics.median(repeated):.0f}",
        "chunk ends at: end of a paragraph": share("end of a paragraph"),
        "  end of a line": share("end of a line"),
        "  end of a sentence": share("end of a sentence"),
        "  between two words of a sentence": share("between two words of a sentence"),
        "  inside a word": share("inside a word"),
        f"paragraphs of <= {max_tokens} tokens, cut": f"{cut_fitting} of {fitting:,}",
        "markdown tables not whole in a chunk": f"{cut_tables} of {tables}",
        f"code blocks of <= {max_tokens} tokens, cut": f"{cut_code} of {code:,}",
        "markdown-doc chunks mixing sections": f"{mixed:,} of {markdown_total:,}",
    }
    added, headed = header_rows(results, titles, counter, max_tokens)
    return row | added, headed[EXAMPLE]


async def main() -> None:
    settings = get_settings()
    max_tokens, overlap = settings.CHUNK_MAX_TOKENS, settings.CHUNK_OVERLAP_TOKENS
    counter = load_token_counter(find_tokenizer_file(settings.TOKENIZER_PATH))
    documents = await read_documents()
    titles = await read_titles()
    total = sum(counter.count(text) for _, text in documents)
    print(
        f"{len(documents)} documents, {total:,} tokens | max {max_tokens} tokens, "
        f"overlap up to {overlap}\n"
    )

    results = {
        name: report(name, documents, counter, max_tokens, overlap, titles) for name in CHUNKERS
    }
    print(f"{'':<42}" + "".join(f"{name:>18}" for name in results))
    for key in next(iter(results.values()))[0]:
        print(f"{key:<42}" + "".join(f"{row[key]:>18}" for row, _ in results.values()))

    for name, (_, chunks) in results.items():
        print(f"\n===== {name}: the first boundary of {EXAMPLE} ({len(chunks)} chunks)")
        print(f"--- chunk 0 ({chunks[0].token_count} tokens) ends:\n{chunks[0].text[-200:]}")
        print(f"--- chunk 1 ({chunks[1].token_count} tokens) begins:\n{chunks[1].text[:200]}")
    print(f"\n===== markdown: the heading path of every chunk of {EXAMPLE}")
    for chunk in results["markdown"][1]:
        print(f"  {chunk.index:>2}  {chunk.token_count:>3} tokens  {' > '.join(chunk.headings)}")

    example = results["markdown"][1][7]
    print(f"\n===== what is embedded for chunk {example.index} of {EXAMPLE}, headers on")
    print(example.embedding_text[:330])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
