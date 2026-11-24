# Reading order: Day 8 (branch `day-8`)

This branch is the project at the end of **Day 8**: LLM orchestration. It is the Day 7 service plus an intent router, a parallel summarise chain and a LangGraph version of the router.

## What was done on Day 8
- **Router** (`POST /route`): regex rules first, then a small-model classifier (`classify_query` v5, which adds an `unclear` label), then a specialised prompt per intent; an unclear request goes to the large model. Accuracy: 90 of 90 on the Day 7 sets plus 30 new queries, with all 6 unclear queries sent to the large model.
- **Cost and latency** against always-large (`gemini-3-flash-preview`, 14 queries): $0.001504 against $0.003265 per query (53.9% cheaper; 74.2% on clear-intent queries) with equal rubric quality, but slower (median 4.63 s against 3.8 s). About 450 of the large model's 544 output tokens were thinking tokens.
- **Parallel chain** (`POST /summarise`): `asyncio.gather` with `Semaphore(4)`, one retry, a stop after more than 2 failures. 10 documents took 10.24 s in parallel against 25.74 s one by one; with one injected failure it returned 9 of 10 and reported the failed one.
- **LangGraph router**: 145 against 119 lines, identical requests on 14 live queries (no hidden prompts), about 2 ms extra per request, a 10-frame traceback against 2, and 20 new packages. The API keeps the plain code.
- The decisions are `decisions.md` items 45 to 63, and the traps found are in `gotchas.md` (Day 8).

## Read in this order
1. The concepts and results notes: `ai-eng-learning/day-8/README.md`, then its `learning/` and `Notes/` (the task 1 to 5 notes have the real numbers). In this repo: `notes/day-8/questions/README.md`.
2. `decisions.md` (Day 8, items 45 to 63) and `gotchas.md` (Day 8).
3. **The router:** `src/ai_service/prompts/classify_query/v5.md`, then `services/router_service.py` (rules, classifier, dispatch), `schemas/route.py`, `api/v1/route.py`, and `tests/test_router.py`.
4. **The specialised prompts:** `scripts/build_router_prompts.py` makes `prompts/router_<intent>/v2.md` from the handbook.
5. **The measurements:** `scripts/run_router_eval.py` (accuracy), then `scripts/run_router_cost_eval.py` (cost and latency), with the data in `scripts/data/` (`router_queries.json`, `router_cost_sample.json`) and the results in `scripts/results/`.
6. **The large model and thinking tokens:** `services/llm_openai.py` (usage counts thinking tokens), `core/config.py` (`LLM_LARGE_MODEL`), `services/llm.py` (the `hosted_large` backend), `services/llm_types.py`.
7. **The parallel chain:** `prompts/summarise_document/v1.md` and `combine_summaries/v1.md`, then `services/summarise_service.py`, `schemas/summarise.py`, `api/v1/summarise.py`, and the tests `tests/test_summarise.py` and `tests/test_summarise_api.py`. The run is `scripts/run_summarise_demo.py`.
8. **LangGraph:** `services/router_graph.py`, then `tests/test_router_graph.py`, then the comparison `scripts/run_router_framework_compare.py`.
9. The line-by-line explanation of the graph file: `notes/day-8/questions/05-router-graph-line-by-line.md`.

## To run and check it
Copy `.env.example` to `.env`; for the router, set `LLM_LARGE_MODEL` to use a stronger model for unclear requests.
```
uv sync
docker compose up -d --build api     # rebuild after new migrations or packages
uv run pytest -q
```
Then try `POST /route` and `POST /summarise` (upload some documents first) at `http://localhost:8000/docs`.

## Other days
`day-3`, `day-5`, `day-6`, `day-7`, and `main` (everything, plus the React frontend and an index of all days).
