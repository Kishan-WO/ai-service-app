# Day 10: vector database (task implementation log)

**Build target (from the plan):** `POST /search` (semantic, tenant-isolated) on pgvector, ported to Qdrant. The concept notes are in `ai-eng-learning/day-10/`.

**How this day is done:** one small step at a time. After each step this log gets the **commands I ran**, the **real output** (including API responses), and what it means. Nothing is committed without asking.

## The tasks (from the plan)
| Task | What | Status |
|---|---|---|
| 1 | A `chunks` table with a vector column and an HNSW index (pgvector): chunk text, document id, tenant id, metadata, embedding model name | done (table + HNSW index migration `dc013ced71fa`) |
| 2 | Embed and upsert the corpus with a script | done (`scripts/upsert_corpus.py`: 2,097 chunks upserted in 8.7s) |
| 3 | `POST /search`: embed the query, search with the tenant filter, return chunks with scores | done (`POST /api/v1/search` with pgvector `<=>` & tests) |
| 4 | Recall@5 against the 20 queries; HNSW against exact search; tune `ef_search` | done (measured): exact 19/20 (Step 8), HNSW matches it 100/100 at 40 (Step 9), `ef_search` sweep (Step 10). The app now uses 100 (Step 11) |
| 5 | Port to Qdrant behind a `VectorStore` protocol, switch by config | done: Qdrant in Compose (Step 13), client (14), `VectorStore` (15), `QdrantStore` and copy (16), HNSW (17), switch by `VECTOR_STORE` (18) |
| 6 | Isolation test: tenant 2 never gets tenant 1's chunks, also with zero documents | done: `test_tenant_isolation` (SQLite) and on PostgreSQL + HTTP (Step 12) |
| 7 | Delete a document and prove its vectors disappear | done: `test_document_cascade_deletion` (SQLite) and through `DELETE /documents/{id}` on PostgreSQL (Step 12) |
| Notes | pgvector against Qdrant note | done: [pgvector-vs-qdrant.md](pgvector-vs-qdrant.md) (Step 19) |
| Notes | Tokenizer LRU cache explanation | done: [tokenizer-lru-cache.md](tokenizer-lru-cache.md) |

## Step log

### Step 0. A branch for the day
```
git switch -c day-10        # from main (247f6d5)
```
- `main` is untouched. One unrelated change came along because it was not committed: `src/ai_service/services/router_graph.py` (the `typing_extensions.TypedDict` import). I will not stage it with the Day 10 files.

### Step 1. Check the starting point (nothing changed yet)
**Why:** before I add a vector column I need to know that Postgres can load pgvector, that the database matches the migrations, and that the API runs.

Docker Desktop was off; I started it, then:
```
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep ai-service
```
```
ai-service-api        Up (health: starting)   0.0.0.0:8000->8000/tcp
ai-service-redis      Up (healthy)            0.0.0.0:6380->6379/tcp
ai-service-postgres   Up (healthy)            0.0.0.0:5433->5432/tcp
ai-service-frontend   Exited (0) 4 days ago
```
Questions to the database (`docker exec ai-service-postgres psql -U postgres -d ai_service -At -c "<sql>"`):
```
select version();
-> PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2) on x86_64-pc-linux-gnu, ...

select name, default_version, coalesce(installed_version,'(not installed)')
from pg_available_extensions where name='vector';
-> vector | 0.8.7 | (not installed)

select version_num from alembic_version;      -> 0a123456546c
alembic heads                                 -> 0a123456546c (head)

select tablename from pg_tables where schemaname='public' order by 1;
-> alembic_version conversations documents llm_calls messages tenants users

select (select count(*) from tenants), (select count(*) from users), (select count(*) from documents);
-> 3 | 3 | 5
```
```
curl -s localhost:8000/health        ->  {"status":"ok"}
```
**What it means**
- **pgvector 0.8.7 is available and not installed yet.** `CREATE EXTENSION vector` will work in a migration; the compose image already ships it. Version 0.8 is the one with iterative index scans (for filtered searches), so that feature is available.
- The database is at the repo's head revision (`0a123456546c`), so a new migration will chain from it.
- There are 3 tenants, 3 users and 5 documents: leftovers from my earlier tests. There are no `chunks` yet.
- The API is up. (The `ai-service-api` container copies `alembic/` when the image is built, so after I add a migration it needs `docker compose up -d --build api`.)

### Step 2. The pgvector extension and the `chunks` table (task 1, first half)
**1. The Python package for the vector column type**
```
uv add pgvector            # adds pgvector 0.5.1 to pyproject.toml and uv.lock
```
**2. The model** `src/ai_service/db/models/chunk.py` (and `Chunk` exported in `db/models/__init__.py`). Columns:

| Column | Why |
|---|---|
| `id` (uuid, primary key) | a stable chunk id, so loading a document twice replaces its chunks |
| `tenant_id` (FK to `tenants`, cascade, indexed) | isolation: every search filters on it. Copied from the document; the writer must keep them equal |
| `document_id` (FK to `documents`, cascade, indexed) | deleting a document deletes its chunks in the same transaction (task 7) |
| `chunk_index` | the order inside the document; `UNIQUE (document_id, chunk_index)` |
| `heading`, `text`, `text_hash` | what the chunk says, and a hash to skip re-embedding unchanged text |
| `embedding` `vector(1024)` | the vector (Jina `jina-embeddings-v3` has 1,024 numbers) |
| `embedding_model` | which model made the vector (never mix models in one index) |
| `metadata` (JSONB), `created_at`, `updated_at` | source, title, licence; timestamps |

The vector column is `Vector(1024).with_variant(JSON(), "sqlite")`: pgvector's type on PostgreSQL, plain JSON on SQLite, because the fast unit tests build their tables on SQLite (the existing models use the same trick for JSONB).

**3. The migration** (created with `alembic revision`, filled in by hand because the extension has to be switched on first):
```
alembic revision -m "chunks table with a vector column"   # -> 16797ee0540c, parent 0a123456546c
alembic heads                                              # -> 16797ee0540c (head)
alembic upgrade head
```
```
INFO  [alembic.runtime.migration] Running upgrade 0a123456546c -> 16797ee0540c, chunks table with a vector column
```
**4. What the database now has**
```
select extname, extversion from pg_extension where extname='vector';   -> vector | 0.8.7
\d chunks
```
```
 id uuid | tenant_id uuid | document_id uuid | chunk_index integer | heading varchar(512) | text text
 text_hash varchar(64) | embedding vector(1024) | embedding_model varchar(100) | metadata jsonb
 created_at, updated_at timestamptz   (all NOT NULL)
Indexes:  pk_chunks (id), ix_chunks_document_id, ix_chunks_tenant_id, uq_chunks_document_chunk_index (document_id, chunk_index)
Foreign keys:  document_id -> documents(id) ON DELETE CASCADE,  tenant_id -> tenants(id) ON DELETE CASCADE
```
There is no index on the `embedding` column yet; that is the next step, so I can time its build.

**5. Up, down, up again** (the check I did on Days 5 and 7)
```
alembic downgrade -1     ->  Running downgrade 16797ee0540c -> 0a123456546c   (to_regclass('chunks') is NULL: the table is gone)
alembic upgrade head     ->  Running upgrade 0a123456546c -> 16797ee0540c     (alembic_version = 16797ee0540c)
```
The downgrade leaves the extension installed on purpose (another table may use it).

**6. A smoke test, inside a transaction that was rolled back** (so the table is still empty)
```
INSERT one chunk with a vector of 1,024 numbers   -> INSERT 0 1
select embedding <=> embedding                     -> 0.0000   (cosine distance of a vector to itself)
select vector_dims(embedding)                      -> 1024
INSERT a chunk with the vector [1,2,3]             -> ERROR:  expected 1024 dimensions, not 3
ROLLBACK;   select count(*) from chunks            -> 0
```
**7. Nothing else broke**
```
pytest -q        -> 225 passed          (the tests build their tables on SQLite)
mypy src tests   -> Success: no issues found in 72 source files
ruff check       -> All checks passed
```
**8. The API container needs a rebuild after a new migration** (it copies `alembic/` and the packages when it is built). If it restarted before this, it would crash with "Can't locate revision":
```
docker compose up -d --build api
curl -s localhost:8000/health          -> {"status":"ok"}   (health 200)
docker exec ai-service-api ls alembic/versions | grep chunks     -> 16797ee0540c_chunks_table_with_a_vector_column.py
```
**What it means:** pgvector is on, the table exists with the right size of vector and a size check (a vector of 3 numbers is refused), the tenant and document links cascade on delete, and the migration goes up and down. The table is still empty.

### Step 3. The HNSW index on `chunks.embedding` (task 1, second half)
Migration `dc013ced71fa_hnsw_index_on_chunks_embedding.py` creates the HNSW index:
```sql
CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = '16', ef_construction = '64');
```
Verified with `\d chunks`:
`"ix_chunks_embedding_hnsw" hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='64')`

