# Qdrant: the complete guide (concepts, setup, HNSW, results, comparison with pgvector)

Based on your Day 10 work in `ai-service-app` (branch `day-10`, Steps 13 to 19).
All **measured** numbers come from `notes/day-10/README.md`, `pgvector-vs-qdrant.md` and the saved result files
(2,097 chunks, one laptop, 20 queries, 2026-11-25). Parts marked **(general knowledge)** are standard Qdrant
facts that were not measured in your project; I wrote them from memory, so check them against the Qdrant docs
before relying on them.

This is the companion of `hnsw-pgvector-guide.md`. Read that first if HNSW and `ef` are new to you.

---

## Table of contents

1. [What Qdrant is, and why it was added](#1-what-qdrant-is-and-why-it-was-added)
2. [Core concepts](#2-core-concepts)
3. [Running Qdrant (Docker Compose)](#3-running-qdrant-docker-compose)
4. [The Python client](#4-the-python-client)
5. [The `VectorStore` protocol](#5-the-vectorstore-protocol)
6. [`QdrantStore`: the code, piece by piece](#6-qdrantstore-the-code-piece-by-piece)
7. [Copying the corpus into Qdrant](#7-copying-the-corpus-into-qdrant)
8. [HNSW in Qdrant (the trap and the fix)](#8-hnsw-in-qdrant-the-trap-and-the-fix)
9. [Tuning in Qdrant: what was done and what was not](#9-tuning-in-qdrant)
10. [The config switch and deletion order](#10-the-config-switch-and-deletion-order)
11. [Isolation and deletion on the real server](#11-isolation-and-deletion-on-the-real-server)
12. [When Qdrant is down](#12-when-qdrant-is-down)
13. [Results: Qdrant against pgvector](#13-results-qdrant-against-pgvector)
14. [The `localhost` slowdown (a measurement trap)](#14-the-localhost-slowdown)
15. [Traps and lessons](#15-traps-and-lessons)
16. [Checklist: adding Qdrant to a project](#16-checklist-adding-qdrant-to-a-project)
17. [What was not measured](#17-what-was-not-measured)
18. [Glossary](#18-glossary)

---

## 1. What Qdrant is, and why it was added

**Qdrant** is a database built only for vector search. pgvector gives PostgreSQL a vector column and an index;
Qdrant is a separate server whose whole job is nearest-neighbour search with filters.

Task 5 of Day 10 asked for the same search on a second store, behind an interface, switched by config. The aim
was to **compare and decide**, not to replace pgvector.

| | pgvector | Qdrant |
|---|---|---|
| What it is | An extension inside PostgreSQL | A separate vector database server |
| Where the vectors live | A column of the `chunks` row | A **point** in a **collection** |
| Your decision | **Default** (`VECTOR_STORE=pgvector`) | Optional (`VECTOR_STORE=qdrant`) |

---

## 2. Core concepts

**(general knowledge, except where tied to your code)**

| Term | Meaning | In your project |
|---|---|---|
| **Collection** | A named set of points, like a table | One collection called `chunks` |
| **Point** | One record: an `id`, a vector, and a payload | One chunk (id = the chunk's UUID) |
| **Vector** | The embedding numbers | 1,024 numbers, cosine distance |
| **Payload** | A JSON object of metadata stored with the point | `tenant_id`, `document_id`, `chunk_index`, `heading`, `text`, `text_hash`, `embedding_model`, `metadata` |
| **Payload index** | An index on one payload field, so filters are fast | `tenant_id` (tenant mode) and `document_id` |
| **Filter** | A condition applied **inside** the search | `tenant_id` must equal the caller's tenant |
| **Segment** | A piece of a collection; each segment has its own index | Your collection had 5 to 6 segments |
| **Optimizer** | A background job that builds indexes and merges segments | Builds the HNSW graph when a segment is large enough |

### Score with cosine distance

With cosine distance, Qdrant's `score` is **already a similarity** (1.0 for the same vector). pgvector's `<=>`
returns a **distance**, so your SQL computes `1 - distance`. Both reach the API as the same kind of score
(your smoke test: same vector gave `1.0`).

---

## 3. Running Qdrant (Docker Compose)

### 3.1 The image (Step 13)

```
docker pull qdrant/qdrant:v1.20.2          # about 74 MB compressed, 282 MB on disk
docker run --rm --entrypoint ./qdrant qdrant/qdrant:latest --version     -> qdrant 1.20.2
```

The compose file pins **`v1.20.2`**, not `latest`, so a later pull cannot change the version unnoticed.

### 3.2 The service

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

| Setting | Why |
|---|---|
| `127.0.0.1:6333:6333` | Reachable **only from your computer**. This Qdrant has **no API key**, so anyone who could reach the port could read or delete the data |
| `QDRANT__TELEMETRY_DISABLED` | No anonymous usage reports |
| `qdrant_data` volume | Data survives a container being recreated |
| Health check with `bash` | The image has no `curl` and no `wget` |
| Port 6334 (gRPC) not published | The plan uses the HTTP API |

### 3.3 Checks that passed

```
docker compose up -d qdrant       -> Started ; health: healthy
curl localhost:6333/readyz        -> all shards are ready (HTTP 200)
curl localhost:6333/collections   -> {"result":{"collections":[]},"status":"ok"}
docker stats                      -> 59.4 MiB of memory (empty)
docker logs ai-service-qdrant     -> "Version: 1.20.2", "Telemetry reporting disabled"
```

The health check can also fail: the same command with a non-existent path exited with code 1, so "healthy" means
Qdrant really answered `200 OK`.

### 3.4 Addresses

| From | Address |
|---|---|
| Inside Docker (the API container) | `http://qdrant:6333` |
| Your computer | `http://127.0.0.1:6333` (**not** `localhost`, see section 14) |

The `api` service does **not** depend on `qdrant`, and a plain `docker compose up` starts the extra container too.

---

## 4. The Python client

```
uv add qdrant-client        # qdrant-client 1.20.0 (+ grpcio, protobuf, h2, hpack, hyperframe, portalocker)
```

Only those 7 packages were added; no other package changed version.

### Smoke test (3 points, 4-number vectors, temporary collection)

Tenant B's point had **the same vector** as tenant A's point. The query used a filter:

```
filter tenant=A      -> [('A-near', 1.0), ('A-far', 0.0)]
filter tenant=B      -> [('B-identical', 1.0)]
filter tenant=nobody -> []
client 1.20.0 | server 1.20.2 | warnings: none
```

The filter is part of the query (`query_filter=`), just like `WHERE tenant_id = ...` in SQL. This was a 3-point
test, not the isolation test of the real search (that is section 11).

The API image was rebuilt (it installs packages at build time); after it, search still returned 5 results at
`limit=5` and 50 at `limit=50`, first score 0.7603, and `verify_isolation.py` passed.

---

## 5. The `VectorStore` protocol

Before writing `QdrantStore`, the endpoint was made to depend on an interface, with **only pgvector** behind it,
so any change in results could only come from the restructuring.

```python
class VectorStore(Protocol):
    async def search(self, *, tenant_id, query_vector, limit=5, threshold=None) -> list[SearchResultItem]: ...
    async def upsert(self, records) -> None: ...                        # added in Step 16
    async def delete_document(self, *, tenant_id, document_id) -> None: ...   # added in Step 16
```

Rules every store must keep (from the docstring):

- Return only the given tenant's chunks, filtered **inside the store's own query**.
- `tenant_id` is required.
- Highest score first; the score is cosine similarity; nothing below `threshold`.

A `Protocol` needs no inheritance: any class with matching methods fits, and `mypy` checks it.

### Same behaviour proof (Step 15)

```
9 saved HTTP responses (3 queries x limit 5 / 50 / 50 with threshold 0.4): identical before and after, byte for byte
PgVectorStore vs saved exact results: 20/20 queries identical (ids, scores, order); 100/100 chunks
```

### The transaction difference (written into the protocol's docstring)

| | pgvector | Qdrant |
|---|---|---|
| Writes | Belong to the caller's database transaction (can roll back) | Applied **at once**, cannot be rolled back |

---

## 6. `QdrantStore`: the code, piece by piece

File: `src/ai_service/services/qdrant_store.py`.

### 6.1 Constants

```python
HNSW_M = 16
HNSW_EF_CONSTRUCT = 64
HNSW_EF = 100                  # search time, same as pgvector's hnsw.ef_search
INDEXING_THRESHOLD_KB = 100    # a segment larger than this gets an HNSW index
FULL_SCAN_THRESHOLD_KB = 10    # 10 is the smallest allowed
_BATCH_SIZE = 256
```

### 6.2 `ensure_collection()`

```python
hnsw = models.HnswConfigDiff(m=16, ef_construct=64, full_scan_threshold=10)
optimizers = models.OptimizersConfigDiff(indexing_threshold=100)

if await client.collection_exists(name):
    await client.update_collection(name, hnsw_config=hnsw, optimizers_config=optimizers)
    return False                                  # existing points are kept

await client.create_collection(
    name,
    vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
    hnsw_config=hnsw, optimizers_config=optimizers,
)
# tenant payload index: is_tenant=True tells Qdrant to keep each tenant's points together
await client.create_payload_index(name, "tenant_id",
    field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True))
await client.create_payload_index(name, "document_id", field_schema=models.PayloadSchemaType.KEYWORD)
```

Key points:

- An **existing** collection gets the current HNSW settings with its points kept (this is how Step 17 changed
  the settings without re-copying).
- The **schema lives in code**, not in migrations (pgvector uses Alembic).
- `is_tenant=True` is the multi-tenant optimisation **(general knowledge)**: it groups a tenant's points so
  tenant-filtered search stays efficient.

### 6.3 `search()`

```python
response = await client.query_points(
    collection,
    query=query_vector,
    query_filter=models.Filter(must=[_match("tenant_id", tenant_id)]),   # inside the query
    limit=limit,
    score_threshold=threshold,
    search_params=models.SearchParams(hnsw_ef=HNSW_EF),                   # 100
    with_payload=True,                                                    # text comes back, no 2nd query
)
```

Then each point becomes a `SearchResultItem` with `score = round(point.score, 4)`.

Compare with pgvector:

| Need | pgvector | Qdrant |
|---|---|---|
| Tenant isolation | `WHERE tenant_id = ...` | `query_filter` on `tenant_id` |
| Threshold | `WHERE 1 - distance >= threshold` | `score_threshold=threshold` |
| Search effort | `set_config('hnsw.ef_search', ...)` | `SearchParams(hnsw_ef=...)` per request |
| Small-tenant filter fix | `hnsw.iterative_scan` | Not needed in your tests (see section 8.5) |
| Text of the chunk | Same row | Stored in the payload (a second copy) |

### 6.4 `upsert()`

- Batches of **256** points, `wait=True` (return only when points are applied and searchable).
- Payload carries everything a search needs: tenant, document, chunk index, heading, text, hash, model, metadata.

### 6.5 `delete_document()`

Deletes with a filter on **both** `tenant_id` and `document_id`, `wait=True`. So another tenant cannot delete a
document even if it knows the id (proved in section 11).

### 6.6 Error handling

Every Qdrant call runs inside `_reported(...)`: any `ApiException` (unreachable, timeout, error response) is
logged with the details and re-raised as `VectorStoreError`, which the API returns as
**503 `VECTOR_STORE_UNAVAILABLE`**. The user never sees the raw Qdrant error.

### 6.7 The price of copying text into Qdrant

A search needs no second query, but there are **two copies** of the text that must be kept in step.
PostgreSQL stays the main copy; Qdrant can be rebuilt from it.

---

## 7. Copying the corpus into Qdrant

Script: `scripts/copy_to_qdrant.py` (uses the vectors already in PostgreSQL, so **no embedding API call**).

What it does:

1. Reads all rows of `chunks` from PostgreSQL.
2. Creates or updates the collection.
3. Upserts every row (batches of 256).
4. **Removes points that no longer exist in PostgreSQL** (so a re-run makes Qdrant a mirror).
5. Verifies the copy.
6. Waits until Qdrant is `green` (5 checks in a row).
7. Runs the 20 queries and compares with exact search.

### Result (Step 16, first run)

```
PostgreSQL: 2097 chunks
upsert of 2097 points: 3.4 s                         # measured with the slow localhost address, see section 14
removed from Qdrant (not in PostgreSQL): 0
points in Qdrant: 2097 | missing: 0
points whose tenant, document, text, heading or metadata differ: 0
largest difference in any vector number: 1.7e-08
points per tenant equal to PostgreSQL: True
```

- The `1.7e-08` difference is the rounding of 32-bit numbers: Qdrant stores cosine vectors scaled to length 1,
  and the comparison was made after the same scaling.
- A second run said `already there`, removed 0, upsert 3.2 s, same checks and same results.

### The sync rule

There is **no automatic sync**. While the app runs on `pgvector`, a delete does not touch Qdrant, so an old copy
can still hold a deleted document.

> **Run `copy_to_qdrant.py` before choosing `VECTOR_STORE=qdrant`.**

---

## 8. HNSW in Qdrant (the trap and the fix)

### 8.1 The trap (Step 16): Qdrant had no HNSW index

After the first copy, Qdrant returned a perfect **100/100** against exact search. That looked great, but:

```
indexed_vectors_count: 0      segments: 6
hnsw_config: m=16, ef_construct=100, full_scan_threshold=10000
indexing_threshold: 10000 KB
```

Your vectors take about **8,388 KB** (2,097 x 1,024 numbers x 4 bytes), **below** the default
`indexing_threshold` of **10,000 KB**. Qdrant therefore built no graph, and **every search compared every
vector** (exact search). The 100/100 proved the copy and the score were right and said **nothing about
Qdrant's HNSW**.

Also, at that point Qdrant used `ef_construct = 100` while pgvector's index used `ef_construction = 64`, so it
was not "the same index".

### 8.2 How to see which search really ran

Qdrant's own counters (`GET /telemetry?details_level=10`, a local request; outgoing usage reporting stays off)
count per segment how searches were answered. The meaning was read from the v1.20.2 source
(`hnsw/read_view/dispatch.rs`):

| Counter | What ran |
|---|---|
| `filtered_large_cardinality` | **The HNSW graph**, with the filter |
| `filtered_small_cardinality` | Matching points found through the payload index and scored one by one (no graph) |
| `filtered_plain` | Scored one by one: a segment answered without an HNSW search (or exact search was asked for) |

**Before the change:** `{'filtered_plain': 738}`. Every search since Qdrant started had been plain.

### 8.3 The settings (Step 17)

| Setting | Value | Default seen | Why |
|---|---|---|---|
| `m` | 16 | 16 | Same as the pgvector index |
| `ef_construct` | 64 | 100 | Same as pgvector's `ef_construction` |
| `indexing_threshold` | 100 KB | 10,000 KB | A segment above this gets an HNSW index |
| `full_scan_threshold` | 10 KB (smallest allowed) | 10,000 KB | Below it, Qdrant scans even when an index exists |
| `hnsw_ef` (per search) | 100 | not set | Same as pgvector's `ef_search` in this app |

### 8.4 Indexing happens in the background

After `ensure_collection()` was applied to the existing collection:

```
created new: False
  0.9s status=yellow points=2097 indexed=689  segments=5
  1.6s status=green  points=2097 indexed=2097 segments=6
hnsw: m 16 ef_construct 64 full_scan_threshold 10 | indexing_threshold 100
```

- `yellow` = still building; `green` = done.
- That is why `copy_to_qdrant.py` waits for **`green` 5 times in a row** before comparing searches.

### 8.5 Proof that HNSW ran (Step 17, 20 queries each)

```
[A] Corpus Tenant, limit 5      segments answered: {'filtered_large_cardinality': 80}
      vs exact: 100/100 chunks, 20/20 queries same order
[B] Corpus Tenant, limit 50     segments answered: {'filtered_large_cardinality': 80}
      rows per query 50-50
[C] tenant with 3 points        segments answered: {'filtered_plain': 20, 'filtered_small_cardinality': 80}
      rows per query [3], rows of another tenant: 0
[D] after delete_document: tenant has 0 rows; points in collection 2097
```

(80 = 20 queries x 4 segments.)

What it means:

- Qdrant now searches the Corpus Tenant **through HNSW**, and at `hnsw_ef = 100` it returned the same top 5 as
  exact search for all 20 queries **in this run**. (In Step 18, after more re-upserts, it missed 1 of 100.)
- A request for 50 returned 50 (no row cap seen).
- The **3-point tenant got all 3 without the graph**. In pgvector the same case returned **0** when HNSW was
  used, until `iterative_scan` was set. [C] says nothing about how Qdrant's graph behaves with a filter on a
  *bigger* small tenant.

### 8.6 The odd `indexed vectors` number

`indexed vectors: 3347 of 2097`. It was exactly 2,097 after the first build and grew after all points were
upserted again (3,074, then 3,347; later 3,219). Qdrant's docs call these counts approximate and say it may keep
changed or deleted points for a while. The notes say this is the likely reason, **not checked point by point**.

---

## 9. Tuning in Qdrant

### What was done

| Item | Status |
|---|---|
| `m = 16`, `ef_construct = 64` | Set to match pgvector (not tuned) |
| `hnsw_ef = 100` | **Copied from pgvector**, not tuned on its own |
| `indexing_threshold`, `full_scan_threshold` | Lowered so a tiny dataset really uses HNSW |
| Recall against exact search | Measured: 100/100 (Step 17), 99/100 top 5 and 990/1000 top 50 (Steps 18 and 19) |
| Speed against pgvector | Measured (section 13) |

### What was NOT done

There was **no `hnsw_ef` sweep** like pgvector's `ef_search` sweep (5, 10, 20, 40, 100, 200, 400). So you cannot
say from the notes which `hnsw_ef` is best for Qdrant. The notes say so clearly: "I did not tune `hnsw_ef` in
Qdrant."

### The two search knobs side by side

| Build time | Search time |
|---|---|
| pgvector `ef_construction` = Qdrant `ef_construct` | pgvector `ef_search` = Qdrant `hnsw_ef` |

Both stores use **100** at search time, so the comparison is fair, but the same number does not mean the same
behaviour: Qdrant keeps **one graph per segment**, pgvector has **one index**.

### How you would tune Qdrant (apply the pgvector method)

**(suggested, not run in your project)**

1. Fix the saved query vectors and the exact baseline.
2. Sweep `hnsw_ef` (for example 10, 20, 40, 100, 200) on the **same** 20 queries; record "same as exact",
   median, p95 and rows for `limit = 50`.
3. Check the counters to confirm `filtered_large_cardinality` (HNSW) was used each time.
4. Re-run after more upserts, because the graph can change (the 100/100 became 99/100).
5. Test a bigger "small" tenant with the filter, to see how the graph behaves with filters.

---

## 10. The config switch and deletion order

### 10.1 The switch (Step 18)

`VECTOR_STORE=pgvector` (default) or `qdrant`. Any other value stops the app at startup.

```
docker compose up -d api                          # pgvector
VECTOR_STORE=qdrant docker compose up -d api      # Qdrant
# PowerShell:  $env:VECTOR_STORE='qdrant'; docker compose up -d api
#              Remove-Item Env:VECTOR_STORE        # to clear it
```

In Compose the `api` service gets `VECTOR_STORE=${VECTOR_STORE:-pgvector}` and `QDRANT_URL=http://qdrant:6333`.

| Part | Change |
|---|---|
| `main.py` | One Qdrant client per process, only when `qdrant` is chosen; logs `vector store: ...` |
| `api/deps.py` | `get_vector_store` returns `PgVectorStore` or `QdrantStore` |
| `DELETE /documents/{id}` | Also deletes from the chosen store |
| `QdrantStore` | Any Qdrant failure becomes `503 VECTOR_STORE_UNAVAILABLE` |

New settings: `QDRANT_URL` (default `http://127.0.0.1:6333`) and `QDRANT_COLLECTION` (`chunks`).

### 10.2 The order of a delete (important)

In `document_service.delete_document`:

1. Check the document belongs to the tenant.
2. **Delete from the vector store first.**
3. Then delete from PostgreSQL.

| What fails | Result |
|---|---|
| Qdrant fails | **Nothing is deleted**; the request can be repeated |
| PostgreSQL fails after Qdrant succeeded | Document remains in PostgreSQL without vectors in Qdrant; `copy_to_qdrant.py` restores them |

The opposite order could leave the **text of a deleted document searchable**, which is worse.

### 10.3 Tests (7 new, `tests/test_vector_store_switch.py`)

Default store, each value choosing its store, an unknown value refused, the delete order, a failing store deletes
nothing, another tenant's delete never reaches the store, an unreachable Qdrant raises the clean error.
Results: `ruff` passed, `mypy` 80 files OK, `pytest` **251 passed**.

Contract tests (`tests/test_vector_store.py`, 12 passed = 6 rules x 2 stores): isolation, order and limit,
threshold, upsert replaces, delete removes only that document, another tenant cannot delete it. Note these run
`PgVectorStore` on SQLite and Qdrant in the client's **in-memory mode**, so they check the contract, **not the
two real servers**.

### 10.4 Which database really searched (counters around 9 HTTP requests)

| API setting | PostgreSQL scans of `chunks` | Qdrant searches |
|---|---|---|
| `pgvector` | 3 HNSW, 6 full table scans | none |
| `qdrant` | none | 36 `filtered_large_cardinality` (HNSW) |

---

## 11. Isolation and deletion on the real server

### Step 16: direct on Qdrant (one temporary point with the same vector as a corpus chunk)

```
[A] a tenant with no points, 20 queries          -> rows: 0
[B] tenant 2 (1 point, copy of a corpus vector)  -> rows per search [1], foreign rows: 0
[C] Corpus Tenant, 21 searches                   -> tenant 2's point returned 0 times in 1050 rows
[D] delete by the wrong tenant                   -> tenant 2 still has 1 point
copy script again                                -> removed from Qdrant (not in PostgreSQL): 1 ; points: 2097
```

### Step 18: `verify_isolation.py --qdrant` (ALL CHECKS PASSED)

| Stage | What was proved |
|---|---|
| [A] zero documents | Qdrant returned 0 rows to tenant 2; 3 HTTP searches gave 0 results |
| [B] tenant 2 owns 5 copies of corpus chunks | Tenant 2 got only its own 5 (0 foreign rows); the Corpus Tenant **never** got tenant 2's copies (0 leaked rows in 11,250); HTTP: tenant 2 gets 5 own, Corpus gets 50 with 0 of tenant 2's |
| [C] delete | 5 rows and 5 Qdrant points before; delete by another tenant: **HTTP 404**, 5 rows left; delete by owner: **HTTP 204**; 0 rows, 0 Qdrant points after; searching with the deleted vectors: 0 rows |
| Cleanup | Corpus chunks untouched (2,097 rows); Qdrant has the same 2,097 points; table counts `(4, 4, 239, 2097)` before and after |

The pgvector mode of the script passed again as well.

---

## 12. When Qdrant is down

Test: Qdrant container stopped, temporary organisation and script.

```
search                  -> HTTP 503  VECTOR_STORE_UNAVAILABLE   (3.9 s)
delete a document       -> HTTP 503  VECTOR_STORE_UNAVAILABLE   (3.3 s)
document in PostgreSQL  -> 1 row  (nothing deleted)
GET /health             -> HTTP 200
(Qdrant started again)
delete again            -> HTTP 204 ; document in PostgreSQL -> 0 rows
search again            -> HTTP 200, 5 results
```

What to learn:

- Failures are **clean and safe**: a 503 with a stable code; the raw error goes to the log
  (`Qdrant search failed: [Errno -2] Name or service not known`).
- Nothing is half-deleted, and the retry works.
- `/health` stays **200** while Qdrant is down, because it does not check the vector store. A monitoring system
  that only calls `/health` would not notice.
- The 3 to 4 second wait before the error is a timeout cost.
- With pgvector the whole app depends on PostgreSQL anyway, so there is no separate "vector store down" case.

---

## 13. Results: Qdrant against pgvector

Setup: the **same saved query vectors**, called through the same `VectorStore.search`
(`scripts/compare_stores.py`, two runs). Both used HNSW: `m = 16`, build ef 64, search ef 100.
PostgreSQL forced to its HNSW index. 1 warm-up round + 10 timed rounds. "Exact" = PostgreSQL comparing the query
with every chunk. The exact searches of both stores agreed on all 20 queries (top 5 and top 50), so both hold
the same data.

### 13.1 Accuracy and speed

| | pgvector | Qdrant |
|---|---|---|
| top 5: exact results returned | **100/100** | 99/100 |
| top 50: exact results returned | **1000/1000** | 990/1000 |
| top 5: median (run 1 / run 2) | 4.70 / 4.73 ms | 4.67 / 4.76 ms |
| top 5: p95 | 5.62 / 5.98 ms | 7.34 / 6.30 ms |
| top 50: median | 20.85 / 20.99 ms | **7.18 / 6.54 ms** |
| top 50: p95 | 23.28 / 23.30 ms | **10.56 / 9.50 ms** |

Which chunks Qdrant missed: **Q18** (Le Chatelier, Hinglish): 1 chunk in the top 5 (rank 3) and 9 in the top 50;
**Q2**: 1 chunk (rank 8) in the top 50.

### 13.2 How to read it honestly

- **Top 5: the same speed.**
- **Top 50: the gap is not the index.** The pgvector query loads whole rows including the unused 1,024-number
  vector. Qdrant returns no vectors. Same SQL with HNSW forced, 200 timed searches:

  | pgvector query | limit 5 | limit 50 |
  |---|---|---|
  | whole rows, with the vector (the app today) | 4.30 ms | 21.20 ms |
  | without the vector column | 3.12 ms | **3.60 ms** |

  Not loading the vector would remove most of the top-50 gap. **It has not been applied.**
- **Recall:** Qdrant missed a few near chunks, pgvector none. This is one graph of each on a very small set;
  Qdrant got 100/100 in Step 17; and `hnsw_ef` was not tuned. It does **not** show that one index is better.
- The pgvector row at limit 50 is **not a test of its HNSW**: the planner used an exact scan there.
- All of these times are small next to embedding the query (about **0.5 to 0.9 s** in the Jina calls; an HTTP
  search took 592 ms and 532 ms in Step 7).

### 13.3 Counters from the final run

```
limit  5  pgvector  100/100   median 4.73 ms  p95 5.98 ms   {'ix_chunks_tenant_id': 1, 'ix_chunks_embedding_hnsw': 220, 'full table scan': 20}
limit  5  qdrant     99/100   median 4.76 ms  p95 6.3 ms    {'filtered_large_cardinality': 880}
limit 50  pgvector 1000/1000  median 20.99 ms p95 23.3 ms   {'ix_chunks_embedding_hnsw': 220}
limit 50  qdrant    990/1000  median 6.54 ms  p95 9.5 ms    {'filtered_large_cardinality': 880}
```

The 20 full table scans in the first line are most likely the 20 exact searches made just before (counted late).
The single tenant-index scan was not traced.

### 13.4 Operating differences

| Topic | pgvector | Qdrant |
|---|---|---|
| What you run | The PostgreSQL you already have + an extension | One more service (about 74 MB image; 59 MiB memory when empty), one more volume |
| Schema changes | Alembic migrations | Code creates the collection and its settings |
| Copies of the data | One | Two: PostgreSQL main, Qdrant also holds the text |
| Deleting a document | Foreign key cascade, **same transaction** | App deletes from Qdrant too, Qdrant first; no shared transaction |
| Keeping in step | Nothing to do | No automatic sync: run `copy_to_qdrant.py` before switching |
| Tenant isolation | `WHERE tenant_id = ...` | Filter inside the query + tenant payload index |
| Small tenant with a filter | **0 of 3 chunks** with HNSW until `iterative_scan` | All 3 (plain scoring) |
| HNSW on small data | Planner picks HNSW or exact by tenant and limit | Built **no** index at default thresholds; had to be lowered |
| Many results | At most `ef_search` rows without iterative scan | No cap seen: 50 of 50 at `hnsw_ef` 100 |
| If the store is down | The whole app depends on PostgreSQL anyway | Search and delete answer 503; `/health` stays 200 |
| Security setup | Database login | No API key; port open to your computer only |

### 13.5 The decision

**pgvector stays the default.** At this size it was as fast for the top 5 and missed nothing in these tests;
delete and tenant data stay correct through one transaction and foreign keys with no second copy; and it is one
service less to run, back up and secure.

Look at Qdrant again if the vectors grow far beyond what was tested and PostgreSQL search becomes too slow or
heavy for the database that also serves the app, or if filtered search over many tenants becomes a problem that
`iterative_scan` and indexes cannot solve. Measure on your own data first. The `VectorStore` interface keeps the
switch small: one setting plus a data copy.

---

## 14. The `localhost` slowdown

The first comparison run showed Qdrant at **266 ms** (limit 5 and limit 50), but Qdrant's own timer said under
1 ms. Checked with one query, 15 calls each:

```
http://localhost:6333   qdrant-client median 265.0 ms | plain httpx (reused connection) 2.8 ms | Qdrant reports 0.62 ms
http://127.0.0.1:6333   qdrant-client median   5.9 ms | plain httpx (reused connection) 3.2 ms | Qdrant reports 0.65 ms
localhost resolves to: ['::1', '127.0.0.1']
qdrant-client's httpx pool: {'max_keepalive': 0, 'http2': False}
```

**Cause:** `localhost` tries `::1` (IPv6) first; Qdrant is published on `127.0.0.1` only; and the client opened a
**new connection for every request**, so every request paid for the failed first try.

**Fix:** the default `QDRANT_URL` is now `http://127.0.0.1:6333` (`config.py`, `.env.example`). The API in Docker
was not affected, because Compose sets `http://qdrant:6333`. The upsert time of 3.4 s in section 7 was taken with
the old address, so it includes this delay.

**Lesson:** when a number looks wrong, compare it with the server's own timer before writing it down.

---

## 15. Traps and lessons

| # | Trap | What happened | Lesson |
|---|---|---|---|
| 1 | No index built | 8,388 KB of vectors < 10,000 KB default threshold; `indexed_vectors_count: 0` | "Perfect recall" can just be exact search. Check `indexed_vectors_count` and the counters |
| 2 | Different build settings | Qdrant's default `ef_construct` was 100, pgvector's was 64 | Set both stores to the same `m` and `ef` before comparing |
| 3 | Background indexing | Status `yellow` while building | Wait for `green` before measuring |
| 4 | `localhost` | About 265 ms per call instead of about 5.9 ms | Use `127.0.0.1`; compare with the server's timer |
| 5 | Counts that look wrong | `indexed vectors: 3347 of 2097` after re-upserts | Treat the count as approximate; likely old points kept for a while (not verified) |
| 6 | Recall changes | 100/100 in Step 17, 99/100 in Step 18 after more upserts | Re-measure after data changes; graph may change (suspected, not tested) |
| 7 | Two copies | Qdrant holds the text too | Keep PostgreSQL as the main copy; run `copy_to_qdrant.py` before switching |
| 8 | Delete order | Deleting from PostgreSQL first could leave searchable text | Delete from the vector store first |
| 9 | No shared transaction | Qdrant writes apply at once, no rollback | Design for retries; make the copy script idempotent |
| 10 | `/health` stays 200 | It does not check the vector store | Add a vector-store check if you need alerts |
| 11 | In-memory tests | Qdrant in-memory mode ignores payload indexes (it warns) | These tests check the contract, not the real server |
| 12 | Embedding drift | Same text to Jina gave slightly different vectors (lowest cosine 0.999943; up to 0.0016 in one number) | Compare runs with **saved** vectors; 2 of 9 Qdrant HTTP responses differed (both Q18) |
| 13 | Heavy pgvector rows | The vector column inflated pgvector's top-50 time, not Qdrant's speed advantage | Compare equal work: same columns returned |
| 14 | No API key | The open port gives full access | Publish only on `127.0.0.1`; add a key before any wider exposure |

---

## 16. Checklist: adding Qdrant to a project

1. **Run it:** pinned image version, port on `127.0.0.1` only, a volume, a health check.
2. **Install the client** and run a small smoke test with a temporary collection.
3. **Put an interface first** (`VectorStore`) with the current store behind it; prove the results are unchanged.
4. **Create the collection in code:** vector size and distance, tenant payload index (`is_tenant=True`),
   document payload index.
5. **Set HNSW explicitly:** `m`, `ef_construct`, `hnsw_ef`; lower `indexing_threshold` and `full_scan_threshold`
   if the data is small and you want HNSW.
6. **Copy the data** with a script that upserts, removes stale points and verifies; make it safe to re-run.
7. **Prove HNSW ran** with the telemetry counters and `indexed_vectors_count`, and wait for `green`.
8. **Test isolation** with identical vectors in two tenants; test zero documents; test delete by the wrong tenant.
9. **Design the delete order** and failures: vector store first; clean 503; retry-safe.
10. **Add the config switch** with a default, and refuse unknown values at startup.
11. **Compare fairly:** same saved vectors, same interface, same settings, equal columns, warm-up, p95, two runs.
12. **Check the address and connection reuse** if times look too high.
13. **Decide with data** and write down the limits and the conditions to revisit.

---

## 17. What was not measured

- `hnsw_ef` tuning in Qdrant (only the copied value 100 was used).
- Index build time, disk size, and memory with data (only 59.4 MiB for an empty server).
- More than 2,097 vectors; more than one big tenant; a filtered search on a bigger "small" tenant.
- Concurrency or load: one request at a time on one laptop; Qdrant over gRPC (only HTTP).
- Updates and deletes under load; behaviour with a strict `threshold`.
- Why recall moved from 100/100 to 99/100 (a graph change after re-upserts is suspected, not tested).
- Qdrant's default thresholds (exact search) as an alternative for data this small; for 2,097 vectors that would
  be a reasonable choice.
- Tenant deletion cleanup: there is no route for deleting a whole tenant, so Qdrant is not cleaned for that case.
- Ingestion through the API (Day 11): nothing writes new chunks to either store through the API yet.

---

## 18. Glossary

| Term | Meaning |
|---|---|
| Collection | A named set of points in Qdrant |
| Point | One record: id + vector + payload |
| Payload | JSON metadata stored with a point |
| Payload index | An index on a payload field, used by filters |
| `is_tenant` | Payload index option that keeps one tenant's points together |
| Segment | A piece of a collection; Qdrant keeps one HNSW graph per segment |
| Optimizer | Background job that builds indexes and merges segments |
| `indexing_threshold` | Segment size (KB of vectors) above which an HNSW index is built |
| `full_scan_threshold` | Size (KB) below which Qdrant scans instead of using the graph |
| `m` | Max links per point (build time) |
| `ef_construct` | Candidate list size while building (pgvector: `ef_construction`) |
| `hnsw_ef` | Candidate list size while searching (pgvector: `ef_search`) |
| `green` / `yellow` | Collection status: fully indexed / still indexing |
| `filtered_large_cardinality` | Counter: search used the HNSW graph with the filter |
| `filtered_small_cardinality` | Counter: matches found through the payload index, scored one by one |
| `filtered_plain` | Counter: scored one by one, no graph |
| `VectorStore` | Your Python `Protocol` that both stores follow |
| `VectorStoreError` | Error that becomes HTTP 503 `VECTOR_STORE_UNAVAILABLE` |
| recall / "same as exact" | Share of exact-search chunks that the approximate search also returned |
| p95 | 95% of searches were faster than this time |

---

### Files to open in the repo

| Topic | File |
|---|---|
| Step-by-step log (Steps 13 to 19) | `notes/day-10/README.md` |
| pgvector vs Qdrant note | `notes/day-10/pgvector-vs-qdrant.md` |
| Qdrant code | `src/ai_service/services/qdrant_store.py` |
| Interface and pgvector store | `src/ai_service/services/vector_store.py` |
| Switch and deletion | `src/ai_service/api/deps.py`, `main.py`, `services/document_service.py` |
| Copy script | `scripts/copy_to_qdrant.py` |
| Checks | `scripts/verify_isolation.py --qdrant`, `scripts/compare_stores.py` |
| Contract and switch tests | `tests/test_vector_store.py`, `tests/test_vector_store_switch.py` |
| Saved results | `scripts/results/day10_qdrant_comparison.json`, `day10_store_comparison.json`, `day10_isolation_deletion_qdrant.txt` |
