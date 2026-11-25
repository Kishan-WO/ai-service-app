# pgvector against Qdrant

What I saw after running the same search on both. The numbers are from my laptop with **2,097 chunks** of one tenant, 1,024-number vectors and 20 queries (2026-11-25). The commands and full outputs are in [README.md](README.md).

## 1. Search results and speed
Both searched through HNSW (`m = 16`, build `ef` 64, search `ef` 100), got the same saved query vectors, and were called through the same `VectorStore.search` (`scripts/compare_stores.py`, two runs). "Exact" is PostgreSQL comparing the query with every chunk.

| | pgvector | Qdrant |
|---|---|---|
| top 5: exact results returned | 100/100 | 99/100 |
| top 50: exact results returned | 1000/1000 | 990/1000 |
| top 5: median time (run 1 / run 2) | 4.70 / 4.73 ms | 4.67 / 4.76 ms |
| top 5: p95 | 5.62 / 5.98 ms | 7.34 / 6.30 ms |
| top 50: median time | 20.85 / 20.99 ms | 7.18 / 6.54 ms |
| top 50: p95 | 23.28 / 23.30 ms | 10.56 / 9.50 ms |

- **Top 5: the same speed.**
- **Top 50: the gap is not the index.** My pgvector query loads the whole chunk row, **including its 1,024-number vector**, which the response does not use. The Qdrant search does not return vectors. The same SQL in a separate test (200 timed searches each):

  | pgvector query | limit 5 | limit 50 |
  |---|---|---|
  | whole rows, with the vector (the app today) | 4.30 ms | 21.20 ms |
  | without the vector column | 3.12 ms | 3.60 ms |

  So not loading the vector is an easy improvement for `search_chunks`. **I have not changed it.**
- **Recall:** at these settings Qdrant missed a few near chunks (mostly for one Hinglish query, Q18) and pgvector missed none. This is one graph of each, on a very small set. I did not tune `hnsw_ef` in Qdrant, and it returned 100/100 in an earlier run. It does **not** show that one index is better.
- Both times are small next to embedding the query (about 0.5 to 0.9 s in my Jina calls).

## 2. What it was like to build and run

| Topic | pgvector | Qdrant |
|---|---|---|
| What I run | the PostgreSQL I already had, plus an extension | one more service (about 74 MB image; 59 MiB of memory when empty), one more volume |
| Schema changes | Alembic migrations | the code creates the collection and its settings |
| Copies of the data | one | two: PostgreSQL stays the main copy; Qdrant also holds the text |
| Deleting a document | the foreign key cascade removes the vectors **in the same transaction** | the app must delete from Qdrant too. I delete there first; there is no shared transaction |
| Keeping in step | nothing to do | no automatic sync: I run `copy_to_qdrant.py` before switching |
| Tenant isolation | `WHERE tenant_id = ...` | a filter inside the query, with a tenant payload index |
| Small tenant with a filter | **got 0 of its 3 chunks** when the HNSW index was used; `hnsw.iterative_scan` fixed it | got all 3, by plain scoring |
| Using HNSW on small data | the planner chose it or an exact scan, depending on tenant and limit | built **no** index at the default thresholds; I had to lower them |
| Many results | with HNSW, at most `ef_search` rows (40 by default) without iterative scan | I saw no such cap: 50 of 50 at `hnsw_ef` 100 |
| If the store is down | the whole app depends on PostgreSQL anyway | search and delete answer 503; the rest of the app is a separate database |
| Security setup | the database login | this setup has **no API key**, so the port is open to my computer only |

## 3. My decision
**I keep pgvector as the default.**
- At this size it was as fast for the top 5 and missed nothing in these tests.
- Delete and tenant data stay correct through one transaction and the foreign keys, with no second copy to keep in step.
- It is one service less to run, back up and secure.

**I would look at Qdrant again if** the vectors grew far beyond what I tested and PostgreSQL search became too slow or heavy for the database that also serves the app, or if filtered search on many tenants became a problem that `iterative_scan` and indexes did not solve. I would measure that on my own data first. The `VectorStore` interface keeps the switch small: one setting, plus a copy of the data.

## 4. Limits of this comparison
- 2,097 vectors, 20 queries, one tenant owning everything. At this size even exact search is fast (median 16.76 ms in pgvector, Step 9).
- One laptop, one request at a time: no load or concurrency test.
- Not measured: index build time, memory with data, disk size, filtered search with many tenants, updates under load.
- Qdrant was used over its HTTP API, not gRPC.
- A trap while measuring: with `QDRANT_URL=http://localhost:6333` every Qdrant call took about **265 ms**. "localhost" tried `::1` first while Qdrant listens on `127.0.0.1` only, and the client did not reuse the connection. With `127.0.0.1` it was 5.9 ms. My first comparison run showed 266 ms for Qdrant, which was this and not Qdrant's search (it reported 0.6 ms).