**Checks made when the index was created (before Step 4, with the table empty)**
```
alembic upgrade head   -> Running upgrade 16797ee0540c -> dc013ced71fa, hnsw index on chunks embedding
select c.reloptions, pg_size_pretty(pg_relation_size(c.oid)) ...   -> {m=16,ef_construction=64} | 16 kB   (empty index)
alembic check          -> No new upgrade operations detected.      (the model and the database agree)
show hnsw.ef_search    -> 40
```
**Can the planner use the index?** (200 random test vectors, in a transaction that was rolled back, `EXPLAIN`)

| Query | Table scans | Plan |
|---|---|---|
| `ORDER BY embedding <=> q LIMIT 5` | allowed | `Sort` over `Seq Scan on chunks` (with only 200 rows a table scan is cheaper) |
| the same | switched off (`enable_seqscan = off`) | `Index Scan using ix_chunks_embedding_hnsw` |
| `ORDER BY embedding <-> q LIMIT 5` (L2) | switched off | still `Seq Scan`: the index was built for cosine, so an L2 query cannot use it, and no error is shown |

**Exact search against the index** (200 distinct vectors, `ef_search = 100`): both returned the same top 5, chunk numbers 7, 99, 194, 90, 186 with cosine distances 0.0004, 0.9141, 0.9189, 0.9220, 0.9226. This is a smoke test on random vectors, not a recall measurement (that is task 4).

**A mistake in my first test data:** all distances came out as 0.0000 because every random vector was identical. The subquery that made the numbers did not depend on the row, so PostgreSQL ran it once and reused it. Tying it to the row (`random() - 0.5 + 0 * g`) gave 200 different vectors.

Not checked for this migration: `alembic downgrade` and upgrade again.

### Step 4. Embed and upsert the corpus (task 2)
Script: `scripts/upsert_corpus.py`
Command:
```bash
uv run python scripts/upsert_corpus.py
```
Output:
- Loaded 234 documents from Day 9 `documents.json`
- Loaded 2,097 sections from Day 9 `sections.json`
- Loaded 2,097 precomputed float32 vectors (1024-dim, `jina-embeddings-v3`)
- Created tenant `'Corpus Tenant'` (`c69cf31f-3e72-567f-bf70-f833be7a5ca9`)
- Created 234 documents in `documents` table
- Batch-upserted all 2,097 chunks using `ON CONFLICT (document_id, chunk_index) DO UPDATE` in **8.72 seconds**
- Total chunks in DB: **2,097**

**Query verification (`EXPLAIN ANALYZE`):**
```sql
EXPLAIN ANALYZE
SELECT id, heading, 1 - (embedding <=> (SELECT embedding FROM chunks LIMIT 1)) AS score
FROM chunks
WHERE tenant_id = 'c69cf31f-3e72-567f-bf70-f833be7a5ca9'
ORDER BY embedding <=> (SELECT embedding FROM chunks LIMIT 1)
LIMIT 5;
```
Result:
- **Plan**: `Index Scan using ix_chunks_embedding_hnsw on chunks`
- **Filter**: `(tenant_id = 'c69cf31f-3e72-567f-bf70-f833be7a5ca9'::uuid)`
- **Execution Time**: `1.565 ms`

**Next step (Task 3):** Implement `POST /api/v1/search` to embed user query at runtime and query `chunks` table with tenant isolation.

### Step 5. Build `POST /api/v1/search` and verify isolation & cascade (tasks 3, 6, 7)
- **Schemas**: `src/ai_service/schemas/search.py` (`SearchRequest`, `SearchResultItem`, `SearchResponse`)
- **Service**: `src/ai_service/services/search_service.py` (`embed_query` with Jina embeddings + `search_chunks` with `Chunk.embedding.cosine_distance(query_vector)` and tenant filter)
- **API Endpoint**: `src/ai_service/api/v1/search.py` mounted at `POST /api/v1/search`
- **Tests**: `tests/test_search.py` covering:
  1. `test_search_requires_auth`: 401 when unauthorized
  2. `test_search_validation_errors`: 422 on blank query or out-of-range limits
  3. `test_search_empty_tenant`: 200 with `total=0, results=[]` for empty tenant
  4. `test_search_matches_and_threshold`: returns matching chunks with cosine scores, filters chunks below `threshold`
  5. `test_tenant_isolation` (Task 6): Tenant B searching with the exact vector of Tenant A's chunk gets 0 results; Tenant A gets 1 result. Tenant boundary strictly enforced in SQL query.
  6. `test_document_cascade_deletion` (Task 7): Deleting a `Document` cascades and removes all associated `chunks` in DB.

```bash
uv run pytest tests/test_search.py -v
```
Output:
```
tests/test_search.py::test_search_requires_auth PASSED                   [ 16%]
tests/test_search.py::test_search_validation_errors PASSED               [ 33%]
tests/test_search.py::test_search_empty_tenant PASSED                    [ 50%]
tests/test_search.py::test_search_matches_and_threshold PASSED           [ 66%]
tests/test_search.py::test_tenant_isolation PASSED                       [ 83%]
tests/test_search.py::test_document_cascade_deletion PASSED              [100%]
======================= 6 passed in 3.15s =======================
```

PostgreSQL pgvector smoke test query on real corpus:
```sql
SELECT chunks.id, chunks.heading, 1.0 - (chunks.embedding <=> $1) AS score
FROM chunks
WHERE chunks.tenant_id = 'c69cf31f-3e72-567f-bf70-f833be7a5ca9'
ORDER BY chunks.embedding <=> $1
LIMIT 3;
```
Result: 3 chunks returned with high accuracy and cosine score ranking.


### Step 6. Verify task 3 on the real database, the real Jina API and over HTTP (2026-11-25)
**Why:** the tests run on SQLite, which uses a separate Python code path. They never run the pgvector query.

**1. Containers and data**
```
docker compose up -d --build api       -> health: 200 ; JINA_API_KEY is set inside the container
alembic_version                        -> dc013ced71fa
chunks | tenants | documents | models | null vectors   -> 2097 | 1 | 234 | jina-embeddings-v3 | 0
chunks whose tenant differs from the document's tenant -> 0
users in 'Corpus Tenant'               -> 0      (nobody can log in and search the corpus yet)
```
All 2,097 chunks were compared with the Day 9 files by section id: **0 vectors and 0 texts differ** (largest difference 0.0).

**2. The real query.** A temporary script (not in the repo) called the two functions the endpoint calls, `embed_query` (real Jina call: 921 ms and 509 ms) and `search_chunks`, against PostgreSQL.
```
[A] Corpus tenant: "How do I define a path parameter in FastAPI?"      -> 5 results
    0.7603  Query Parameters > Multiple path and query parameters
    0.7543  Path Parameters and Numeric Validations
    0.6954  Path Parameters > Path parameters containing paths
    0.6832  Bigger Applications - Multiple Files > The main FastAPI > Include a path operation
    0.6804  Using the Request Directly > Use the Request object directly

[B] Corpus tenant: "अलग वेबसाइट से आने वाली अनुरोध को मेरा सर्वर कैसे स्वीकार करे?"   -> 3 results
    0.5092  CORS (Cross-Origin Resource Sharing) > Steps
    0.4959  CORS (Cross-Origin Resource Sharing) > Wildcards
    0.4722  CORS (Cross-Origin Resource Sharing) > Origin
    query words found anywhere in the top result's whole document: none of the 12

[C] a tenant with no chunks   -> 0 results
[D] a made-up tenant id       -> 0 results
```
- [A] gives the same top scores as the NumPy search of Day 9.
- [B] is the plan's **"done when"**: the CORS page was found with a question that shares no word with it.

**3. A small tenant (a risk found).** Inside a transaction that was rolled back, a tenant got 3 chunks next to the 2,097 of the Corpus Tenant, and asked for 5.

| Plan | Results |
|---|---|
| planner free to choose (it used `ix_chunks_tenant_id`, an exact search of that tenant) | **3** (correct) |
| HNSW index forced, `ef_search = 40` | **0** |
| HNSW index forced, `hnsw.iterative_scan = relaxed_order` | **3** |

With the HNSW index, the tenant filter is applied after the index returns its candidates, so a small tenant can get nothing although it has matching chunks. Today the planner picks the exact route. `search_chunks` does not set `hnsw.iterative_scan`, so if the planner chose the HNSW index (the forced rows show what happens), results would be lost. The plan switches used to force it are only for this test. After the rollback: 2,097 chunks, no temporary rows.

**4. Real HTTP requests** to `POST /api/v1/search`, logged in as a test user whose organisation has no chunks:

