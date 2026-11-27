# Tokenizer Loading & LRU Caching Explanation

**Source file reference:** [`src/ai_service/services/tokenizer.py:55-59`](../../src/ai_service/services/tokenizer.py#L55-L59)

```python
@lru_cache(maxsize=4)
def load_token_counter(tokenizer_file: Path) -> BgeM3TokenCounter:
    """One loaded tokenizer per file: loading 17 MB for every document would be wasteful."""
    return BgeM3TokenCounter(tokenizer_file)
```

---

## 1. Code Breakdown

### `def load_token_counter(tokenizer_file: Path) -> BgeM3TokenCounter`
- **Input:** A `Path` object pointing to the local `tokenizer.json` file (typically from Hugging Face cache `models--BAAI--bge-m3/snapshots/.../tokenizer.json`).
- **Output:** An initialized `BgeM3TokenCounter` instance wrapping Hugging Face's `tokenizers.Tokenizer.from_file()`.
- **Purpose:** Exposes token counting and offset calculation (`offsets(text)`, `count(text)`) for chunking operations.

### `@lru_cache(maxsize=4)` (from Python standard library `functools`)
- **LRU** stands for **Least Recently Used**.
- It is a memoization decorator: it wraps the function and stores return values in an in-memory hash table keyed by the input arguments (`tokenizer_file`).
- **Cache Hit:** When `load_token_counter` is called with a path that has already been loaded, Python skips the function body entirely and instantly returns the existing object from RAM (0 ms latency).
- **`maxsize=4`:** Keeps up to 4 distinct tokenizer instances in memory simultaneously. If a 5th distinct path is requested, the least recently used entry is evicted.

---

## 2. Why This Is Necessary (Performance & Memory)

1. **Heavy Disk & Parsing Cost (~17 MB):**
   - The BGE-M3 `tokenizer.json` file is approximately **17 MB** of JSON data.
   - Reading 17 MB from disk and parsing the full vocabulary/merge table into memory takes ~50–150 ms of CPU time per call.

2. **Preventing High-Frequency Churn:**
   - In document ingestion or chunking pipelines, token counting is called repeatedly for every uploaded document and section.
   - Without caching, loading 100 documents would re-read and re-parse ~1.7 GB of JSON data from disk, creating severe CPU bottlenecks and GC memory churn.

3. **Singleton-Like In-Memory Reuse:**
   - With `@lru_cache`, the tokenizer is parsed into memory **exactly once** and reused across all subsequent document processing calls.
