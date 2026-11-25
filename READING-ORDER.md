# Reading order: Day 11 (branch `day-11`)

This branch is the project at the end of **Day 11**: RAG I, ingestion, parsing and chunking. It is the Day 10 service plus an ingestion pipeline that runs in a background worker: a file or a link goes through parse, clean, chunk, embed and upsert, and becomes searchable.

**The goal of the day:** running ingestion again on the whole corpus does not change the `chunks` table. **Met.**

## What was done on Day 11
- **Ingestion** (`POST /api/v1/ingest`, `GET /api/v1/ingest/{job_id}`): the API saves the file (or only the link) and queues a job in Redis with ARQ; a separate worker does the work. Original files are kept in PostgreSQL. Links are downloaded with checks against private addresses.
- **Three chunkers** (fixed-size, recursive, Markdown-aware), compared on the old corpus. Of its 28 Markdown tables, 14 were not whole in any chunk with fixed-size, and 4 with each of the other two. Code blocks small enough to fit were cut 127, 80 and 0 times.
- **Contextual headers** (document title and section path) are an option, **off by default**. Their effect on search is not measured.
- **Cleaning:** PDF headers, footers, page numbers and hyphens; website markup in Markdown.
- **Whole corpus, twice:** 234 documents became 4,683 chunks (billed 1,403,236 tokens, as estimated). The second run wrote 0 chunks, and the `chunks` table was identical.
- **One file from upload to search** with the real Jina: for 4 questions the expected chunk was in the top 3.
- **Difficult PDFs:** the worst failure is a scanned page in a normal PDF, which disappears while the job succeeds.
- **A split table:** with 5 results, search found none of either test table. In the first 10 (looked at afterwards) it had 5 of 7 and 8 of 22 rows.

The new corpus is in its own organisation, `Day 11 Corpus`. The old Day 9 corpus was not changed.

## Read in this order
1. The concepts: `ai-eng-learning/day-11/README.md`, then its `learning/` notes (1 the pipeline, 2 parsing, 3 chunking, 4 ingestion) and `Notes/`.
2. `notes/day-11/parsing-failure-catalogue.md`: what broke, with examples. The short way to see the limits.
3. `notes/day-11/README.md`: the log of every step with its commands and outputs. It starts with the task table and ends with what is still open.

| Steps | About |
|---|---|
| 0 to 8 | the start, the tokenizer, the three chunkers, contextual headers |
| 9 to 18 | the job queue, the worker service, where files are kept, the old corpus rules, two new tables, the tokenizer in Docker |
| 19 to 25 | parse and clean, chunk, embed, upsert, the worker job, `POST /ingest` for a file |
| 26 to 28 | links, and the status endpoint |
| 29 and 30 | upload to search |
| 31 to 33 | the whole corpus: preparation, Markdown cleaning, the two runs |
| 34 and 35 | the difficult PDFs |
| 36 and 37 | the split table |
| 38 | the failure catalogue |

Then the code. `services`, `schemas`, `api` and `db` are in `src/ai_service/`.

4. **Counting and chunking:** `services/tokenizer.py`, `services/chunking.py`, `services/chunk_stage.py`; `tests/test_chunking.py`, `tests/test_chunk_stage.py`.
5. **Parse and clean:** `services/parsing.py`, `services/cleaning.py`, `services/markdown_cleaning.py`; `tests/test_parse_and_clean.py`, `tests/test_markdown_cleaning.py`.
6. **Embed and save:** `services/embedding.py` (batches, pace, retries), then `services/chunk_save.py` (pipeline version, chunk ids, why a second run changes nothing); `tests/test_embedding.py`, `tests/test_chunk_save.py`.
7. **The two new tables:** `db/models/document_file.py`, `db/models/ingestion_job.py`, the migrations `5520625dcc98` and `3951de4daf53` in `alembic/versions/`, then `services/file_store.py` and `services/ingestion_jobs.py`.
8. **The job:** `services/ingestion.py` (all the stages, what is retried and what fails), then `worker.py` (time limit, retries, logging) and `services/job_queue.py`.
9. **The API:** `api/v1/ingest.py`, `schemas/ingest.py`, `services/ingest_request.py` (which document a file belongs to); `tests/test_ingest_api.py`, `tests/test_ingestion.py`.
10. **Links:** `services/url_fetch.py` (read its first lines: the six rules) and `tests/test_url_fetch.py`.
11. **Setup:** `core/config.py` (the new settings), `.env.example`, `docker-compose.yml` (the `worker` service), `Dockerfile` (the tokenizer file, pinned by checksum).

## To run and check it
Copy `.env.example` to `.env` and set `LLM_API_KEY` and `JINA_API_KEY`.

> **Ingestion and search call the real Jina and use tokens.** Loading my whole corpus once used about 1.4 million.

```
uv sync
docker compose up -d --build     # postgres, redis, qdrant, the API (8000), the frontend (3000) and the worker
uv run pytest -q                 # 746 tests; no server needed (see the tokenizer note)
docker compose ps                # the worker should be "healthy"
```
- The API applies the database migrations when it starts.
- **To try ingestion:** open `http://localhost:8000/docs`, register, use **Authorize**, send a `.md`, `.txt` or `.pdf` file to `POST /api/v1/ingest`, and ask `GET /api/v1/ingest/{job_id}` until it says `succeeded` or `failed`. Then use `POST /api/v1/search`.
- **The worker does not reload** when the code changes: `docker compose restart worker`. After a new package or migration: `docker compose up -d --build api worker`.
- **Do not use `docker compose down -v`** on a database you care about: it deletes the data.
- **The tokenizer:** Docker builds it into the image. On your own machine the code uses the BGE-M3 `tokenizer.json` in the Hugging Face cache, or the file named by `TOKENIZER_PATH`. Without it, 36 tests that need the real file are skipped (710 pass).
- Run ARQ in the Linux container: on Windows, ARQ 0.28.0 failed while closing a worker.

**Reports that need my Day 9 data** (they read files and the database, and call no API):
```
uv run python scripts/parse_report.py              # needs the Day 9 files from ai-eng-learning
uv run python scripts/chunk_report.py              # needs the old corpus in the database
uv run python scripts/prepare_report.py            # needs both
uv run python scripts/old_corpus_fingerprint.py    # compares with the fingerprint of MY database
```
On another database the last one will report a difference: the saved fingerprint in `scripts/results/` is of my local data.

**Not in the project:** the script that loaded the whole corpus twice, and the temporary scripts of the live checks. The log describes what they did and shows their output.

## Not done or not proved
- A real internet download through a link, with its certificate check, is not tested.
- Search quality on the new corpus is not measured.
- Ingestion saves to PostgreSQL only; it does not update Qdrant.
- `pypdf` is the only PDF parser; there is no OCR. The open parsing failures are in the catalogue.

## Other days
`day-3`, `day-5`, `day-6`, `day-7`, `day-8`, `day-10`, and `main`. Day 9 (embeddings) is in `ai-eng-learning/day-9`.