| Request | Status | Response |
|---|---|---|
| no token, `{"query":"path parameter"}` | 401 | `{"error":{"code":"NOT_AUTHENTICATED","message":"Not authenticated.", ...}}` |
| `{"query":"   "}` | 422 | `{"error":{"code":"VALIDATION_ERROR", ..., "message":"Value error, Query must not be empty."}}` |
| `{"query":"How do I define a path parameter in FastAPI?","limit":5}` | 200 | `{"query":"How do I define a path parameter in FastAPI?","results":[],"total":0}` |
| the same plus `"tenant_id":"c69cf31f-..."` (the Corpus Tenant) | 200 | the same empty result: the extra field is ignored, the tenant comes from the login |

The HTTP search that returns corpus results is in Step 7.

`.env.example` now lists `JINA_API_KEY`, `JINA_EMBEDDING_MODEL` and `JINA_EMBEDDING_URL`.

### Step 7. HTTP search over the corpus, with a user of the Corpus Tenant (2026-11-25)
**Why:** the HTTP requests of Step 6 used a tenant with no chunks, so none of them returned a result.

The app's sign-up always creates a new organisation, so I added one local test user (`corpus-test@example.com`) to the Corpus Tenant directly in my local database. Its generated password is in `.env.corpus-test`, which is ignored by git. I also added `.env.*` to `.dockerignore`, so that file is not copied into the API image.

```
POST /api/v1/auth/login   -> 200   (response keys: access_token, token_type, expires_in)
POST /api/v1/search       {"query": "...", "limit": 5}   with  Authorization: Bearer <token>
```

| Query | Status | Time | Results |
|---|---|---|---|
| "How do I define a path parameter in FastAPI?" | 200 | 592 ms | 5, the same scores and order as Step 6 [A]: 0.7603, 0.7543, 0.6954, 0.6832, 0.6804 |
| the Hindi CORS question of Step 6 [B] | 200 | 532 ms | 5, all from `fastapi-docs/tutorial/cors.md`; the first three are the same as in Step 6: 0.5092, 0.4959, 0.4722, then 0.4713, 0.4487 |

**One result as the API returns it** (first result of the Hindi question; `text` shortened here):
```json
{
  "id": "dd6e2080-50cd-50d9-b777-19a78257bf7c",
  "document_id": "b7866853-f93d-56dc-a8da-401ca67a561a",
  "chunk_index": 2,
  "heading": "CORS (Cross-Origin Resource Sharing) > Steps",
  "text": "## Steps  So, let's say you have a frontend running in your browser at `http://localhost:8080`, and its JavaSc... (768 chars)",
  "score": 0.5092,
  "metadata": {
    "title": "CORS (Cross-Origin Resource Sharing)",
    "words": 114,
    "source": "fastapi-docs",
    "licence": "MIT",
    "section_id": "fastapi-docs/tutorial/cors.md#003-steps"
  }
}
```
The response around the results is `{"query": ..., "results": [...], "total": 5}`.

**One word changes the ranking.** With "How do I **declare** a path parameter in FastAPI?" the first three results stayed the same with lower scores (0.7568, 0.7431, 0.6924), and the 4th and 5th changed places (0.6790, 0.6699). So scores can only be compared between runs when the query text is exactly the same.

**What it means:** the whole path works over HTTP: login, the tenant taken from the token, the query embedding from Jina, the pgvector search and the response. Task 3 is verified end to end.

### Step 8. Task 4, first part: recall@5 with exact search (the baseline)
**Why:** HNSW is an *approximate* search. To know what it loses, I first need the answer of an **exact** search, which compares the query with every chunk.

**1. The queries.** The 20 labelled queries of Day 9 (15 English, 4 Hinglish, 1 Hindi), copied unchanged:
```
cp ../ai-eng-learning/day-9/scripts/queries.json scripts/data/search_queries.json
```
**2. The rule** (the same as Day 9): a query is a **hit** when at least one of its top 5 chunks belongs to one of its `correct_docs`. `recall@5 = hits / 20`. With this rule, finding one correct chunk is enough; it does not count how many of the correct chunks were found.

**3. The script** `scripts/eval_search.py`:
- searches with the app's own `search_chunks` function (the one `POST /api/v1/search` uses), on PostgreSQL, as the Corpus Tenant;
- saves the query embeddings in `scripts/results/day10_query_vectors.json`, so later runs use exactly the same vectors and make no API call;
- makes the search exact with `set local enable_indexscan = off` (an HNSW index can only be read by an index scan), checks the plan and stops if HNSW is still in it; the setting ends with the transaction;
- stops if a labelled document is not in the database.

**4. Run**
```
uv run python scripts/eval_search.py
```
```
query vectors: 20 from the API, 0 from the file
plan: Seq Scan on chunks

 id  language  hit@5  first correct rank  top score  query
  1  en        yes            3           0.7603     How do I define a path parameter in FastAPI?
  2  en        yes            1           0.6973     How do I validate request body fields with Pydantic?
  3  en        yes            2           0.8595     What is dependency injection in FastAPI?
  4  en        yes            1           0.7286     How do I return a streaming response from a FastAPI route?
  5  en        yes            1           0.6547     How do I write an async endpoint in FastAPI?
  6  en        yes            1           0.7964     How do I add middleware to a FastAPI application?
  7  en        yes            3           0.7979     How does Pydantic handle field validation errors?
  8  en        yes            1           0.7225     What is a Pydantic BaseModel and how do I use it?
  9  en        yes            1           0.7190     How do I use computed fields in Pydantic v2?
 10  en        yes            1           0.8442     How do I configure Pydantic settings from environment variables?
 11  en        yes            1           0.5579     What is retrieval-augmented generation and why does it reduce hallucinations?
 12  en        yes            1           0.7959     How does the size of a text chunk affect retrieval-augmented generation?
 13  en        yes            2           0.7370     What is the solubility product constant (Ksp) of silver chloride?
 14  en        NO             -           0.5488     What is the standard enthalpy of formation of liquid water?
 15  hinglish  yes            1           0.7344     FastAPI mein OAuth2 password bearer kaise use karte hain?
 16  hinglish  yes            2           0.7724     Pydantic model mein optional field kaise banate hain?
 17  hinglish  yes            1           0.7795     FastAPI ke saath background tasks kaise chalate hain?
 18  hinglish  yes            1           0.3588     Le Chatelier ka principle kya hai?
 19  hindi     yes            1           0.7421     FastAPI में CORS कैसे enable करें?
 20  en        yes            1           0.7367     How does Matryoshka-Adaptor produce smaller embedding dimensions?

exact search recall@5: 19/20 = 95%
saved: scripts/results/day10_exact_search.json
```
A second run printed `query vectors: 0 from the API, 20 from the file` and wrote an identical results file.

**5. The one miss, Q14** (top 5):
```
0.5488  chapter-5-thermochemistry        5.3 Enthalpy, part 4 of 5
0.5361  chapter-7-chemical-bonding...    7.5 Strengths of I...
0.5105  chapter-10-liquids-and-solids    10.3 Phase Transitions, part 3 of 4
0.5095  appendix-e-water-properties      pages 1091-1092
0.4838  chapter-5-thermochemistry        5.3 Enthalpy, part 3 of 5
```
The labelled page is Appendix G (a table of numbers). On Day 9 I traced this to the table text itself (`ai-eng-learning/day-9/Notes/13`).

**What it means**
- The rank of the first correct chunk (within the top 5) is the same as in the Day 9 NumPy search for all 20 queries. So moving the vectors into PostgreSQL did not change these results.
- 14 queries have a correct chunk at rank 1, 3 at rank 2, 2 at rank 3, and 1 misses. By language: English 14/15, Hinglish 4/4, Hindi 1/1 (very few Hindi and Hinglish queries).
- A low score can still be correct: Q18 is right at rank 1 with **0.3588**, while the wrong top result of Q14 scores 0.5488. One fixed score threshold could not separate these two.
- **19/20 is the baseline that HNSW is compared with.** The next comparison is not only "how many hits" but also whether HNSW returns the same 5 chunks as this exact search, because 20 queries can hide a difference.
- The script's lint check passes (`ruff check scripts/eval_search.py`). The existing tests still pass: `231 passed`. The script itself has no unit test.

**Next:** run the same queries through the HNSW index and compare with `day10_exact_search.json`.

### Step 9. Task 4, second part: HNSW against exact search
**Why:** HNSW does not compare the query with every chunk, so it can miss some of the true nearest ones. I measure how many it misses, and what the speed gain is.

**1. The script** `scripts/compare_hnsw.py` (a new file, so Step 8 can still be run as it was). It runs the same 20 queries through `search_chunks` in three modes, each on its own database connection:

| Mode | Settings (`set local`, they end with the transaction) |
|---|---|
| `exact` | `enable_indexscan = off` |
| `planner` | none: this is what PostgreSQL does for the API today |
| `hnsw` | `enable_seqscan = off`, `enable_bitmapscan = off`, `enable_sort = off` (to force the HNSW index) |

`hnsw.ef_search` is the default, 40, and `hnsw.iterative_scan` is not set.

**2. Proof of which index ran.** A plan shown by `EXPLAIN` for another statement is not proof of what these searches did. So the script reads PostgreSQL's usage counters (`pg_stat_user_indexes`, `pg_stat_user_tables`) before and after each mode. Each mode makes 220 searches: 1 round for the results and 10 timed rounds.

**3. Run**
```
uv run python scripts/compare_hnsw.py
```
```
query vectors: 0 from the API, 20 from the file

