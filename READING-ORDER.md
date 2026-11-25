# Reading order: Day 10 (branch `day-10`)

This branch is the project at the end of **Day 10**: vector databases. It is `main` (the Day 8 service plus the React frontend) plus tenant-isolated semantic search on pgvector, ported to Qdrant. It searches the Day 9 corpus: 2,097 chunks with 1,024-number Jina vectors.

## What was done on Day 10
- **Storage:** a `chunks` table with a `vector(1024)` column and an HNSW index (`m=16`, `ef_construction=64`), and a script that loads the Day 9 chunks and vectors.
- **Search** (`POST /api/v1/search`): the query is embedded, and the tenant filter is inside the database query. A Hindi question found the CORS page without using any word from it.
- **Recall:** on the 20 queries of Day 9, exact search got recall@5 of 19 of 20. HNSW at `ef_search` 40 returned the same 100 top-5 chunks.
- **`ef_search`:** at 5 it lost 7 of the 100 exact chunks without any error; from 10 upwards it lost none. The app uses 100 with `hnsw.iterative_scan`: with the HNSW index forced, a tenant with 3 chunks got 0 results before and 3 after.
- **Qdrant:** a `VectorStore` protocol with `PgVectorStore` and `QdrantStore`, chosen by `VECTOR_STORE` (default `pgvector`). Deleting a document removes its vectors from the chosen store first.
- **Isolation and deletion:** checked on the real servers with both stores, including two tenants that own identical vectors.
- **Comparison:** in the last run Qdrant returned 99 of 100 exact top-5 chunks and pgvector 100 of 100. The top 5 took the same time. At 50 results pgvector was slower, mostly because my query loads vectors it does not use (not changed yet). I keep pgvector as the default.

## Read in this order
1. The concepts: `ai-eng-learning/day-10/README.md`, then its `learning/` notes and `Notes/`.
2. `notes/day-10/pgvector-vs-qdrant.md`: the short comparison and my decision.
3. `notes/day-10/README.md`: the log of every step with its commands and outputs. Steps 0 to 7 are the table, the index, loading and search; 8 to 11 recall and `ef_search`; 12 isolation and deletion; 13 to 18 Qdrant; 19 the comparison. Its end lists the open points.
4. **The table:** `src/ai_service/db/models/chunk.py`, then the two migrations in `alembic/versions/` (`16797ee0540c` the table, `dc013ced71fa` the HNSW index), then `scripts/upsert_corpus.py`.
5. **Search on pgvector:** `schemas/search.py`, `services/search_service.py`, `api/v1/search.py`, `tests/test_search.py`. (The `services`, `schemas` and `api` folders are in `src/ai_service/`.)
6. **Recall and `ef_search`:** `scripts/data/search_queries.json`, then `scripts/eval_search.py` (exact), `scripts/compare_hnsw.py`, `scripts/tune_ef_search.py`. Their results are the `day10_*` files in `scripts/results/`.
7. **The two stores:** `services/vector_store.py` (the protocol and pgvector), `services/qdrant_store.py`, `tests/test_vector_store.py` (the same rules run against both).
8. **The switch and deletion:** `core/config.py` (`VECTOR_STORE`, `QDRANT_URL`), `main.py` (the Qdrant client), `api/deps.py` (`get_vector_store`), `services/document_service.py` (`delete_document`, and why the vectors go first), `tests/test_vector_store_switch.py`, and the `qdrant` service in `docker-compose.yml`.
9. **Checks on the real servers:** `scripts/copy_to_qdrant.py`, `scripts/verify_isolation.py` (also with `--qdrant`), `scripts/compare_stores.py`.

## To run and check it
Copy `.env.example` to `.env` and set `LLM_API_KEY` and `JINA_API_KEY`.
```
uv sync
docker compose up -d --build         # postgres, redis, qdrant, the API (8000) and the frontend (3000)
uv run pytest -q                     # 251 tests; no server needed
uv run python scripts/upsert_corpus.py       # needs the Day 9 files from ai-eng-learning
uv run python scripts/eval_search.py
uv run python scripts/compare_hnsw.py
uv run python scripts/tune_ef_search.py
uv run python scripts/copy_to_qdrant.py      # also: do this before VECTOR_STORE=qdrant
uv run python scripts/compare_stores.py
uv run python scripts/verify_isolation.py
```
- The measurement scripts use the id of the `Corpus Tenant` that `upsert_corpus.py` creates.
- `verify_isolation.py` also needs a user of the Corpus Tenant: an account in `.env.corpus-test` (`CORPUS_TEST_EMAIL`, `CORPUS_TEST_PASSWORD`). That file is not in git, and the sign-up page cannot create such a user, because it always makes a new organisation. I added mine to my local database directly.
- To run on Qdrant: `VECTOR_STORE=qdrant docker compose up -d api` (PowerShell: `$env:VECTOR_STORE='qdrant'; docker compose up -d api`), and `verify_isolation.py --qdrant`.

## Other days
`day-3`, `day-5`, `day-6`, `day-7`, `day-8`, and `main`. Day 9 (embeddings) is in `ai-eng-learning/day-9`.
