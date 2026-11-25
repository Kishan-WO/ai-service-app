"""Day 11, task 1: parse, clean and chunk the ORIGINAL corpus files, and check the result.

The 190 Markdown files and 10 research PDFs of the corpus, read from disk (the textbook is not
read from its PDF). The old documents are only read, to know which files they are and what
their titles were. Nothing is written and no API is called.

    uv run python scripts/prepare_report.py
"""

import asyncio
import sys
import time
from collections import Counter

from eval_search import CORPUS_TENANT_ID
from parse_report import CORPUS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ai_service.core.config import get_settings
from ai_service.db.models import Document
from ai_service.services.chunk_stage import ChunkOptions, chunk_document
from ai_service.services.cleaning import clean_document
from ai_service.services.parsing import parse_file
from ai_service.services.tokenizer import find_tokenizer_file, load_token_counter


async def old_titles() -> dict[str, str]:
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    async with AsyncSession(engine) as session:
        rows = await session.execute(
            select(Document.source, Document.title).where(Document.tenant_id == CORPUS_TENANT_ID)
        )
        titles = {source: title for source, title in rows}
    await engine.dispose()
    return titles


def main() -> None:
    settings = get_settings()
    options = ChunkOptions(
        settings.CHUNK_STRATEGY,
        settings.CHUNK_MAX_TOKENS,
        settings.CHUNK_OVERLAP_TOKENS,
        context_headers=True,  # on here, to look at the headers; the setting is off by default
    )
    counter = load_token_counter(find_tokenizer_file(settings.TOKENIZER_PATH))
    titles = asyncio.run(old_titles())
    files = {
        source: CORPUS / source
        if source.endswith(".md")
        else CORPUS / "arxiv-papers" / f"{source.split('/')[1]}.pdf"
        for source in sorted(titles)
        if not source.startswith("openstax")
    }
    missing = [source for source, path in files.items() if not path.is_file()]
    if missing:
        sys.exit(f"original files not found: {missing[:5]}")

    seconds: Counter[str] = Counter()
    stats: Counter[str] = Counter()
    chunkers: Counter[str] = Counter()
    new_titles: dict[str, str] = {}
    example = None
    for source, path in files.items():
        data = path.read_bytes()
        started = time.perf_counter()
        parsed = parse_file(path.name, data)
        parsed_at = time.perf_counter()
        cleaned = clean_document(parsed)
        cleaned_at = time.perf_counter()
        # a research paper's title comes with the corpus; the PDF has no reliable one
        result = chunk_document(
            cleaned,
            kind=parsed.kind,
            filename=path.name,
            counter=counter,
            options=options,
            given_title=titles[source] if parsed.kind == "pdf" else None,
        )
        seconds["parse"] += parsed_at - started
        seconds["clean"] += cleaned_at - parsed_at
        seconds["chunk"] += time.perf_counter() - cleaned_at
        new_titles[source] = result.title
        chunkers[f"{parsed.kind} -> {result.chunker}"] += 1
        stats["documents"] += 1
        stats["chunks"] += len(result.chunks)
        spans = {p.number: p for p in cleaned.pages}
        covered_pages: set[int] = set()
        for prepared in result.chunks:
            chunk = prepared.chunk
            stats["over the limit"] += counter.count(chunk.text) > options.max_tokens
            stats["not a slice of the cleaned text"] += (
                cleaned.text[chunk.start : chunk.end] != chunk.text
            )
            stats["without a header"] += not chunk.header
            stats["largest embedded text"] = max(
                stats["largest embedded text"], counter.count(chunk.embedding_text)
            )
            if parsed.kind != "pdf":
                continue
            stats["PDF chunks"] += 1
            if prepared.page_start is None or prepared.page_end is None:
                stats["PDF chunks without pages"] += 1
                continue
            first, last = spans[prepared.page_start], spans[prepared.page_end]
            # the chunk starts inside its first page and ends inside its last page
            stats["wrong pages"] += not (
                first.start <= chunk.start < first.end and last.start < chunk.end <= last.end
            )
            stats["PDF chunks on two or more pages"] += prepared.page_start != prepared.page_end
            covered_pages.update(range(prepared.page_start, prepared.page_end + 1))
            if source.endswith("2504.08748") and example is None and prepared.page_start > 3:
                example = prepared
        stats["PDF pages with text"] += len(cleaned.pages)
        stats["PDF pages in no chunk"] += len(set(spans) - covered_pages) if spans else 0

    print(f"options: {options}")
    print(f"chunkers used: {dict(chunkers)}")
    for key in (
        "documents",
        "chunks",
        "over the limit",
        "not a slice of the cleaned text",
        "without a header",
        "largest embedded text",
        "PDF chunks",
        "PDF chunks without pages",
        "wrong pages",
        "PDF chunks on two or more pages",
        "PDF pages with text",
        "PDF pages in no chunk",
    ):
        print(f"  {key:<34}{stats[key]:>7,}")
    print(f"seconds: { {k: round(v, 1) for k, v in seconds.items()} }")

    markdown = [s for s in files if s.endswith(".md")]
    changed = [s for s in markdown if new_titles[s] != titles[s]]
    print(f"\nMarkdown titles that differ from the old ones: {len(changed)} of {len(markdown)}")
    for source in changed:
        print(f"  {source:<46} {titles[source][:42]!r:<45} -> {new_titles[source]!r}")
    if example:
        chunk = example.chunk
        print(f"\none PDF chunk: pages {example.page_start} to {example.page_end}")
        print(chunk.embedding_text[:260])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