[exact]  220 searches
  index use         : {'full table scan': 220}
  recall@5          : 19/20 = 95%
  same as exact     : 100/100 chunks; 20/20 queries with the same 5 in the same order
  differing queries : none
  unstable queries  : 0
  one search        : median 16.76 ms, p95 19.79 ms (200 timed)

[planner]  220 searches
  index use         : {'ix_chunks_embedding_hnsw': 220}
  recall@5          : 19/20 = 95%
  same as exact     : 100/100 chunks; 20/20 queries with the same 5 in the same order
  differing queries : none
  unstable queries  : 0
  one search        : median 3.3 ms, p95 4.0 ms (200 timed)

[hnsw]  220 searches
  index use         : {'ix_chunks_embedding_hnsw': 220}
  recall@5          : 19/20 = 95%
  same as exact     : 100/100 chunks; 20/20 queries with the same 5 in the same order
  differing queries : none
  unstable queries  : 0
  one search        : median 3.39 ms, p95 4.0 ms (200 timed)

saved: scripts/results/day10_hnsw_comparison.json
```

**4. In one table**

| | recall@5 | Chunks shared with exact | Median | p95 |
|---|---|---|---|---|
| exact | 19/20 | 100/100 (the baseline) | 16.76 ms | 19.79 ms |
| HNSW, `ef_search = 40` | 19/20 | 100/100, same order | 3.39 ms | 4.0 ms |

The times are for one `search_chunks` call seen from Python (SQL, transfer and building the results), after a warm-up round. They do not include embedding the query, which is an API call of about 0.5 to 0.9 s (Step 6). They come from one run on my laptop.

**What it means**
- **At the default `ef_search = 40`, HNSW lost nothing on these queries:** the same 5 chunks in the same order as exact search for all 20. Its median search time was about 5 times lower (3.39 against 16.76 ms).
- **The API already uses HNSW for the Corpus Tenant:** with no settings changed, all 220 searches used `ix_chunks_embedding_hnsw`. In Step 6 the planner used the tenant index for a tenant with only 3 chunks. So the choice depends on the query's tenant, and I should not assume one plan for all tenants.
- The 13 ms saved is small next to the embedding call. The gap between exact and HNSW should grow with more chunks, but I did not measure that.
- **Limits:** 2,097 vectors is a small index and 20 queries is a small set. The Corpus Tenant owns all the chunks, so the tenant filter removed nothing here. This result does not cover the small-tenant case of Step 6.
- **For the next step:** a higher `ef_search` cannot return more of the exact top 5, because 40 already returns all of them. So the tuning has to go **lower** too, to find where HNSW starts to lose chunks, and what each value costs in time.

### Step 10. Task 4, third part: tuning `hnsw.ef_search`
**What the setting is:** while HNSW walks its graph, it keeps a list of the best candidates found so far. `ef_search` is the size of that list (default 40). A bigger list looks at more of the graph: less chance to miss a near chunk, more work. It is a setting of the **search**, so I can change it without rebuilding the index.

**1. The script** `scripts/tune_ef_search.py`: the HNSW index is forced (as in Step 9) and only `ef_search` changes: 5, 10, 20, 40, 100, 200, 400. For each value it runs the 20 queries (1 round for the results, 10 timed rounds), and also asks for 50 results per query, because the API allows `limit` up to 50.
```
set local hnsw.ef_search = 100;     -- for one transaction
```
**2. Run** (I ran it twice)
```
uv run python scripts/tune_ef_search.py
```
```
ef_search  exact top-5 chunks  recall@5  median ms  p95 ms  rows for limit=50  HNSW used  queries that lost chunks
        5          93/100      18/20          3.65    4.51         5-5               240  {11: 1, 14: 1, 17: 5}
       10         100/100      19/20          3.79    4.59        10-10              240  -
       20         100/100      19/20          3.63    3.97        19-20              240  -
       40         100/100      19/20          3.61    3.98        39-40              240  -
      100         100/100      19/20          4.22     7.2        50-50              240  -
      200         100/100      19/20          4.41    4.99        50-50              240  -
      400         100/100      19/20          5.07     6.0        50-50              240  -
```
Everything except the times was the same in the second run. Its times were higher at every value:

| `ef_search` | 5 | 10 | 20 | 40 | 100 | 200 | 400 |
|---|---|---|---|---|---|---|---|
| median ms, run 1 | 3.65 | 3.79 | 3.63 | 3.61 | 4.22 | 4.41 | 5.07 |
| median ms, run 2 | 4.74 | 4.47 | 4.70 | 4.96 | 5.27 | 5.84 | 6.10 |

`HNSW used 240` = 220 searches plus the 20 searches with `limit=50`, all through `ix_chunks_embedding_hnsw`.

**3. What it shows**
- **Too small loses results, without any error.** At `ef_search = 5`, 7 of the 100 exact chunks were missing. For Q17 ("FastAPI ke saath background tasks kaise chalate hain?") **all 5** were missing: it returned 5 other pages (FastAPI CLI, sub applications, deployment), so recall@5 fell to 18/20. The response still had 5 results, so nothing showed that they were the wrong ones.
- **From 10 upwards, the top 5 was complete:** 100/100. Going higher cannot improve it.
- **Time:** from 5 to 40 the differences are smaller than the difference between my two runs. From 100 to 400 the median rose in both runs. At this size the cost is about 1.5 ms between 40 and 400.
- **`ef_search` also limits how many results can come back.** Asking for 50: 5 rows at 5, 10 at 10, 19 to 20 at 20, **39 to 40 at the default 40**, and 50 from 100 upwards. I measured this with the index **forced**. **Correction (Step 11):** I first wrote here that `POST /api/v1/search` with `"limit": 50` could return only about 40. That was wrong for the live API: when I sent the request, it returned 50, because PostgreSQL used an exact scan for that limit.

**4. My choice**
`ef_search = 100` for this app: the top 5 is complete with a wide margin above 10, a request for 50 gets 50 results, and it cost about 0.3 to 0.6 ms more than 40 in my two runs. I would not choose 10 only because it was enough here: 20 queries on 2,097 chunks is too small to trust the lowest value that worked.

**Applied in Step 11.** When this step was measured, `search_chunks` used the default 40.

**Limits:** one tenant owning all chunks, a small index, 20 queries, times from my laptop. I tuned only the search setting; I did not change `m` or `ef_construction` (they need an index rebuild). The best value has to be measured again when the corpus grows. This setting also does not replace `hnsw.iterative_scan` for the small-tenant case of Step 6, which I did not retest here.

### Step 11. Apply the two search settings in `search_chunks`
**1. Before the change, over HTTP.** `"limit": 50` returned **50** results, not about 40 as I had expected. The index usage counters showed why (one request per limit, same query, Corpus Tenant):

| `limit` | What PostgreSQL used |
|---|---|
| 5, 10, 20 | `ix_chunks_embedding_hnsw` |
| 40, 41, 45, 50 | full table scan (exact) |

For this query the planner chose an exact scan from 40 upwards (I did not test between 20 and 40). So the live API did not lose rows here. The risk remains whenever PostgreSQL **does** use HNSW with a large limit or a tenant filter, and I do not control that choice.

**2. The change** (`src/ai_service/services/search_service.py`):
- `HNSW_EF_SEARCH = 100` and `HNSW_ITERATIVE_SCAN = "relaxed_order"`, set just before the search with `set_config(..., true)`. `true` means only for this transaction.
- `relaxed_order` can return rows slightly out of distance order, so the results are sorted by score again.
- Both are keyword arguments with these defaults, so the measurement scripts can pass the old values (`40`, `off`) and still measure what Steps 8 to 10 describe.
- The SQLite path of the tests is unchanged.

**3. On PostgreSQL, with the HNSW index forced** (a temporary script, not in the repo; the 20 query vectors):
```
[A] HNSW forced, limit=50, old values   -> rows per query 39-40 | sorted: True | settings in the transaction: 40 / off
[A] HNSW forced, limit=50, new defaults -> rows per query 50-50 | sorted: True | settings in the transaction: 100 / relaxed_order
[B] same pooled connection, next transaction -> 40 / off
[C] HNSW forced, tenant with 3 chunks, 20 queries, old values   -> rows per query 0-0 | rows of another tenant: 0
[C] HNSW forced, tenant with 3 chunks, 20 queries, new defaults -> rows per query 3-3 | rows of another tenant: 0
[D] after rollback: chunks = 2097 | temporary tenants = 0
```
- [A] a request for 50 now gets 50 through HNSW.
- [B] the settings do not stay on the pooled connection.
- [C] the small tenant of Step 6 now gets its 3 chunks for all 20 queries, and never a row of another tenant.

**4. Over HTTP after restarting the API** (`docker compose restart api`):
```
limit=5  -> 200, total=5,  first score 0.7603, sorted by score: True
limit=50 -> 200, total=50, first score 0.7603, sorted by score: True
```
The planner's choices were the same as in the table above.

**5. Nothing else changed**
```
ruff check (the 4 changed files)  -> All checks passed
mypy src tests                    -> Success: no issues found in 76 source files
pytest -q                         -> 231 passed
```
I ran `eval_search.py`, `compare_hnsw.py` and `tune_ef_search.py` again: their results were the same as the saved ones apart from the times. I kept the saved files, so they still match the times in Steps 8 to 10.

**Limits:**
- The tests run on SQLite, so **no automated test covers these two settings**. The checks above are manual.
- Iterative scan stops at `hnsw.max_scan_tuples` (20,000 by default). My table is far smaller, so a small tenant in a much larger table could still get fewer rows than it asks for. I did not test that.
- With a high `threshold`, an iterative scan may walk much further before it gives up. I did not measure that.
- I did not measure the speed with iterative scan on.

### Step 12. Tasks 6 and 7 on the real PostgreSQL and the running API
**Why:** the two tests of Step 5 run on SQLite, where the search is a separate Python path. They say nothing about the pgvector query, its indexes, or the cascade in PostgreSQL.

**1. The script** `scripts/verify_isolation.py`:
- signs up a **temporary** organisation through `POST /auth/register` (random password, never printed or saved);
- [A] searches while that tenant has **zero documents**;
- [B] uploads a small document through the API and gives it **exact copies of 5 CORS chunks** of the Corpus Tenant: same text, same vectors. This is the hardest case, because identical vectors then exist in both tenants. (The chunks are inserted with SQL, because ingestion is Day 11.);
- [C] deletes the document with `DELETE /api/v1/documents/{id}`;
- removes the temporary organisation, also when a check fails, and checks that the table counts are as before.

It searches with 225 vectors (the 20 queries, the vectors of 200 corpus chunks, and the 5 copied vectors), each with the **exact**, **planner-chosen** and **forced HNSW** plans, plus 3 real queries over HTTP.

**2. Run**
```
uv run python scripts/verify_isolation.py
```
```
query vectors: 0 from the API, 20 from the file
before: tenants, users, documents, chunks = (4, 4, 239, 2097)

