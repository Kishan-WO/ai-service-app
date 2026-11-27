Analyzed chunk_stage.py:L80-122
Analyzed chunk_stage.py:L1-80

`chunk_document` (lines 91–122 in `chunk_stage.py`) is the **orchestrator function** for the chunking stage of document ingestion. 

It takes cleaned raw text (`CleanDocument`) and turns it into embedded-ready pieces (`ChunkedDocument`), enriched with document titles, heading breadcrumbs, PDF page numbers, and optional contextual retrieval headers.

---

### Step-by-Step Breakdown

```python
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
```
> **Note on Performance:** The docstring points out that tokenizing and regex parsing are CPU-heavy and synchronous. In FastAPI or async tasks, this function is executed via `asyncio.to_thread(...)` so it never blocks the async event loop.

---

#### 1. Title Resolution (Line 101)
```python
title = document_title(cleaned.text, kind, filename, given_title)
```
Resolves the document's canonical title following a priority ladder:
1. Explicit title given by the user in the upload request.
2. If it's Markdown, the first `# Title` (level-1 heading) found in the text (ignoring `#` comments inside code blocks).
3. Fallback to clean filename (e.g. `rag_architecture.md` $\rightarrow$ `"Rag Architecture"`).

---

#### 2. Chunker Selection & Execution (Lines 102–108)
```python
name = choose_chunker(options.strategy, kind)
chunks = _CHUNKERS[name](
    cleaned.text,
    counter,
    max_tokens=options.max_tokens,
    overlap_tokens=options.overlap_tokens,
)
```
- If `options.strategy == "auto"`:
  - Chooses **`markdown_chunks`** if `kind == "markdown"`.
  - Chooses **`recursive_chunks`** for PDFs and plain text.
- Executes the selected chunker with the configured `max_tokens` (default 400) and `overlap_tokens` (default 60).

---

#### 3. Clearing False Headings for Non-Markdown (Lines 109–112)
```python
if kind != "markdown":
    chunks = [replace(c, headings=()) for c in chunks]
```
Ensures that if a plain text or PDF document contains lines starting with `#` (like Python code or comments), they are **not** mistakenly treated as Markdown section breadcrumbs.

---

#### 4. Contextual Retrieval Headers (Lines 113–117)
```python
if options.context_headers:
    if kind == "markdown":
        chunks = add_contextual_headers(cleaned.text, chunks, title)
    else:
        chunks = [replace(c, header=contextual_header(title, ())) for c in chunks]
```
If `context_headers` is enabled (inspired by Anthropic's *Contextual Retrieval*):
- Prepends document title and section breadcrumbs to each chunk (e.g. `"[RAG System > Storage > PostgreSQL] ..."`).
- This significantly improves embedding search recall because isolated paragraphs regain their parent context.

---

#### 5. PDF Page Mapping & Output Packaging (Lines 118–122)
```python
return ChunkedDocument(
    title=title,
    chunker=name,
    chunks=tuple(PreparedChunk(c, *pages_of(c.start, c.end, cleaned.pages)) for c in chunks),
)
```
- **`pages_of(c.start, c.end, cleaned.pages)`**: Compares the character offsets `(start, end)` of each chunk against the PDF page span map to record which page(s) this chunk originated from (`page_start`, `page_end`). For `.txt` or `.md`, this is `(None, None)`.
- Returns `ChunkedDocument` ready for embedding and database storage.