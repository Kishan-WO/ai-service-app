# Day 11: RAG I, ingestion, parsing and chunking (task implementation log)

My build log for Day 11, written step by step with the commands I ran and what came back. I update it after each step. The concept notes are in `ai-eng-learning/day-11/`.

**Goal of the day (from the plan):** an idempotent ingestion pipeline with three chunking strategies.
**Done when:** I can run ingestion again on the whole corpus and the `chunks` table does not change.

## The tasks (from the plan)
| Task | What | Status |
|---|---|---|
| 1 | `POST /ingest` (file or URL) starts a worker job: parse, clean, chunk, embed, upsert. A status endpoint | **done**, with limits (Step 30): stages, worker job (Steps 19 to 24) and `POST /ingest` for a **file** (Step 25) or a **link** (Steps 26 and 27; a real internet download is not tested). Status endpoint (Step 28). Upload to search with the real Jina (Step 30) |
| 2 | Three chunkers: fixed-size by tokens with overlap, recursive, Markdown/structure-aware | done: fixed-size (Step 4), recursive (Steps 5, 6), Markdown/structure-aware (Step 7) |
| 3 | Contextual headers (document title and section path) on each chunk, as an option | done (Step 8); the effect on search is not measured. The wrong titles of the old corpus (Step 8) come from the Day 9 loader; 3 checked ones are right in the new corpus |
| 4 | Parse 5 difficult PDFs (tables, two columns, a scanned page) and record what broke | **done** (Step 35): 4 papers and a drawn "scan"; failures recorded, none fixed |
| 5 | Split a table over two chunks, ask a question that needs both halves, and show that retrieval returns only half | **done, with a caveat** (Steps 36, 37): with 5 results search found **none** of either table; with the first 10 (looked at afterwards) it had 5 of 7 and 8 of 22 rows |
| 6 | Ingest the whole corpus again and prove there are no duplicate chunks | **done**: 234 documents, 4,683 chunks; the second run left the chunks table identical (Steps 31 to 33) |
| Notes | A catalogue of parsing failures | **done** (Step 38): [`parsing-failure-catalogue.md`](parsing-failure-catalogue.md) |
| Notes | Ingestion jobs model & status transitions | [`ingestion_jobs.md`](ingestion_jobs.md) |
| Notes | Document cleaning pipeline & helpers | [`clean_document_explanation.md`](clean_document_explanation.md) |
| Notes | Chunk stage orchestrator & page mapping | [`chunked document explanation.md`](<chunked document explanation.md>) |
| Notes | Structure-aware Markdown chunker | [`markdown_chunker.md`](markdown_chunker.md) |
| Notes | Tokenizer loading & LRU cache | [`tokenizer-lru-cache.md`](tokenizer-lru-cache.md) |
| Notes | Ingestion request, advisory locks & identity | [`ingest_request_explanation.md`](ingest_request_explanation.md) |
| Notes | Secure URL fetch & SSRF protection | [`url_fetch_ssrf_protection.md`](url_fetch_ssrf_protection.md) |
| Notes | Job queue architecture, locks & lifecycle | [`job_queue_architecture.md`](job_queue_architecture.md) |

### Detailed Component & Architecture Notes
- [**Parsing Failure Catalogue**](parsing-failure-catalogue.md): Comprehensive catalog of edge cases and parsing breakdowns across 5 difficult PDFs.
- [**Secure URL Fetching & SSRF Protection**](url_fetch_ssrf_protection.md): IP pinning against DNS rebinding, public IP checks, manual redirect security, and decompression bomb prevention (`url_fetch.py`).
- [**Job Queue Architecture & Lifecycle**](job_queue_architecture.md): FastAPI lifespan injection, mutex lock lazy connection initialization, fail-fast timeouts, and method-to-function delegation (`job_queue.py`, `deps.py`, `main.py`).
- [**Ingestion Request & Document Identity**](ingest_request_explanation.md): PostgreSQL advisory locks (`pg_advisory_xact_lock`), document identity resolution, duplicate checks, and binary file storage (`ingest_request.py`).
- [**Ingestion Jobs Architecture**](ingestion_jobs.md): Data model, job state machine, SQL-level atomic status transitions, and worker concurrency rules (`ingestion_job.py` and `ingestion_jobs.py`).
- [**Document Cleaning Pipeline**](clean_document_explanation.md): Deep-dive into character normalization, ligature expansion, PDF header/footer removal, and hyphen-joining (`cleaning.py`).
- [**Chunk Stage Orchestration**](<chunked document explanation.md>): Orchestration of document title resolution, chunker selection, contextual headers, and PDF page span tracking (`chunk_stage.py`).
- [**Structure-Aware Markdown Chunker**](markdown_chunker.md): 4-stage pipeline for block extraction, section hierarchy tracking, and code/table preserving chunking (`chunking.py`).
- [**Tokenizer Loading & LRU Caching**](tokenizer-lru-cache.md): Memory and latency analysis of BGE-M3 tokenizer caching via `@lru_cache` (`tokenizer.py`).

**"Done when" is met** (Step 33): running ingestion again on the whole corpus left the `chunks` table identical.

## Step log

### Step 0. A branch for the day
```
git switch -c day-11        # from day-10 (7aa21bf)
```
It starts from `day-10`, not from `main`: Day 11 writes into the `chunks` table of Day 10, and I have not merged `day-10` into `main`.

### Step 1. Check the starting point (nothing changed)
**Containers:** `api`, `postgres`, `redis` and `qdrant` are up and healthy.

**Database**
```
select version_num from alembic_version;      -> dc013ced71fa
uv run alembic heads                          -> dc013ced71fa (head)
uv run alembic check                          -> No new upgrade operations detected.
tenants | users | documents | chunks          -> 4 | 4 | 239 | 2097
documents without any chunk                   -> 5 of 239
```
**Redis**
```
docker exec ai-service-redis redis-cli ping          -> PONG
redis_version                                        -> 7.4.12
redis-cli dbsize                                     -> 0
docker exec ai-service-api printenv REDIS_URL        -> not set
```
Redis runs, but **the app does not use it**: there is no Redis setting in `config.py` or `.env.example`.

**Libraries**
| Installed | Not installed |
|---|---|
| `pypdf` 6.21.4 | queue: `arq`, `celery`, `redis` · parsing: `pymupdf`, `docling`, `unstructured`, `beautifulsoup4` · OCR: `pytesseract`, and the `tesseract` program is not on my `PATH` · tokens and splitting: `tokenizers`, `transformers`, `tiktoken`, `langchain-text-splitters` |

**The upload route today** accepts `.txt`, `.md` and `.pdf` up to 20 MB, and saves **only the extracted text** (`documents.content`). The original file is not kept.

**Inputs:** the Day 9 corpus is in `ai-eng-learning/day-9/corpus/data/`; I counted 11 PDF files under `day-9/`. I have not checked yet whether any of them has the hard cases of task 4 (two columns, a scanned page).

**Baseline before any change**
```
pytest -q          -> 251 passed
mypy src tests     -> Success: no issues found in 80 source files
```

**What it means for the build**
- Task 1 needs new parts: a job queue and its client library, a worker service in Docker Compose, a Redis setting, a job table, and a place to keep the uploaded file.
- A token-based chunker needs a tokenizer, and none is installed.
- `chunks` has `UNIQUE (document_id, chunk_index)` and no pipeline version column. A re-ingest must delete the old rows before it inserts the new ones, in one transaction (`ai-eng-learning/day-11/Notes/01`, section 4).
- The 5 documents without chunks come from before ingestion existed.

### Step 2. The tokenizer (checked; nothing installed, no tokenizer file downloaded)
**Why:** "512 tokens" has to mean what the embedding model counts. My chunks are embedded with Jina `jina-embeddings-v3`.

**1. What is already on my laptop:** the BGE-M3 tokenizer from Day 9 (`~/.cache/huggingface/hub/models--BAAI--bge-m3/.../tokenizer.json`, 17,098,108 bytes). `day-4` and `day-9` have the `tokenizers` library (0.23.3); `ai-service` does not.

**2. The published files** (read from the Hugging Face and PyPI metadata, without downloading them)
| | `jinaai/jina-embeddings-v3` | `BAAI/bge-m3` |
|---|---|---|
| licence tag of the model repository | `cc-by-nc-4.0` (non-commercial) | `mit` |
| `tokenizer.json` | 17,082,756 bytes, sha256 `f59925fc...` | 17,098,108 bytes, sha256 `21106b6d...` |

My local BGE-M3 file has the same sha256 as the published one. **Jina's file is not the same file**, so I tested whether it counts the same.

**3. The test:** count the exact texts of my Day 9 run with the local BGE-M3 tokenizer and compare with what **Jina billed**, batch by batch (a temporary script, run with the `day-9` environment; it changes nothing there). It first checks that its 116 batches have the same sizes as the batches of that run.
```
2097 texts in 116 batches (same sizes as the Day 9 run); counted in 0.8 s
Jina billed                    : 1,270,018
local count with <s> </s>      : 1,251,145  (-1.486%) | batches equal to the bill: 0/116 | difference per batch -288 to -9 | per text, average -9.00
local count without them        : 1,246,951  (-1.816%) | batches equal to the bill: 0/116 | difference per batch -352 to -11 | per text, average -11.00
rule 'local count with <s> </s> + 9 per text == billed': true in 116/116 batches (batch sizes 1 to 32 texts)
```
**Result:** `billed = BGE-M3 count with <s> </s> + 9 for each text`, in every one of the 116 batches. In content tokens (without `<s> </s>`), Jina billed 11 more per text.
- I did not find out what the 9 extra tokens are. Jina's `config.json` adds an instruction before a passage ("Represent the document for retrieval: "), but that string is 10 tokens in this tokenizer, so I do not claim it is the explanation.
- This is shown for my corpus (English and code, from Day 9), at batch level. I did not test it on Hindi passages.

**4. What the count shows about my current chunks** (heading path plus text, with `<s> </s>`)
```
tokens per section: median 311, max 4,431, over 512: 713, over 8,194: 0
words 683,893 -> 1.82 tokens per word on this corpus
English  44 characters -> 13 tokens
Hindi    34 characters -> 12 tokens
```
- **713 of the 2,097 sections (34%) are longer than 512 tokens**, the largest 4,431. The Day 9 chunks had no size limit.
- None is over 8,194, the `max_position_embeddings` in Jina's `config.json`. (I could not read the model card, which is too large for my fetch, so I have not confirmed the documented input limit.)
- "words x 1.35", my estimate on Day 9, is low for this corpus.

**5. My proposal:** count tokens with the `tokenizers` library and the **BGE-M3 `tokenizer.json`**: MIT, already on my laptop, and its counts match Jina's bill by a fixed rule. The chunkers will limit **content tokens** (without `<s> </s>`).
- `uv pip install --dry-run tokenizers` would add 6 packages: `tokenizers` 0.23.3 (2.9 MB wheel for Windows, 3.4 MB for Linux), `huggingface-hub` 2.4.0, `hf-xet` 1.8.0, `filelock` 4.3.1, `fsspec` 2026.9.0, `tqdm` 4.70.1.
- The 17 MB file should not go into git. How the Docker image gets it (a pinned download with a checksum is my idea) is decided when the ingestion worker needs it.

### Step 3. Install `tokenizers` and check what the chunkers need
**1. Install**
```
uv add tokenizers
```
```
 + filelock==4.3.1   + fsspec==2026.9.0   + hf-xet==1.8.0
 + huggingface-hub==2.4.0   + tokenizers==0.23.3   + tqdm==4.70.1
```
`pyproject.toml` got one line, `"tokenizers>=0.23.3"`. In `uv.lock` these 6 packages are the only name or version changes. The tokenizer is loaded from the local file with `Tokenizer.from_file`; I downloaded no tokenizer file.

**2. Same counts as in Step 2**, now in the `ai-service` environment (a temporary script):
```
corpus: 1,246,951 content tokens, 1,251,145 with <s> </s> | billing rule true in 116/116 batches
```
**3. Offsets.** For every token the tokenizer gives the start and end **character** in the original string. A chunker can use them to cut the original text without rebuilding it from tokens.
```
corpus  1,246,951 tokens | visible characters no token covers: 8 | offsets going backwards: 0 | tokens sharing a character with the one before: 41 | zero-width tokens: 0
Hindi          67 tokens | visible characters no token covers: 0 | offsets going backwards: 0 | tokens sharing a character with the one before: 0 | zero-width tokens: 0
```
The 8 uncovered characters are invisible ones left by PDF text extraction: 6 `U+200B` (zero-width space, in the chemistry textbook) and 2 control characters `U+0010`, `U+0011` (in one arXiv paper). No readable letter or digit is lost. They belong in the cleaning step. The Hindi test was only three short strings (a sentence, two sentences, a small table).

**4. A piece cut out and counted alone can have a different count**
```
corpus windows of 256 tokens: 5,868 | same count when counted alone: 5,276 | difference -2 to 1
corpus windows of 32 tokens: 39,982 | same count when counted alone: 34,220 | difference -3 to 1
Hindi  windows of 8 tokens: 10 | same count when counted alone: 8 | difference 0 to 1
```
Tokens also cut inside words:
```
'FastAPI में CORS कैसे enable करें?' -> 'Fast' 'API' ' में' ' COR' 'S' ' कैसे' ' en' 'able' ' करें' '?'
```
**What the chunkers must do because of this**
- Cut between words, not in the middle of one (`COR` | `S`).
- **Count each finished chunk again.** A window of N tokens can count N + 1 on its own, so choosing N tokens is not enough to promise "at most N".
- Test that no readable text is lost.

**5. Nothing broke**
```
mypy src tests   -> Success: no issues found in 80 source files
pytest -q        -> 251 passed
ruff check .     -> 2 errors, both older than Day 11 (alembic/env.py, core/middlewares.py:64)
```
No app code uses the tokenizer yet. The API image needs a rebuild before code inside Docker can import it.

**Next:** the first chunker, fixed size with overlap.

### Step 4. Task 2, first chunker: fixed size with overlap
**1. Files**
| File | What |
|---|---|
| `services/tokenizer.py` | `TokenCounter` (`offsets`, `count`), the BGE-M3 implementation, and where the file is found |
| `services/chunking.py` | `fixed_size_chunks(text, counter, max_tokens=400, overlap_tokens=60)` returning `TextChunk(index, text, start, end, token_count)` |
| `tests/test_chunking.py` | the tests |
| `scripts/chunk_report.py` | runs the chunker over the 234 corpus documents and checks the result |

New settings (also in `.env.example`): `CHUNK_MAX_TOKENS=400`, `CHUNK_OVERLAP_TOKENS=60` (15%), `TOKENIZER_PATH` (empty: the copy in the Hugging Face cache). The function takes the sizes as arguments; today only the report script reads the settings.

**2. How it works**
1. The whole text is tokenized **once**. Each token is given to the word that holds its last character.
2. Whole words are added to a chunk while their token counts fit.
3. The finished chunk is **counted again on its own** (Step 3) and shrunk if it is over the limit.
4. The next chunk starts a few whole words back: up to `overlap_tokens`.
5. A word with more tokens than the limit (a long URL) is cut between tokens.
6. Every chunk is an exact slice of the original, `text[start:end]`. Nothing is rewritten.

**3. A bug the tests found.** My first version could produce a chunk that **added nothing new**: the next chunk started in the overlap, the recount shrank it, and it ended where the one before had ended. The random test failed:
```
FAILED test_random_texts_and_settings_keep_every_rule[counter1]
E  assert (7 > 0 and 47 > 47)      # chunk 1 ended at character 47, like chunk 0
```
It showed up with the fake tokenizer that counts one more on a cut piece, which the real tokenizer also does (Step 3). **Fix:** every chunk must include at least one new word; if that does not fit, overlap is given up first.

**4. Tests**
- With a small fake tokenizer (pieces of 4 characters), so they run anywhere and the numbers can be worked out by hand.
- 12 cases with the **real** tokenizer: English, Hindi, a table and a very long word, at three sizes (they are skipped where the tokenizer file is missing).
- The rules checked: no chunk over the limit (counted on its own), no visible character lost, each chunk an exact slice, each starts and ends after the one before.
```
uv run pytest tests/test_chunking.py   -> all passed (29 tests)
pytest -q                              -> 280 passed
mypy src tests                         -> Success: no issues found in 83 source files
ruff check (the new and changed files) -> All checks passed
stress run (not in pytest): 20,000 random cases with each of the two fake tokenizers -> every rule held
```

**5. On the whole corpus** (it reads the local database and writes nothing)
```
uv run python scripts/chunk_report.py
```
```
234 documents | fixed size: max 400 tokens, overlap up to 60
chunked in 7.9 s -> 3,632 chunks
tokens per chunk   : median 400, min 38, max 400
tokens to embed    : 1,408,340 in chunks against 1,209,588 in the documents (+16%, the repeated overlap)
chunks over 400     : 0
visible characters in no chunk : 0
overlap between neighbours     : median 60 tokens, min 6, max 60; none at 0 of 3,398 boundaries
boundaries inside a word       : 0 of 3,398
chunk ends without . ! ? । : ``` or | : 3133 of 3,398 (92%)
markdown tables not whole in any chunk : 14 of 28

one boundary in fastapi-docs/tutorial/cors.md (5 chunks)
--- chunk 0 (399 tokens) ends:
rom this different origin (`http://localhost:8080`) then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.

To achieve this, the `:80`-backend must have a list of "allowed
--- chunk 1 (397 tokens) begins:
then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.

To achieve this, the `:80`-backend must have a list of "allowed origins".

In this case, the list would have to inc
```

**What it means**
- **The limit holds and nothing is lost:** 0 chunks over 400 and 0 visible characters outside every chunk, in 3,632 chunks.
- **No word is cut** at any of the 3,398 boundaries, and every boundary has some overlap (6 to 60 tokens).
- **Overlap has a price:** 16% more tokens to embed (1,408,340 against 1,209,588). The overlap setting is 15% of the chunk size.
- **It ignores meaning.** 92% of the boundaries do not end with sentence punctuation. This is a rough sign only: a heading or a line of code can end correctly without it. The example above shows a real one: chunk 0 stops at `a list of "allowed`, and "origins" is in chunk 1 (which, through the overlap, has the whole sentence).
- **14 of the 28 Markdown tables are not whole in any chunk.** This is the kind of break task 5 will show. My check finds only tables written with `|`; tables inside the PDF text of the chemistry book and the papers are not counted.
- These documents are joined Day 9 sections, not freshly parsed files, so the numbers can change when ingestion parses the originals.
- Compared with Day 9 there are more pieces (3,632 against 2,097 sections), now with a size limit. **I did not measure whether search gets better or worse**; Day 12 compares the three chunkers on questions.

**Not done:** the chunks are not stored or embedded, and the API does not use the chunker yet.

**Next:** the recursive chunker.

### Step 5. Task 2, second chunker: recursive splitting
**1. How it works** (`recursive_chunks` in `services/chunking.py`)
- If a piece of text fits the limit, it stays whole. If not, it is cut at the next kind of boundary, and each part is treated the same way: **blank line (paragraph) → line break → sentence end → space (word)**; a single word that is too long is cut between tokens.
- A sentence ends at `. ! ?` or the Hindi danda `।` `॥`, also when a quote or bracket closes there.
- Neighbouring pieces are then packed together up to the limit.
- **The packing, the recount and the overlap are the same code as the fixed-size chunker** (now the function `_pack`). The difference is only the pieces: words there, whole paragraphs, lines and sentences here.
- **Overlap repeats whole pieces only.** If the last piece of a chunk is larger than the overlap limit, there is no overlap at that boundary.

**2. The refactor did not change the fixed-size chunker.** Before and after, a fingerprint of every chunk (document, number, start, end, token count) over the whole corpus at 400/60 and 50/10:
```
fixed-size chunks at 400/60 and 50/10: 34,136 | fingerprint 60f6b26ecade51280b40     (identical before and after)
```
**3. Tests** (28 new): a paragraph that fits is not cut; small neighbours are packed; each boundary level in turn; the danda and closing marks; overlap in whole pieces; a very long word; random texts with both fake tokenizers; the real tokenizer on English, Hindi, a table and a long word.
```
pytest -q                              -> 308 passed
mypy src tests                         -> Success: no issues found in 83 source files
ruff check (the new and changed files) -> All checks passed
stress run (not in pytest): 20,000 random cases with each fake tokenizer -> every rule held
```
**4. Both chunkers on the whole corpus.** `scripts/chunk_report.py` now runs them side by side and says **where each chunk ends**, which is more useful than the punctuation check of Step 4.
```
uv run python scripts/chunk_report.py
```
```
234 documents, 1,209,588 tokens | max 400 tokens, overlap up to 60

                                              fixed size       recursive
chunks                                             3,632           3,678
time to chunk                                      7.9 s          11.8 s
tokens per chunk, median                             400             386
tokens per chunk, min / max                     38 / 400        31 / 400
chunks over 400 tokens                                 0               0
visible characters in no chunk                         0               0
tokens to embed                                1,408,340       1,354,840
  more than the documents have                      +16%            +12%
boundaries between chunks                          3,398           3,444
  with no overlap                                 0 (0%)        274 (8%)
  median overlap where there is one                   60              49
chunk ends at: end of a paragraph                68 (2%)     1,224 (36%)
  end of a line                                371 (11%)     2,220 (64%)
  end of a sentence                              70 (2%)          0 (0%)
  between two words of a sentence            2,889 (85%)          0 (0%)
  inside a word                                   0 (0%)          0 (0%)
paragraphs of <= 400 tokens, cut           256 of 13,014     0 of 13,014
markdown tables not whole in a chunk            14 of 28         4 of 28

===== fixed size: the first boundary of fastapi-docs/tutorial/cors.md (5 chunks)
--- chunk 0 (399 tokens) ends:
rigin (`http://localhost:8080`) then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.