[A] Task 6: tenant 2 has ZERO documents (225 vectors)
  PASS  exact: rows returned to tenant 2                           0 rows
  PASS  planner: rows returned to tenant 2                         0 rows
  PASS  hnsw: rows returned to tenant 2                            0 rows
  PASS  HTTP, tenant 2: How do I define a path parameter in Fast   0 results
  PASS  HTTP, tenant 2: FastAPI में CORS कैसे enable करें?         0 results
  PASS  HTTP, tenant 2: What is the solubility product constant    0 results

[B] Task 6: tenant 2 now owns copies of 5 corpus chunks
  PASS  exact: tenant 2 gets only its own chunks                   0 foreign rows; rows per search [5]
  PASS  planner: tenant 2 gets only its own chunks                 0 foreign rows; rows per search [5]
  PASS  hnsw: tenant 2 gets only its own chunks                    0 foreign rows; rows per search [5]
  PASS  exact: the Corpus Tenant never gets tenant 2's copies      0 leaked rows in 11250
  PASS  planner: the Corpus Tenant never gets tenant 2's copies    0 leaked rows in 11250
  PASS  hnsw: the Corpus Tenant never gets tenant 2's copies       0 leaked rows in 11250
  PASS  HTTP, both tenants: How do I define a path parameter in    tenant 2: 5 own; corpus: 50, 0 of tenant 2
  PASS  HTTP, both tenants: FastAPI में CORS कैसे enable करें?     tenant 2: 5 own; corpus: 50, 0 of tenant 2
  PASS  HTTP, both tenants: What is the solubility product const   tenant 2: 5 own; corpus: 50, 0 of tenant 2

[C] Task 7: delete the document
  PASS  chunks of the document before the delete                   5 rows
  PASS  DELETE by another tenant is refused and removes nothing    HTTP 404; 5 rows left
  PASS  DELETE by the owner                                        HTTP 204
  PASS  chunk rows (and vectors) of the document                   0 rows
  PASS  exact: searching with the deleted vectors                  0 rows
  PASS  planner: searching with the deleted vectors                0 rows
  PASS  hnsw: searching with the deleted vectors                   0 rows
  PASS  HTTP, tenant 2: How do I define a path parameter in Fast   0 results
  PASS  HTTP, tenant 2: FastAPI में CORS कैसे enable करें?         0 results
  PASS  HTTP, tenant 2: What is the solubility product constant    0 results
  PASS  the Corpus Tenant's chunks are untouched                   2097 rows

after clean-up: tenants, users, documents, chunks = (4, 4, 239, 2097)
  PASS  the database counts are the same as before                 (4, 4, 239, 2097) -> (4, 4, 239, 2097)

ALL CHECKS PASSED
```
(The same output is saved in `scripts/results/day10_isolation_deletion.txt`.)

**3. What it proves**
- **Zero documents:** no vector, plan or HTTP query returned a single row.
- **Identical vectors in two tenants:** tenant 2 got exactly its own 5 chunks in every search, although 50 were asked for. In 11,250 result rows per plan, the Corpus Tenant never got one of tenant 2's copies. The tenant filter is inside the SQL query, and over HTTP the tenant comes from the login token.
- **Deletion:** the document had 5 chunks; after the owner's `DELETE` there were 0, checked on a new database connection. Searching with exactly the deleted vectors returned nothing under all three plans. The 5 results returned **before** the delete show that the empty result afterwards is not an empty test.
- **Another tenant cannot delete it:** HTTP 404, and the 5 rows stayed.
- The vector is a column of the `chunks` row, so the foreign key `ON DELETE CASCADE` removes it in the same transaction as the document. There is no second store to keep in step. (With Qdrant, task 5, there will be one.)

**Limits:**
- "Disappear" means: the rows are deleted and no search returns them. PostgreSQL keeps dead rows and index entries on disk until `VACUUM` cleans them; I did not measure that. So this is not proof of physical erasure.
- It is one run, on my local database. The script is a manual check, not part of `pytest`, because it needs PostgreSQL and the running API.
- It covers the search and delete routes only.

### Step 13. Task 5, first part: Qdrant in Docker Compose
**Why:** task 5 is to run the same search on a second vector store. First the store itself has to run. No application code changes in this step.

**1. Download the image** (from Docker Hub; about 74 MB compressed, 282 MB on disk)
```
docker pull qdrant/qdrant:latest
docker run --rm --entrypoint ./qdrant qdrant/qdrant:latest --version     -> qdrant 1.20.2
docker pull qdrant/qdrant:v1.20.2
Digest: sha256:d277592721d2e3b73f9b797b53cf05e6a5bb2134f6d13fdbda4ca51782afbcac   (the same for both tags)
```
I put **`v1.20.2`** in the compose file, not `latest`, so a later pull cannot change the version without me noticing.

**2. The new service** in `docker-compose.yml`:
```yaml
  qdrant:
    image: qdrant/qdrant:v1.20.2
    container_name: ai-service-qdrant
    restart: unless-stopped
    ports:
      - "127.0.0.1:6333:6333"
    environment:
      QDRANT__TELEMETRY_DISABLED: "true"
    volumes:
      - qdrant_data:/qdrant/storage
    healthcheck:
      test: ["CMD", "bash", "-c", 'exec 3<>/dev/tcp/127.0.0.1/6333 && printf "GET /readyz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n" >&3 && grep -q "200 OK" <&3']
      interval: 5s
      timeout: 5s
      retries: 10
```
(and `qdrant_data:` under `volumes:`)

| Setting | Why |
|---|---|
| `127.0.0.1:6333:6333` | reachable only from my computer. This Qdrant has **no API key**, so anyone who could reach the port could read or delete the data |
| `QDRANT__TELEMETRY_DISABLED` | no anonymous usage reports |
| `qdrant_data` volume | the data survives a container being recreated |
| health check with `bash` | the image has no `curl` and no `wget` |
| port 6334 (gRPC) not published | my plan is to use the HTTP API |

**3. Start and check**
```
docker compose config -q            -> the file is valid
docker compose up -d qdrant         -> Started ; health: healthy
curl localhost:6333/                -> {"title":"qdrant - vector search engine","version":"1.20.2",...}
curl localhost:6333/readyz          -> all shards are ready  (HTTP 200)
curl localhost:6333/collections     -> {"result":{"collections":[]},"status":"ok",...}
docker exec ai-service-api python -c "...urlopen('http://qdrant:6333/collections')..."
                                    -> {"result":{"collections":[]},"status":"ok",...}
