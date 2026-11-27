# HNSW in pgvector: the complete guide (concepts, tuning, results, Qdrant comparison)

Based on your Day 10 work in `ai-service-app` (branch `day-10`).
All **measured** numbers come from your `notes/day-10/README.md` and `pgvector-vs-qdrant.md`
(2,097 chunks, one laptop, 20 queries, 2026-11-25). Parts marked **(general knowledge)** are standard
pgvector / HNSW facts that were not measured in your project.

---

## Table of contents

1. [The problem HNSW solves](#1-the-problem-hnsw-solves)
2. [How HNSW works (simple model)](#2-how-hnsw-works-simple-model)
3. [The three knobs: `m`, `ef_construction`, `ef_search`](#3-the-three-knobs)
4. [HNSW in pgvector: how you set it up](#4-hnsw-in-pgvector-setup)
5. [Your project setup and data](#5-your-project-setup-and-data)
6. [The tuning process, step by step, with results](#6-the-tuning-process-with-results)
7. [The tenant-filter problem and `iterative_scan`](#7-the-tenant-filter-problem-and-iterative_scan)
8. [Final settings used by the app](#8-final-settings-used-by-the-app)
9. [Isolation and deletion checks](#9-isolation-and-deletion-checks)
10. [Qdrant: HNSW there, and pgvector vs Qdrant results](#10-qdrant-hnsw-and-the-comparison)
11. [Traps and lessons](#11-traps-and-lessons)
12. [A repeatable tuning checklist](#12-a-repeatable-tuning-checklist)
13. [What was not measured](#13-what-was-not-measured)
14. [Glossary](#14-glossary)

---

## 1. The problem HNSW solves

A vector search asks: *"Which stored vectors are closest to this query vector?"*

- **Exact search** compares the query with **every** vector. It is always correct, but the work grows with the
  number of vectors.
- **Approximate search (ANN)** compares the query with only a **small part** of the vectors, found through a
  smart structure. It is much faster, but it **can miss** a true nearest neighbour.

HNSW (Hierarchical Navigable Small World) is one ANN structure. Every ANN index trades:

| You gain | You pay |
|---|---|
| Speed | A small chance of missing true nearest chunks (recall below 100%) |
| | Extra disk and memory for the index, and build time **(general knowledge, not measured)** |

The goal of tuning is to get the speed while keeping the misses at a level you accept.

### Your numbers for exact search (the baseline)

| Measure | Value |
|---|---|
| Exact search time, one search | median **16.76 ms**, p95 19.79 ms |
| Exact recall@5 (the project's own metric) | **19/20 = 95%** |

---

## 2. How HNSW works (simple model)

**(general knowledge)**

Think of a **road map with several zoom levels**:

- **Top layer:** very few points, long-distance links, like highways between cities.
- **Middle layers:** more points, shorter links, like main roads.
- **Bottom layer (layer 0):** **all** the points, with short links to close neighbours, like streets.

### Search

1. Start at an entry point on the top layer.
2. Move greedily to the neighbour closest to the query, until no neighbour is closer.
3. Drop one layer down and keep going from there.
4. On the bottom layer, do a wider search: keep a **candidate list** of the best points found so far.
   The size of that list is **`ef_search`**.
5. Return the best `limit` points from that list.

### Why it can miss

The walk follows links. If the true nearest chunk is not reachable through the part of the graph the walk
explored, it is never looked at. A larger candidate list explores more, so it misses less, but it does more work.

### Build

Each new vector is inserted by searching for its neighbours (this search uses **`ef_construction`**) and linking
it to up to **`m`** of them. A careful build gives a better graph, but the build is slower.

---

## 3. The three knobs

| Knob | When it acts | Meaning | pgvector default | Your value | Changing it needs |
|---|---|---|---|---|---|
| `m` | Build time | Max links per point | 16 | **16** | Index rebuild |
| `ef_construction` | Build time | Candidate list size while building | 64 | **64** | Index rebuild |
| `hnsw.ef_search` | Query time | Candidate list size while searching | **40** | **100** | Nothing: set it per transaction |

Why this matters for tuning: **`ef_search` is the cheap knob.** It can be changed per query with no rebuild.
`m` and `ef_construction` need a new index, so they are changed rarely. Your Day 10 work tuned only `ef_search`
and says so.

Also **(general knowledge)**:

- Bigger `m` and `ef_construction` give a better graph (higher recall) but a bigger index and slower build.
- In pgvector, `ef_search` can be set between 1 and 1000, and `ef_construction` must be at least `2 * m`.
- `ef_search` must be **at least as large as `limit`** for a plain HNSW scan to return `limit` rows
  (shown in your measurements in section 6, Experiment C).

---

## 4. HNSW in pgvector: setup

### 4.1 The extension and the column

```sql
CREATE EXTENSION vector;                       -- in your project this runs inside an Alembic migration

-- column: embedding vector(1024)              -- Jina jina-embeddings-v3 gives 1,024 numbers
```

Your smoke test showed that the column rejects a wrong size:

```
INSERT a chunk with the vector [1,2,3]  ->  ERROR:  expected 1024 dimensions, not 3
select embedding <=> embedding          ->  0.0000   (cosine distance of a vector to itself)
```

### 4.2 The index

```sql
CREATE INDEX ix_chunks_embedding_hnsw
ON chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

The **operator class must match the query operator**:

| Distance | Query operator | Index operator class |
|---|---|---|
| Cosine distance | `<=>` | `vector_cosine_ops` (**the one you use**) |
| L2 distance | `<->` | `vector_l2_ops` |
| Inner product | `<#>` | `vector_ip_ops` |

Your Step 3 test proved this trap: an `ORDER BY embedding <-> q` (L2) query on the cosine index still did a
`Seq Scan`, with no error message.

### 4.3 The query shape that can use the index

```sql
SELECT chunks.*, 1 - (embedding <=> :q) AS score
FROM chunks
WHERE tenant_id = :tenant
ORDER BY embedding <=> :q      -- the raw distance, ascending
LIMIT :limit;
```

- **`ORDER BY` must use the raw distance expression** (`embedding <=> :q`), not the score. An index can only
  speed up the ordering by the same expression it was built for.
- **`LIMIT` is required** for the index to make sense.
- Cosine **score = 1 - distance**, so a higher score means more similar.

### 4.4 Settings at query time

```sql
-- inside one transaction; the third argument `true` makes it behave like SET LOCAL
select set_config('hnsw.ef_search', '100', true),
       set_config('hnsw.iterative_scan', 'relaxed_order', true);
```

`true` means the values **end with the transaction**, so a pooled connection does not carry them to the next
request. Your Step 11 test [B] proved that: the next transaction on the same connection showed `40 / off` again.

---

## 5. Your project setup and data

| Item | Value |
|---|---|
| Database | PostgreSQL 16.15, pgvector 0.8.7 (0.8 is the version with iterative scans) |
| Corpus | 234 documents, **2,097 chunks**, one tenant (`Corpus Tenant`) |
| Embeddings | `jina-embeddings-v3`, 1,024 dimensions, query task `retrieval.query` |
| Load | 2,097 chunks upserted in **8.72 s** (`ON CONFLICT (document_id, chunk_index) DO UPDATE`) |
| Table | `chunks`: tenant_id, document_id (both with `ON DELETE CASCADE`), text, heading, hash, `embedding vector(1024)`, model name, metadata |
| Index | `ix_chunks_embedding_hnsw`, cosine, `m=16`, `ef_construction=64` |
| Test queries | 20 labelled queries: 15 English, 4 Hinglish, 1 Hindi |
| First real plan | `Index Scan using ix_chunks_embedding_hnsw`, execution time **1.565 ms** (Step 4) |

**The recall rule used in the project:** a query is a *hit* when at least one of its top 5 chunks belongs to a
correct document. `recall@5 = hits / 20`. Finding one correct chunk is enough.

> **Two different "recall" ideas** (do not mix them):
> 1. `recall@5` (19/20) measures **retrieval quality**: did a correct page show up? Even exact search gets 19/20.
> 2. **"Same as exact"** (100/100) measures **HNSW quality**: did HNSW return the same chunks as exact search?
>
> Only the second one tells you what HNSW loses.

---

## 6. The tuning process with results

The method: **fix the questions, change one thing, measure, repeat.**
Query vectors were saved once (`scripts/results/day10_query_vectors.json`), so every run used exactly the same
vectors and made no embedding API call.

### Experiment A: exact search (the baseline, Step 8)

`set local enable_indexscan = off` forces a table scan. The script checks the plan and stops if HNSW is
still in it.

Result: **19/20 = 95%**. The one miss, Q14 ("standard enthalpy of formation of liquid water"), is a data
problem, not an index problem: the labelled page is a table of numbers.

Useful observations from this baseline:

- 14 queries have a correct chunk at rank 1, 3 at rank 2, 2 at rank 3, 1 misses.
- By language: English 14/15, Hinglish 4/4, Hindi 1/1.
- Q18 (Hinglish, "Le Chatelier ka principle kya hai?") is right at rank 1 with a score of only **0.3588**, while
  the wrong top result of Q14 scores **0.5488**. One fixed score threshold could not separate these two.

### Experiment B: HNSW against exact (Step 9)

Three modes, 220 searches each (1 round for results + 10 timed rounds), with the plan **proved by PostgreSQL's
usage counters**, not guessed from `EXPLAIN`:

| Mode | How | Index actually used | Same as exact | Median | p95 |
|---|---|---|---|---|---|
| exact | `enable_indexscan = off` | full table scan (220) | baseline | **16.76 ms** | 19.79 ms |
| planner | no settings (what the API does) | `ix_chunks_embedding_hnsw` (220) | 100/100, same order | **3.30 ms** | 4.0 ms |
| hnsw (forced) | `enable_seqscan/bitmapscan/sort = off` | `ix_chunks_embedding_hnsw` (220) | 100/100, same order | **3.39 ms** | 4.0 ms |

Findings:

- At the default `ef_search = 40`, HNSW lost **nothing**: 100 of 100 exact chunks, same order, all 20 queries.
- HNSW was about **5 times faster** (3.39 vs 16.76 ms).
- The API already used HNSW for the Corpus Tenant without any change.
- The saving is about 13 ms, which is small next to embedding the query (**0.5 to 0.9 s** with Jina).

### Experiment C: tuning `ef_search` (Step 10)

HNSW forced; only `ef_search` changed. Each value: 20 queries, 1 result round + 10 timed rounds, plus 20 searches
with `limit = 50`.

| `ef_search` | Exact top-5 found | recall@5 | Median ms | p95 ms | Rows returned for `limit=50` | Queries that lost chunks |
|---|---|---|---|---|---|---|
| 5 | **93/100** | **18/20** | 3.65 | 4.51 | **5** | Q11: 1, Q14: 1, **Q17: 5** |
| 10 | 100/100 | 19/20 | 3.79 | 4.59 | 10 | none |
| 20 | 100/100 | 19/20 | 3.63 | 3.97 | 19 to 20 | none |
| 40 (default) | 100/100 | 19/20 | 3.61 | 3.98 | **39 to 40** | none |
| **100** | 100/100 | 19/20 | 4.22 | 7.2 | **50** | none |
| 200 | 100/100 | 19/20 | 4.41 | 4.99 | 50 | none |
| 400 | 100/100 | 19/20 | 5.07 | 6.0 | 50 | none |

A second run gave the same results, with higher times at every value:

| `ef_search` | 5 | 10 | 20 | 40 | 100 | 200 | 400 |
|---|---|---|---|---|---|---|---|
| median ms, run 1 | 3.65 | 3.79 | 3.63 | 3.61 | 4.22 | 4.41 | 5.07 |
| median ms, run 2 | 4.74 | 4.47 | 4.70 | 4.96 | 5.27 | 5.84 | 6.10 |

#### What the table teaches

1. **Too small fails silently.** At `ef_search = 5`, Q17 ("FastAPI ke saath background tasks kaise chalate
   hain?") lost **all 5** of its correct chunks and returned 5 *other* pages. The response still had 5 results,
   so nothing signalled the problem.
2. **From 10 upward the top 5 is complete.** A bigger list cannot improve a perfect top 5.
3. **Time:** from 5 to 40 the differences are smaller than the difference between the two runs (noise). From 100
   to 400 the median rose in both runs, about 1.5 ms from 40 to 400 at this size.
4. **`ef_search` caps the number of rows a plain HNSW scan can return.** Asking for 50 rows with the index forced
   returned about `ef_search` rows. So if the API allows `limit` up to 50, `ef_search` must be at least 50.

#### Why 100 was chosen

| Reason | Evidence |
|---|---|
| Far above the failure point | 5 lost results; 10 and above were complete |
| At least the largest allowed `limit` (50) | 100 is the first tested value that returned 50 rows |
| No benefit above it | 200 and 400 did not improve the top 5, only cost time |
| Cheap | about 0.3 to 0.6 ms slower than 40 in two runs |
| Safety margin | 20 queries on 2,097 chunks is a small test; the lowest value that worked (10) is not trusted to hold on a bigger corpus |

> 50 would also satisfy the `limit` rule but was never tested, so 100 is the next tested value with a margin.
> That reasoning is an inference from the table, not a statement in the project notes.

### Experiment D: what the planner chooses (Step 11 and Step 3)

PostgreSQL decides between the HNSW index, the tenant index and a table scan. You do not control it.

| Situation | What PostgreSQL used |
|---|---|
| Empty-ish table (200 test rows) | Table scan (cheaper than the index) |
| Corpus Tenant, `limit` 5, 10, 20 | HNSW index |
| Corpus Tenant, `limit` 40, 41, 45, 50 | **Full table scan** (exact) for that query |
| Tenant with 3 chunks (Step 6) | `ix_chunks_tenant_id` (exact search of that tenant) |

So the live API did **not** lose rows at `limit = 50`, because the planner chose exact search there.
But the risk remains whenever the planner *does* pick HNSW with a large limit or a tenant filter, which is why
the app sets both settings in section 8.

---

## 7. The tenant-filter problem and `iterative_scan`

### The problem

With an HNSW index, the order of work is:

1. HNSW finds its nearest candidates over **all tenants** (up to `ef_search`).
2. **Then** `WHERE tenant_id = ...` removes rows from other tenants.

If your tenant is small, almost all candidates belong to someone else and are removed. You can get **too few
rows, or zero**, although the tenant has matching chunks.

### Your measured proof (Step 6): a tenant with 3 chunks next to 2,097

| Plan | Results |
|---|---|
| Planner free to choose (used the tenant index, exact) | **3** (correct) |
| HNSW forced, `ef_search = 40` | **0** |
| HNSW forced, `iterative_scan = relaxed_order` | **3** |

### The fix: `hnsw.iterative_scan`

**(general knowledge, pgvector 0.8+)**

| Value | Behaviour |
|---|---|
| `off` | Default. One pass, no extra scanning. |
| `strict_order` | Keeps scanning until enough rows pass the filter, results stay in exact distance order. |
| `relaxed_order` | Keeps scanning until enough rows pass, results can be **slightly out of order** (faster, higher recall). |

It stops when enough rows are found or when it has scanned `hnsw.max_scan_tuples` rows (default 20,000).

Your app uses **`relaxed_order`** and then **sorts the results by score again** in Python, which fixes the small
ordering looseness.

---

## 8. Final settings used by the app

`src/ai_service/services/search_service.py`:

```python
HNSW_EF_SEARCH = 100
HNSW_ITERATIVE_SCAN = "relaxed_order"
```

Applied before each search with `set_config(..., true)` (transaction only). The same values are keyword
arguments, so measurement scripts can pass the old values (`40`, `off`) and still reproduce Steps 8 to 10.

### Verification on PostgreSQL with HNSW forced (Step 11)

| Check | Old values (`40` / `off`) | New values (`100` / `relaxed_order`) |
|---|---|---|
| [A] `limit = 50`, rows per query | 39 to 40 | **50** |
| [A] results sorted by score | True | True |
| [B] next transaction on the same pooled connection | `40 / off` | settings did **not** leak |
| [C] tenant with 3 chunks, 20 queries | **0 rows** | **3 rows** |
| [C] rows of another tenant | 0 | 0 |
| [D] after rollback | 2,097 chunks, 0 temporary tenants | clean |

Over HTTP after a restart: `limit=5` returned 5 results, `limit=50` returned 50, same top score 0.7603, sorted.

Checks that passed: `ruff`, `mypy` (76 files), `pytest` (231 tests).

### Known weak spots of this setup

- **No automated test covers these settings.** Tests run on SQLite, which has no pgvector. The checks above were
  manual.
- `max_scan_tuples` (20,000): in a much larger table, a small tenant might still get fewer rows than asked. Not tested.
- A strict `threshold` may make iterative scan walk much further. Not measured.
- Speed with iterative scan on was not measured.

---

## 9. Isolation and deletion checks

`scripts/verify_isolation.py` ran on the real PostgreSQL and the running API. It tested tenant 2 with:

- **[A] zero documents:** 225 vectors, each with exact, planner and forced HNSW plans: **0 rows** every time.
- **[B] exact copies of 5 corpus chunks** (same text and vectors, the hardest case):
  tenant 2 got only its own 5 chunks; across **11,250 result rows per plan**, the Corpus Tenant never got a copy
  of tenant 2's chunks.
- **[C] delete:** 5 chunks before, 0 after `DELETE` by the owner (HTTP 204). A delete by another tenant got
  **HTTP 404** and removed nothing. Searching with the deleted vectors returned 0 rows under all three plans.

Why deletion is simple in pgvector: the vector is a column of the `chunks` row, and
`ON DELETE CASCADE` removes it **in the same transaction** as the document.

Limit: "disappear" means rows are deleted and no search returns them. PostgreSQL keeps dead rows until `VACUUM`.
That is not proof of physical erasure.

---

## 10. Qdrant: HNSW and the comparison

### 10.1 The trap in Step 16: Qdrant had no HNSW at first

After copying 2,097 chunks, Qdrant returned 100/100 against exact search, but its report said
`indexed_vectors_count: 0`. The vectors take about **8,388 KB** (2,097 x 1,024 x 4 bytes), below the default
`indexing_threshold` of **10,000 KB**, so Qdrant built no graph and every search compared all vectors (exact).
A perfect score from exact search says nothing about HNSW.

### 10.2 The fix in Step 17: same graph settings as pgvector

| Setting | Qdrant value | Default seen | pgvector equivalent |
|---|---|---|---|
| `m` | 16 | 16 | `m = 16` |
| `ef_construct` | 64 | 100 | `ef_construction = 64` |
| `hnsw_ef` (sent with each search) | **100** | not set | `hnsw.ef_search = 100` |
| `indexing_threshold` | 100 KB | 10,000 KB | no equivalent |
| `full_scan_threshold` | 10 KB (smallest allowed) | 10,000 KB | no equivalent |

Name map to avoid confusion:

| Build time | Search time |
|---|---|
| pgvector `ef_construction` = Qdrant `ef_construct` | pgvector `ef_search` = Qdrant `hnsw_ef` |

Both stores use **100** at search time. Qdrant's value was copied from pgvector so the comparison is fair;
**it was not tuned separately.**

### 10.3 Proving which search ran (Qdrant counters)

| Counter | What really ran |
|---|---|
| `filtered_large_cardinality` | The HNSW graph, with the filter |
| `filtered_small_cardinality` | Matching points found through the payload index, scored one by one (no graph) |
| `filtered_plain` | Scored one by one (no HNSW search) |

- Before the change: `{'filtered_plain': 738}`.
- After: Corpus Tenant searches showed `filtered_large_cardinality: 80` (20 queries x 4 segments), so the graph was used.
- A tenant with 3 points used no graph (`filtered_plain` and `filtered_small_cardinality`) and got all 3 points.

### 10.4 Head-to-head results (`scripts/compare_stores.py`, two runs)

Same 20 saved query vectors, same `VectorStore.search` interface, both with HNSW (`m=16`, build ef 64,
search ef 100). "Exact" means PostgreSQL comparing the query with every chunk.

| | pgvector | Qdrant |
|---|---|---|
| top 5: exact results returned | **100/100** | 99/100 |
| top 50: exact results returned | **1000/1000** | 990/1000 |
| top 5: median time (run 1 / run 2) | 4.70 / 4.73 ms | 4.67 / 4.76 ms |
| top 5: p95 | 5.62 / 5.98 ms | 7.34 / 6.30 ms |
| top 50: median time | 20.85 / 20.99 ms | **7.18 / 6.54 ms** |
| top 50: p95 | 23.28 / 23.30 ms | **10.56 / 9.50 ms** |

Where Qdrant missed: Q18 (Le Chatelier, Hinglish): 1 chunk in the top 5 and 9 in the top 50; Q2: 1 chunk in the top 50.

Reading the table honestly:

- **Top 5: same speed.**
- **Top 50: the gap is not the index.** The pgvector query loads whole rows, including the unused 1,024-number
  vector. Same SQL, HNSW forced, 200 timed searches each:

  | pgvector query | limit 5 | limit 50 |
  |---|---|---|
  | whole rows, with the vector (the app today) | 4.30 ms | **21.20 ms** |
  | without the vector column | 3.12 ms | **3.60 ms** |

  Not loading the vector is an easy improvement for `search_chunks`. **It has not been applied.**
- **Recall:** Qdrant missed a few near chunks and pgvector none. This is one graph of each on a very small set.
  Qdrant returned 100/100 in an earlier run (Step 17), so it does **not** prove one index is better.
- The pgvector row at limit 50 is **not a test of its HNSW**: the planner used an exact scan there (section 6, D).
- All these times are small next to the embedding call (0.5 to 0.9 s).

### 10.5 Operating differences

| Topic | pgvector | Qdrant |
|---|---|---|
| What you run | The PostgreSQL you already have + an extension | One more service (image about 74 MB; 59 MiB memory when empty) and one more volume |
| Schema changes | Alembic migrations | Code creates the collection and its settings |
| Copies of the data | One | Two: PostgreSQL is the main copy, Qdrant also holds the text |
| Deleting a document | Foreign key cascade, **same transaction** | App must delete from Qdrant too. Delete there first; no shared transaction |
| Keeping in step | Nothing to do | No automatic sync: run `copy_to_qdrant.py` before switching |
| Tenant isolation | `WHERE tenant_id = ...` | Filter inside the query + tenant payload index |
| Small tenant + filter | **0 of 3 chunks** with HNSW until `iterative_scan` was set | All 3 (plain scoring) |
| HNSW on small data | Planner picks HNSW or exact depending on tenant and limit | Built **no** index at default thresholds; they had to be lowered |
| Many results | At most `ef_search` rows without iterative scan | No such cap seen: 50 of 50 at `hnsw_ef` 100 |
| If the store is down | The whole app depends on PostgreSQL anyway | Search and delete return 503 `VECTOR_STORE_UNAVAILABLE`; `/health` stays 200 |
| Security setup | Database login | This setup has **no API key**; port open to the local computer only |

### 10.6 The decision

**pgvector stays the default** (`VECTOR_STORE=pgvector`):

- As fast for the top 5 and missed nothing in these tests.
- Delete and tenant data stay correct through one transaction and foreign keys, with no second copy.
- One service less to run, back up and secure.

Look at Qdrant again if the vectors grow far beyond what was tested and PostgreSQL search becomes too slow or
heavy for the database that also serves the app, or if filtered search over many tenants becomes a problem that
`iterative_scan` and indexes cannot solve. Measure on your own data first. The `VectorStore` interface keeps the
switch small: one setting plus a data copy.

---

## 11. Traps and lessons

| # | Trap | What happened | Lesson |
|---|---|---|---|
| 1 | Silent recall loss | `ef_search = 5` returned 5 wrong pages with no error | Always measure against exact search; never trust "it returned results" |
| 2 | Row cap | Plain HNSW returns at most about `ef_search` rows | Keep `ef_search` >= the largest `limit` |
| 3 | Filter after index | A 3-chunk tenant got 0 rows with HNSW | Use `iterative_scan` for filtered search |
| 4 | Wrong operator | `<->` on a cosine index silently did a table scan | Match operator and operator class |
| 5 | Planner choice | `limit` 5-20 used HNSW, 40+ used a table scan | Prove the plan with usage counters, not just `EXPLAIN` |
| 6 | Pooled connections | Plain `SET` would stick to a pooled connection | Use `set_config(..., true)` (transaction only) |
| 7 | Qdrant built no index | 8,388 KB < 10,000 KB threshold | "Perfect recall" may just be exact search; check `indexed_vectors_count` |
| 8 | `localhost` slowdown | ~265 ms per Qdrant call vs ~5.9 ms with `127.0.0.1` (`::1` tried first, no connection reuse) | Check timings against the server's own timer |
| 9 | Heavy rows | Loading the vector made top-50 ~21 ms instead of ~3.6 ms | Do not select columns the response does not use |
| 10 | Embedding drift | Same text to Jina gave slightly different vectors (lowest cosine 0.999943; up to 0.0016 in one number) | Compare runs with **saved** vectors |
| 11 | Small test data | 200 rows made the planner prefer a table scan | Test on realistic sizes |
| 12 | Score is relative | Q18 right at 0.3588; Q14 wrong at 0.5488; one word changed the ranking | Do not use one fixed score threshold blindly; compare scores only for the same query text |
| 13 | Wrong test data | Identical random vectors gave all distances 0.0000 | Check the test data before trusting the result |

---

## 12. A repeatable tuning checklist

1. **Fix the data and queries.** Save labelled queries and **save the query vectors** so runs are identical.
2. **Build the exact baseline.** Force a table scan, confirm the plan, record recall and time.
3. **Measure the default HNSW** (`ef_search = 40`) against exact: same chunks? same order? time?
4. **Prove which index ran** with usage counters (`pg_stat_user_indexes`, `pg_stat_user_tables`).
5. **Sweep `ef_search`** (for example 5, 10, 20, 40, 100, 200, 400). For each: recall vs exact, median and p95, rows
   returned at your **largest `limit`**.
6. **Run the sweep twice** and compare the noise between runs before trusting small time differences.
7. **Pick the value** that is safely above the failure point, at least the largest `limit`, and cheap, not the
   lowest value that happened to pass.
8. **Test the filtered case** with a small tenant next to a big one; add `iterative_scan` if rows go missing.
9. **Apply settings per transaction** (`set_config(..., true)`) and test that they do not leak.
10. **Re-run after changes** and keep the old result files so the log still matches.
11. **Re-measure when the corpus grows.** `m` and `ef_construction` need an index rebuild; `ef_search` does not.
12. **Add an automated test on real PostgreSQL** (not SQLite) if the settings matter in production.

---

## 13. What was not measured

- Index build time, index size on disk, memory with data (pgvector and Qdrant).
- Anything beyond 2,097 vectors, or more than one big tenant; filtered search with many tenants.
- Concurrency or load: one request at a time on one laptop.
- Tuning of `m` and `ef_construction` (they need a rebuild).
- Speed with `iterative_scan` on; behaviour with a strict `threshold`; the `max_scan_tuples` limit.
- Qdrant `hnsw_ef` tuning; Qdrant over gRPC (only HTTP was used).
- Updates under load; physical erasure after delete (`VACUUM`).
- Why Qdrant's recall changed between runs: a graph change after re-upserting is suspected, **not tested**.

---

## 14. Glossary

| Term | Meaning |
|---|---|
| ANN | Approximate nearest neighbour search |
| HNSW | Hierarchical Navigable Small World: a layered graph index |
| recall | Share of the true nearest neighbours that the search returned |
| recall@5 (project metric) | Share of queries with at least one correct-document chunk in the top 5 |
| "same as exact" | Share of exact-search chunks that HNSW also returned |
| `m` | Max links per point in the graph (build time) |
| `ef_construction` / `ef_construct` | Candidate list size while building the graph |
| `ef_search` / `hnsw_ef` | Candidate list size while searching |
| cosine distance | `1 - cosine similarity`; 0 = same direction (`<=>` in pgvector) |
| score | `1 - distance` in your API; higher = more similar |
| planner | PostgreSQL's choice of how to run a query (index scan, table scan, ...) |
| iterative scan | pgvector 0.8 feature: keep scanning the index until enough rows pass the filter |
| `relaxed_order` | Iterative scan mode that may return rows slightly out of order |
| `max_scan_tuples` | Limit for an iterative scan (20,000 by default) |
| p95 | 95% of searches were faster than this time |
| segment (Qdrant) | A piece of a collection; Qdrant keeps one HNSW graph per segment |
| payload index (Qdrant) | An index on a metadata field, used by filters such as `tenant_id` |

---

### Files to open in the repo for details

| Topic | File |
|---|---|
| Step-by-step log with raw outputs | `notes/day-10/README.md` |
| pgvector vs Qdrant note | `notes/day-10/pgvector-vs-qdrant.md` |
| The search code | `src/ai_service/services/search_service.py` |
| Qdrant code | `src/ai_service/services/qdrant_store.py`, `vector_store.py` |
| Measurement scripts | `scripts/eval_search.py`, `compare_hnsw.py`, `tune_ef_search.py`, `compare_stores.py` |
| Saved results | `scripts/results/day10_*.json` |
