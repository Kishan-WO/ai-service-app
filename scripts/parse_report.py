"""Day 11, task 1: parse and clean real files, and show what cleaning changed.

Reads the 10 research PDFs and the Markdown files of the Day 9 corpus from disk. Writes nothing
and calls no API.

    uv run python scripts/parse_report.py            the PDFs and the Markdown files
    uv run python scripts/parse_report.py --lines    also every dropped header and footer line
"""

import re
import sys
import time
from collections import Counter
from pathlib import Path

from ai_service.services.cleaning import CLEANER_VERSION, CleanDocument, clean_document
from ai_service.services.parsing import PDF_PARSER, ParsedDocument, parse_file

CORPUS = Path(__file__).parents[2] / "ai-eng-learning" / "day-9" / "corpus" / "data"


def words(text: str) -> Counter[str]:
    return Counter(re.findall(r"[^\W\d_]{2,}", text.lower()))


def run(path: Path) -> tuple[ParsedDocument, CleanDocument, float]:
    started = time.perf_counter()
    parsed = parse_file(path.name, path.read_bytes())
    cleaned = clean_document(parsed)
    return parsed, cleaned, time.perf_counter() - started


def main() -> None:
    pdfs = sorted((CORPUS / "arxiv-papers").glob("*.pdf"))
    print(f"parser {PDF_PARSER} | cleaner version {CLEANER_VERSION}\n")
    print(
        "PDF             pages  characters before -> after  headers  numbers  joined  kept-  "
        "unreadable  ligatures  seconds"
    )
    total: Counter[str] = Counter()
    lines: list[tuple[str, str]] = []
    for path in pdfs:
        parsed, cleaned, seconds = run(path)
        before = sum(len(p) for p in parsed.pages)
        r = cleaned.removed
        print(
            f"{path.stem:<14} {len(parsed.pages):>6}  {before:>10,} -> {len(cleaned.text):>9,}  "
            f"{r.get('header or footer lines removed', 0):>7}  "
            f"{r.get('page numbers removed', 0):>7}  {r.get('words joined', 0):>6}  "
            f"{r.get('hyphens kept, line break removed', 0):>5}  "
            f"{r.get('unreadable characters removed', 0):>10}  "
            f"{r.get('ligatures expanded', 0):>9}  {seconds:>7.1f}"
        )
        total.update(r)
        total.update({"pages": len(parsed.pages), "empty pages": len(cleaned.empty_pages)})
        lines += [(path.stem, line) for line in cleaned.removed_lines]
    print(f"\ntotal: {dict(total)}")
    if "--lines" in sys.argv:
        print("\nevery different header or footer line that was dropped (# = a number):")
        for name, line in lines:
            print(f"  {name:<12} {line}")

    markdown = sorted(
        p for source in ("fastapi-docs", "pydantic-docs") for p in (CORPUS / source).rglob("*.md")
    )
    changed = 0
    lost_words = 0
    for path in markdown:
        parsed, cleaned, _ = run(path) if path.read_text(encoding="utf-8").strip() else (None,) * 3
        if parsed is None or cleaned is None:
            continue
        changed += cleaned.text != parsed.pages[0].strip("\n")
        lost_words += sum((words(parsed.pages[0]) - words(cleaned.text)).values())
    print(
        f"\nMarkdown files: {len(markdown)} | changed by cleaning: {changed} | "
        f"words lost: {lost_words}"
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
