# Reading order: Day 5 (branch `day-5`)

This branch is the project at the end of **Day 5**: the first LLM code. It is the Day 3 service plus chat and extract.

## What was done on Day 5
- `conversations` and `messages` tables, scoped by tenant, with an Alembic migration tested up and down on PostgreSQL.
- `POST /chat`: streams the answer over Server-Sent Events, sends the stored history to the model, and saves both messages with their token counts. A client disconnect cancels the stream, closes the upstream call and saves the partial answer as `cancelled`.
- `POST /extract`: structured output with a Pydantic schema (73 tests). In the hosted against local test, Gemini got 98% of the fields right and `gemma3:270m` got 39%, although 9 of its 10 replies were valid.
- The decisions are `decisions.md` items 16 to 24, and the traps found are in `gotchas.md` (Day 5).

## Read in this order
1. `notes/day-5-chat-and-extract.md`: the overview of the day. (Day 3's order is on the `day-3` branch.)
2. `decisions.md` (Day 5) and `gotchas.md` (Day 5).
3. The data: `src/ai_service/db/models/conversation.py`, then the migration `alembic/versions/c2b3d69c8756_conversations_and_messages.py`.
4. The model layer: `core/config.py` (the new LLM settings), then `services/llm.py` (the client interface, the Gemini and local adapters, and the shared types).
5. Wiring: `api/deps.py` (how a route receives the model client), `main.py`, `api/v1/router.py`.
6. Chat: `schemas/chat.py`, then `services/chat_service.py` (history, SSE events, saving, disconnects), then `api/v1/chat.py`.
7. Extract: `schemas/extract.py` (the target shape), then `services/extract_service.py`, then `api/v1/extract.py`.
8. The tests: `tests/fakes.py` (a fake model that needs no network), `tests/conftest.py`, then `test_llm_provider.py`, `test_chat.py`, `test_extract.py`.
9. The measurement: `scripts/compare_extract.py`, then its result `scripts/results/extract_comparison.json`.
10. The explanation notes: `notes/how-to-run-ai-service.md` and the other files in `notes/`.

## To run and check it
Copy `.env.example` to `.env` and fill in `LLM_API_KEY` (a Gemini key) and `LLM_MODEL`.
```
uv sync
docker compose up -d
uv run pytest -q
```
Then open `http://localhost:8000/docs` and try `POST /chat` and `POST /extract`.

## Other days
`day-3` (the base service), `day-6` (reliability and cost), `day-7` (prompt registry), `day-8` (router and parallel chain), and `main` (everything, with an index). Day 4 has no code in this repo; see `ai-eng-learning/day-4`.
