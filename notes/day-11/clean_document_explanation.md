Analyzed cleaning.py:L140-175
Analyzed cleaning.py:L1-144

`clean_document` (lines 145–176 in `cleaning.py`) is the **central coordinator of the document cleaning pipeline** (Day 11, Task 1). 

Its goal is to take raw parsed pages (`ParsedDocument`) from PDFs, Markdown, or plain text, strip out artifacts (headers, footers, unreadable glyphs, broken hyphenations, trailing spaces), and produce unified clean text with exact character-to-page tracking (`CleanDocument`).

---

### Step-by-Step Walkthrough of `clean_document`

```python
def clean_document(parsed: ParsedDocument) -> CleanDocument:
    done: Counter[str] = Counter()
    # 1. Clean characters on all pages
    pages = [_clean_characters(page, done) for page in parsed.pages]
    dropped: set[str] = set()

    # 2. Format-specific cleanup
    if parsed.kind == "pdf":
        split = [_tidy_lines(page).split("\n") for page in pages]
        split, dropped = _remove_running_lines(split, done)
        pages = _join_hyphenated(["\n".join(lines) for lines in split], done)
    elif parsed.kind == "markdown":
        pages = [clean_markdown(page, done) for page in pages]

    # 3. Tidy line breaks and trailing whitespace
    pages = [_tidy_lines(page) for page in pages]

    # 4. Stitch pages together and record character spans
    text, spans, empty = "", [], []
    for number, page in enumerate(pages, 1):
        if not page.strip():
            empty.append(number)
            continue
        if text:
            text += "\n\n"
        spans.append(PageSpan(number, len(text), len(text) + len(page)))
        text += page

    # 5. Guard check & return
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
```

1. **Audit Counter (`done = Counter()`):** Tracks every modification made (e.g. how many ligatures expanded, footers deleted, words unhyphenated) for observability.
2. **Character Sanitization:** Cleans every page with `_clean_characters`.
3. **Format Branching:**
   - **PDFs:** Splits lines, identifies and removes repetitive running headers/footers/page numbers with `_remove_running_lines`, then heals line-wrap hyphens with `_join_hyphenated`.
   - **Markdown:** Strips web navigation/HTML boilerplate with `clean_markdown`.
4. **Stitching & Page Spans:** Joins pages separated by double newlines (`\n\n`), while creating `PageSpan(number, start, end)` character offsets so any chunk can later cite its exact original PDF page.
5. **Validation:** Raises `InvalidDocumentError` if the document ended up completely blank (e.g. image-only PDF without OCR).

---

### The Helper Functions Used by `clean_document`

Below are the functions it calls to do the work:

---

#### 1. `_clean_characters(text, done)` (Line 54)
Cleans characters at the Unicode level:
- **Ligature expansion:** Converts typographic ligatures into regular ASCII letters (e.g., `ﬁ` $\rightarrow$ `fi`, `ﬂ` $\rightarrow$ `fl`) using `_LIGATURES`.
- **Newlines:** Normalizes `\r\n` (Windows) and `\r` (old Mac) to standard `\n`.
- **Invisible characters:** Drops zero-width space (`\u200b`), Byte Order Marks (`\ufeff`), and soft hyphens.
  > **Key nuance:** It deliberately preserves the Zero-Width Joiner (`\u200d`) and Non-Joiner (`\u200c`), because deleting them alters meaning in Indic languages (like Hindi Devanagari conjuncts).
- **Control characters:** Removes non-printable characters (`Cc`) and private-use font icons (`Co`).
- **NFC Normalization:** Runs `unicodedata.normalize("NFC")` so combined accent characters are standard.

---

#### 2. `_tidy_lines(text)` (Line 68)
Whitespace cleanup:
- Strips trailing spaces and tabs from the ends of lines: `re.sub(r"[ \t]+(?=\n|$)", "", text)`.
- Collapses 3 or more blank lines (`\n{3,}`) down to a clean paragraph break (`\n\n`).

---

#### 3. `_remove_running_lines(pages, done)` (Line 84)
Strips repeated PDF headers, footers, and page numbers:
- **How it works:**
  1. Inspects only the "edges" of each page via `_edge_positions(lines)` (the first 2 and last 2 non-empty lines).
  2. Normalizes numbers to `#` using `_line_key(line)` (e.g., `"Chapter 2 — Page 14"` becomes `"Chapter # — Page #"`).
  3. If a normalized line appears on $\ge 40\%$ of pages and at least 3 times, it is flagged as a recurring header/footer and deleted.
  4. Detects isolated page numbers by checking if `int(line) - page_index` is constant across pages.

---

#### 4. `_join_hyphenated(pages, done)` (Line 115)
Fixes words broken across line wraps in PDFs (e.g. `"curricu-\nlum"`):
- Detects the pattern: `([A-Za-z]{2,})-\n(?=([a-z]{2,}))`.
- **Deciding whether to keep the hyphen:**
  - Builds a vocabulary set of all words across the document.
  - If `"curriculum"` appears without a hyphen elsewhere in the document, it removes the hyphen: $\rightarrow$ `"curriculum"`.
  - If `"state-of-\nthe-art"` or prefix/suffix compounds (`non-`, `self-`, `-aware`, `-based`) are detected, it preserves the hyphen: $\rightarrow$ `"state-of-"`.

---

#### 5. `clean_markdown(page, done)` (Imported from `markdown_cleaning.py`)
Cleans web-scraped Markdown documents:
- Strips HTML navigation menus, breadcrumbs, social share links, and cookie disclaimers that web scrapers often leave behind.

---

### Summary of What Returns (`CleanDocument`)
- `text`: Fully normalized, continuous text.
- `pages`: Tuple of `PageSpan(number, start, end)` (for PDF citation).
- `removed`: A dictionary audit log (e.g. `{"ligatures expanded": 14, "header or footer lines removed": 6, "words joined": 21}`).
- `removed_lines`: Exact text of all dropped header/footer lines.
- `empty_pages`: Page numbers that contained no text (e.g. blank or full-page scans).