# Reading order: Day 6 (branch `day-6`)

This branch is the project at the end of **Day 6**: reliability, cost and provider abstraction. It is the Day 5 service with a proper model layer.

## What was done on Day 6
- An `LLMClient` (generate, stream, structured output) with Gemini, Anthropic and Ollama adapters chosen by config, wrapped in layers: accounting, limits, retry with backoff and jitter, and a fallback to a second provider (checked live with a dead provider and a 25-request burst).
- Every call is logged (tokens, cached tokens, cost, latency, request id, tenant) into an `llm_calls` table.
- Prompt caching in `/chat` (a stable prefix first): cache reads of about 2,030 tokens from turn 2, about 30% cheaper per turn.
- A repair loop and sanity checks for `/extract`, and 50 messy inputs with zero unhandled errors (Gemini: 44 valid, 1 repaired, 5 correctly refused).
- A database pool bug found and fixed (10 of 25 simultaneous requests had failed). 162 tests, `mypy --strict` clean.
- The decisions are `decisions.md` items 25 to 38, and the traps found are in `gotchas.md` (Day 6).

## Read in this order
1. `notes/00-reading-guide.md` (the whole project, step by step; the Day 6 part is the last big section) and `notes/day-6/README.md` (the day's tasks mapped to notes).
2. `decisions.md` (Day 6) and `gotchas.md` (Day 6).
3. The types and the contract: `src/ai_service/services/llm_types.py` (requests, responses, the error taxonomy, the `LLMClient` protocol).
4. The adapters: `services/llm_openai.py` (Gemini and Ollama), then `services/llm_anthropic.py`.
5. The layers: `services/llm_wrappers.py` (limits, retry, fallback), then `services/llm_accounting.py` and `services/llm_cost.py` (what a call costs and how it is recorded).
6. The registry that builds the layers from settings: `services/llm.py`, with `core/config.py`.
7. The call log: `db/models/llm_call.py`, then the migration `alembic/versions/86fde07d2bc3_llm_calls.py`.
8. Where it is used: `services/chat_service.py` and `api/v1/chat.py` (caching, the `chat_system` prompt in `prompts/chat_system.md`), then `services/extract_service.py` (the repair loop) and `api/v1/extract.py`.
9. The tests: `tests/fakes.py`, then `test_llm_wrappers.py`, `test_llm_accounting.py`, `test_llm_anthropic.py`, `test_llm_registry.py`, `test_llm_provider.py`.
10. The 50 messy inputs: `scripts/messy_inputs.py` and `scripts/run_messy_inputs.py`, then the reports in `scripts/results/messy_inputs_report_*.json`.
11. The line-by-line explanations of the new files: `notes/day-6/explanation/`.

## To run and check it
Copy `.env.example` to `.env` (a Gemini key is enough; Anthropic and Ollama are optional).
```
uv sync
docker compose up -d --build api     # rebuild when alembic/ or the dependencies change
uv run pytest -q
```

## Other days
`day-3`, `day-5`, `day-7` (prompt registry), `day-8` (router and parallel chain), and `main` (everything, with an index).