To achieve this, the `:80`-backend must have a list of "allowed
--- chunk 1 (397 tokens) begins:
then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.

To achieve this, the `:80`-backend must have a list of "allowed origins".

In this case, the li

===== recursive: the first boundary of fastapi-docs/tutorial/cors.md (5 chunks)
--- chunk 0 (379 tokens) ends:
riate headers authorizing the communication from this different origin (`http://localhost:8080`) then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.
--- chunk 1 (366 tokens) begins:
To achieve this, the `:80`-backend must have a list of "allowed origins".

In this case, the list would have to include `http://localhost:8080` for the `:8080`-frontend to work correctly.

## Wildcard
```
**What it means**
- **The promise holds:** of the 13,014 paragraphs that fit in 400 tokens, recursive cut **0**; fixed size cut 256.
- **Tables:** 4 of 28 are not whole, against 14. The 4 are larger than the limit (472, 481, 781 and 882 tokens), so no setting of 400 can keep them whole.
- **Less overlap:** 274 boundaries (8%) have none, and 12% extra tokens instead of 16%.
- **No boundary is in the middle of a line or word**, against 85% of fixed-size boundaries ending between two words of a sentence.
- On the CORS page, recursive ends chunk 0 after a complete sentence; fixed size stopped at `a list of "allowed`.

**5. "End of a line" is not as good as it looks.** 64% of the recursive chunks end at a line break, and my PDF text has **hard line breaks inside sentences**:
```
'...In Proceedings of the 45th International
ACM SIGIR Conference on Research and Development in Information Retrieval
(SIGIR ...'
```
So I counted chunks that end with `. ! ? :` or a closing code fence:
| | PDF text | Markdown docs |
|---|---|---|
| fixed size | 6% | 11% |
| recursive (code today) | **27%** | 52% |

The cause is that my code tries **line breaks before sentence ends**. The plan lists "paragraph → sentence → word", without lines. I tested other orders with a temporary run (the code is unchanged):
| Order | Chunks | Ends with sentence punctuation: PDF / Markdown | Tables not whole | Table rows cut apart |
|---|---|---|---|---|
| paragraph, line, sentence, word (code today) | 3,678 | 27% / 52% | 4 of 28 | 0 of 198 |
| paragraph, sentence, line, word | 3,705 | **93%** / 54% | 4 of 28 | 1 of 198 |
| paragraph, sentence, word (the plan's list) | 3,704 | 93% / 54% | 4 of 28 | 1 of 198 |

- Putting sentences first helps the PDF text a lot and barely changes the Markdown.
- This is a rough measure: a full stop after an abbreviation ("et al.", "e.g.") also counts, and my splitter would cut there. I did not check how often.
- **My recommendation:** paragraph, sentence, line, word. It follows the plan's order and still has line breaks for text with no sentence ends, such as code and lists. (On this corpus it differs from the plan's list by one chunk.) **Decided in Step 6: I follow the plan's order.** The numbers in this step are from the first version, with the line level.

**Limits:** these measures describe where the cuts fall, not whether search gets better; Day 12 tests that. The chunks are not stored or embedded, and the app does not use either chunker yet.

### Step 6. The recursive chunker now follows the plan's order: paragraph, sentence, word
**Why:** the plan describes recursive splitting as **paragraph → sentence → word**. I had added a line level between paragraph and sentence. Step 5 showed that it cuts sentences apart in PDF text, which has line breaks inside sentences.

**Change** (`services/chunking.py`): I removed the line-break separator. A single line break is now just a space between words. As before, a word that is too long is cut between tokens, so the limit always holds. The fixed-size chunker is not affected.

**Tests:** I updated the order test and added one: a line break inside a sentence is not a boundary.
```
pytest -q                              -> 309 passed
mypy src tests                         -> Success: no issues found in 83 source files
ruff check (the changed files)         -> All checks passed
stress run, 20,000 random cases with each fake tokenizer -> every rule held
fixed-size fingerprint                 -> 60f6b26ecade51280b40, identical to before
```
**On the whole corpus**
```
uv run python scripts/chunk_report.py
```
```
234 documents, 1,209,588 tokens | max 400 tokens, overlap up to 60

                                              fixed size       recursive
chunks                                             3,632           3,704
time to chunk                                      7.5 s          11.8 s
tokens per chunk, median                             400             380
tokens per chunk, min / max                     38 / 400        31 / 400
chunks over 400 tokens                                 0               0
visible characters in no chunk                         0               0
tokens to embed                                1,408,340       1,332,817
  more than the documents have                      +16%            +10%
boundaries between chunks                          3,398           3,470
  with no overlap                                 0 (0%)       528 (15%)
  median overlap where there is one                   60              45
chunk ends at: end of a paragraph                68 (2%)     1,301 (37%)
  end of a line                                371 (11%)       707 (20%)
  end of a sentence                              70 (2%)     1,370 (39%)
  between two words of a sentence            2,889 (85%)         92 (3%)
  inside a word                                   0 (0%)          0 (0%)
paragraphs of <= 400 tokens, cut           256 of 13,014     0 of 13,014
markdown tables not whole in a chunk            14 of 28         4 of 28

===== fixed size: the first boundary of fastapi-docs/tutorial/cors.md (5 chunks)
--- chunk 0 (399 tokens) ends:
rigin (`http://localhost:8080`) then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.

To achieve this, the `:80`-backend must have a list of "allowed
--- chunk 1 (397 tokens) begins:
then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.

To achieve this, the `:80`-backend must have a list of "allowed origins".

In this case, the li

===== recursive: the first boundary of fastapi-docs/tutorial/cors.md (5 chunks)
--- chunk 0 (379 tokens) ends:
riate headers authorizing the communication from this different origin (`http://localhost:8080`) then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.
--- chunk 1 (366 tokens) begins:
To achieve this, the `:80`-backend must have a list of "allowed origins".

In this case, the list would have to include `http://localhost:8080` for the `:8080`-frontend to work correctly.

## Wildcard
```
**Recursive, before and after this change**
| | With the line level (Step 5) | Plan's order (now) |
|---|---|---|
| chunks | 3,678 | 3,704 |
| tokens to embed | 1,354,840 (+12%) | 1,332,817 (+10%) |
| boundaries with no overlap | 274 (8%) | 528 (15%) |
| PDF chunks ending with `. ! ? :` or a code fence | 27% | **93%** |
| Markdown chunks, the same measure | 52% | 54% |
| paragraphs that fit, cut | 0 of 13,014 | 0 of 13,014 |
| tables not whole | 4 of 28 | 4 of 28 |
| table rows cut apart (a temporary check) | 0 of 198 | 1 of 198 |

**What it means**
- In PDF text, chunks now end much more often at a sentence end.
- **Two things got worse:** one table row is now cut apart, and twice as many boundaries have no overlap. Overlap repeats whole pieces only, and a whole paragraph or sentence at the end of a chunk is more often larger than the 60-token limit.
- 92 boundaries (3%) are between two words of a sentence: pieces longer than the limit with no paragraph or sentence break to use. I did not look at each one.
- The report's "end of a line" now counts places where a sentence or paragraph rule, or a word split, happened to fall at a line break. It is no longer a rule of the splitter.
- The punctuation measure is rough: the full stop of "et al." or "e.g." counts as an end, and my sentence rule can cut there. I did not measure how often.
- Whether search improves is not tested yet (Day 12).

### Step 7. Task 2, third chunker: Markdown / structure-aware
**1. First I looked at the corpus** (a temporary check): 190 of the 234 documents are Markdown, 44 are text from PDFs.
```
real heading lines                        1757
'# ...' lines inside code (not headings)   124     e.g. '# This is not asynchronous'
code blocks                               1017     (129 of them indented by 4 or more spaces)
unclosed code blocks                         0
```
So a splitter that treats every `#` line as a heading would cut code apart 124 times. The chunker has to know when it is inside a code block.

**2. How it works** (`markdown_chunks` in `services/chunking.py`)
1. `markdown_blocks` reads the text as **headings, fenced code blocks, pipe tables and paragraphs**. Inside a fence, `#` is code. A fence closes only with the same mark, at least as long.
2. The blocks are grouped into **sections**: a heading and its text. A heading with no text of its own stays with the next section. `{ #anchor }` is removed from titles.
3. **A chunk never holds text of two sections.** A section that fits is one chunk.
4. In a longer section, a **code block or table that fits stays whole**. If it is longer than the limit, it is cut between lines. Longer text is cut by sentence, then word. A heading stays with the first piece below it when they fit.
5. Overlap stays inside a section. The packing and the recount are the same code as the other two chunkers.
6. Each chunk now carries its **heading path** (`TextChunk.headings`). Task 3 will use it.

**What it does not do:** a long table is cut without repeating its header row (every chunk is an exact slice of the document); headings underlined with `===` are not recognised (my corpus has none of the `===` kind); it does not understand HTML or PDF layout.

**3. A bug the tests found:** with a code block that was never closed, a chunk could end with a blank line. Blank lines at the end are now left out. (One of my new tests was also wrong: its text was too short to show the difference from the recursive chunker.)

**4. Tests** (28 new): heading paths; a heading without text; anchors; `#` inside four kinds of fence; fence lengths; an unclosed fence; code that fits is not cut (and the recursive chunker does cut it); a long table is cut between rows only; a heading stays with its text; overlap does not cross sections; text without headings; random Markdown with both fake tokenizers; the real tokenizer with a Hindi heading.
```
pytest -q                              -> 337 passed
mypy src tests                         -> Success: no issues found in 83 source files
ruff check (the new and changed files) -> All checks passed
stress run, 20,000 random cases with each fake tokenizer -> every rule held
fixed-size and recursive fingerprint (69,989 chunks)     -> identical before and after
```
**5. All three on the whole corpus**
```
uv run python scripts/chunk_report.py
```
```
234 documents, 1,209,588 tokens | max 400 tokens, overlap up to 60

                                              fixed size       recursive        markdown
chunks                                             3,632           3,704           4,578
time to chunk                                      7.9 s          11.4 s          13.1 s
tokens per chunk, median                             400             380             354
tokens per chunk, min / max                     38 / 400        31 / 400        11 / 400
chunks under 50 tokens                                 1               3             177
chunks over 400 tokens                                 0               0               0
visible characters in no chunk                         0               0               0
tokens to embed                                1,408,340       1,332,817       1,306,424
  more than the documents have                      +16%            +10%             +8%
boundaries between chunks                          3,398           3,470           4,344
  with no overlap                                 0 (0%)       528 (15%)     2,059 (47%)
  median overlap where there is one                   60              45              45
chunk ends at: end of a paragraph                68 (2%)     1,301 (37%)     2,135 (49%)
  end of a line                                371 (11%)       707 (20%)       760 (17%)
  end of a sentence                              70 (2%)     1,370 (39%)     1,366 (31%)
  between two words of a sentence            2,889 (85%)         92 (3%)         83 (2%)
  inside a word                                   0 (0%)          0 (0%)          0 (0%)
paragraphs of <= 400 tokens, cut           256 of 13,014     0 of 13,014    20 of 13,014
markdown tables not whole in a chunk            14 of 28         4 of 28         4 of 28
code blocks of <= 400 tokens, cut             127 of 981       80 of 981        0 of 981
markdown-doc chunks mixing sections         942 of 1,288    932 of 1,313      0 of 2,187

===== fixed size: the first boundary of fastapi-docs/tutorial/cors.md (5 chunks)
--- chunk 0 (399 tokens) ends:
rigin (`http://localhost:8080`) then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.

To achieve this, the `:80`-backend must have a list of "allowed
--- chunk 1 (397 tokens) begins:
then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.

To achieve this, the `:80`-backend must have a list of "allowed origins".

In this case, the li

===== recursive: the first boundary of fastapi-docs/tutorial/cors.md (5 chunks)
--- chunk 0 (379 tokens) ends:
riate headers authorizing the communication from this different origin (`http://localhost:8080`) then the `:8080`-browser will let the JavaScript in the frontend send its request to the `:80`-backend.
--- chunk 1 (366 tokens) begins:
To achieve this, the `:80`-backend must have a list of "allowed origins".

In this case, the list would have to include `http://localhost:8080` for the `:8080`-frontend to work correctly.

## Wildcard

===== markdown: the first boundary of fastapi-docs/tutorial/cors.md (10 chunks)
--- chunk 0 (100 tokens) ends:
US/docs/Web/HTTP/CORS) refers to the situations when a frontend running in a browser has JavaScript code that communicates with a backend, and the backend is in a different "origin" than the frontend.
--- chunk 1 (122 tokens) begins:
## Origin

An origin is the combination of protocol (`http`, `https`), domain (`myapp.com`, `localhost`, `localhost.tiangolo.com`), and port (`80`, `443`, `8080`).

So, all these are different origins

===== markdown: the heading path of every chunk of fastapi-docs/tutorial/cors.md
   0  100 tokens  CORS (Cross-Origin Resource Sharing)
   1  122 tokens  CORS (Cross-Origin Resource Sharing) > Origin
   2  214 tokens  CORS (Cross-Origin Resource Sharing) > Steps
   3   93 tokens  CORS (Cross-Origin Resource Sharing) > Wildcards
   4  216 tokens  CORS (Cross-Origin Resource Sharing) > Use `CORSMiddleware`
   5  358 tokens  CORS (Cross-Origin Resource Sharing) > Use `CORSMiddleware`
   6  190 tokens  CORS (Cross-Origin Resource Sharing) > Use `CORSMiddleware`
   7   79 tokens  CORS (Cross-Origin Resource Sharing) > Use `CORSMiddleware` > CORS preflight requests
   8   43 tokens  CORS (Cross-Origin Resource Sharing) > Use `CORSMiddleware` > Simple requests
   9  138 tokens  CORS (Cross-Origin Resource Sharing) > More info
```
The last two rows are measured on the **190 Markdown documents** only. They use `markdown_blocks`, the same reading of headings and code that the third chunker uses. So its 0 shows that it follows its own rules, which the tests check separately; the numbers of the other two show how often they break those structures.

**What it means**
- **Sections:** about 7 in 10 chunks of the other two chunkers on Markdown (942 of 1,288 and 932 of 1,313) hold text from before **and** after a heading. The structure-aware chunker has none.
- **Code:** of 981 code blocks that fit in 400 tokens, fixed size cuts 127 and recursive 80; this one cuts 0.
- **20 fitting paragraphs are cut**, all of them inside code blocks that are longer than 400 tokens (they had to be cut somewhere).
- **Tables:** 4 not whole, like recursive: they are longer than the limit.
- **The price: more and smaller chunks.** 4,578 against about 3,700, and **177 chunks under 50 tokens** (160 are a heading with a little text, 16 other short text, 1 only headings). For example, a whole section of 20 tokens: `# Advanced Python Types` and one sentence. A very small chunk may carry too little to be found or to answer with. I did not merge small sections, because that would mix sections again.
- **Overlap is rare:** 47% of its boundaries have none, partly because overlap never crosses a section.
- **On PDF text it is almost the recursive chunker:** the same cuts in 40 of the 44 documents. These texts have no Markdown headings, so there is no structure to use; one line, `# Synth.` in an arXiv paper, is wrongly taken as a heading. I did not look into the other differences. Real PDF sections need a layout-aware parser (task 4).
- None of this says which chunker **finds** better. Day 12 compares the three on questions.

**Task 2 is done.** The chunks are not stored or embedded, and the app does not use the chunkers yet.

**Next:** task 3, contextual headers.

### Step 8. Task 3: contextual headers, as an option
**Why:** a chunk that says "the fee is 500" does not say which fee. A header puts the document title and the section path in front of it **when it is embedded**.

**1. What I built** (`services/chunking.py`)
| Part | What it does |
|---|---|
| `contextual_header(title, headings)` | `Title > Section > Subsection`; the title is not repeated when the first heading says the same |
| `add_contextual_headers(text, chunks, title)` | gives every chunk its header; works with all three chunkers |
| `TextChunk.header`, `TextChunk.embedding_text` | `embedding_text` is header, blank line, text; with no header it is just the text |
| `CHUNK_CONTEXT_HEADERS=false` (setting) | the option, off by default; it will be read by the ingestion job (task 1) |

**Choices**
- **`text` does not change.** It stays the exact slice of the document, for storing, showing and citing. Only `embedding_text` has the header.
- **Chunk boundaries do not change.** With headers on or off I compare the same pieces, so a difference in search can come only from the header (Day 12).
- **The header comes on top of the 400 tokens**, as a result of the choice above.
- A fixed-size or recursive chunk can run into the next section; its header names the section it **starts** in.
- The header line has the same form as what I embedded on Day 9 (path, blank line, text).

**2. Tests** (14 new): the header text; headers off leave the text as it is; for all three chunkers headers change nothing else; a chunk before the first heading; a `#` comment in code never reaches a header; the default is off; an empty HTML anchor is removed.
```
pytest -q                              -> 351 passed
mypy src tests                         -> Success: no issues found in 83 source files
ruff check (the changed files)         -> All checks passed
fixed-size and recursive fingerprint   -> identical to before
```
**A small fix:** a heading with an empty HTML anchor (`<a name="..."></a>`) put the tag into the header. It is now removed from heading titles (other `<...>` text stays). The longest header went from 102 to 74 tokens; the chunk rows of the report did not change.

**3. On the whole corpus** (`uv run python scripts/chunk_report.py`; the header rows, with the chunk counts for comparison)
```
                                                  fixed size         recursive          markdown
chunks                                                 3,632             3,704             4,578
headers on: tokens added, median / max               12 / 74           12 / 74           12 / 74
  tokens to embed                          1,455,420 (+3.3%) 1,380,925 (+3.6%) 1,368,580 (+4.8%)
  largest embedded text, tokens                          474               463               457
  embedded texts over 400 tokens                       3,314             1,175               897
```
What is embedded for one chunk:
```
CORS (Cross-Origin Resource Sharing) > Use `CORSMiddleware` > CORS preflight requests

### CORS preflight requests

These are any `OPTIONS` request with `Origin` and `Access-Control-Request-Method` headers.
...
```
- A header adds a median of **12 tokens** (at most 74): **3.3% to 4.8%** more tokens to embed.
- With headers, many embedded texts are over 400 tokens; the largest is 474. All are far below 8,194.

**4. Two limits I found** (temporary checks)
**a. PDF text gets only the title.** With the Markdown chunker, 2,437 of 4,578 headers are just the title, and 2,366 of those are chunks of PDF text (of 2,391 PDF chunks). That text has no Markdown headings, so there is no section path. It needs a parser that knows the PDF's sections (task 4).

**b. 14 document titles are wrong, so their headers would be wrong.** My Day 9 loader took the first line starting with `# ` as the title **without skipping code blocks**, so code comments became titles:
```
pydantic-docs/concepts/serialization.md   title: 'returns a dictionary:'            (the first real heading is 'Serializing data')
pydantic-docs/concepts/models.md          title: 'normal copy gives the same object reference for bar:'
pydantic-docs/internals/resolving_annotations.md   title: 'module1.py:'
```
| Where the wrong title is | How much |
|---|---|
| `documents.title` | 14 of 234 documents |
| `title` in chunk metadata, which the search API returns | 276 chunks of those documents |
| the text embedded on Day 9 (current vectors) | **14 of 2,097 sections** (one per document) |
| headers built now: fixed / recursive / Markdown | 322 / 327 / 490 chunks |

- The correct pages of 4 of the 20 Day 9 queries (7, 8, 9, 16) are among these documents. I did not test whether the wrong titles changed any result.
- This is a **parsing failure** for the catalogue: a title must be read with the same care about code blocks as the headings (`markdown_blocks`).
- **I have not changed any stored title or vector.** Fixing the titles before headers are used is an open point.
- The other titles of the Markdown documents: 146 equal the first heading, 30 are made from the file path (for example `Concepts/Strict Mode`).

**Not done:** I have not measured whether headers improve search; that needs embeddings and questions (Day 12).

**Next:** task 1, the ingestion job.

### Step 9. Task 1, first part: which job queue (checked; nothing installed)
**Why a queue at all:** parsing, chunking and embedding a document takes seconds to minutes, too long for one HTTP request. The API stores the work, a **separate worker** does it, and a status endpoint reports it. Redis already runs in my Compose file and the app does not use it yet.

**1. What each would add** (`uv pip install --dry-run`, 2026-11-25; PyPI metadata)
| | `arq` | `celery[redis]` |
|---|---|---|
| packages added | 3: `arq` 0.28.0, `hiredis` 3.5.0, `redis` 5.3.1 | 15: `celery` 5.7.0, `redis` 8.2.0, `kombu` and 12 more |
| latest release | 0.28.0, 2026-04-16 (before it: 2026-02-02, 2025-01-06) | 5.7.0, 2026-11-05 (before it: 2026-03-26, 2026-01-04) |
| Redis client | **held back**: arq requires `redis<6` (latest is 8.2.0) | the latest, 8.2.0 |