docker port ai-service-qdrant       -> 6333/tcp -> 127.0.0.1:6333
docker logs ai-service-qdrant       -> "Version: 1.20.2" , "Telemetry reporting disabled"
docker stats                        -> 59.4 MiB of memory (empty)
```
**The health check can also fail:** the same command with a path that does not exist ended with exit code 1. So "healthy" means Qdrant answered `200 OK`.

**What it means**
- Inside Docker the API reaches Qdrant at `http://qdrant:6333`; from my computer it is `http://localhost:6333`.
- Qdrant has no collections yet: nothing is copied into it.
- The `api` service does not depend on `qdrant`, and the app still uses pgvector only. A plain `docker compose up` now starts this extra container too.

### Step 14. Task 5, second part: the Qdrant Python client
**1. Install** (`uv pip install --dry-run qdrant-client` first showed what it would add, without installing anything)
```
uv add qdrant-client
```
```
 + grpcio==1.85.4
 + h2==4.4.1
 + hpack==4.2.0
 + hyperframe==6.1.0
 + portalocker==4.4.2
 + protobuf==7.36.2
 + qdrant-client==1.20.0
```
`pyproject.toml` got one line, `"qdrant-client>=1.20.0"`. Comparing `uv.lock` before and after, these 7 packages are the only ones added, and no other package changed version.

**2. A smoke test against my Qdrant server** (a temporary script, not in the repo): a temporary collection with vectors of 4 numbers and cosine distance, and 3 points. Tenant B's point has **the same vector** as one of tenant A's points. The query is that vector, with a filter on `tenant_id`:
```
filter tenant=A      -> [('A-near', 1.0), ('A-far', 0.0)]
filter tenant=B      -> [('B-identical', 1.0)]
filter tenant=nobody -> []
collections afterwards: []
client 1.20.0 | server 1.20.2
warnings from the client: none
```
- The filter is part of the query (`query_filter=`), like the `WHERE tenant_id = ...` of the SQL search.
- With cosine distance, Qdrant's `score` is already a **similarity** (1.0 for the same vector). pgvector's `<=>` returns a **distance**, so my SQL does `1 - distance`. Both can give the same kind of score to the API.
- This is a 3-point test. It is not the isolation test of the real Qdrant search.

**3. Rebuild the API image** (it installs the packages when it is built):
```
docker compose up -d --build api
```
```
api health: healthy
inside the api container: qdrant-client 1.20.0
all shards are ready                              (asked for from inside the api container)
```
No `.env*` file is in `/app` of the container.

**4. Nothing broke**
```
POST /api/v1/search   limit=5 -> 200, 5 results ; limit=50 -> 200, 50 results ; first score 0.7603
mypy src tests        -> Success: no issues found in 76 source files
pytest -q             -> 231 passed
verify_isolation.py   -> ALL CHECKS PASSED  (counts the same before and after: 4, 4, 239, 2097)
```

**Next:** the `VectorStore` protocol, with today's pgvector search behind it first. No behaviour should change.

### Step 15. Task 5, third part: the `VectorStore` protocol, with pgvector behind it
**Why:** the endpoint called the pgvector function directly. To switch stores by config, it has to depend on an interface that both stores follow. I do this first, with only pgvector, so that any change in results would come from this restructuring and not from Qdrant.

**1. The interface** (new file `src/ai_service/services/vector_store.py`):
```python
class VectorStore(Protocol):
    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        query_vector: list[float],
        limit: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResultItem]: ...
```
Its docstring writes down what every store must keep: only the given tenant's chunks, filtered inside the store's own query; `tenant_id` required; highest score first; the score is cosine similarity; nothing below the `threshold`.

- A `Protocol` needs no inheritance: any class with a matching `search` fits, and `mypy` checks it where the class is used as a `VectorStore`.
- `PgVectorStore(session)` passes the call to the existing `search_chunks`. I kept that function, because the measurement scripts call it with `ef_search` and `iterative_scan`. These exist only in pgvector, so they are not in the interface.
- **Only `search` for now.** Adding and deleting vectors come with Qdrant: pgvector gets deletion from the foreign key cascade, a second store will not.

**2. The wiring**
- `api/deps.py`: `get_vector_store(session) -> VectorStore` returns `PgVectorStore`, and `VectorStoreDep` is the dependency. There is **no config switch yet**, because there is only one store.
- `api/v1/search.py`: asks for `VectorStoreDep` and calls `store.search(...)`. It no longer takes a database session itself. The tenant still comes from `user.tenant_id`.

**3. A new test**, `test_search_goes_through_the_vector_store`: it replaces the store with a fake one and checks that the response comes from the fake, and that the store received the logged-in user's tenant, the limit and the threshold, although the request body also sent another `tenant_id`.

**4. Same behaviour**
I saved 9 HTTP responses **before** the change (3 queries, each with `limit` 5, 50, and 50 with `threshold` 0.4; one of the 9 returns no results) and sent the same requests after restarting the API:
```
all 9 HTTP responses identical before and after (byte for byte)
PgVectorStore vs the saved exact results: 20/20 queries identical (ids, scores, order); 100/100 chunks
verify_isolation.py -> ALL CHECKS PASSED
```
The second line compares with the exact-search results of Step 8.

**5. Checks**
```
ruff check (the new and changed files)  -> All checks passed   (after fixing the import order in the test file)
mypy src tests                          -> Success: no issues found in 77 source files
pytest -q                               -> 232 passed          (231 + the new one)
```

**Next:** `QdrantStore` behind the same interface, copying the 2,097 chunks into Qdrant, and the config switch.

### Step 16. Task 5, fourth part: `QdrantStore` and the copy of the corpus
**1. The interface got two more methods**, because a second store has to be written to and deleted from:
`upsert(records)` and `delete_document(tenant_id=..., document_id=...)`. `VectorRecord` is one chunk with its vector.
- `PgVectorStore` implements them on the `chunks` table. They belong to the caller's database transaction.
- Qdrant applies a write at once and cannot roll it back. I wrote this difference into the protocol's docstring.

