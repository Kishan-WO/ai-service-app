# Reading order: Day 7 (branch `day-7`)

This branch is the project at the end of **Day 7**: prompt engineering. It is the Day 6 service with every prompt moved into files, and a measured comparison of four prompt versions.

## What was done on Day 7
- A prompt registry: prompts are files `prompts/<name>/vN.md`, loaded by name and version with validated names, caching and strict templates (a missing variable is an error). All 5 `/extract` and `/chat` prompts moved into it.
- The prompt name and version are logged with every model call (a log line and two new `llm_calls` columns; the migration was tested up and down, and existing rows were kept).
- `classify_query` v1 to v4 and a hard 30-case labelled test set, run twice with identical answers: v1 86.7%, v2, v3 and v4 100%.
- Kept v2 at $0.086 per 1,000 calls: the rules gave the whole gain, examples added 30% cost and reasoning 103% for no accuracy. The decision and its limits are in `decisions.md`.
- The decisions are `decisions.md` items 39 to 44, and the traps found are in `gotchas.md` (Day 7).

## Read in this order
1. `notes/day-7/README.md` (the day's tasks and steps) and the concept notes in `ai-eng-learning/day-7/learning/`.
2. `decisions.md` (Day 7) and `gotchas.md` (Day 7).
3. The registry: `src/ai_service/services/prompt_registry.py` (`get_prompt(name, version)`, name validation, the cache, `StrictUndefined`).
4. The prompts: `src/ai_service/prompts/classify_query/v1.md` to `v4.md` (read them in order and see what each version adds), then `chat_basic/v1.md`, `chat_system/v1.md`, `extract_system/v1.md`, `extract_user/v1.md`, `extract_repair/v1.md`.
5. Logging the version: `services/llm_types.py` (the call context now carries the prompt name and version), `services/llm_accounting.py`, `db/models/llm_call.py`, the migration `alembic/versions/0a123456546c_llm_calls_prompt_version.py`, `core/logging.py`.
6. Where the registry is used: `core/config.py` (`CHAT_PROMPT_NAME`, `CHAT_PROMPT_VERSION`), `services/chat_service.py`, `api/v1/chat.py`, `services/extract_service.py`.
7. The measurement: `scripts/data/classify_cases.json` (the hard set), `scripts/data/classify_cases_easy.json` (the independent easy set), then `scripts/run_classify_eval.py`.
8. The results: `scripts/results/classify_eval_hard_run1.json` and `classify_eval_hard_run2.json` (two runs), then `classify_eval_easy_newprompts.json`.
9. The tests that changed: `tests/test_llm_registry.py` and `tests/test_chat.py` (they check that the chat prompt comes from the registry and that the name and version reach the call context).
10. The questions I asked while building: `notes/day-7/questions/`. The results note is `ai-eng-learning/day-7/Notes/tasks-1-6-prompt-registry-and-measured-prompts.md`.

## To run and check it
Copy `.env.example` to `.env` and fill in the Gemini key.
```
uv sync
docker compose up -d --build api     # the new migration is inside the image
uv run pytest -q
uv run python scripts/run_classify_eval.py    # runs the prompt versions on the hard set (uses the model API)
```

## Other days
`day-3`, `day-5`, `day-6`, `day-8` (router and parallel chain), and `main` (everything, with an index).