**2. What the docs say** (read 2026-11-25)
**ARQ**
- Its maintainers wrote, in [issue 510](https://github.com/python-arq/arq/issues/510) (2025-10-18): they will "continue to fix critical security issues" and will not archive it, "but don't expect work on new fixes". No successor is named. (0.28.0 was released after that.)
- Jobs are `async def` functions. Retries: raise `Retry`, `max_tries` is 5 by default, `job_timeout` 300 s.
- Its docs warn: "Jobs may be called more than once!"

**Celery**
- Actively released.
- **Celery 5.7.0 does not run `async def` tasks by itself.** The docs pages I checked say nothing about them, so I read the source. In [`app/trace.py`](https://github.com/celery/celery/blob/v5.7.0/celery/app/trace.py) the worker gets the result with a plain call, `R = retval = fun(*args, **kwargs)`, and then marks the task successful. I found no `await`, `asyncio` or coroutine check in that file (`task.py` and `base.py` showed none either, but I could read only part of them).
  - *My inference, not run:* calling an `async def` function only creates a coroutine object, so its body would not run. With a stored result, saving that object would probably fail with an encoding error.
  - So each async job needs a normal task that starts the event loop itself, for example with `asyncio.run(...)`.
  - [Issue 6552, "Support async function"](https://github.com/celery/celery/issues/6552) is **open** (opened 2020, in milestone 6.0 when read); I could not read all of its comments.
- The [FAQ](https://docs.celeryq.dev/en/stable/faq.html) says it does not support Windows. My worker would run in a Linux container; running it directly on my Windows machine would be unsupported.
- By default a task is acknowledged **before** it runs (`task_acks_late` is disabled), so a task can be lost if a worker dies. To get it back I would enable `task_acks_late`, and `task_reject_on_worker_lost` for a killed worker process (its docs warn this "can cause message loops").
- With Redis, a task not acknowledged within `visibility_timeout` (**1 hour** by default) is given to another worker and **run again**.
- Automatic retries with backoff are built in (`autoretry_for`, `retry_backoff`).

**With either queue a job can run twice.** So ingestion has to be idempotent anyway, which is the goal of the day (task 6).

**3. Comparison for this project**
| What matters here | ARQ | Celery |
|---|---|---|
| my code is async (FastAPI, SQLAlchemy async, `httpx`) | fits directly | **no native async tasks**: each needs a bridge to the async code; the async database engine has to be handled with care across event loops |
| maintenance | critical security fixes only is what is promised | actively released |
| size and setup | 3 packages, small | 15 packages, many settings, some defaults to change for safety |
| in a long-lived product | a risk | the safer choice |

**4. My proposal: ARQ**, for this project, knowing the risk.
- It matches the async code I have, so ingestion can call my existing async database and embedding functions.
- The plan allows either, and what I want to learn (queue, worker, status, retries, idempotency) is the same.
- **To limit the risk:** the ingestion itself will be a plain `async` function that knows nothing about the queue; only a thin layer enqueues it and calls it. Changing to Celery would then mean rewriting that layer and the worker settings.
- If this were a service that had to live for years, or needed Celery's features (schedules, several brokers, workflows), I would choose Celery.

**Not done at this step:** I had not run either one and nothing was installed. The decision is in Step 10.

**5. Taskiq (also considered).** It is built for asyncio. I did not choose it: my plan's stack table lists **"ARQ or Celery with Redis"** under *use actively* (Dramatiq and Temporal are *awareness only*), and Taskiq is not in it. What I checked (2026-11-25; retries and duplicate job ids I did not read about):
- the core package is classed "3 - Alpha" on PyPI; latest `taskiq` 0.13.0 (2026-09-26);
- Redis is a separate plugin, [`taskiq-redis`](https://github.com/taskiq-python/taskiq-redis); with it, 13 packages would be added;
- in that plugin only the **stream** broker supports acknowledgements; its README says that with the list and pub/sub brokers a message is lost if the worker is killed.

### Step 10. ARQ chosen, installed and tested against the Redis in Docker
**Decision: ARQ.** It is one of the two queues my plan allows, and it runs my `async` code directly. The ingestion itself will be a plain async function; only a thin layer will know about ARQ.

**1. Install**
```
uv add arq
 + arq==0.28.0   + hiredis==3.5.0   + redis==5.3.1
```
`pyproject.toml` got one line, `"arq>=0.28.0"`; in `uv.lock` these 3 are the only name or version changes.

**2. A problem on Windows.** I first ran a test from Windows against the Docker Redis (`127.0.0.1:6380`). The jobs were queued, then closing the worker crashed:
```
File ".../arq/worker.py", line 870, in close
    self.handle_sig(signal.SIGUSR1)
AttributeError: module 'signal' has no attribute 'SIGUSR1'
```
Windows has no `SIGUSR1`. My worker will run in a **Linux container**, so I ran the test there. I did not look for a way to run the ARQ worker directly on Windows. (The test removed its keys even though it crashed.)

**3. The test inside Docker** (rebuilt the API image so that it has ARQ; Redis is the compose service `redis`, 7.4.12; a temporary script with its own queue)
```
docker compose up -d --build api
docker exec -i ai-service-api python - < arq_smoke.py
```
```
arq 0.28.0 | redis client 5.3.1 | redis server 7.4.12 | keys before: 0
status before a worker runs: queued
  ok            status complete  success True  result 'done: a'
  flaky         status complete  success True  result 'worked on try 3'
  broken        status complete  success False result ValueError('a corrupt file')
  always_retry  status complete  success False result JobExecutionFailed('max 3 retries exceeded')
runs in order: ['ok(a) try 1', 'flaky try 1', 'broken try 1', 'always_retry try 1', 'always_retry try 2', 'always_retry try 3', 'flaky try 2', 'flaky try 3']
same id while queued      : first accepted, second refused (None)
same id after it finished : refused (None) | seconds the result is still kept: 3600
after the result is gone  : accepted
keep_result=0, enqueue    : accepted
keep_result=0, enqueue    : accepted
   it ran: ['ok(no-result) try 1', 'ok(no-result) try 1'] | status afterwards: not_found
clean-up: removed 5 test keys | keys now: 0
```
**4. What this means for the ingestion job**
| Seen | So in ingestion |
|---|---|
| an `async def` job with an `await` ran | I can call my async database and embedding code directly |
| `raise Retry(...)` ran the job again, up to `max_tries` | use it for **temporary** errors (network, 429, 5xx) |
| an ordinary exception failed the job on **try 1**, with no retry | right for **permanent** errors (a corrupt file). A temporary error must be turned into `Retry` on purpose, or it is not retried |
| `complete` is also the status of a **failed** job | the status endpoint must report success or failure, not only "complete" |
| a repeated `_job_id` was refused while queued **and after it finished**, as long as the result was kept (3,600 s by default) | a job id made only from the document would silently skip a real re-ingest within the hour. And `enqueue_job` returns `None`, which my code must check |
| with `keep_result=0` the id was accepted again, but the status became `not_found` | ARQ's result cannot be the status record: **my own job table** in PostgreSQL keeps status, stage and error |

Not tested: a worker killed in the middle of a job, job timeouts, several workers.

**5. Nothing else broke:** `mypy src tests` no issues; `pytest -q` 351 passed; `curl localhost:8000/health` -> `{"status":"ok"}`.

**Also seen:** the API image has `tokenizers` now, but **not the tokenizer file** (it lives in my laptop's Hugging Face cache). The worker needs it, so how the image gets the file has to be solved in task 1.

**Next in task 1:** the Redis setting and a worker service in Docker Compose, the job table, where the uploaded file is kept, and the endpoints.

### Step 11. Task 1: the Redis setting and the worker service
**1. What I added**
| File | What |
|---|---|
| `core/config.py`, `.env.example` | `REDIS_URL`, default `redis://127.0.0.1:6380/0` (the compose Redis seen from my machine) |
| `services/job_queue.py` | the thin layer over ARQ: the queue name `ai-service:jobs`, `create_job_queue(settings)` |
| `worker.py` | `WorkerSettings` and one job, `ping`, which does no work and reports which machine ran it |
| `docker-compose.yml` | a `worker` service (same image as the API, command `arq ai_service.worker.WorkerSettings`, container `ai-service-worker`); the API and the worker get `REDIS_URL=redis://redis:6379/0` |
| `tests/test_worker.py` | 5 tests: `ping`, the same queue name on both sides, a short health interval, the URL parsing, the default URL |

- The worker waits for the API to be healthy, because the API applies the database migrations.
- **Health check:** `arq ... --check` succeeds only while the worker keeps writing an "alive" key. ARQ writes it **once an hour** by default; I set `health_check_interval = 10`.
- The worker does **not** reload when the code changes: `docker compose restart worker`.
- The app only has the function that connects to the queue. **No API endpoint queues a job yet.**

**2. Checks**
```
pytest -q                          -> 356 passed
mypy src tests                     -> Success: no issues found in 86 source files
ruff check (new and changed files) -> All checks passed
docker compose config -q           -> valid
docker compose up -d --build api worker  -> api: healthy | worker: healthy
```
**3. Live tests** (jobs queued by a script run inside the API container, with the app's own `create_job_queue` and settings)
```
container ids: api 612e14d1b801 | worker 24acbe07919a

[1] a job from the api container
queued from host 612e14d1b801 | job id f45726960603... | status queued
result after 154 ms: {'message': 'hello from the api container', ..., 'job_try': 1, 'worker_host': '24acbe07919a'}

[2] stop the worker, then queue a job
queued from host 612e14d1b801 | job id 51f143c37734... | status queued
5 s later: status queued

[3] the health check with no worker
--check exit code: 1 | Health check failed: no health check sentinel value found

[2b] start the worker again
the waiting job: status complete, success True, waited in the queue 8.8 s
worker health: healthy | --check exit code 0

redis keys: ai-service:jobs:health-check, arq:result:<job id> (two)
worker memory: 39.27 MiB
```
**What it shows**
- The job was queued on one machine and **run on another**: `worker_host` is the worker container's id.
- With the worker stopped, the job **waited in Redis** and ran when the worker came back. The API did not need the worker to accept it.
- The health check fails when no worker is alive, and passes again afterwards.
- The two results stay in Redis for an hour (ARQ's default).

**4. Seen in the worker log**
```
07:43:08:   0.11s → f4572696...:ping('hello from the api container')
07:43:08:   0.00s ← f4572696...:ping ● {'message': 'hello from the api container', 'job_id': ...
07:43:08: shutdown on SIGTERM ◆ 1 jobs complete ◆ 0 failed ◆ 0 retries ◆ 1 ongoing to cancel
```
- **ARQ logs every job's arguments and its result.** So the ingestion job must be given **ids only** (a document or job id), never file contents, text or secrets, and must return nothing sensitive.
- At the stop, the log said "1 ongoing to cancel" although the one job had completed. I did not find out why.

**Not tested:** stopping the worker **in the middle** of a job; Redis restarting with jobs waiting; two workers at once.

**Next in task 1:** where the uploaded file is kept, and the job table (status, stage, error).

### Step 12. Task 1: where the uploaded file is kept (options; nothing changed)
**The need:** today only the extracted text is saved. The worker needs the **original file** to parse it, and a better parser later needs it again. The API and the worker are two containers, so the file must be somewhere both can read. The job itself should carry only an id (Step 11).

**1. Facts I checked**
```
PDF files under day-9: 11 | total 220.0 MB | median 1.02 MB | largest 207.7 MB (chemistry-2e_-_WEB.pdf)
the database today: 42 MB

20 MB of random bytes in PostgreSQL (a temporary table, rolled back):
  5 files: write median 148 ms, read median 69 ms, all read back identical, 104 MB on disk for 100 MB of files

a new shared Docker volume (throw-away, removed afterwards), mounted into my image:
  the app runs as uid=999(appuser)
  drwxr-xr-x 2 root root /data/uploads
  touch: cannot touch '/data/uploads/x': Permission denied
```
- **The chemistry textbook (207.7 MB) is over my 20 MB upload limit**; the other 10 PDFs are within it. So the whole corpus cannot simply go through the upload route (this matters for task 6).
- The volume problem is fixable: create the folder in the Dockerfile and give it to `appuser` before the volume is first used.

**2. The options**
| | A. PostgreSQL (`bytea`, its own table) | B. Shared Docker volume (files on disk) | C. Object storage (S3 or MinIO) |
|---|---|---|---|
| new parts | a table (a migration) | a volume and a Dockerfile change | a new service, a client library, keys |
| saving the file, the document and the job record | **one transaction** | two separate places: a failure between them leaves a file or a record alone | two separate places |
| deleting a document | the foreign key **cascade** removes the file | my code must delete the file too | my code must delete the object too |
| tenant isolation | the same `tenant_id` filter as everything else | by folder names and my code | by key names and my code |
| the API on my machine with the worker in Docker | works (same database) | they do not see the same folder without extra setup | works |
| the cost | the database and its backups grow; a file passes through memory as a whole | keeps the database small | made for large files and many machines |

**3. My proposal: A, PostgreSQL**, in a table of its own (for example `document_files`: document id, tenant id, the bytes, their SHA-256, file name, type), so that listing documents never loads file contents.
- It keeps what Day 10 taught me: with **one store, one transaction and a cascade** there is nothing to keep in step. With Qdrant I had to order the deletes myself.
- At my limit of 20 MB, 148 ms to write and 69 ms to read are small next to parsing and embedding.
- **The price:** every file makes the database bigger. My corpus PDFs without the textbook are about 12 MB; thousands of large files would be a different matter.
- **When I would move:** large or very many files, or several servers: then object storage. I would keep reading and writing the file behind two small functions, so that only they change.
- My learning note (`ai-eng-learning/day-11`, note 4) suggested a shared volume for now. After these checks I prefer the database, mainly for the single transaction and the cascade.

**Limits of the check:** random bytes, one user, my laptop; I did not measure backups, memory use or many files at once. I did not try object storage.

**Open until I decide.** The 207.7 MB textbook is looked at first, in Step 13.

### Step 13. Large files: what to do with the 207.7 MB textbook (measured; nothing changed)
**Why first:** how large a file may be affects where files are stored (Step 12). Day 9 holds this textbook as **34 documents** with page ranges (mostly one per chapter or appendix). Some of my labelled queries point to these documents.

**1. Measurements** (temporary script, `pypdf` 6.21.4, my laptop, one run; the parts were made in memory)
```
whole book: 1203 pages, 2,524,430 characters, read in 65 s, peak memory of the process 564 MB

split into 34 parts (the Day 9 page ranges) in 7 s
  MB per part: median 12.8, min 0.0, max 72.6 | total 823 MB | parts over 20 MB: 13
    72.6 MB  pages 1017-1070  Chapter 21 Nuclear Chemistry
    68.9 MB  pages 931-972    Chapter 19 Transition Metals and Coordination Chemistry
    68.7 MB  pages 973-1016   Chapter 20 Organic Chemistry
  pages in the parts: 1112 of 1203 | not in any part: 91
  same pages split twice: identical bytes True
  reading one part (Chapter 5, 12.4 MB, 44 pages): 4 s, 106,821 characters
```
- **Splitting the PDF does not get it under the limit:** 13 of 34 parts are still over 20 MB, and all parts together are about **four times** the original. I think each part carries its own copy of shared images and fonts; I did not check that. ("min 0.0" is a part under 0.05 MB.)
- The text is small (2.5 million characters); the size of the file is something else, most likely the pictures (not measured).

**2. The options**
| Option | For | Against |
|---|---|---|
| **A. Raise the upload limit** to about 250 MB for everyone | simplest to describe | any user can make the app handle huge files. Today the upload route reads a file whole into memory and extracts its text **inside the request**: for this book 65 s and 564 MB at the peak. Each copy adds about 208 MB to the database if files are stored there |
| **B. Split into chapter PDFs** and upload those | each document stays separate | 13 parts are still over 20 MB; 823 MB in total |
| **C. Save only the extracted text** for large files | small | no original to parse again; the textbook is where I expect hard tables (task 4) |
| **D. Keep 20 MB for uploads; load the textbook with a script of my own** that reads it from disk, page range by page range | the public limit stays; the same 34 documents as Day 9; one part at a time needs less work than the whole book | this book's original is **not** in the database: parsing it again needs the file on disk (`download_corpus.py` downloads it again) |

**3. My proposal: D.**
- The limit of 20 MB protects the app from users. This book is not a user upload: it is my own corpus, loaded by me.
- The script will call **the same ingestion function** as the worker (parse, clean, chunk, embed, upsert), so there is one pipeline, not two. The difference is only where the bytes come from.
- For each of the 34 documents I keep the **file name, the file's SHA-256 and the page range**. With those I can tell whether the file or the range changed, which idempotency needs (task 6).
- It keeps the page ranges of Day 9, so the document ids that my labelled queries use can stay.
- If users ever need files this large, that is the moment for object storage and a streaming upload, not a bigger number in a setting.

**What D costs:** two ways in (the API for users, a script for my corpus) and one source whose original is outside the database. I will say so in the document's metadata.

**Not measured:** parsing the book with another parser; saving 208 MB in PostgreSQL; the memory of reading page ranges from the one open file.

**Open until I decide.**

### Step 14. The old corpus stays; new corpus work goes into a new organisation
**Decisions**
- **The old corpus is not removed or changed in any way:** the `Corpus Tenant` of Days 9 and 10 (234 documents, 2,097 chunks).
- **All new corpus work goes into a new organisation, `Day 11 Corpus`.**
- **No script that reads the 207.7 MB PDF** (this replaces my proposal D of Step 13): the textbook's new version will start from the **text already saved** in the old documents, which I will copy and chunk again. It cannot repair mistakes of the first text extraction, and its original file is not stored. The upload limit stays 20 MB.

**1. A fingerprint of the old data first** (`scripts/old_corpus_fingerprint.py`, read only): one md5 over the old tenant's documents and one over its chunks, including the vectors. Saved in `scripts/results/day11_old_corpus_fingerprint.json`, which the script does not overwrite.
```
documents    234 rows   md5 d5d40df5d9a20f4a769c3da412fff8b1
chunks      2097 rows   md5 0124810fda482343a9d5b8f81e14195d
```
My first version took **every** column of a row. Adding a column later (such as a pipeline version) would then have looked like a change of the old data. It now lists today's columns by name; I checked that the old data still matched the first fingerprint before I replaced it. A test inside a rolled-back transaction:
```
after ADDING a column      -> same fingerprint: True
after changing ONE old row -> same fingerprint: False
after the rollback         -> same fingerprint: True
```
**2. The new organisation**, created through the app's own sign-up with one local test account, `corpus-day11@example.com`. Its random password is in `.env.day11-test` (ignored by git and by Docker).
```
POST /auth/register -> 201
POST /auth/login    -> 200
organisation 'Day 11 Corpus' id beb99e9d-a673-4112-8a37-635f6c30e3e9
GET /documents      -> 0 documents
POST /search        -> 200 | 0 results (the old corpus did not appear)
old corpus, checked again afterwards -> THE OLD CORPUS IS UNCHANGED
```
| Organisation | Accounts | Documents | Chunks |
|---|---|---|---|
| Corpus Tenant (old) | 1 | 234 | 2,097 |
| Day 11 Corpus (new) | 1 | 0 | 0 |
| three earlier test organisations | 1 each | 1, 1, 3 | 0 |

**Rules I follow so that the old data stays**
- After every step that writes to the database: `uv run python scripts/old_corpus_fingerprint.py`.
- New ingestion writes only to `Day 11 Corpus`.
- I do not run `scripts/upsert_corpus.py` again (it writes into the old tenant), and I do not delete old documents through the API.
- `docker compose down -v` would delete the database volume with everything in it, so I do not use `-v`.

**What this does not promise**
- The fingerprint covers the old tenant's **documents and chunks** in PostgreSQL. It does not cover its account, Qdrant, or the files of Day 9 on disk. It is a check, **not a backup**; I have not made a backup.
- **The HNSW index is shared by all organisations.** Adding thousands of vectors changes that graph. A search still returns only its own organisation's chunks, but if I run the Day 10 measurements again, HNSW results or speed may differ, although the old rows have not changed.

### Step 15. Task 1: the table for original files
**Decision:** the original file of a normal upload (up to 20 MB) is stored in PostgreSQL, in a table of its own (option A of Step 12).

**1. The table `document_files`**
| Column | Why |
|---|---|
| `document_id` (primary key, FK `documents`, **cascade**) | at most one file per document; deleting the document deletes the file |
| `tenant_id` (FK `tenants`, cascade, indexed) | every read filters on it |
| `filename`, `content_type` | what was uploaded |
| `byte_size`, `sha256` | size and fingerprint of the bytes |
| `data` (`bytea`) | the file |
| `created_at`, `updated_at` | timestamps |

A separate table, so that listing or reading documents never loads file contents. A document may have no file (the textbook).

**2. Two functions** (`services/file_store.py`; the rest of the app is meant to use only these)
- `save_file(...)` first checks that the document belongs to the tenant (otherwise the same "document not found" as for a missing one), saves or **replaces** the file, and **does not commit**: the caller saves document, file and job in one transaction.
- `get_file(...)` filters on tenant and document.
- The database does not itself force `tenant_id` to equal the document's tenant (the same as in `chunks`); `save_file` does.

**3. The migration** `5520625dcc98`, made with `alembic revision --autogenerate` and read before use: it creates only this table and its index.
```
upgrade     -> Running upgrade dc013ced71fa -> 5520625dcc98
downgrade   -> the table is gone     (it was empty)
upgrade     -> again
alembic check -> No new upgrade operations detected.
```
**4. Tests** (`tests/test_file_store.py`, 5 new, on SQLite): the same bytes come back (all 256 byte values); saving again replaces; another tenant can neither read nor write; no commit means nothing is kept; `DELETE /documents/{id}` removes the file.
```
pytest -q                          -> 361 passed
mypy src tests                     -> Success: no issues found in 89 source files
ruff check (new and changed files) -> All checks passed
docker compose up -d --build api worker -> api: healthy | worker: healthy
```
**5. Live on PostgreSQL**, in `Day 11 Corpus`, with a real research PDF (a temporary script and a temporary document, both removed)

My first run failed at the login with `Server disconnected without sending a response`. I had started it while the rebuild of the API was still running in the background, which is probably why. It stopped before creating anything (0 documents, 0 files). After the rebuild had finished:
```
file: 2601.05264.pdf, 2.8 MB (the largest of the 10 research PDFs)
temporary document through the API -> 201 | files stored by the upload route today: 0
[save] 206 ms   [read, new session] 17 ms | same bytes: True | sha256 matches: True | byte_size 2,917,925
in PostgreSQL: octet_length(data) = 2,917,925 | tenant matches the document: True
[other tenant reads it] -> None
[other tenant writes it] -> DocumentNotFoundError, nothing written
DELETE /documents/{id} by the owner -> 204 | file rows left: 0
after: Day 11 Corpus documents 0 | document_files rows 0
```
Then the old corpus:
```
documents    234 rows   md5 d5d40df5d9a20f4a769c3da412fff8b1
chunks      2097 rows   md5 0124810fda482343a9d5b8f81e14195d
THE OLD CORPUS IS UNCHANGED
```
**What it shows**
- A 2.8 MB PDF came back byte for byte from a new session (206 ms to save, 17 ms to read, one run).
- Another tenant got nothing when reading and could not write.
- The owner's delete through the API removed the file by the cascade.
- `Day 11 Corpus` is empty again, and the old corpus is unchanged.

**Not done:** the **upload route does not save the file yet** ("files stored by the upload route today: 0"). That comes with `POST /ingest`. I did not repeat the 20 MB timing of Step 12 through these functions.

**Next:** the table for job status.

### Step 16. Task 1: the job status table
**Why my own table:** ARQ forgets a result after a while and calls a failed job "complete" too (Step 10). The status endpoint will read this table.

**1. The table `ingestion_jobs`**
| Column | Meaning |
|---|---|
| `id`, `tenant_id` | the queue message will carry **only the job id**; the worker takes the tenant from this row |
| `document_id` (optional, `SET NULL`) | empty while a URL is not fetched yet; if the document is deleted, the job **stays** as history (with its file name or URL) |
| `source_kind`, `source` | `file` or `url`, and the file name or URL |
| `status` | `queued`, `running`, `succeeded`, `failed` |
| `stage` | `fetch` (URL only), `parse`, `clean`, `chunk`, `embed`, `upsert`: where it is, or **where it failed** |
| `error` | one short line that is safe to show to the user (at most 500 characters) |
| `attempts`, `chunks_written`, `started_at`, `finished_at` | how many tries, the result, and when |

The database itself refuses an unknown status, stage or source kind, and a `failed` job without an error (or an error on any other job).

**2. The allowed changes** (`services/ingestion_jobs.py`)
```
queued -> running -> succeeded
   |         |
   +---------+----> failed          (running -> running again for a retry)
```
- Each change is **one `UPDATE` that names the statuses it may start from**, so the database decides. A finished job cannot change. A function returns `False` when the change is not allowed.
- **No function commits.** The worker can commit each stage (so the status can be seen), and can commit `succeeded` **in the same transaction as the chunks**.
- `get_job` filters on the tenant; another tenant's job is "not found" (404), like a missing one.
- **What this table does not do:** it does not stop two workers from running the same job at once, because `running` may start again (ARQ can run a job twice, and a retry starts it again). So **saving the chunks has to be safe to repeat** (the next step but one, and task 6).
- The pipeline version (parser, chunker settings, embedding model) is not in this table yet. I decide that with the chunk ids, when I build the ingestion job.

**3. The migration** `3951de4daf53` (autogenerated and read before use: only this table and two indexes). Upgrade, downgrade (the table was empty; `document_files` stayed) and upgrade again; `alembic check`: no new operations.

**4. Tests** (`tests/test_ingestion_jobs.py`, 15 new): each way through the statuses; a retry counts attempts and keeps the start time; the error is one line of at most 500 characters; a finished job cannot change; unknown job and stage; another tenant; the job stays when its document is deleted; the database refuses 5 kinds of wrong values.
```
pytest -q                              -> 376 passed
mypy src tests                         -> Success: no issues found in 92 source files
ruff check (new and changed files)     -> All checks passed
docker compose up -d --build api worker -> api: healthy | worker: healthy
```
**5. Live on PostgreSQL**, in `Day 11 Corpus` (a temporary script; its job row was removed). SQLite cannot show what two writers do, so [4] is checked here:
```
[1] created                status queued    stage None   attempts 0 chunks None error None
[2] started, parsing       status running   stage parse  attempts 1 chunks None error None
[3] a retry                status running   stage embed  attempts 2 chunks None error None
     started_at kept on the retry: True | updated_at moved: True
[4] two writers finish the same job at the same time
     writer A: succeed -> True (not committed) | after 1 s, writer B is still waiting for the row: True
     A commits | writer B: fail -> False
     the job               status succeeded stage None   attempts 2 chunks 7 error None
[5] start the finished job again -> False
[6] another tenant reads it -> IngestionJobNotFoundError
[7] PostgreSQL refuses wrong values written with plain SQL:
     status = 'done'                    refused by ck_ingestion_jobs_status
     stage = 'dancing'                  refused by ck_ingestion_jobs_stage
     status = 'failed', error = null    refused by ck_ingestion_jobs_error_when_failed
     error = 'x'                        refused by ck_ingestion_jobs_error_when_failed
after clean-up: ingestion_jobs rows = 0
old corpus -> THE OLD CORPUS IS UNCHANGED
```
**What it shows:** the second writer **waited** while the first had not committed, and then got `False`: the job stayed `succeeded` with 7 chunks and was not turned into `failed`. PostgreSQL also refused all four wrong values written with plain SQL.

**Not done:** no endpoint shows a job yet (that is a later step), and nothing creates jobs outside the tests. I did not test on PostgreSQL that deleting a document leaves its job (tested on SQLite).

**Next:** the tokenizer file inside Docker.

### Step 17. Task 1: how Docker gets the tokenizer file (options; nothing changed or downloaded)
**The need:** the worker will count tokens, and the image does not have the file. It lives in my laptop's Hugging Face cache.
```
inside the worker today:
FileNotFoundError: BGE-M3 tokenizer.json not found under /home/appuser/.cache/huggingface/hub. Set TOKENIZER_PATH to the file.
```
**Checked** (metadata only): my local file is exactly the published one at a fixed version.
```
local file    : 17,098,108 bytes
sha256        : 21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08
revision      : 5617a9f61b028005a4858fdac845db406aefb181   (BAAI/bge-m3, licence tag: mit)
published at that revision: same size, same sha256
the API and worker images today: 663 MB      docker engine 29.9.2, buildx 0.38.1
```
**Options**
| | A. Download while building the image, pinned and checked | B. Copy my local file into the build | C. Mount my cache folder into the worker | D. Put the file in git |
|---|---|---|---|---|
| how | `ADD --checksum=sha256:...` from the revision's URL | an extra build context pointing at the cache | a read-only volume and `TOKENIZER_PATH` | commit it |
| download | once per build cache: 17.1 MB | none | none | none |
| works on another machine | yes, with internet at build time | only where the file is already cached | only where it is cached | yes |
| wrong or changed file | **the build fails** | only if I add a check | not checked | fixed by git |
| cost | the build depends on huggingface.co | my laptop's path in the setup | the container depends on my laptop at run time | 17 MB in the repository for ever |

**My proposal: A.**
- A fresh clone can build the same image, and the **checksum** means a different file cannot slip in. The **revision** in the URL means the file does not change when the model repository changes.
- It is added to the image, so the worker needs no network or laptop folder when it runs. `TOKENIZER_PATH` points to it.
- The image grows by about 17 MB.
- The test for it: build, count a known text inside the worker, and get the same count as on my laptop.

**Not verified:** I have not run a build with `ADD --checksum` here, or checked that a build on this machine can reach huggingface.co.

**Approved and done in Step 18.**

### Step 18. Task 1: the tokenizer file is built into the Docker image
**1. The change**
`Dockerfile`, above the application code:
```
RUN mkdir -p /opt/bge-m3
ADD --checksum=sha256:21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08 \
    --chmod=644 \
    https://huggingface.co/BAAI/bge-m3/resolve/5617a9f61b028005a4858fdac845db406aefb181/tokenizer.json \
    /opt/bge-m3/tokenizer.json
ENV TOKENIZER_PATH=/opt/bge-m3/tokenizer.json
```
(the folder is made first with `mkdir`, so that `--chmod=644`, which is meant for the file, cannot leave the folder without the right to open it.)

`docker-compose.yml` also sets `TOKENIZER_PATH` for `api` and `worker`. **Why:** `.env.example` has an empty `TOKENIZER_PATH=`. If someone copied it into `.env`, that empty value would hide the image's path, because Compose passes `.env` to the containers. A value set under `environment:` wins over `.env`. (My own `.env` has no such line. I did not run the stack with an empty value to see it fail.)

**2. Build and start:** the build succeeded (the download step took about 3 s); `api` and `worker` are healthy. Each image went from **663 MB to 684 MB**.
```
-rw-r--r-- 1 root root 17098108  /opt/bge-m3/tokenizer.json     (the app runs as appuser and can read it)
drwxr-xr-x root root             /opt/bge-m3
```
**3. The same results on my laptop and in the worker.** One script, run on Windows and inside the Linux container, over all 234 documents of the old corpus (read only):
```
my laptop (Windows, the Hugging Face cache file)
tokenizer file sha256 : 21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08
234 documents, tokens : 1,209,588
'How do I define a path parameter in FastAPI?' -> 11 tokens
'FastAPI में CORS कैसे enable करें?' -> 10 tokens
fixed size : 3,632 chunks, fingerprint 8cdd14e530feb3f19e7b
recursive  : 3,704 chunks, fingerprint 290f3b549d60b1df28d1
markdown   : 4,578 chunks, fingerprint bfe1bf1aed92bd441767

inside the worker (Linux, /opt/bge-m3/tokenizer.json)
the same lines; a diff of the two outputs, without the first line, was empty: IDENTICAL
```
So the worker counts tokens as I did in the earlier steps, and all three chunkers make **the same chunks**: same positions, token counts and heading paths.

**4. A code change does not download the file again.** I added a temporary line to a source file, rebuilt, and restored the file exactly:
```
CACHED   [runner 5/6] ADD --checksum=sha256:21106b6d...      (a first check of the step took 0.0 s)
ran      [runner 6/6] COPY --from=builder ... /app /app      (the new code)
```
A new machine, or a cleared build cache, downloads it again.

**5. Nothing else broke:** `pytest -q` 376 passed; `curl /health` ok; search returned 5 and 50 results; the old corpus is unchanged.

**Not tested:** that a build really fails when the bytes do not match the checksum (I rely on Docker's `--checksum`; testing it would have meant another download), and a build without internet.

**Next:** the ingestion job itself.

### Step 19. Task 1: the first two stages of the job, parse and clean
**1. I looked at the text of the 10 research PDFs first** (274 pages), so that the rules answer real problems. I found running headers and footers (`Preprint`, `IBM Granite Embeddings`, a journal line, two alternating running titles), lines that are only a number, 1,374 words broken by a hyphen at a line end, 62 ligatures, and unreadable symbol glyphs from fonts. No page was without text. One repeated edge pattern was a **decimal** (`#.#`, on 11 of 21 pages of one paper), which may be table data.

**2. What I built**
| File | What |
|---|---|
| `services/parsing.py` | `parse_file(filename, data)`: PDF page by page with `pypdf`; `.md` and `.txt` as UTF-8 (a byte-order mark is removed). Its errors are **permanent** (unreadable, encrypted, not UTF-8, unsupported type) |
| `services/cleaning.py` | `clean_document(parsed)`: the cleaned text, **where each PDF page is** in it (for citations later), what each rule removed, and the pages that gave no text. A file with no text left is refused |

**The cleaning rules** (conservative: a leftover footer is better than lost content)
| Files | Rule |
|---|---|
| all | Unicode NFC; ligatures expanded; control characters, zero-width space, byte-order mark, soft hyphen and "private use" glyphs removed; spaces at line ends and extra blank lines removed |
| all | the zero-width **joiner and non-joiner are kept** (Hindi and other scripts need them); tabs and indentation are kept |
| PDF | a line **with letters** among the first or last 2 lines of a page, repeated (numbers ignored) on at least 40% of the pages and at least 3, is a header or footer |
| PDF | a **bare number** at a page edge is a page number only if it **counts up with the pages** (so table values stay) |
| PDF | a word broken by a hyphen is rejoined (see 4) |

**3. On the real files** (`uv run python scripts/parse_report.py --lines`)
```
parser pypdf-6.21.4 | cleaner version 1

PDF             pages  characters before -> after  headers  numbers  joined  kept-  unreadable  ligatures  seconds
2204.13679          5      30,219 ->    30,118        0        0      47      2          11          0      0.6
2212.10528         11      44,379 ->    44,169        0        0     139     11           0         62      0.6
2309.12871         14      49,257 ->    48,954       14       14      76      6          12          0      0.8
2407.20243         19      59,725 ->    59,342        0       19     175      5           0          0      2.6
2411.17299         21      39,920 ->    39,679        0       21     108      4           5          0      1.0
2504.08748         80     359,277 ->   351,531      159        0     143     36           0          0      4.1
2506.02527          8      26,162 ->    26,158        0        7       1      2           0          0      0.3
2601.05264         86     155,997 ->   154,945        0       86     463     47           0          0      1.7
2605.13521         20      65,636 ->    64,974       20       20      92      5           0          0      0.7
2607.24767         10      15,172 ->    15,145        0        9      11      4           0          0      0.2

total: {'unreadable characters removed': 28, 'words joined': 1255, 'hyphens kept, line break removed': 122, 'pages': 274, 'empty pages': 0, 'ligatures expanded': 62, 'header or footer lines removed': 193, 'page numbers removed': 176}

every different header or footer line that was dropped (# = a number):
  2309.12871   Preprint
  2504.08748   # Trovato et al.
  2504.08748   , Vol. #, No. #, Article . Publication date: April #.
  2504.08748   A Survey on Multimodal Retrieval-Augmented Generation #
  2605.13521   IBM Granite Embeddings

Markdown files: 249 | changed by cleaning: 62 | words lost: 0
```
- The **5 different header and footer lines** dropped are all real headers or footers.
- **Markdown:** 62 of the 249 files on disk were changed, and the report found **no words lost**. (249 is every `.md` file there; Day 9 used 190 of them.)

**4. Hyphens: my first rule made mistakes**
My first rule joined the two halves unless the document wrote the word with a hyphen elsewhere. A random sample of the joins with no evidence showed wrong ones: `in-the` → `inthe`, `to-text` → `totext`, `Reference-free` → `Referencefree`. **Fix:** keep the hyphen when the break is inside a longer hyphenated word (`state-of-⏎the-art`), or for a few prefixes and endings (`non-`, `self-`, `-based`, `-free`, `-aware`, `-level`, `-specific`, `-trained`) when the document gives no answer. Afterwards `inthe`, `totext`, `stateof`, `Referencefree`, `Noncompliant` and `promptbased` no longer appear in the cleaned text. The decisions now:
```
  267  joined: no evidence either way
  988  joined: the document writes the joined word elsewhere
   20  kept: inside a longer hyphenated word
   88  kept: the document writes it with a hyphen elsewhere
   14  kept: the short list
```
**267 of the 1,377 broken words are still joined without any evidence.** In my sample most such joins were ordinary words (`distances`, `optimized`), but some compounds will be glued wrongly. I did not check all 267.

**5. Inside the worker, from PostgreSQL** (a temporary document in `Day 11 Corpus` with the 80-page PDF, removed afterwards): read with `get_file`, parsed and cleaned in a thread.
```
2504.08748.pdf: 80 pages | characters 359,277 -> 351,531 | page spans 80 | empty pages 0
removed: {'header or footer lines removed': 159, 'hyphens kept, line break removed': 36, 'words joined': 143}
dropped lines: ['# Trovato et al.', ', Vol. #, No. #, Article . Publication date: April #.', 'A Survey on Multimodal Retrieval-Augmented Generation #']
sha256 of the cleaned text: a9bc31cc7919134946fb4cfddeb87a904166117064dc328504832cbb8e474e97
```
The same output on my laptop, **identical** including the sha256 of the cleaned text. Parsing took 3.9 s in the worker (3.8 s on the laptop), cleaning under 0.2 s.

**6. Tests** (`tests/test_parse_and_clean.py`, 31): every rule, and what must **not** be removed (table numbers at a page edge, a repeated line in the middle of a page, Hindi joiners, Markdown untouched by the PDF rules). One of my first tests contradicted itself and was rewritten.
```
pytest -q                          -> 407 passed
mypy src tests                     -> Success: no issues found in 95 source files
ruff check (new and changed files) -> All checks passed
old corpus                         -> unchanged
```
**Parsing failures seen so far** (for the catalogue, task 4 and the notes)
- Some PDF text has **lost its spaces**, for example `Kubernetesandothercontainerorchestrationplatforms...`. It comes from reading the PDF, and cleaning does not repair it.
- The unreadable glyphs I remove are parts of formulas, so formulas lose symbols.
- Tables and two-column pages are not looked at yet.

**Limits:** the rules were chosen on these 10 papers; another kind of PDF can need other rules. The header rule needs at least 3 pages. The Day 9 Markdown cleaning (directives, admonitions) is not part of this. The **upload route still uses its old text extraction**, and nothing runs these stages as a job yet.

**Next:** the chunk stage.

### Step 20. Task 1: the third stage, chunk
**1. What it does** (`services/chunk_stage.py`, `chunk_document`): from the cleaned text to chunks that carry their **heading path**, their **first and last PDF page**, and the **contextual header** when the option is on.

| Decision | How | Why |
|---|---|---|
| which chunker | new setting `CHUNK_STRATEGY`: `auto` (default), `fixed`, `recursive`, `markdown`. `auto` = Markdown chunker for `.md`, recursive for PDF and text | PDF text has no Markdown headings, and a line starting with `#` in it is not one (Step 7) |
| headings of a PDF or text file | none, with every chunker | the same reason; their header is the title only |
| title | the title the user gives; else in Markdown the **first level-1 heading outside code blocks**; else a readable file name (`strict_mode.md` → `Strict Mode`) | the Day 9 loader took code comments as titles (Step 8) |
| pages | every page whose text the chunk overlaps | for citations (Day 12) |

**2. Tests** (`tests/test_chunk_stage.py`): which chunker; the size options; a `#` comment in code is never the title; where the title comes from; readable file names; the page rule at its edges (also a page without text); headers off by default; headers with all four strategies; a `#` line in a PDF is not a section.
```
pytest -q                          -> 443 passed
mypy src tests                     -> Success: no issues found in 97 source files
ruff check (new and changed files) -> All checks passed
```
(Making file-name titles readable changed what two tests expected; I updated them.)

**3. The three stages on the original corpus files** (`uv run python scripts/prepare_report.py`: the 190 Markdown files and 10 PDFs from disk; headers switched on to look at them; the titles of the PDFs come from the corpus list)
```
options: ChunkOptions(strategy='auto', max_tokens=400, overlap_tokens=60, context_headers=True)
chunkers used: {'pdf -> recursive': 10, 'markdown -> markdown': 190}
  documents                             200
  chunks                              2,908
  over the limit                          0
  not a slice of the cleaned text         0
  without a header                        0
  largest embedded text                 448
  PDF chunks                            684
  PDF chunks without pages                0
  wrong pages                             0
  PDF chunks on two or more pages       234
  PDF pages with text                   274
  PDF pages in no chunk                   0
seconds: {'parse': 11.0, 'clean': 0.9, 'chunk': 4.8}
```
- Over the limit: 0. Every chunk is an exact slice of the cleaned text.
- **All 684 PDF chunks have page numbers**, each starting inside its first page and ending inside its last, and no page with text is left out. 234 chunks cover more than one page.
- 2,908 chunks for 200 documents. This is **not** comparable with the earlier chunk counts: those used the cleaned Day 9 text and included the textbook.

**The 14 wrong titles are gone** (of 41 Markdown titles that differ from the old ones, these 14 were code comments):
```
  pydantic-docs/concepts/dataclasses.md          'Option 1 -- using the decorator argument:'   -> 'Dataclasses'
  pydantic-docs/concepts/models.md               'normal copy gives the same object referenc'  -> 'Models'
  pydantic-docs/concepts/serialization.md        'returns a dictionary:'                       -> 'Serialization'
  pydantic-docs/concepts/type_adapter.md         '`item_data` could come from an API call, e'  -> 'Type Adapter'
  pydantic-docs/concepts/types.md                'We now create an `Annotated` wrapper that '  -> 'Types'
  pydantic-docs/concepts/unions.md               'Errors are quite verbose with a normal uni'  -> 'Unions'
  pydantic-docs/concepts/validation_decorator.md 'ignore-above'                                -> 'Validation Decorator'
  pydantic-docs/contributing.md                  'Clone your fork and cd into the repo direc'  -> 'Contributing'
  pydantic-docs/errors/usage_errors.md           'this works, though'                          -> 'Usage Errors'
  pydantic-docs/errors/validation_errors.md      'from Python'                                 -> 'Validation Errors'
  pydantic-docs/examples/cyclic_references.md    'Create data with cyclic references represe'  -> 'Cyclic References'
  pydantic-docs/integrations/visual_studio_code.md 'type: ignore'                                -> 'Visual Studio Code'
  pydantic-docs/internals/resolving_annotations.md 'module1.py:'                                 -> 'Resolving Annotations'
  pydantic-docs/migration.md                     'V1'                                          -> 'Migration'
```
The other differences are titles that Day 9 had made from the folder and file name (`Concepts/Strict Mode` → `Strict Mode`). A level-2 heading is not used as a title, so `serialization.md` is "Serialization" and not "Serializing data". **The old stored titles are unchanged**; these are what the new pipeline will save.

**4. Inside the worker, from PostgreSQL** (a temporary document with the 80-page PDF, removed afterwards; the app's own settings, so headers are off; no title was given, so it is the file name)
```
2504.08748.pdf | settings ChunkOptions(strategy='auto', max_tokens=400, overlap_tokens=60, context_headers=False)
title '2504.08748' | chunker recursive | 279 chunks | tokens min 161 max 400 total 105,705
first three: [(0, 380, 'pages 1-1'), (1, 357, 'pages 1-1'), (2, 372, 'pages 1-2')]
fingerprint of all chunks: f91ab460ab4890098087c1bd1c7baba9602604da4d13599e507fe1870059680d
```
The same output on my laptop, **identical**, including a fingerprint over every chunk's position, size, pages and embedded text. The three stages took 8.2 s in the worker and 5.6 s on my laptop (one run each).

**5. A problem for the corpus reload: FastAPI's code examples are not in its Markdown files**
```
FastAPI original files on disk: 158
lines that only INCLUDE a code file ({* ../../docs_src/... *}):  455 in 92 files
the included docs_src folder on disk: no
old FastAPI chunks that contain such a line: 0
```
- Day 9 **removed** those lines, so my current searchable data **never had that code** either.
- The new pipeline keeps the line as it is, so a chunk would contain a file path instead of code.
- This goes into the parsing-failure catalogue. Whether to remove the lines or to download the code is a decision for the reload (task 6).

**Not done:** nothing is embedded or saved yet, and no job runs these stages.

**Next:** the embed stage.

### Step 21. Task 1: the fourth stage, embed (with a fake Jina; no real request)
**1. What it does** (`services/embedding.py`, `embed_passages`): the texts of one document become vectors.
| Rule | How |
|---|---|
| the request | the same as my Day 9 passages: `jina-embeddings-v3`, task `retrieval.passage`, 1,024 numbers, `truncate: false` (a too long text is an error, not a silently cut vector) |
| the same text twice | sent **once** per call, and its vector is given to every place it came from |
| batches | at most 32 texts and about 12,000 tokens (the Day 9 values) |
| pace | at most 90,000 tokens and 90 requests a minute (`Pacer`, one per process; **a retry counts too**) |
| temporary problems | 429, 5xx, network errors and timeouts: 4 attempts, pauses of about 5, 10 and 20 s with jitter, or the server's `Retry-After` (at most 60 s). Then `TransientEmbeddingError`: the job is to be retried later |
| everything else | `PermanentEmbeddingError`, **not retried** (400, 401, 403, 422, no key, an empty text) |
| the answer | accepted only with one vector for every text, matching indexes, 1,024 finite numbers, not all zero, and the billed token count. A wrong shape is never saved: a vector next to the wrong text would stay wrong unnoticed |
| all or nothing | if any batch fails, nothing is returned |
| secrets | the API key and the texts are not logged; error messages are short and safe to show |

New settings: `JINA_BATCH_TEXTS`, `JINA_BATCH_TOKENS`, `JINA_TOKENS_PER_MINUTE`, `JINA_REQUESTS_PER_MINUTE`.

**2. Tests** (`tests/test_embedding.py`, 38) with a fake Jina (`httpx.MockTransport`) and a fake clock, so nothing goes to the network and nothing waits: the exact request; vectors in input order although the fake answers backwards; one send for repeated texts; batch limits; each kind of retry and the growing pauses; `Retry-After`; no retry for refusals; 7 kinds of wrong answers; a failing second batch; nothing sent without a key; **the key and the text do not appear in logs or errors**; the pacer, also with 5 requests at once.
```
pytest -q                          -> 481 passed
mypy src tests                     -> Success: no issues found in 99 source files
ruff check (new and changed files) -> All checks passed
```
**3. The whole corpus through parse, clean, chunk and embed, with the fake Jina** (a temporary script; the 200 original files from disk and the textbook from its saved text; real tokenizer; simulated clock; the old corpus is only read, and is unchanged)
```
whole corpus, headers off: 234 documents | 4,720 chunks, 4,720 different texts in their documents | 313 requests (largest batch 32) | about 1,436,459 tokens | simulated waiting for the limits 16.0 min
      of which the textbook: 1,812 chunks, about 691,093 tokens
whole corpus, headers on : 234 documents | 4,720 chunks, 4,720 different texts in their documents | 314 requests (largest batch 32) | about 1,497,052 tokens | simulated waiting for the limits 17.0 min
      of which the textbook: 1,812 chunks, about 707,441 tokens
small first real test, cors.md: 10 chunks -> 1 request(s), about 1,769 tokens
```
**What it means**
- A real reload would be about **1.44 million tokens** with headers off (**1.50 million** with headers on), in about **313 requests**. This is an **estimate** (the tokenizer's count plus 11 per text, the rule that matched Jina's bill on Day 9); the real number comes from Jina's answer.
- My limit of 90,000 tokens a minute makes it take **about 16 to 17 minutes** at least (simulated waiting only, without the requests themselves). The Day 9 run billed 1,270,018 tokens.
- The textbook is almost **half** of it (691,093 tokens).
- No document had the same chunk text twice, so the "send once" rule saved nothing here. It also works only **inside one document and one run**: texts shared between documents are sent again.
- **A point for the job:** with the pace limit, a job can spend minutes waiting. ARQ stops a job after **300 s by default** (Step 10), so the job needs a longer timeout.

**Not verified:** how the real Jina answers errors and `Retry-After`, and the limits of my key today (the values are from Day 9). **No real request was made in this step.**

**Next:** one small real request, after I approve it. After that, the last stage: upsert.

### Step 22. The embed stage with the real Jina: one small request
**What was sent** (after I approved it): the 10 chunks of FastAPI's public document `tutorial/cors.md`, through parse, clean, chunk and `embed_passages`, with my key from `.env`. **Nothing was saved to the database.** The second half of the script compares the vectors with ones I already had (the saved Day 10 query vectors and the old chunks), without another request.
```
title 'CORS (Cross-Origin Resource Sharing)' | chunker markdown | 10 chunks | headers off
HTTP requests sent: 1 to {'https://api.jina.ai/v1/embeddings'} | took 1.03 s
result: 10 vectors of 1024 numbers | model jina-embeddings-v3 | successful requests 1 | retries 0 | different texts 10
vector lengths (norm): 1.000000 to 1.000000 | all different: True
tokens: estimated 1,769 | billed by Jina 1,769 | difference +0 (+0.00%)

the 20 saved Day 10 queries, by their best score against these 10 chunks:
   0.6876  Q19  FastAPI में CORS कैसे enable करें?
   0.6153  Q6   How do I add middleware to a FastAPI application?
   0.5204  Q1   How do I define a path parameter in FastAPI?
   0.0081  Q12  How does the size of a text chunk affect retrieval-augmented generation?
   -0.0019  Q11  What is retrieval-augmented generation and why does it reduce hallucinations?
best chunk for Q19 'FastAPI में CORS कैसे enable करें?': CORS (Cross-Origin Resource Sharing) > Use `CORSMiddleware`

each new chunk against the 7 OLD cors.md chunks (nearest one):
   0.9935  new: CORS (Cross-Origin Resource Sharin -> old: CORS (Cross-Origin Resource Sharin
   0.9539  new: Origin                             -> old: Origin
   0.9461  new: Steps                              -> old: Steps
   0.9068  new: Wildcards                          -> old: Wildcards
   0.9035  new: Use `CORSMiddleware`               -> old: Use `CORSMiddleware`
   0.7172  new: Use `CORSMiddleware`               -> old: Wildcards
   0.7081  new: Use `CORSMiddleware`               -> old: Use `CORSMiddleware`
   0.9299  new: CORS preflight requests            -> old: CORS preflight requests
   0.7079  new: Simple requests                    -> old: CORS preflight requests
   0.9524  new: More info                          -> old: Simple requests
old corpus -> unchanged ; chunks per organisation: Corpus Tenant=2097, Day 11 Corpus=0
```
**What it shows**
- **The real service works with my code:** 1 request, no retry, 10 vectors of 1,024 numbers, each of length 1.
- **Tokens: estimated 1,769, billed 1,769.** The rule of Step 2 (tokenizer count plus 11 per text) was exact for this request. Step 2 showed it per batch on the Day 9 corpus; this is one more case, with new chunks.
- **The vectors mean what they should:** of the 20 saved queries, the best match for these chunks is Q19, the Hindi CORS question (0.6876), and the two paper questions score about 0. For Q19 the best chunk is in the `CORSMiddleware` section.
- **They live in the same space as the old vectors:** the new introduction chunk against the old one scores 0.9935. The other pairs are lower, which fits chunks that are cut differently (7 old chunks, 10 new). I did not examine each pair.
- This is **not** a search-quality test: the comparison is against 10 chunks only.

**Still not verified with the real service:** error answers, rate limits and `Retry-After` (nothing failed), Hindi or very large batches, and the limits of my key today.

### Step 23. Task 1: the last stage, upsert (chunk ids and the pipeline version)
**1. The decisions**
| Question | Decision |
|---|---|
| **Pipeline version** | a short hash of everything apart from the file that decides the result: parser, kind of file, cleaner and chunker versions, chunker, chunk size, overlap, headers, embedding model, vector size. It needs only the file name, so it is known **before parsing** |
| **Chunk id** | made from the document id, the pipeline version, the position and the **embedded** text (`uuid5`). The same input gives the same ids |
| `chunks.text_hash` | the hash of the **embedded** text (with its header): the same hash and model mean the same vector |
| where the record is kept | `documents.metadata["ingestion"]`: file sha256, pipeline and version, the title that was given, the number of chunks. Each chunk's `metadata` has the title, headings, header, token count, first and last page, chunker and pipeline version |
| the table | **no migration**: today's columns and `UNIQUE (document_id, chunk_index)` are enough |

**2. How a save works** (`services/chunk_save.py`)
1. `is_up_to_date`: same file, pipeline version and given title, and all recorded chunks still there → **stop before parsing**: no vector request, no write.
2. `reusable_vectors`: a vector the document already has for exactly the same embedded text and model is not asked for again.
3. `save_chunks`, in **the caller's one transaction** (it does not commit): lock the document row; delete the rows whose id is not in the new set (**first**, because of the unique position); insert the new ids; rows with the same id keep their text and vector, and their metadata is written only if it differs; update the document's title, text, hash and record.

**3. Tests** (`tests/test_chunk_save.py`, 19), comparing **every column of every row, timestamps included**: the id and the version change with each thing they depend on; saving again changes nothing; only a changed chunk is replaced; fewer chunks leave no extra rows; a new pipeline leaves one version; a failure after the deletes keeps the old version; no commit keeps nothing; another tenant writes nothing; "up to date" is false for each kind of change; which vectors may be reused (not the Day 10 rows, whose hash was of other text).
- One test was wrong at first: on SQLite a missing vector is stored as the text `null`, so the database did not refuse it. It now causes a real constraint error.
```
pytest -q                          -> 500 passed
mypy src tests                     -> Success: no issues found in 101 source files
ruff check (new and changed files) -> All checks passed
```
**4. Live on PostgreSQL**, in `Day 11 Corpus`: the real `cors.md` through parse, clean and chunk, with a **fake Jina (no real request)** whose vectors depend on the text. A temporary document, removed afterwards.
```
[1] first run
     chunks 10 | inserted 10 deleted 0 kept 0 | vectors reused 0 | Jina requests 1 with 10 texts
[2] the same again
     up to date: skipped before parsing | Jina requests 0
     every chunk row and the document row identical to before: True
[3] the save stage itself run again (no skip): inserted 0 deleted 0 kept 10 | all rows identical, timestamps too: True
[4] one paragraph of the file edited
     chunks 10 | inserted 1 deleted 1 kept 9 | vectors reused 9 | Jina requests 1 with 1 texts
     old rows still there and byte for byte the same: 9 of 10
[5] a failure while saving (20 new chunks, the last vector has 3 numbers): DBAPIError: expected 1024 dimensions, not 3
     old version unchanged (rows and document): True
[6] chunk size changed to 120 / 20
     chunks 20 | inserted 20 deleted 10 kept 0 | vectors reused 4 | Jina requests 1 with 16 texts
     old rows left: 0 | duplicate positions: 0
[7] back to 400 / 60
     chunks 10 | inserted 10 deleted 20 kept 0 | vectors reused 4 | Jina requests 1 with 6 texts
[8] two saves of one document at once: A inserted 20, not committed | after 1 s B is still waiting: True
     B afterwards: inserted 0 deleted 0 kept 20 | rows 20 for 20 chunks | duplicate positions 0
[9] search with the saved vector of chunk 2 finds: chunk 2, score 1.0, heading 'Origin'
clean-up: DELETE -> 204 | Day 11 Corpus chunks 0, documents 0 | document_files 0
old corpus -> THE OLD CORPUS IS UNCHANGED
```
**What it shows**
- **Running again changes nothing** [2]: skipped before parsing, 0 requests, every row identical. Even running the save itself again [3] wrote nothing, with identical timestamps. This is what task 6 needs.
- **An edit replaces only what changed** [4]: 1 chunk out, 1 in, 9 rows byte for byte the same, and Jina would have been asked for **1 text**.
- **All or nothing** [5]: PostgreSQL refused a bad vector after the old rows had been deleted inside the transaction, and the old version stayed complete.
- **A new chunk size leaves no old rows** [6]; 4 vectors were reused because those 4 texts did not change.
- **Two saves at once** [8]: the second waited for the first, then changed nothing. No duplicate positions.

**Limits**
- **PostgreSQL only.** With `VECTOR_STORE=qdrant`, a new or changed document does not reach Qdrant; `copy_to_qdrant.py` would have to be run. This is open.
- Vectors are reused only **within one document**, from its rows as they are now.
- Two jobs for one document at the same time could both pay for vectors before one waits to save.
- The live test used fake vectors in the shared HNSW index; their rows are deleted, and PostgreSQL clears the index entries later (`VACUUM`).
- **No job calls these stages yet.** The order shown in the test (skip check, parse, clean, chunk, reuse, embed, save) is what the worker job will do.

**Next:** the worker job that runs the stages and writes the job status.

### Step 24. Task 1: the worker job that connects the stages
**1. Two layers**
- `services/ingestion.py`, `run_ingestion(deps, job_id)`: a **plain async function** that knows nothing about the queue.
- `worker.py`, `ingest_document`: the thin ARQ layer (time limit, retries, giving up).

**2. The flow**
```
read the job row (tenant and document come from it, NOT from the queue message) -> start_job
-> the stored file -> up to date? yes: "succeeded" with 0 chunks written, nothing else
-> parse -> clean -> chunk -> embed (reusing vectors) -> save chunks AND "succeeded" in ONE transaction
```
Each stage is committed to the job row before it starts, so the status can be watched.

| Situation | What happens |
|---|---|
| a broken, unsupported or empty file; a refused embedding request; no stored file | **failed at once**, with a short readable reason and the stage; no retry |
| Jina not available after its own 4 attempts; the database connection lost | the job stays `running`; ARQ tries again after 30, 60, 90 s; on the **4th** attempt it is marked failed ("Gave up after 4 attempts") |
| the file was replaced while the job worked on the old one | nothing is saved; retried |
| longer than the time limit | marked failed ("took too long") |
| the job is already finished (delivered twice) | nothing is touched |
| an unexpected error (a bug) | failed with a general message; the log has the error **type and place in the code, not its message** |

**Why my own time limit:** if ARQ's limit stopped a job, nothing would write "failed" and it would show `running` for ever. So the job limits itself (`INGEST_TIMEOUT_SECONDS=1800`, far above ARQ's default of 300 s, because a job can wait minutes for Jina's limits) and ARQ gets 60 s more. Other new settings: `INGEST_MAX_TRIES=4`, `WORKER_MAX_JOBS=2` (a job can hold a 20 MB file in memory).

**Only ids on the queue:** the argument is the job row's id and the result is one word, because ARQ logs both.

**No secrets from the database:** the worker has its **own engine with `echo` off and `hide_parameters`**. The app's engine uses `echo=settings.DEBUG`, which would print every SQL value, whole files included.

**3. Tests** (`tests/test_ingestion.py`): each row of the table above, the stage seen while Jina works, "succeeded" and the chunks saved together, the retry pauses, only the id on the queue, ARQ's limit above mine.
- One failure was in the test itself: the log shows lines of **source code** where an error happened, and the test had typed its "private" text into the raising line. The text is now put together at run time, as real data is. (So the log can show my source lines, not the values.)
```
pytest -q                          -> 524 passed
mypy src tests                     -> Success: no issues found in 103 source files
ruff check (new and changed files) -> All checks passed
```
**4. Live: real Redis, real worker container, real PostgreSQL, a FAKE Jina**
To be sure that nothing could reach the real Jina, I stopped the normal worker and ran a **temporary** one with `JINA_EMBEDDING_URL` pointing to a fake Jina container and a fake key, and checked both before queueing anything. Jobs ran in `Day 11 Corpus` and were removed afterwards.

**My first run found a leak:** for the broken PDF, the `pypdf` library logged the **first bytes of the file** (`invalid pdf header: b'this '`). With a real file those would be a tenant's content. The log was also full of low-level network lines (`DEBUG` is on in my setup) and showed each ARQ line twice. **Fix:** `quiet_libraries()` in the worker. The second run:
```
[1] a Markdown file
     seen: queued > running:parse > running:embed > succeeded
     1.6 s | attempts 1 | chunks_written 10 | error None | chunks of the document 10
     fake Jina: successful requests +1 (texts +10), refused with 503 +0
[2] a new job for the same file
     seen: queued > succeeded
     0.4 s | attempts 1 | chunks_written 0 | error None | chunks of the document 10
     fake Jina: successful requests +0 (texts +0), refused with 503 +0
     chunks table (this organisation) unchanged: True
[3] the job of [1] put on the queue again -> accepted: False
[4] a temporary problem: the next 4 requests get 503 (all 4 tries of one attempt)
     seen: queued > running:chunk > running:embed > running:upsert > succeeded
     65.2 s | attempts 2 | chunks_written 1 | error None | chunks of the document 10
     fake Jina: successful requests +1 (texts +1), refused with 503 +4
[5] a broken PDF
     seen: queued > failed:parse
     0.4 s | attempts 1 | chunks_written None | error 'The PDF could not be read.' | chunks of the document 0
     fake Jina: successful requests +0 (texts +0), refused with 503 +0
[6] the job rows: [('succeeded', None, 1, 10), ('succeeded', None, 1, 0), ('succeeded', None, 2, 1), ('failed', 'parse', 1, None)]
clean-up: Day 11 Corpus documents, chunks | document_files, ingestion_jobs = (0, 0, 0, 0)
```
The worker's log in that run (ids shortened, health lines left out):
```
10:49:01:   0.40s → ingest:609dfa8c:ingest_document('609dfa8c…')
10:49:02 INFO ai_service.ingestion: embedded 10 texts, 0 reused, 10 tokens billed
10:49:02 INFO ai_service.ingestion: job 609dfa8c…: saved (10 new, 0 removed, 0 unchanged)
10:49:02:   1.17s ← ingest:609dfa8c:ingest_document ● 'succeeded'
10:49:02:   0.31s → ingest:e8b080c7:ingest_document('e8b080c7…')
10:49:03:   0.02s ← ingest:e8b080c7:ingest_document ● 'up_to_date'
10:49:03:   0.41s → ingest:081b8f69:ingest_document('081b8f69…')
10:49:04 WARNING ai_service.embedding: embedding attempt 1 of 4 failed (HTTP 503); next in 5 s
10:49:10 WARNING ai_service.embedding: embedding attempt 2 of 4 failed (HTTP 503); next in 9 s
10:49:19 WARNING ai_service.embedding: embedding attempt 3 of 4 failed (HTTP 503); next in 16 s
10:49:37 WARNING ai_service.worker: job 081b8f69…: attempt 1 failed, next in 30 s
10:49:37:  33.55s ↻ ingest:081b8f69:ingest_document retrying job in 30.00s
10:50:07:  64.06s → ingest:081b8f69:ingest_document('081b8f69…') try=2
10:50:08 INFO ai_service.ingestion: embedded 1 texts, 9 reused, 1 tokens billed
10:50:08 INFO ai_service.ingestion: job 081b8f69…: saved (1 new, 1 removed, 9 unchanged)
10:50:08:   1.08s ← ingest:081b8f69:ingest_document ● 'succeeded'
10:50:08:   0.34s → ingest:93a55d4d:ingest_document('93a55d4d…')
10:50:08 WARNING ai_service.ingestion: job 93a55d4d… failed: InvalidDocumentError
10:50:08:   0.02s ← ingest:93a55d4d:ingest_document ● 'failed'
```
In that log I searched for the fake key, four phrases of the CORS document and the broken file's bytes: **0 lines** for each.

**5. Afterwards:** the queue was empty before I started the normal worker again (so it could not send a test job to the real Jina); the temporary containers are removed; `api` and `worker` healthy; old corpus unchanged.

**What the live test shows:** the status moved `queued > running:<stage> > succeeded`; a second job for the same file did nothing; the same job could not be queued twice; after four 503 answers the job was retried 30 s later and succeeded on attempt 2, asking for **only the 1 changed chunk**; the broken PDF failed at `parse` without a retry.

**Limits**
- **Not tested: a worker killed in the middle of a job.** ARQ should run it again, but if it gave up without calling my function, the row would stay `running`. Nothing looks for such stuck jobs.
- While it waits for a retry, a job shows `running`.
- The time limit and the real Jina's error answers were tested only with fakes.
- The quiet logging is set in the **worker**. The old upload route in the API also uses `pypdf`, and I did not change its logging.
- With `VECTOR_STORE=qdrant` the job saves to PostgreSQL only (the worker logs a warning).
- **Nothing creates these jobs from outside yet:** that is `POST /ingest`, the next step. If queueing fails after the row is saved, that endpoint must mark the job failed.

### Step 25. Task 1: `POST /ingest` for a file
**1. The endpoint** `POST /api/v1/ingest` (form data, with a login): `file` (`.txt`, `.md`, `.pdf`, up to 20 MB), optional `title` and `source`. It answers **202** with `job_id`, `document_id`, `status: queued`, `source` and `new_document`. **Nothing is parsed in the request.**

**Which document does a file belong to?** (`services/ingest_request.py`)
| Sent | Result |
|---|---|
| with a `source` (an identifier such as a path) | the tenant's document with that source gets the new file; none: a new document with that source, **full path kept** |
| without a `source`, the same bytes as a document of the tenant | that document again |
| without a `source`, new bytes | a new document. Two files that only share a name (`index.md`) never replace each other |
| with a `source`, but another document already has these bytes | 409 `DUPLICATE_DOCUMENT` |
| with a `source` that two documents have (made by the old upload route) | 409 `AMBIGUOUS_SOURCE`: I do not guess |

- **One transaction** saves the document, the original file and the job row. **Only after the commit** is the job queued, so the worker can see it.
- **If it cannot be queued** (Redis down), the job is marked `failed` and the answer is 503 `QUEUE_UNAVAILABLE`, without internal details. Otherwise it would say `queued` for ever.
- The tenant comes from the login only. A PostgreSQL advisory lock keeps two requests with the same new `source` from creating two documents; the unique constraint covers the same new file arriving twice. **I did not test either race live.**
- The API connects to Redis when it first queues a job, so it also starts without Redis; it gives up quickly (about 2 s in the test).
- A new document has empty text until the worker has run; when a file is replaced, **the old chunks stay searchable** until the new ones are saved.

**2. A leak I fixed before adding the route.** With `DEBUG=true` (my `.env`), the app's database engine wrote SQL **values** to the log. A check with a throw-away database showed that saving bytes logs their beginning:
```
INFO sqlalchemy.engine.Engine [...] (b'PRIVATE-FILE-CONTENT PRIVATE-FILE-CONTENT ...
with hide_parameters=True:  [SQL parameters hidden due to hide_parameters=True]
```
This route saves whole files, and the old upload route saves document text the same way. The app's engine now has **`hide_parameters=True`** (statements are still logged).

**3. Tests** (`tests/test_ingest_api.py`, 17): the saved document, file and job; committed before queued; same bytes; same name; same source; both 409s; tenants never share; a `tenant_id` in the body is ignored; refused files save and queue nothing; the size limit; no login; the queue down; the real queue gives up when Redis is not there; the engine hides values.
```
pytest -q                          -> 541 passed
mypy src tests                     -> Success: no issues found in 107 source files
ruff check (new and changed files) -> All checks passed
```
**4. Live:** real HTTP, PostgreSQL, Redis and a worker container, with a **fake Jina** (the normal worker stopped, the temporary one checked to use the fake address and key). `Day 11 Corpus`, cleaned up afterwards.
```
[1] a Markdown file with a source and a title
     HTTP 202 in 70 ms: status 'queued' | new_document True | source 'fastapi-docs/tutorial/cors.md'
     job succeeded | attempts 1 | chunks_written 10 | error None | chunks of the document 10 | texts sent to the fake Jina +10
     stored file identical: True | document: title 'CORS guide', hash matches True, text saved by the worker True
     GET /documents -> 1 document
[2] the same file and source again
     HTTP 202 in 61 ms: status 'queued' | new_document False | source 'fastapi-docs/tutorial/cors.md'
     job succeeded | attempts 1 | chunks_written 0 | error None | chunks of the document 10 | texts sent to the fake Jina +0
     same document: True | chunk rows unchanged: True | documents: 1
[3] a new version under the same source
     HTTP 202 in 69 ms: status 'queued' | new_document False | source 'fastapi-docs/tutorial/cors.md'
     job succeeded | attempts 1 | chunks_written 1 | error None | chunks of the document 10 | texts sent to the fake Jina +1
     same document: True
[4] a broken PDF
     HTTP 202 in 59 ms: status 'queued' | new_document True | source 'broken.pdf'
     job failed | attempts 1 | chunks_written None | error 'The PDF could not be read.' | chunks of the document 0 | texts sent to the fake Jina +0
[5a] an unsupported type
     HTTP 415 in 48 ms: UNSUPPORTED_FILE_TYPE - Only .txt, .md and .pdf files are supported.
[5b] an empty file
     HTTP 422 in 49 ms: INVALID_DOCUMENT - The file is empty.
[5c] 20 MB + 1 byte
     HTTP 413 in 204 ms: FILE_TOO_LARGE - The file is larger than the 20 MB limit.
[5d] bytes that another document already has
     HTTP 409 in 15 ms: DUPLICATE_DOCUMENT - This document was already uploaded (id e87aab9b-0d0a-4afe-806b-3c97c92c38f0).
[5e] without a login -> HTTP 401
     documents and jobs before and after [5a-e]: (2, 4) -> (2, 4)
[6] Redis stopped
     POST /ingest
     HTTP 503 in 2068 ms: QUEUE_UNAVAILABLE - The file could not be queued for processing. Please send it again.
     the job row: status 'failed', error 'The job could not be queued. Please send the file again.' | GET /health -> 200
[7] Redis back: the same file sent again
     HTTP 202 in 60 ms: status 'queued' | new_document False | source 'down.md'
     job queued | attempts 0 | chunks_written None | error None | chunks of the document 0 | texts sent to the fake Jina +0
clean-up: Day 11 Corpus documents, chunks, jobs | document_files = (0, 0, 0, 0)
```
**Step [7] did not finish.** The job stayed `queued`: my temporary worker had **crashed** when Redis went away (exit code 1, `redis.exceptions.TimeoutError`) and, started with `docker compose run`, it had **no restart policy**. (The one waiting job's row had been removed by the clean-up.) My normal worker has `restart: unless-stopped`, so I gave the temporary one the same policy and repeated the outage:
```
temporary worker: healthy | the leftover job -> 'skipped'
before: running, restarts 0, healthy
Redis stopped for 20 s
   worker during the outage: running, restarts 1, starting
Redis started
POST /ingest -> 202, 0.0 s after Redis started
the job: succeeded (attempts 1, chunks 1), 6.3 s after Redis started
after: running, restarts 2, healthy
clean-up: documents, chunks, jobs, files = (0, 0, 0, 0)
```
- **ARQ's worker does not survive a Redis outage by itself; Docker's restart policy brings it back.** Here the new job succeeded 6.3 s after Redis returned. This needs `restart: unless-stopped` on the worker service, which it has.

**5. The logs.** After the run in 4, I searched the API and worker logs for a marker I had put into the test file (`ZEBRA-MARKER-7319`), three phrases of the CORS document, the text sent while Redis was down, and the fake key: **0 lines in both logs.** The API log had 162 `SQL parameters hidden` lines, including the inserts into `document_files`.

**6. Afterwards:** the queue was empty before the normal worker came back; temporary containers removed; old corpus unchanged.

**Limits**
- `source` is not unique in the database; the old upload route can still create two documents with the same source.
- No limit on how many files one tenant may send, and each one can cost embedding tokens.
- If the API stopped between the commit and queueing, the job would stay `queued`; nothing looks for such jobs.
- The user has **no way yet to ask for the job's status** (the next steps), and links (URLs) are not supported yet.
- This is a real route now: **a file sent to it is embedded by the real Jina** and uses tokens.

**Next:** `POST /ingest` with a link (URL), safely.

### Step 26. Task 1: downloading a link safely (not connected to `/ingest` yet)

**The risk.** My worker runs inside my own network. If it downloads any link a user gives, the user can make it call my database, Redis, API or a cloud machine's metadata address. This is called **server-side request forgery (SSRF)**. So I built the downloader and its blocks first.

**New:** `services/url_fetch.py` (`fetch_url`) and `tests/test_url_fetch.py`. No new package (`httpx` was already installed). Nothing calls `fetch_url` yet.

| Rule | What it stops |
|---|---|
| 1. Only `http`/`https`, ports 80/443, no user name or password in the link, no spaces or backslashes, at most 2,048 characters | `file:///etc/passwd`, `http://redis:6379/`, `http://good.com@bad-host/` |
| 2. The name is looked up and **every** address must be public | `http://postgres/`, `localhost`, `169.254.169.254`, a name with one public and one private address |
| 3. The connection goes to **the address that was checked**; the `Host` header and TLS keep the name | a name that gives a public address when checked and a private one when connecting (DNS rebinding) |
| 4. I follow redirects myself (at most 5) and check each new link again | a public link that redirects to a private address |
| 5. Proxy settings from the environment are ignored | a proxy doing its own lookup |
| 6. One 30 s limit, a size limit **while reading**, compressed answers refused | endless, huge or slow answers; a small answer that unpacks into a huge one |

It accepts only PDF, Markdown and text (a web page is refused, and a "PDF" must start with `%PDF-`). The file name is made safe. Error messages do not say what a name resolved to, and the log names only the host, because a link's path or query can contain a secret.

**1. The first test run: 20 failed, 100 passed.** The mistake was in my test's fake server: its replies were **already read**, while the downloader reads the body as a stream so it can stop at the limit. I changed the fake to send unread streams; `url_fetch.py` did not change.
```
ruff check (the two files) -> All checks passed!
mypy src tests             -> Success: no issues found in 109 source files
pytest test_url_fetch.py   -> 120 passed
pytest (everything)        -> 661 passed
```
`ruff check .` still shows 2 errors.

**2. Do the tests really notice a missing protection?** I turned off one protection at a time in a temporary **copy** of the project and ran the 120 tests:
```
no public-address check                        -> 26 failed, 94 passed
only the first address is checked              -> 1 failed, 119 passed
connect by name, not by the checked address    -> 3 failed, 117 passed
redirect target not checked as a link          -> 12 failed, 108 passed
environment proxy allowed                      -> 1 failed, 119 passed
no size limit while reading                    -> 1 failed, 119 passed
compressed answers accepted                    -> 1 failed, 119 passed
any port allowed                               -> 8 failed, 112 passed
```

**3. Inside the real worker container**, with the real name lookup. First a **control without protection**, to show that the worker can reach the API:
```
docker exec -i ai-service-worker python - < live_blocks.py      (a temporary script)

CONTROL without protection: api answered HTTP 404
```
| Link | The name gives | Result |
|---|---|---|
| `http://api/ssrf-probe-blocked` | 172.24.0.4 | This address is not allowed. |
| `http://api:8000/ssrf-probe-blocked` | 172.24.0.4 | Only the standard web ports are allowed. |
| `http://postgres/` | 172.24.0.3 | This address is not allowed. |
| `http://redis/` | 172.24.0.7 | This address is not allowed. |
| `http://redis:6379/` | 172.24.0.7 | Only the standard web ports are allowed. |
| `http://qdrant/` | 172.24.0.5 | This address is not allowed. |
| the worker's own host name | 172.24.0.2 | This address is not allowed. |
| `http://localhost/` | ::1, 127.0.0.1 | This address is not allowed. |
| `http://127.0.0.1/`, `127.1`, `2130706433`, `0x7f000001` | 127.0.0.1 | This address is not allowed. |
| `http://[::1]/`, `http://[::ffff:127.0.0.1]/`, `http://0.0.0.0/` | the same address | This address is not allowed. |
| `http://169.254.169.254/latest/meta-data/` | 169.254.169.254 | This address is not allowed. |
| `http://host.docker.internal/` | 192.168.65.254 | This address is not allowed. |
| `file:///etc/passwd` | (no host) | Only http and https links are allowed. |

All 18 links were refused. In the API log, searching for `ssrf-probe` found **only the control request** (`GET /ssrf-probe-control -> 404`). `2130706433` and `0x7f000001` are other ways to write `127.0.0.1`; checking the **looked-up address** catches them, which a list of forbidden names would not. The old corpus is unchanged. Nothing was downloaded from the internet and nothing was queued.

**Limits**
- **A real download from a public address is not tested.** All successful downloads used a fake server, so the real connection to a checked address, including the TLS certificate check against the name, still has to be proved.
- The live test covered direct links. Redirects and a changing DNS answer were tested with the fake server only.
- Only the first checked address is tried; if it does not answer, the others are not.
- A web page (HTML) is not accepted.
- These checks are inside the application. A network rule that stops the worker from reaching private addresses would be a second protection; I have not added one.

**Next:** connect links to `POST /ingest` and add the worker's `fetch` stage.

### Step 27. Task 1: `POST /ingest` with a link, and the worker's `fetch` stage

`POST /ingest` now takes **either** `file` **or** `url`. No migration: the job table already had `source_kind = url`, the stage `fetch` and an optional `document_id`.

```
POST /ingest (url)  -> checks that need no network -> job "queued" -> 202
worker              -> fetch (download with the Step 26 checks, save the document and file)
                    -> parse -> clean -> chunk -> embed -> upsert
```

| Decision | Why |
|---|---|
| The API downloads nothing | a slow link must not hold up the request, and the checks that need the network belong where the download happens |
| The link (without `#...`) is the document's `source` | the same link again updates the **same** document; if the file did not change, nothing is embedded again |
| A redirect does not change the `source` | the identifier is what the user sent |
| Links longer than 512 characters are refused | that is the size of `documents.source` |
| `title` and `source` with a `url` are refused | there is nowhere to keep a title before the document exists, and ignoring it silently would be worse |
| The document, file and job are linked in **one** transaction | if another attempt linked the job first, nothing is left behind |
| A later attempt of the same job uses the stored file | an embedding retry does not download again |
| If the document is deleted between attempts, the job fails | a retry must not bring back a deleted document |

| Download result | Job |
|---|---|
| blocked address, 404, wrong type, too large, empty | `failed` at once, with the reason |
| timeout, network problem, 429, 5xx | tried again (30, 60, 90 s), `failed` after 4 attempts |
| the same bytes as another document of the tenant | `failed`: "This document was already uploaded (id ...)" |

For a link, the answer has `document_id: null` and `new_document: null`, because the document is not known yet.

**Changed:** `api/v1/ingest.py`, `schemas/ingest.py`, `services/ingest_request.py` (`check_link`, `accept_link`, `save_document_file`; `GIVEN_TITLE_KEY` moved here, which avoids a circular import), `services/ingestion.py` (the `fetch` stage), `services/ingestion_jobs.py` (`attach_document`), `core/exceptions.py`, and the two test files.

**1. Tests**
```
the 41 existing ingestion tests, before adding new ones -> 41 passed
with 28 new link tests                                 -> 69 passed
pytest (everything)                                    -> 689 passed
mypy src tests                                         -> Success: no issues found in 109 source files
ruff check .                                           -> 2 errors
```
Turning protections off one at a time in a temporary copy:
```
(nothing turned off)                       -> 69 passed
a deleted document is made again           -> 1 failed, 68 passed
a new download on every attempt            -> 1 failed, 68 passed
temporary download problem not retried     -> 2 failed, 67 passed
link not kept as the document's source     -> 4 failed, 65 passed
no link check in the API                   -> 6 failed, 63 passed
```

**2. Live, through the running `/ingest`.** As in Step 24, the normal worker was stopped and a temporary one used a **fake Jina and a fake key** (both checked first; queue length 0).
```
tenant: Day 11 Corpus

[1] refused at once by /ingest
  {"url": "file:///etc/passwd"}                       -> 422 Only http and https links are allowed.
  {"url": "http://redis:6379/"}                       -> 422 Only the standard web ports are allowed.
  {"url": "https://files.example/a.md", "title": "T"} -> 422 title and source cannot be used with a url. The url is the document's source.
  {"url": ""}                                         -> 422 Send either a file or a url.

[2] accepted, then refused by the worker
  http://postgres/ssrf-probe.md
    POST -> 202 document_id=None new_document=None status=queued
    job  -> {'kind': 'url', 'status': 'failed', 'stage': 'fetch', 'attempts': 1, 'document_id': None, 'chunks': None, 'error': 'This address is not allowed.'}
```
`http://api/...`, `http://169.254.169.254/...` and `http://host.docker.internal/...` gave the same result.
```
[3] a file upload still works (fake Jina)
    POST -> 202 new_document=True source=live-step27.md
    job  -> {'kind': 'file', 'status': 'succeeded', 'stage': None, 'attempts': 1, 'document_id': '9689b10f-...', 'chunks': 1, 'error': None}
```
There is no status endpoint yet, so the job rows were read from the database.

**3. The success path of a link on the real PostgreSQL.** Without internet access, **only the download was a stand-in**; the database, tokenizer and stages were real, and the embeddings fake. Run inside the temporary worker:
```
before         : {'documents': 1, 'document_files': 1, 'chunks': 1, 'ingestion_jobs': 5}     (from part 2)
  succeeded  chunks_written=2 document=4fdb9994 chunk ids=['c5f8f51f', '550f25d7']
  up_to_date chunks_written=0 document=4fdb9994 chunk ids=['c5f8f51f', '550f25d7']
after two runs : {'documents': 2, 'document_files': 2, 'chunks': 3, 'ingestion_jobs': 7}
document deleted; its jobs stay: [('succeeded', None), ('succeeded', None)]
after clean-up : {'documents': 0, 'document_files': 0, 'chunks': 0, 'ingestion_jobs': 0}
```
The same link twice: one document, the same chunk IDs, nothing written the second time.

**4. Logs and clean-up.** Neither the API log nor the temporary worker's log had the probe links' path or the uploaded text (0 lines). A search for `GET /ssrf-probe` found one API line: the **control** request of Step 26 (`11:13:10 GET /ssrf-probe-control`), inside my 10-minute search window. The queue was empty before the normal worker came back, the temporary containers are removed, the normal worker is healthy and uses its normal Jina setting, and the old corpus is unchanged.

**Limits**
- **A real download from the internet is still not tested**, including the TLS certificate check against the name. All successful downloads used stand-ins.
- A link job cannot be given a `title`; the title comes from the file's first heading or its name.
- A link with a token in its query is stored in the database as the document's `source`, and the tenant sees it there. It is not written to the logs by my code.
- The file behind a link is not checked again by itself; sending the link again does that.
- The Step 25 limits still apply. A successful link in normal use is embedded by the **real** Jina and uses tokens.

**Next:** the status endpoint.

### Step 28. Task 1: the status endpoint

`GET /api/v1/ingest/{job_id}` reads the job's row in `ingestion_jobs` (not ARQ, which forgets results after a while). It uses `get_job` from Step 16, which filters by the **tenant of the login**. No migration.

| Field | Meaning |
|---|---|
| `status` | `queued`, `running`, `succeeded`, `failed` (the last two are final) |
| `stage` | where a running job is, or where a failed one stopped; `null` when queued or succeeded |
| `error` | only for a failed job: a short reason that is safe to show |
| `attempts` | how many times a worker started it |
| `chunks_written` | new chunks saved; `0` means nothing needed to change |
| `document_id` | `null` for a link that is not downloaded yet, or after the document is deleted |
| `job_id`, `source_kind`, `source`, `created_at`, `started_at`, `finished_at` | what was sent, and when |

The answer has no tenant id. Another tenant's job gets the **same 404 as a job that does not exist**, so an id tells nothing.

**Changed:** `api/v1/ingest.py`, `schemas/ingest.py` (`IngestJobStatus`), `tests/test_ingest_api.py` (5 new tests).

**1. Tests**
```
ruff check (the 3 files) -> All checks passed!
mypy src tests           -> Success: no issues found in 109 source files
pytest (everything)      -> 694 passed
ruff check .             -> 2 errors
```
The tenant filter, removed in a temporary copy:
```
nothing turned off     -> 35 passed
no tenant filter       -> 1 failed, 34 passed
    FAILED tests/test_ingest_api.py::test_another_tenants_job_looks_like_a_missing_one
```

**2. Live.** Same safety setup as Step 24: a temporary worker with a **fake Jina and a fake key**, both checked, then paused before anything was queued (queue length 0).

With no worker running:
```
GET /ingest/{file job} -> 200
{
  "job_id": "bd0a153c-f614-45dd-8790-47efbf6ee070",
  "status": "queued",
  "stage": null,
  "error": null,
  "source_kind": "file",
  "source": "live-step28.md",
  "document_id": "7e68e390-8590-49fb-973d-7a0d30cd62a1",
  "attempts": 0,
  "chunks_written": null,
  "created_at": "2026-11-25T11:27:47.073227Z",
  "started_at": null,
  "finished_at": null
}
```
Who else can read it (the "other organisation" is my old corpus account, which only read):
```
another organisation                 -> 404 INGESTION_JOB_NOT_FOUND: Ingestion job not found.
another organisation, the link job   -> 404 INGESTION_JOB_NOT_FOUND: Ingestion job not found.
my account, an unknown id            -> 404 INGESTION_JOB_NOT_FOUND: Ingestion job not found.
my account, not an id                -> 422 VALIDATION_ERROR: The request is not valid.
no login                             -> 401 Not authenticated.
```
Then the fake Jina was told to fail 4 times and the worker was started. I asked for the status every 0.5 s and printed it when it changed:
```
  0.0 s  status=queued    stage=None   attempts=0 chunks_written=None error=None
  2.5 s  status=running   stage=embed  attempts=1 chunks_written=None error=None
 70.0 s  status=running   stage=embed  attempts=2 chunks_written=None error=None
 71.1 s  status=succeeded stage=None   attempts=2 chunks_written=1 error=None

fake Jina: {'requests': 1, 'texts': 1, 'fail_next': 0, 'failed': 4}
```
The embed stage tried 4 times, gave up for that attempt, and the job was tried again 30 s later. The finished job also had `started_at` 11:27:50 and `finished_at` 11:28:58. The link job (`http://postgres/step28.md`) ended as `status "failed"`, `stage "fetch"`, `error "This address is not allowed."`, `document_id null`.

Short stages (`parse`, `clean`, `chunk`, `upsert`) took less than my 0.5 s between requests for this tiny file, so they did not show up.

**3. Clean-up:** I deleted this check's 2 jobs and 1 document (afterwards 0 rows in `Day 11 Corpus`), the queue was empty, the temporary containers are removed, the normal worker is healthy with its normal Jina setting, and the old corpus is unchanged.

**Limits**
- A job **waiting to be retried** still says `running`, with the stage that failed and `error: null`. The answer does not say that it is waiting, or when the next attempt is.
- There is no list of jobs: the caller has to keep the `job_id`.
- A job that was never picked up stays `queued`; nothing marks it (see Step 25).
- No progress inside a stage (for example, how many batches are embedded).

**Next:** the whole flow for task 1: upload a file, wait for `succeeded`, and find its text through search.

### Step 29. The upload-to-search test: plan and token estimate (nothing sent)

**Why the real Jina:** the fake Jina's vectors are made from a hash of the text, so only identical text matches. It cannot show that a question finds the right part of a document.

**The file:** `fastapi-docs/tutorial/cors.md` (5,364 bytes, public FastAPI documentation, already sent to Jina in Step 22). Processed **locally** with the worker's settings (`auto`, 400, 60, headers off):
```
title: 'CORS (Cross-Origin Resource Sharing)'  chunker: markdown  chunks: 10
upload: 10 chunks, 1 request(s), about 1,769 tokens
questions: 4 requests, about 109 tokens [28, 28, 25, 28]
total: 5 requests, about 1,878 tokens
```
The upload estimate equals Step 22, where Jina billed exactly 1,769. The estimate for questions uses the same rule, which I have **not** checked for search queries.

**The questions, and the chunk that should answer each (decided before any search):**
| Question | Expected chunk | Section |
|---|---|---|
| What three things must be equal for two addresses to count as the same origin? | 1 | Origin |
| How long does a browser keep a CORS answer cached if I change nothing? | 6 | Use `CORSMiddleware` |
| Can I allow every website and still let the browser send login cookies? | 3 or 6 | Wildcards / Use `CORSMiddleware` |
| What does the middleware do with the OPTIONS request a browser sends first? | 7 | CORS preflight requests |

**The run:** `POST /ingest` into `Day 11 Corpus` with `source=fastapi-docs/tutorial/cors.md` -> ask the status endpoint until `succeeded` -> `POST /search` for each question with `limit` 5 (normal API and worker, real Jina) -> delete the test document and its job.

**Pass:** the job succeeds with 10 chunks; for every question an expected chunk is in the **top 3**; every result belongs to the new document. I will report the actual position of each, also when it is first or fails.

**What it will not show:** `Day 11 Corpus` is empty, so search chooses among these 10 chunks only. It proves that the whole flow works and can pick the right part of one document, **not** search quality on the whole corpus.

### Step 30. The upload-to-search test with the real Jina: passed

I approved the 5 requests. Before sending: the API and the normal worker used `https://api.jina.ai/v1/embeddings`, model `jina-embeddings-v3`, `pgvector`, headers off; the queue was empty and `Day 11 Corpus` had 0 rows. The script ran **once** and repeats no request (`upload_to_search.py`, a temporary script).

**1. Upload**
```
POST /ingest -> 202
{"job_id": "69da5359-2641-4710-8860-d808b33b2ac3", "document_id": "6a918621-4528-4f21-a3bb-02f5cdb09925",
 "status": "queued", "source": "fastapi-docs/tutorial/cors.md", "new_document": true}
```
**2. Status** (asked every 0.3 s, printed on change)
```
  0.0 s  status=queued stage=None attempts=0
  0.6 s  status=running stage=embed attempts=1
  1.5 s  status=running stage=upsert attempts=1
  1.8 s  status=succeeded stage=None attempts=1          chunks_written: 10

worker log: embedded 10 texts, 0 reused, 1769 tokens billed
            job 69da5359-...: saved (10 new, 0 removed, 0 unchanged)
```
**Estimated 1,769, billed 1,769.**

**3. Search** (`limit` 5, each answered 200 in 0.47 to 0.75 s). `*` marks an expected chunk.

*Q1. What three things must be equal for two addresses to count as the same origin?* (expected 1)
```
1. chunk 1 *  0.4920  Origin
2. chunk 2    0.2911  Steps
3. chunk 3    0.2536  Wildcards
4. chunk 5    0.2415  Use CORSMiddleware
5. chunk 0    0.1892  (introduction)
```
*Q2. How long does a browser keep a CORS answer cached if I change nothing?* (expected 6)
```
1. chunk 0    0.3989  (introduction)
2. chunk 6 *  0.3501  Use CORSMiddleware
3. chunk 7    0.3482  CORS preflight requests
4. chunk 5    0.3086  Use CORSMiddleware
5. chunk 2    0.2803  Steps
```
*Q3. Can I allow every website and still let the browser send login cookies?* (expected 3 or 6)
```
1. chunk 3 *  0.5299  Wildcards
2. chunk 5    0.4717  Use CORSMiddleware
3. chunk 6 *  0.4374  Use CORSMiddleware
4. chunk 4    0.3722  Use CORSMiddleware
5. chunk 2    0.3569  Steps
```
*Q4. What does the middleware do with the OPTIONS request a browser sends first?* (expected 7)
```
1. chunk 7 *  0.6633  CORS preflight requests
2. chunk 2    0.6262  Steps
3. chunk 8    0.5763  Simple requests
4. chunk 6    0.5429  Use CORSMiddleware
5. chunk 4    0.4554  Use CORSMiddleware
```

| Pass rule (Step 29) | Result |
|---|---|
| The job succeeds with 10 chunks | yes, first attempt |
| An expected chunk in the top 3, for every question | **4 of 4**; first place for 3 of the 4 |
| Every result belongs to the new document | yes, all 20 |

One complete result (Q1, first place):
```json
{
  "id": "1035ee95-44cc-5ecc-a4f4-d9610b567e90",
  "document_id": "6a918621-4528-4f21-a3bb-02f5cdb09925",
  "chunk_index": 1,
  "heading": "CORS (Cross-Origin Resource Sharing) > Origin",
  "text": "## Origin { #origin }\n\nAn origin is the combination of protocol (`http`, `https`), domain (`myapp.com`, `localhost`, `localhost.tiangolo.com`), and port (`80`, `443`, `8080`).\n\nSo, all these are different origins:\n\n* `http://localhost`\n* `https://localhost`\n* `http://localhost:8080`\n\nEven if they are all in `localhost`, they use different protocols or ports, so, they are different \"origins\".",
  "score": 0.492,
  "metadata": {
    "title": "CORS (Cross-Origin Resource Sharing)",
    "header": "",
    "chunker": "markdown",
    "headings": ["CORS (Cross-Origin Resource Sharing)", "Origin"],
    "page_end": null,
    "page_start": null,
    "token_count": 127,
    "pipeline_version": "5ac90c955e5e70a8"
  }
}
```

**What I noticed**
- **Q2 was the weakest.** The introduction ranked above the chunk with the answer, and third place was only 0.0019 behind the right one (0.3501 against 0.3482). The answer is one list item inside a larger chunk, which **may** be why; I have not tested that.
- The scores are not comparable between questions: the right chunk scored 0.35 for Q2 and 0.66 for Q4.
- The tokens Jina billed for the 4 questions are **unknown**: the search route does not log them.

**Clean-up:** I deleted the test document and job (afterwards 0 rows in `Day 11 Corpus`), the queue is empty, and the old corpus is unchanged.

**Task 1 is done:** `POST /ingest` for a file or a link, the worker job through all stages, and the status endpoint; a file went from upload to search with the real Jina. **Not proved:** a real internet download through a link (Step 27), and search quality on more than one document. The other limits are listed in Steps 25, 27 and 28.

**Next:** task 6, ingest the whole corpus again and show that nothing changes the second time.

### Step 31. Task 6, preparation (local only: nothing sent, nothing written to the database)

**1. Every document can be loaded.** For each of the 234 old documents (only read) I took what would be uploaded and ran parse, clean and chunk with the real tokenizer.
```
documents to upload: 234 | {'file on disk, checksum matches': 200, 'textbook: from the saved text': 34}
largest upload: 2.8 MB (limit 20 MB)
problems: 0
```
Checked: no missing or changed file (size and SHA-256 against `data/files.json`), no two documents with the same bytes (the second would get 409), nothing empty, over the limit, or with a too long source or title, and every document gives chunks. The textbook parts will be uploaded as `.txt` (their saved text has no line starting with `#`).

**2. Token estimate** (tokenizer count plus 11 per text)
```
headers off: 234 documents | 4,720 chunks | 4,720 texts to send | 313 requests | about 1,436,459 tokens | tokens / limit per minute = 16.0
   fastapi-docs           135 documents  1,267 chunks    243,960 tokens
   pydantic-docs           55 documents    957 chunks    241,807 tokens
   arxiv-papers            10 documents    684 chunks    259,599 tokens
   openstax-chemistry-2e   34 documents  1,812 chunks    691,093 tokens

headers on : 234 documents | 4,720 chunks | 4,720 texts to send | 314 requests | about 1,497,052 tokens | tokens / limit per minute = 16.6
```
The same numbers as Step 20/21's dry run. The real number comes from Jina's bill. I do **not** know how many tokens are left on my key. A second run of unchanged files should send **nothing** (`up_to_date`); the reload has to show that.

**3. Lines that only include a code file.** A correction first: Step 20 said **455 lines in 92 files**. That counted all FastAPI files on disk. In the **190 loaded** Markdown files it is **452 lines in 90 files**, exactly the `code_include` count that Day 9 recorded when it removed them. (I made the same mistake twice while preparing this step, and repeated the counts on the loaded files only.)
```
chunks (kept)                                       899      (the 90 files)
chunks (removed)                                    888
chunks with an include line                         420
chunks with ONLY include lines (and headings)         3
tokens (kept)                                   172,187
tokens (removed)                                157,229
include lines inside a code fence: 0
```
So **420 of the 4,720 chunks** would hold a file path where the reader expects code. Removing the lines saves about 14,958 tokens (about 1% of the total). `docs_src` is not on disk. There are also 7 `{! ... !}` lines **inside** code blocks; Day 9 kept those.

Examples:
```
The simplest FastAPI file could look like this:

{* ../../docs_src/first_steps/tutorial001.py *}

Copy that to a file `main.py`.
```
A whole chunk (`tutorial/dependencies/index.md`, chunk 4, 41 tokens):
```
### Import `Depends` { #import-depends }

{* ../../docs_src/dependencies/tutorial001.py hl[3] *}
```

**4. Other things Day 9 removed that the new pipeline keeps** (190 loaded Markdown files)
| What | In the files | In the new cleaned text | Day 9 |
|---|---|---|---|
| `{* ... *}` include lines | 452 in 90 files | 452 | removed |
| `::: module` lines (a place where a documentation tool would insert API text) | 51 in 14 files | 51 | removed |
| `///` lines (opening and closing of tip and note boxes) | 896 in 111 files | 896 | opening line turned into "Tip:", closing line removed |
| front matter | 8 files | 8 | removed |
| HTML comments (this count includes code blocks) | 110 in 15 files | 110 | removed |

I have **not** measured whether any of these changes search results.

**5. Decisions needed before the reload**
| Decision | Options |
|---|---|
| Include lines | **A.** keep them (no code change; 420 chunks hold paths). **B.** remove them, as Day 9 did (the sentence before them then points at nothing). **C.** download `docs_src` at the commit in `manifest.json` and put the code in (a download, new code that must handle `hl[...]` and line ranges, more tokens; amount not measured) |
| The other leftovers in the table | keep, or clean as Day 9 did |
| Contextual headers | off (the current default) or on (about 60,593 more tokens; effect on search not measured) |

**My proposal:** **B**, and clean the other leftovers like Day 9, outside code blocks only, with a new `CLEANER_VERSION`; headers **off**. The old and new corpus then differ less, so later comparisons say more about the new chunking. It also changes how **any** uploaded Markdown file is cleaned, so the rules must be narrow and tested. C would give the best content, but task 6 is about repeatable ingestion.

**6. How the proof will work (after the decisions and my approval)**
1. Upload all 234 through `POST /ingest` into `Day 11 Corpus` with full paths as `source`; wait for every job.
2. Take a fingerprint of the new organisation's **chunks table** (every column, vectors included).
3. Upload all 234 again; wait for every job.
4. The fingerprint must be **identical**, every second job `succeeded` with `chunks_written` 0, no duplicate (document, chunk index) or (document, text) rows, and no tokens billed.
5. The old corpus fingerprint must be unchanged.

### Step 32. Task 6: cleaner version 2 (Markdown rules)

**Decided:** clean as Day 9 did, and keep contextual headers **off**.

**Two rules I had missed in Step 31.** Day 9 also removed **heading ids** (`{ #first-steps }`, 1,172 in the 190 files) and rewrote `!!!` note boxes (119 lines). Both are part of "as Day 9", so they are included.

**New:** `services/markdown_cleaning.py`, called by the clean stage for Markdown only (`.txt` and PDF are not touched). `CLEANER_VERSION` is now `"2"`; it is part of the pipeline version, so a document saved with version 1 would be processed again.

| Rule | Narrower than Day 9, because it runs on every uploaded Markdown file |
|---|---|
| front matter at the start | only if every line looks like `key:`, a list item, a comment or indented text, so two `---` dividers keep their text |
| HTML comments | **not in code blocks or inline code** (Day 9's pattern also matched there); only if closed before the next code fence, so a stray `<!--` and a later diagram arrow `-->` delete nothing |
| include lines `{* *}`, `{! !}`, `--8<--` | whole line only |
| `::: package.name` | only a dotted name, so `:::note` boxes of other tools stay |
| `/// tip \| Title` ... `///` -> `Tip: Title` | only when both the opening and the closing line are there |
| `!!! note "Title"` -> `Note: Title` | its text moves left (including code blocks inside it) |
| heading ids | heading lines only |

Not handled, as in Day 9: `???` boxes (42 lines) and `////` tab lines (40).

**1. Tests** (`tests/test_markdown_cleaning.py`, 52): each rule; code blocks with `` ``` ``, `~~~` and four backticks stay as they are; lines that only look similar stay; stray comment starts delete nothing.
```
pytest test_markdown_cleaning.py -> 52 passed
pytest (everything)              -> 746 passed
mypy src tests                   -> Success: no issues found in 111 source files
ruff check (the 3 files)         -> All checks passed      (ruff check . still shows 2 errors)
```

**2. The 190 real files, line by line.** I compared each file with and without the new rules, counting how often every line appears. (My first version of this check could miss a removed line if the same line appeared elsewhere in the file, so I made it stricter.)
```
what the rules counted: {'heading ids removed': 1172, 'note boxes rewritten': 546, 'code include lines removed': 452,
                         'front matter removed': 8, 'HTML comments removed': 110, 'documentation tool lines removed': 51}
removed or changed lines, by kind: {'heading with id': 1172, 'slash box line': 856, 'include line': 452,
                                    'tool line': 51, 'bang box line': 118}
removed lines that were in a comment or front matter: 145
UNEXPLAINED removed lines: 0
new lines: {'label or heading': 1718} | other new lines: 0          (1,172 headings + 428 + 118 labels)
code blocks that are no longer there: 0                              (compared without indentation)
```
452 include lines, 51 tool lines and 8 front matter blocks are **exactly Day 9's counts**. One `!!!` line stays (`pydantic-docs/concepts/serialization.md`): its title has no quotes. Day 9's pattern needs quotes too.

**3. The corpus again** (the Step 31 script, local)
```
documents to upload: 234 | problems: 0
headers off: 234 documents | 4,683 chunks | 313 requests | about 1,403,236 tokens | tokens / limit per minute = 15.6
   fastapi-docs           135 documents  1,234 chunks    213,691 tokens      (before: 1,267 / 243,960)
   pydantic-docs           55 documents    953 chunks    238,853 tokens      (before:   957 / 241,807)
   arxiv-papers            10 documents    684 chunks    259,599 tokens      (unchanged)
   openstax-chemistry-2e   34 documents  1,812 chunks    691,093 tokens      (unchanged)
```
37 fewer chunks and about 33,223 fewer tokens than with version 1.

**A side effect:** `cors.md` (Step 30) is now about 1,663 tokens instead of 1,769, still 10 chunks. The numbers and scores in Steps 22 and 30 were measured with cleaner version 1.

**4.** The worker was restarted and reports cleaner version `2`, headers off. Nothing was sent to Jina or uploaded; the old corpus is unchanged.

**Limits**
- These rules cannot know a writer's intention: an uploaded file with a line that is only `{* ... *}` or `--8<--` loses it.
- HTML comments in **indented** code blocks (without fences) are removed.
- Whether this cleaning changes search results is **not measured**.
- The sentences that introduced the code ("could look like this:") now have no code after them.

**Next:** the reload and the two-run proof (Step 31, part 6), after my approval.

### Step 33. Task 6: the whole corpus twice. Passed

I approved the run. **Before it:** worker and API on cleaner `2`; `auto` 400/60, headers off, `pgvector`, `jina-embeddings-v3`; queue empty; `Day 11 Corpus` had 0 documents and 0 chunks; old corpus unchanged. A temporary script (`reload_corpus.py`) uploaded the same list as Step 31 through **`POST /ingest`** (full paths as `source`; titles for the PDFs and the textbook), waited for every job, read the worker log and took a snapshot. It would have stopped the worker after 3 failed jobs.

**Run 1** (normal worker, real Jina)
```
run 1: worker ai-service-worker uses https://api.jina.ai/v1/embeddings
uploaded 234 files in 15 s (234 new documents)
all jobs finished after 986 s: {'succeeded': 234}
chunks_written total 4683 | jobs with chunks_written 0: 0 | attempts 234 | jobs that needed more than one: 0
worker log: 234 'embedded' lines | texts 4,683 | reused 0 | tokens billed 1,403,236
worker log: 0 warning or error lines
```
**Estimated 1,403,236 tokens, billed 1,403,236.** About 16.4 minutes.

**Run 2: the same 234 files again.** To be safe in case of a bug, this run used a temporary worker with a **fake Jina and a fake key** (both checked first), so nothing could be billed, and the fake **counts what reaches it**. Same upload route and worker code; the Jina address is not part of the pipeline version.
```
run 2: worker tmp-ingest-worker uses http://tmp-fake-jina:8999/v1/embeddings
uploaded 234 files in 14 s (0 new documents)
all jobs finished after 60 s: {'succeeded': 234}
chunks_written total 0 | jobs with chunks_written 0: 234 | attempts 234 | jobs that needed more than one: 0
worker log: 0 'embedded' lines | texts 0 | reused 0 | tokens billed 0
worker log: 0 warning or error lines
fake Jina after: {"requests": 0, "texts": 0, "fail_next": 0, "failed": 0}
```

**The proof.** An md5 over **every column of every row** (ids, text, vectors, dates), after run 1 and after run 2:
| Snapshot | Rows | md5 after run 1 and after run 2 |
|---|---|---|
| **chunks table, every organisation** | 6,780 | `7dc798fb3502304f320b6a63c81677ee`, identical |
| chunks of `Day 11 Corpus` | 4,683 | `04bdefa4e02a3184a5955ae4b37d7057`, identical |
| documents of `Day 11 Corpus` | 234 | `f2e35d17ff4139a40fa5ea98230f3fe0`, identical |
| document_files of `Day 11 Corpus` | 234 | `78e14b82f5a2194b190569c0eb046b0e`, identical |

Does this fingerprint notice a change? In a rolled-back transaction I changed **one** chunk:
```
before            04bdefa4e02a3184a5955ae4b37d7057
one chunk changed a85ae1ad8e6c3f1404e779298a4d9399
after rollback    04bdefa4e02a3184a5955ae4b37d7057
```

**Duplicates and completeness** (`Day 11 Corpus`)
```
same (document, chunk_index): 0 | same (document, text_hash): 0 | same chunk id: 0
two documents with the same source: 0 | with the same file: 0
documents: 234 | without chunks: 0 | with a gap in chunk_index: 0 | chunk count differs from their record: 0
without a stored file: 0 | chunks of another organisation's document: 0 | chunks without a vector: 0
```
| Kind | Pipeline version | Chunker | Documents | Chunks |
|---|---|---|---|---|
| Markdown | `0d890562c7b4b61a` | markdown | 190 | 2,187 |
| PDF | `5ddd4b3d9842ad73` | recursive | 10 | 684 |
| text (textbook) | `376efd41b30f652d` | recursive | 34 | 1,812 |

Three versions are expected: the version includes the kind of file.

**6 texts appear in two different documents** (12 chunks): five are in both `fastapi-docs/index.md` and `tutorial/first-steps.md`, one in `tutorial/server-sent-events.md` and `tutorial/stream-json-lines.md`. The documentation itself repeats these passages. They are not duplicates made by ingestion, and I kept them: each belongs to its own page. (My first display query seemed to show missing letters, `Fa tAPI`. A shell escaping mistake in the **query** had replaced every `s` in the preview. The stored text is intact: 561 chunks contain `FastAPI`, 0 contain `Fa tAPI`.)

**Afterwards:** 468 jobs, all `succeeded`; temporary containers removed; the normal worker is healthy with its normal Jina setting; queue empty; the database is 128 MB (44 MB before); **the old corpus is unchanged**. `Day 11 Corpus` keeps the 234 documents.

**Day 11's "done when" is met:** running ingestion again on the whole corpus did not change the chunks table.

**What this does not show**
- It is for **unchanged files and unchanged settings**. A new cleaner, chunker or model version makes documents be processed again, on purpose. Changed files are covered by tests, not by this run.
- Run 2 used the fake Jina. That the job does not call the embedding service is what the 0 requests show.
- The uploads were one after another; two workers on the same document at once were not part of this.
- Search quality on the new corpus is **not measured**. Qdrant was not updated.
- The textbook comes from the old saved text, so mistakes of the first extraction are still in it.
- The HNSW index is shared, and it now holds 4,683 more vectors: Day 10's search measurements may come out differently.
- The reload script is a temporary one and is not in the project.

**Next:** task 4, the difficult PDFs.

### Step 34. Task 4, preparation: which 5 PDFs (nothing downloaded, installed or sent)

**On disk:** 11 PDFs, all in the Day 9 corpus: 10 research papers and the textbook (not used here: Step 14, no script reads that file). Installed for PDFs: only `pypdf` 6.21.4 (the pipeline's parser). The command line has `pdftotext`; `pdftoppm`, `tesseract` and `mutool` were not found, so I **cannot see a page as a picture** yet.

**1. A rough screen of the 10 papers** (a temporary script; signs for choosing, not proof: it guesses columns from where lines of text start and counts lines that begin with `Table N`)
```
file           pages  two-column pages  table captions on pages                   images pages <200 chars
2204.13679         5                 5               1 [4]                             2               []
2212.10528        11                10               6 [4, 5, 6, 7, 8]                 0               []
2309.12871        14                 2               6 [6, 7, 8, 14]                  14               []
2407.20243        19                 9               5 [8, 11, 12]                    17               []
2411.17299        21                 0               0 []                              0               []
2504.08748        80                 0               2 [34, 39]                      110               []
2506.02527         8                 0               1 [6]                             6               []
2601.05264        86                 0              36 [11, 13, 15, 16, 17, 18, 2      2               []
2605.13521        20                 1              15 [3, 4, 7, 8, 9, 17, 18, 19      2               []
2607.24767        10                 0               0 []                              6              [5]
```
**2. One page of each candidate, checked with `pdftotext -layout`** (text kept about where it is on the page; this is not a picture of the page)

| PDF | Page checked | What is there | What it will test |
|---|---|---|---|
| `2212.10528` | 5 | two columns of text, with a table inside the right column | reading order across columns; a table beside prose |
| `2407.20243` | 8 | two columns, figures with several panels and axis labels, a table | figure labels mixed into the text |
| `2601.05264` | 11 | one column, a table whose cells wrap over several lines | which cell a wrapped line belongs to |
| `2605.13521` | 8 | a wide table of numbers with several column headings and empty cells | whether each number stays with its row and column |
| a scanned page | none yet | **none of the 10 looks scanned** | what the pipeline does with a page that is only a picture |

**3. What I will record for each PDF** (decided before looking at the parser's output): reading order; whether table rows stay rows and numbers keep their column heading; captions, footnotes and figure labels mixed into sentences; broken or wrongly joined words; headers, footers and page numbers; what the **chunks** that hold the table look like. Each failure with the real extracted text beside what the page shows. For the scanned page: the job's result and what the user is told. The pipeline has **no OCR**, and task 4 asks to record failures, not to fix them.

**4. Needed from me: a way to see pages and to make a scanned one**
| Option | For | Against |
|---|---|---|
| **A. `pypdfium2` and `Pillow`, only for these runs** (`uv run --with ...`; the project's dependencies do not change) | draws pages as pictures, so I can compare them with the extracted text; turning one drawn page back into a PDF gives a "scanned" page whose true text I know | downloads two packages; a drawn page is cleaner than a real scan |
| **B. also download a real scanned PDF** (public domain) | real noise and skew | another download from a source to be chosen and checked; without OCR the result is probably the same: no text |
| **C. use a scanned PDF I already have** | real, no download | I have to choose a file with nothing private in it |

**My proposal: A.** Without OCR the point of the scanned page is what the pipeline does with a picture, and a drawn page shows that. I will say in the results that it is not a real scan.

**Not done:** no parsing results yet.

### Step 35. Task 4: five difficult PDFs, and what broke

I approved option A. `pypdfium2` 5.15.2 (4.0 MiB) and `Pillow` 13.0.0 (6.9 MiB) were used only through `uv run --with ...`; `pyproject.toml` and `uv.lock` did not change.

**How:** I drew one page of each paper as a picture and compared it **by eye** with what the pipeline's parse and clean stages give for that page, and looked at where the chunks begin and end.

**A correction to Step 34:** I wrote that the table of `2605.13521` has **empty cells**. The picture shows **none**. The gaps came from the `pdftotext -layout` view, not from the PDF.

#### 1. `2212.10528`, page 5: two columns, a table in the right column
- **Worked:** the left column is read completely, then the right one. Table rows stay rows, and the caption stays with the table.
- **Broke: a sentence is cut in half by a footnote and the table.** The text reads:
```
shows the performance of our proposed reranker
2https://github.com/google-research/t5x          <- footnote of the left column
Model size MRR@10                                <- the whole table and its caption
...
set in MRR@10.
on reranking BM25 top-1000 results. The BM25     <- the rest of the sentence
```
- **Broke: small text below the line is flattened:** `T5` with a small `3B` becomes `T53B`; `RoBERTa` with a small `Large` becomes `RoBERTaLarge`.
- **Broke: spaces:** `trainDE0`, `andRankT5-`, `HLA TR(Zhang`. Footnote numbers join the text (`toolkit 3 with`).
- The first column has no heading, and the bold best value is just a number.
- **Chunks:** the whole table and caption are inside one chunk (15).

#### 2. `2407.20243`, page 8: two columns, three charts, a table
- **Worked:** column order; all rows of the table, with the numbers in the order shown.
- **Broke: the charts become noise.** **37 of the page's 90 lines** are axis numbers, axis names and legends, some glued together:
```
0.08
0.10Average distance
gecko-embedding (pairwise)
...
Matryoshka-Adaptor (b) nDCG@10 vs pairwise distance
```
- **Broke: one word is split by 1,512 characters.** Page 7 ends with `incor-`; page 8 begins with the charts and the table, and `poration of the additional ...` comes after them. Neither half is the word `incorporation`.
- The caption says "the best performance is in bold"; bold is lost.

#### 3. `2601.05264`, page 11: a table with cells that wrap over several lines
- **Broke: the table is no longer a table.** The page shows 4 columns and 5 rows. The text is one fragment per line, with nothing marking where a cell or a row ends:
```
Model Architecture Key Strengths
Primary
Limitations
DPR +
BART
Bi-encoder +
Seq2Seq
Fast retrieval,
composable
Limited citation
control
...
FiD Passage-parallel
decoding
```
- **Broke: spaces disappear in normal prose:** `pas-sagesinnoisy-retrievalsettings[39]. However, thisintroducescomputational`. In the whole paper, 20 pieces have 30 or more letters in a row, such as `Weprovideanexhaustiveevaluation`.
- The caption of Table 3.2 is at the bottom of this page; its table is not on this page.
- **Worked:** the page number was removed.

#### 4. `2605.13521`, page 8: a wide table of numbers
- **Worked well:** **all 12 rows match the picture exactly**, each on one line with its 8 values in order. The running header `IBM Granite Embeddings` was removed.
- **Broke: headings of two lines are flattened** into `Model Params Embed. MTEB Retr. MTEB Code MTEB Retr. LongEmbed RaR-b` and `(M) Size Multi (18) (12) En (10) (6) (17)`. Which words belong to which of the 8 values is no longer written down.
- The line separating the other models from IBM's, and bold, are lost.
- **Broke: spaces** in small capitals (`RETRIEVALPERFORMANCE`) and around code font (`in-cludingmultilingual-e5-base(Wang`).
- **Chunks:** the whole table is in chunk 19; chunk 18 ends with a copy of the first heading line (overlap).

#### 5. Scanned pages
**These are not real scans.** I drew page 5 of `2212.10528` and saved the picture as a PDF, so I know what text it should give. A real scan would also have noise and a tilt.

**(a) A PDF with only that picture**, through the running service (temporary worker with a fake Jina):
```
POST /ingest        -> 202
GET /ingest/{job}   -> {"status": "failed", "stage": "clean", "error": "The file contains no text.", "attempts": 1}
GET /documents/{id} -> 200, title "task4-scanned-page", content: 0 characters
documents before: 234 | after the failed job: 235
fake Jina: {'requests': 0, ...}
```
- **Broke:** there is no OCR. The message is true but does not tell the user that the page is a **picture**.
- **Found: a failed ingestion leaves an empty document in the list.** `POST /ingest` creates the document before the worker processes the file. This is true for any file that fails, not only scans.

**(b) A mixed PDF** (page 4, the **picture** of page 5, page 6), parse, clean and chunk run locally:
```
parse: 3 page(s), characters per page [4736, 0, 4676]
clean: succeeded | empty_pages = [2]
chunk: 9 chunks | chunk 3 runs from page 1 to page 3
    '0.4235'                     on the original page: True | in the result: False
    'Table 2: Reranking'         on the original page: True | in the result: False
    '5.1 Results on MS MARCO'    on the original page: True | in the result: False
  end of page 1  : '... pre-train on C4 for 200k steps with\n1https://github.com/usnistgov/trec_eval\n\n'
  start of page 3: 'Retriever Reranker\nSupervised Baselines'
```
- **The worst failure here: a page disappears and nothing says so.** The stages succeed. The cleaner **notices** the empty page (`empty_pages`), but no other code reads that value: it is not saved or shown. (A mixed file was not uploaded, so this is the local result plus a search of the code.)
- Chunk 3 joins text from both sides of the gap and is labelled **pages 1 to 3**, so a citation would include a page whose text it does not have.

#### Summary
| Failure | Seen in | Effect |
|---|---|---|
| A scanned page in an otherwise normal PDF is skipped silently | mixed file | missing content that nobody knows about |
| A picture-only PDF fails; the message does not say why | scan | the user cannot tell what to do; an empty document stays |
| Wrapped table cells become loose fragments | `2601.05264` | cell and row boundaries are lost |
| Two-line table headings are flattened | `2605.13521` | values are hard to match with their column |
| Tables, charts and footnotes interrupt sentences and words | `2212.10528`, `2407.20243` | broken sentences and a broken word |
| Chart labels become text | `2407.20243` | noise inside chunks |
| Missing or extra spaces; small letters flattened | `2212.10528`, `2601.05264`, `2605.13521` | words that search may not match |

**Tables with one line per row kept their rows and numbers** on the pages I checked (`2212.10528`, `2407.20243`, `2605.13521`). Task 4 asks to **record** failures: I fixed none of them.

**Limits:** one page of each paper was compared by eye (plus the targeted checks above), so these papers have more failures that I have not seen, and "worked" is about that page only. Only `pypdf` was tried. The effect on search or answers was **not measured**. The scans are drawn, not real.

**Afterwards:** the test upload (1 job, 1 document) was deleted by id; `Day 11 Corpus` has 234 documents and its 4,683 chunks have the same md5 as in Step 33 (`04bdefa4...`); the old corpus is unchanged; temporary containers removed; the normal worker is healthy with its normal Jina setting. No request to the real Jina.

**Next:** task 5, a table split over two chunks.

### Step 36. Task 5, preparation: the split tables, the questions and the rule (nothing sent)

**No need to make a split on purpose: the reload already made some.** A temporary script (read only) looked for Markdown tables in `Day 11 Corpus` that are not whole in any one chunk:
```
fastapi-docs/tutorial/metadata.md       |  7 rows | chunk 1: rows 1-5, chunk 2: rows 5-6, chunk 3: rows 7-7 | header row in [1]
pydantic-docs/api/standard_library_types.md | 8 rows | chunk 18: rows 1-3, chunk 19: rows 3-8 | header row in [18]
pydantic-docs/migration.md              |  9 rows | chunk 48: rows 1-7, chunk 49: rows 7-9 | header row in [48, 50]
pydantic-docs/migration.md              | 22 rows | chunk 50: rows 1-8, chunk 51: rows 8-17, chunk 52: rows 17-22 | header row in [48, 50]

Markdown tables in the corpus: 28 | not whole in any one chunk: 4
```
(For `migration.md` two tables have the same header line, so "header row in" cannot tell them apart: chunk 50 holds the header of the 22-row table.)

**A weakness seen here:** the Markdown chunker keeps a table whole when it fits in 400 tokens. A larger table is cut between rows, and **the header row is not repeated**. In chunk 51 and 52 the two columns have no names; in `metadata.md`, chunks 2 and 3 do not say "Parameter | Type | Description". One row is repeated at each cut (overlap). This was a split by the normal settings, not one I forced.

**The two tests**
| # | Question | Table | Chunks with a needed row | A full answer needs |
|---|---|---|---|---|
| 1 (main) | Which of the FastAPI metadata fields for the API are strings, and which are dictionaries? | `metadata.md`, 7 rows | **1, 2, 3** | `str`: `title`, `summary`, `description`, `version`, `terms_of_service` (chunk 1; the last also in 2). `dict`: `contact` (only chunk 2), `license_info` (only chunk 3) |
| 2 | In the Pydantic V2 migration table of deprecated and moved items, which moved to `pydantic.deprecated` and which moved to `pydantic.v1`? | `migration.md`, 22 rows | **50, 51, 52** | all 22 rows: all three chunks |

**Tokens:** 2 requests, about **86 tokens** (35 and 51; the tokenizer count plus 11, a rule I have not checked for search queries). Nothing else is sent; the corpus is not changed.

**How I will run it:** each question once through `POST /search` with `limit` 50, signed in to `Day 11 Corpus`, searching all 4,683 chunks. I report the **position** of every table chunk. "Top 5" (the default `limit`) means the first five of this list; I will not send a second request with `limit` 5.

**The rule, decided now:**
- **Broken (half a table):** in the top 5, at least one table chunk is there and at least one chunk with a needed row is missing. I then list which rows the top 5 does and does not contain.
- **Not broken:** all needed chunks are in the top 5. I report that as the result.
- **Nothing found:** no table chunk in the top 5. A failure too, but a different one.
- I will **not reword a question** to get the result I expect.

There is no answer generation yet (tomorrow's topic), so I show **what the retrieved text can and cannot answer**, not a written wrong answer.

### Step 37. Task 5: the split table and what search returns

Each question was sent **once** to `POST /search` with `limit` 50, through the normal API with the real Jina, searching all 4,683 chunks. Both answered 200. The tokens billed are not known (the search route does not log them).

#### The result by my rule (top 5): "nothing found", in both tests
```
test 1: Which of the FastAPI metadata fields for the API are strings, and which are dictionaries?
  1. 0.6392  fastapi-docs/tutorial/body-multiple-params.md #3
  2. 0.6356  fastapi-docs/tutorial/path-params.md #19
  3. 0.6278  fastapi-docs/index.md #25
  4. 0.6271  fastapi-docs/tutorial/request-forms.md #4
  5. 0.6232  fastapi-docs/tutorial/body.md #4
  table chunk 1: position 6, score 0.6148
  table chunk 2: not in the 50
  table chunk 3: position 48, score 0.5311
  table rows in the top 5 text: 0 of 7

test 2: In the Pydantic V2 migration table of deprecated and moved items, which moved to
        pydantic.deprecated and which moved to pydantic.v1?
  1. 0.6923  fastapi-docs/how-to/migrate-from-pydantic-v1-to-pydantic-v2.md #3
  2. 0.6774  pydantic-docs/migration.md #0
  3. 0.6741  fastapi-docs/how-to/migrate-from-pydantic-v1-to-pydantic-v2.md #1
  4. 0.6656  pydantic-docs/version-policy.md #0
  5. 0.6629  pydantic-docs/migration.md #7
  table chunk 50: position 7, score 0.6575
  table chunk 51: position 20, score 0.5845
  table chunk 52: position 12, score 0.6194
  table rows in the top 5 text: 0 of 22
```
So with the default `limit` of 5, search did **not** return half of either table. It returned **none** of it. I did not reword the questions.

#### Half a table, with 10 results
**I added this look after seeing the result above**, so it was not part of my rule. It uses the same saved responses: no new request. It shows the first N of the 50 results; I did **not** send a separate request with `limit` 10.

| First N results | Test 1: rows of 7 | Test 2: rows of 22 | Test 2: to `pydantic.deprecated` | Test 2: to `pydantic.v1` |
|---|---|---|---|---|
| 5 | 0 | 0 | 0 of 11 | 0 of 11 |
| 7 | 5, **neither dictionary** | 8 | 8 of 11 | **0 of 11** |
| **10** | **5, neither dictionary** | **8** | **8 of 11** | **0 of 11** |
| 12 | 5, neither dictionary | 14 | 8 of 11 | 6 of 11 |
| 20 | 5, neither dictionary | 22 | 11 of 11 | 11 of 11 |
| 48 and 50 | 6 (`license_info` added; `contact` never) | 22 | 11 of 11 | 11 of 11 |

**What an answer from the first 10 results would say wrongly.** There is no answer generation yet, so this is what the retrieved text supports, not an answer a model wrote:
- **Test 1:** the only rows of this table are the five `str` rows. An answer would list five strings and **no dictionary**; `contact` and `license_info` are missing. Nothing in chunk 1 says the table continues.
- **Test 2:** all rows retrieved from this table go to `pydantic.deprecated`. An answer could say **nothing in it moved to `pydantic.v1`**; in the table, 11 of the 22 did.

**Why, as far as I can tell.** In both tests the chunk **with the header row** and the section's text ranked far above the chunks without it: position 6 against 48 and "not in the 50"; position 7 against 12 and 20. The later chunks are rows without column names (Step 36). That fits what I saw, but **two tests do not prove the cause**, and I did not try repeating the header row to see whether it helps. Their long table cells (HTML in `metadata.md`) could matter too.

**Task 5, honestly:** with the default 5 results the failure is bigger than the task expects: neither table is found at all. With the first 10, part of each table is there and part is missing: 5 of 7 rows in test 1, 8 of 22 in test 2.

### Step 38. Notes: the parsing failure catalogue

New: [`parsing-failure-catalogue.md`](parsing-failure-catalogue.md). It collects the failures of Steps 19 to 37, each with an example, its effect and whether it is fixed. Before writing it, I checked old findings on the new corpus: the three wrong titles of Step 8 that I looked at are right now, and the three wrong joined words `Referencefree`, `promptbased` and `Noncompliant` appear in 0 chunks. HTML formatting tags are in 31 chunks of 13 documents.

## Order I followed
| Order | Task | Why there |
|---|---|---|
| 1 | Task 2, the three chunkers | plain functions with tests; no new service needed, and the pipeline needs them |
| 2 | Task 3, contextual headers | a small option on top of the chunkers |
| 3 | Task 1, the ingestion job, worker and status | puts parsing, chunking, embedding and upsert together |
| 4 | Task 6, ingest again and compare | proves task 1 is idempotent |
| 5 | Task 4, the difficult PDFs, and the failure catalogue | tests the parser through the pipeline |
| 6 | Task 5, the broken table | needs the chunkers and search |

(The catalogue was written last, after task 5, so that it includes the table result.)

## What is still open at the end of the day
- **The work is not committed** (branch `day-11`).
- A **real internet download** through a link, with its certificate check, is not tested (Step 27).
- Search quality on the new corpus is not measured, and neither is the effect of contextual headers or of cleaner version 2.
- Ingestion does not update Qdrant.
- The parsing failures marked open in the catalogue are not fixed. Task 4 asked to record them.
- No job is picked up again if it stays `queued`; there is no limit on uploads per tenant (Step 25).

## Decisions
| Decision | When it was needed |
|---|---|
| Which tokenizer counts the tokens | the first chunker. **Decided:** the BGE-M3 tokenizer (Step 2); `tokenizers` installed and checked (Step 3) |
| ARQ (in maintenance-only mode) or Celery | task 1. **Decided: ARQ** (Steps 9 and 10) |
| Where the uploaded file waits for the worker and is kept for re-parsing | task 1. **Decided: PostgreSQL, table `document_files`** (Steps 12, 15) |
| How chunk IDs and a pipeline version fit the table | task 1. **Decided and built** (Step 23), without a migration |
| Which PDF parser, and when to fall back to another | tasks 1 and 4. **Used: `pypdf` only, no fallback and no OCR.** Task 4 recorded what that breaks (Step 35); choosing another parser is still open |