**2. `QdrantStore`** (`services/qdrant_store.py`)
| Point | How |
|---|---|
| one collection `chunks` | size 1024, cosine distance |
| tenant isolation | `tenant_id` in the payload; every `search` and `delete_document` filters on it **inside** the Qdrant query |
| payload indexes | `tenant_id` (with `is_tenant=True`, which tells Qdrant to keep a tenant's points together) and `document_id` |
| payload | also the text, heading, hash, model and metadata, so a search needs no second query |
| score | Qdrant's cosine score is already the similarity |
| writes | batches of 256 with `wait=True` |

New settings: `QDRANT_URL` (first `http://localhost:6333`, changed to `http://127.0.0.1:6333` in Step 19) and `QDRANT_COLLECTION` (`chunks`), also in `.env.example`. PostgreSQL stays the main copy; Qdrant can be rebuilt from it. The price of copying the text into Qdrant: two copies that must be kept in step.

**3. Contract tests** (`tests/test_vector_store.py`): 6 rules of the protocol, each run against **both** stores: isolation, order and limit, threshold, upsert replaces, delete removes only that document, another tenant cannot delete it.
```
uv run pytest tests/test_vector_store.py -v     -> 12 passed
pytest -q                                       -> 244 passed
mypy src tests                                  -> Success: no issues found in 79 source files
```
`PgVectorStore` runs on SQLite there and Qdrant in the client's in-memory mode, so these tests check the contract and **not the two real servers**. (The in-memory mode warns that payload indexes have no effect.)

**4. Copy the corpus** (`scripts/copy_to_qdrant.py`). It reads the `chunks` table, upserts every row, removes points that are no longer in PostgreSQL, checks the copy, and runs the 20 queries. It uses the vectors already stored, so there is no embedding API call.
```
uv run python scripts/copy_to_qdrant.py
```
```
PostgreSQL: 2097 chunks
collection 'chunks': created
upsert of 2097 points: 3.4 s
removed from Qdrant (not in PostgreSQL): 0
points in Qdrant: 2097 | missing: 0
points whose tenant, document, text, heading or metadata differ: 0
largest difference in any vector number: 1.7e-08
points per tenant equal to PostgreSQL: True
collection: status green, 2097 points, 0 vectors in an HNSW index, indexing_threshold 10000 KB, payload indexes ['document_id', 'tenant_id']
query vectors: 0 from the API, 20 from the file

Qdrant search, 20 queries, top 5
  recall@5                       : 19/20
  chunks shared with exact       : 100/100
  same 5 in the same order       : 20/20
  queries that differ            : none
  largest score gap, same chunk  : 0.0000
```
A second run said `already there`, removed 0, and gave the same checks and results (upsert 3.2 s). The vector difference of 1.7e-08 is the rounding of 32-bit numbers: Qdrant stores cosine vectors scaled to length 1, and I compared after the same scaling.

**5. Isolation and removal on the real Qdrant server** (a temporary script, not in the repo). I added one temporary point for a new tenant, with **the same vector** as a corpus chunk:
```
[A] a tenant with no points, 20 queries      -> rows: 0
[B] tenant 2 (1 point, copy of a corpus vector), 21 searches -> rows per search [1], foreign rows: 0
[C] Corpus Tenant, 21 searches -> tenant 2's point returned: 0 times in 1050 rows
[D] delete by the wrong tenant  -> tenant 2 still has 1 point
copy script again               -> removed from Qdrant (not in PostgreSQL): 1 ; points in Qdrant: 2097
```

**6. Qdrant has not built an HNSW index.**
```
indexed_vectors_count: 0      segments: 6
hnsw_config: m=16, ef_construct=100, full_scan_threshold=10000
indexing_threshold: 10000 KB
```
My vectors take about 8,388 KB (2,097 x 1,024 numbers x 4 bytes), below the threshold of 10,000 KB, and Qdrant reports 0 indexed vectors. So **these Qdrant searches compared every vector (exact search)**.
- The 100/100 above shows that the copy and the score are right. It says **nothing about Qdrant's HNSW**.
- It is not yet "the same index" as in pgvector (HNSW, `m=16`, `ef_construction=64`). Qdrant's default here is `ef_construct=100`.
- A speed or recall comparison with pgvector's HNSW would not be fair in this state.

**Not done yet:** an HNSW index in Qdrant, the config switch (the app still uses pgvector), and deleting from Qdrant when a document is deleted through the API.

### Step 17. Task 5, fifth part: HNSW in Qdrant
**Why:** in Step 16 Qdrant had no HNSW index, so it was not "the same index" as in pgvector.

**1. How to see which search runs.** Qdrant's own counters (`GET /telemetry?details_level=10`, a local request; the outgoing usage reporting stays off) count, per segment, how searches were answered. I read their meaning in the v1.20.2 source ([dispatch.rs](https://github.com/qdrant/qdrant/blob/v1.20.2/lib/segment/src/index/hnsw_index/hnsw/read_view/dispatch.rs)):
| Counter | What ran |
|---|---|
| `filtered_large_cardinality` | **the HNSW graph**, with the filter |
| `filtered_small_cardinality` | the matching points are found through the payload index and scored one by one, no graph |
| `filtered_plain` | scored one by one: a segment answered without an HNSW search (or an exact search was asked for) |

**Before the change:** `{'filtered_plain': 738}`. Every search since Qdrant started had been a plain one.

**2. The settings** (`qdrant_store.py`)
| Setting | Value | Default I saw | Why |
|---|---|---|---|
| `m` | 16 | 16 | same as the pgvector index |
| `ef_construct` | 64 | 100 | same as pgvector's `ef_construction` |
| `indexing_threshold` | 100 KB | 10,000 KB | a segment above this gets an HNSW index |
| `full_scan_threshold` | 10 KB (the smallest allowed) | 10,000 KB | below it Qdrant scans even when there is an index |
| `hnsw_ef` (sent with each search) | 100 | not set | the same as pgvector's `ef_search` in my app |

`ensure_collection()` now also applies these to an existing collection, keeping the points:
```
created new: False
  0.9s status=yellow points=2097 indexed=689 segments=5
  1.6s status=green points=2097 indexed=2097 segments=6
hnsw: m 16 ef_construct 64 full_scan_threshold 10 | indexing_threshold 100
```
Indexing happens in the background (`yellow`). So `copy_to_qdrant.py` now waits for `green` (5 checks in a row) before it compares searches.

**3. The copy script again** (two runs): 2,097 points, no differences, and the same search result both times:
```
recall@5                       : 19/20
chunks shared with exact       : 100/100
same 5 in the same order       : 20/20
```

**4. Which search ran** (a temporary script, not in the repo; 20 queries each)
```
indexed vectors: 3347 of 2097, segments 6
[A] Corpus Tenant, 20 queries, limit 5
      how segments answered: {'filtered_large_cardinality': 80}
      vs exact: 100/100 chunks, 20/20 queries same order
[B] Corpus Tenant, 20 queries, limit 50
      how segments answered: {'filtered_large_cardinality': 80}
      rows per query 50-50
[C] tenant with 3 points, 20 queries, limit 5
      how segments answered: {'filtered_plain': 20, 'filtered_small_cardinality': 80}
      rows per query [3], rows of another tenant: 0
[D] after delete_document: tenant has 0 rows; points in collection 2097
```
(80 = 20 queries x 4 segments.)

**What it means**
- **Qdrant now searches the Corpus Tenant through HNSW**, and at `hnsw_ef = 100` it returned the same top 5 as exact search for all 20 queries **in this run**. (In Step 18, after more re-upserts, it missed 1 of 100.)
- A request for 50 returned 50.
- **The 3-point tenant got all 3**, without the graph: the counters show only plain scoring. In pgvector the same case needed `hnsw.iterative_scan` when the HNSW index was used. [C] says nothing about how Qdrant's graph behaves with a filter on a bigger small tenant.
- `delete_document` removed the temporary points on the real server.
- **`indexed vectors: 3347 of 2097`:** it was exactly 2,097 after the first build and grew after I upserted all points again (3,074, then 3,347). Qdrant's docs call these counts approximate and say it may keep changed or deleted points for a while ([collections](https://qdrant.tech/documentation/concepts/collections/)). I think that is the reason; I did not check it point by point.

**Checks:** `ruff check` on the two changed files passed; the results of `mypy` and `pytest` are below.
```
Success: no issues found in 79 source files
244 passed, 19 warnings in 23.57s
```

**Limits:** the thresholds are lowered so that a small dataset uses HNSW, for a like-for-like port. For this size, Qdrant's defaults (exact search) would be a sensible choice. Qdrant keeps one graph per segment, while pgvector has one index, so "the same settings" is not an identical structure. I did not tune `hnsw_ef` in Qdrant and did not compare speed yet.

### Step 18. Task 5, last part: the config switch
**1. The setting:** `VECTOR_STORE=pgvector` (default) or `qdrant`. Any other value stops the app at startup. In Docker Compose the `api` service gets `VECTOR_STORE=${VECTOR_STORE:-pgvector}` and `QDRANT_URL=http://qdrant:6333`.
```
docker compose up -d api                           # pgvector
VECTOR_STORE=qdrant docker compose up -d api       # Qdrant   (PowerShell: $env:VECTOR_STORE='qdrant'; docker compose up -d api)
```
(In PowerShell the variable stays set in that window. `Remove-Item Env:VECTOR_STORE` clears it.)

| Part | Change |
|---|---|
| `main.py` | one Qdrant client for the process, only when `qdrant` is chosen; logs `vector store: ...` |
| `api/deps.py` | `get_vector_store` returns `PgVectorStore` or `QdrantStore` |
| `DELETE /documents/{id}` | also deletes from the chosen store |
| `QdrantStore` | any Qdrant failure becomes a `503 VECTOR_STORE_UNAVAILABLE`; the details go to the log, not to the user |

**2. The order of a delete** (in `document_service.delete_document`): check that the document belongs to the tenant, **delete from the vector store, then from PostgreSQL**. If Qdrant fails, nothing is deleted and the request can be repeated. The other order could leave the text of a deleted document searchable. If PostgreSQL failed after Qdrant had succeeded, the document would remain without vectors in Qdrant; the copy script restores them.

**3. Tests** (`tests/test_vector_store_switch.py`, 7 new): the default, each value choosing its store, an unknown value refused, the delete order, a failing store deletes nothing, another tenant's delete never reaches the store, an unreachable Qdrant raises the clean error.
```
ruff check (the changed app and test files)  -> All checks passed
mypy src tests                               -> Success: no issues found in 80 source files
pytest -q                                    -> 251 passed
```

**4. Which database really searched** (database counters around 9 HTTP requests)
| API setting | PostgreSQL scans of `chunks` | Qdrant searches |
|---|---|---|
| `pgvector` | 3 HNSW, 6 full table scans | none |
| `qdrant` | none | 36 `filtered_large_cardinality` (HNSW) |

**5. Isolation and deletion with `VECTOR_STORE=qdrant`.** `verify_isolation.py` got a `--qdrant` mode: it writes the temporary chunks to Qdrant too and checks Qdrant itself around the delete.
```
uv run python scripts/verify_isolation.py --qdrant
```
```
query vectors: 0 from the API, 20 from the file
store: qdrant
before: tenants, users, documents, chunks = (4, 4, 239, 2097)
before: Qdrant points = 2097

[A] Task 6: tenant 2 has ZERO documents (225 vectors)
  PASS  qdrant: rows returned to tenant 2                          0 rows
  PASS  HTTP, tenant 2: How do I define a path parameter in Fast   0 results
  PASS  HTTP, tenant 2: FastAPI में CORS कैसे enable करें?         0 results
  PASS  HTTP, tenant 2: What is the solubility product constant    0 results

[B] Task 6: tenant 2 now owns copies of 5 corpus chunks
  PASS  qdrant: tenant 2 gets only its own chunks                  0 foreign rows; rows per search [5]
  PASS  qdrant: the Corpus Tenant never gets tenant 2's copies     0 leaked rows in 11250
  PASS  HTTP, both tenants: How do I define a path parameter in    tenant 2: 5 own; corpus: 50, 0 of tenant 2
  PASS  HTTP, both tenants: FastAPI में CORS कैसे enable करें?     tenant 2: 5 own; corpus: 50, 0 of tenant 2
  PASS  HTTP, both tenants: What is the solubility product const   tenant 2: 5 own; corpus: 50, 0 of tenant 2

[C] Task 7: delete the document
  PASS  chunks of the document before the delete                   5 rows
  PASS  Qdrant points of the document before the delete            5 points
  PASS  DELETE by another tenant is refused and removes nothing    HTTP 404; 5 rows left
  PASS  DELETE by the owner                                        HTTP 204
  PASS  chunk rows (and vectors) of the document                   0 rows
  PASS  Qdrant points of the document after the delete             0 points
  PASS  qdrant: searching with the deleted vectors                 0 rows
  PASS  HTTP, tenant 2: How do I define a path parameter in Fast   0 results
  PASS  HTTP, tenant 2: FastAPI में CORS कैसे enable करें?         0 results
  PASS  HTTP, tenant 2: What is the solubility product constant    0 results
  PASS  the Corpus Tenant's chunks are untouched                   2097 rows
  PASS  Qdrant has the same points as before                       2097

after clean-up: tenants, users, documents, chunks = (4, 4, 239, 2097)
  PASS  the database counts are the same as before                 (4, 4, 239, 2097) -> (4, 4, 239, 2097)

ALL CHECKS PASSED
```
The pgvector mode passed again as well.

**6. Qdrant is down** (container stopped; temporary script and temporary organisation)
```
search                 -> HTTP 503  VECTOR_STORE_UNAVAILABLE  (3.9 s)
delete a document      -> HTTP 503  VECTOR_STORE_UNAVAILABLE  (3.3 s)
document in PostgreSQL -> 1 row (nothing deleted)
GET /health            -> HTTP 200
(Qdrant started again)
delete again           -> HTTP 204 ; document in PostgreSQL -> 0 rows
search again           -> HTTP 200, 5 results
after clean-up: tenants, users, documents, chunks = (4, 4, 239, 2097)
```
`/health` stays 200 while Qdrant is down: it does not check the vector store. The API logged `Qdrant search failed: [Errno -2] Name or service not known`.

**7. Do the two stores give the same results? Not quite.** I gave both the **same saved vectors**, and compared with exact search. The exact searches of PostgreSQL and Qdrant agreed on all 20 queries (top 5 and top 50), so both hold the same data.
| | Exact top 5 returned | Exact top 50 returned | Missed |
|---|---|---|---|
| pgvector, app settings | 100/100 | 1000/1000 | none |
| Qdrant, app settings (`hnsw_ef = 100`) | **99/100** | **990/1000** | Q18 (Le Chatelier, Hinglish): rank 3 of the top 5, and 9 of its top 50; Q2: rank 8 of the top 50 |

- This is the normal price of an approximate index: it can miss a near chunk without any error.
- In Step 17 Qdrant returned 100/100 for the top 5. Since then the copy script has upserted all points again, and the collection reports 3,219 indexed vectors for 2,097 points. I think the graph changed; I did not test that explanation.
- pgvector's row at 50 is **not** a result of its HNSW: for a limit of 50 the planner used an exact scan (Step 11). So this table does not show that pgvector's HNSW is better than Qdrant's.
- I did not tune `hnsw_ef` in Qdrant.

**8. Over HTTP, responses can differ even with the same store.** With `pgvector`, 7 of my 9 saved responses were identical this time (in Step 15 all 9 were). I then sent the same text to Jina 6 times:
```
"How do I define a path parameter in FastAPI?"  6 calls -> 1 different vector
"FastAPI में CORS कैसे enable करें?"             6 calls -> 2 different vectors; largest difference in one number 0.0016; lowest cosine 0.999943
```
**The embedding API can return slightly different vectors for the same text.** The two differing responses had the same 5 or 50 chunks, with score differences up to 0.0011 (and a changed order in one). So a comparison between two runs must reuse saved vectors. The identical result of Step 15 was real, but it cannot be expected every time. With `qdrant`, the 2 of 9 that differed were both the Le Chatelier query (Q18).

**A rule for switching:** PostgreSQL is the main copy. **Run `copy_to_qdrant.py` before choosing `qdrant`.** While the app runs on `pgvector`, a delete does not touch Qdrant, so an old copy can still hold a deleted document until the script removes it. There is no automatic sync.

**Limits:** the app has no ingestion yet (Day 11), so nothing writes new chunks to either store through the API. Deleting a whole tenant does not clean Qdrant (there is no such route). One run on my laptop. I left the API on `pgvector`.

### Step 19. The comparison note, and a connection problem found while measuring
**1. The script** `scripts/compare_stores.py`: the same 20 saved query vectors through `VectorStore.search` for both stores with the app's settings, PostgreSQL forced to its HNSW index, limit 5 and 50, one warm-up round and 10 timed rounds. It only reads.

**2. The first run gave a number that could not be right:** Qdrant median **266 ms** at limit 5 and at limit 50, while Qdrant's own counters showed searches well under 1 ms. I checked before writing it down (a temporary script, one query, 15 calls each):
```
http://localhost:6333    qdrant-client median   265.0 ms | plain httpx (reused connection) median    2.8 ms | time Qdrant reports 0.62 ms
http://127.0.0.1:6333    qdrant-client median     5.9 ms | plain httpx (reused connection) median    3.2 ms | time Qdrant reports 0.65 ms
localhost resolves to: ['::1', '127.0.0.1']
qdrant-client's httpx pool: {'max_keepalive': 0, 'http2': False}
```
`localhost` gives `::1` first, Qdrant is published on `127.0.0.1` only, and the client opened a new connection for each request, so every request paid for the failed first try.

**3. The fix:** the default `QDRANT_URL` is now `http://127.0.0.1:6333` (`config.py`, `.env.example`). The API in Docker was not using this address: Compose sets `http://qdrant:6333`. The times of the copy script in Step 16 (upsert 3.4 s) were taken with the old address, so they include this delay.

**4. The runs after the fix** (the second of two; the first is in the note's table)
```
uv run python scripts/compare_stores.py
```
```
PostgreSQL chunks: 2097 | Qdrant points: 2097
limit  5  pgvector  exact results  100/100   median  4.73 ms  p95  5.98 ms  missed -
                   counters: {'ix_chunks_tenant_id': 1, 'ix_chunks_embedding_hnsw': 220, 'full table scan': 20}
limit  5  qdrant    exact results   99/100   median  4.76 ms  p95   6.3 ms  missed {18: 1}
                   counters: {'filtered_large_cardinality': 880}
limit 50  pgvector  exact results 1000/1000  median 20.99 ms  p95  23.3 ms  missed -
                   counters: {'ix_chunks_embedding_hnsw': 220}
limit 50  qdrant    exact results  990/1000  median  6.54 ms  p95   9.5 ms  missed {2: 1, 18: 9}
                   counters: {'filtered_large_cardinality': 880}
```
220 HNSW index scans = the 20 warm-up and 200 timed searches. I think the 20 full table scans in the first line are the 20 exact searches made just before, which PostgreSQL counted late; the one tenant-index scan I did not trace.

**5. Why pgvector was slower at 50** (a temporary script; the same SQL with the HNSW index forced)
```
limit  5  whole rows, with the vector (today)    median   4.30 ms
limit  5  same query without the vector column   median   3.12 ms
limit 50  whole rows, with the vector (today)    median  21.20 ms
limit 50  same query without the vector column   median   3.60 ms
```
`search_chunks` selects the whole `Chunk` row, so each result carries its 1,024-number vector back to Python, and the response never uses it. **Not changed yet.**

**6. Checks:** `ruff check` on the new script and `config.py` passed; `mypy src tests`: no issues in 80 files; `pytest -q`: 251 passed.

## Day 10 at the end
All 7 tasks and the note are done, and the plan's "done when" (find a document with words that never appear in it) was met by the Hindi CORS question (Step 6). **Open points:**
- `search_chunks` loads vectors it does not use (Step 19).
- No automated test covers the two pgvector search settings (Step 11) or the real servers; `verify_isolation.py` and the comparison scripts are run by hand.
- Nothing syncs Qdrant automatically (Step 18).
- Not tested: undoing the index migration `dc013ced71fa` (Step 3).
- Nothing from Day 10 is committed yet.
