# Reading order: Day 3 (branch `day-3`)

This branch is the project as it was at the end of **Day 3**: the base service. It has no LLM code yet.

## What was done on Day 3
- A FastAPI service with async SQLAlchemy and PostgreSQL, multi-tenant from the start (`tenants`, `users`, `documents`).
- Register and login with JWT; every request carries a tenant, and every query is scoped to it.
- Document upload (`.txt`, `.md`, `.pdf`), list, read and delete, with a content hash to refuse duplicates.
- Request ids and JSON logs, one error format for every error, and a Server-Sent Events demo route.
- Tests (including a tenant-isolation test), a `Dockerfile` and a `docker-compose.yml`.
- The decisions are `decisions.md` items 1 to 15, and the traps found are in `gotchas.md` (Day 3).

## Read in this order
1. `README.md`, then `notes/how-to-run-ai-service.md` (how to start it).
2. `src/ai_service/main.py`: the app, its lifespan and what is wired together.
3. `src/ai_service/core/`: `config.py` (settings), `exceptions.py` (the error format), `logging.py`, `middlewares.py` (request id and timing), `security.py` (passwords and tokens).
4. The database: `db/base.py`, `db/session.py`, then the models `db/models/tenant.py`, `user.py`, `document.py`.
5. The migration: `alembic/env.py`, then `alembic/versions/a7920f359d11_.py`.
6. How a route gets its data: `api/deps.py` (current user, database session), then the schemas `schemas/common.py`, `auth.py`, `document.py`.
7. Auth: `services/auth_service.py`, then `api/v1/auth.py`.
8. Documents: `services/document_service.py`, then `api/v1/documents.py`.
9. `api/v1/router.py` (all routes in one place) and `api/v1/stream.py` (the SSE demo).
10. The tests: `tests/conftest.py` first, then `test_auth.py`, `test_documents.py`, `test_isolation.py`, `test_middleware.py`, `test_stream.py`.
11. Running it: `Dockerfile` and `docker-compose.yml`.
12. Why it is built this way: `decisions.md` (Day 3) and `gotchas.md` (Day 3).
13. The explanation notes in `notes/`: start with `express_to_fastapi.md` (written for a MERN reader), then `request-response-cycle.md`, `async-engine-and-session.md`, `dependencies-deps-walkthrough.md`.

## To run and check it
```
uv sync
docker compose up -d        # PostgreSQL and the API (the API is on http://localhost:8000/docs)
uv run pytest -q
```

## Other days
Each day has its own branch with its own `READING-ORDER.md`: `day-5` (chat and extract), `day-6` (reliability and cost), `day-7` (prompt registry), `day-8` (router and parallel chain). `main` has the latest code and an index of all days. Days 1, 2 and 4 have no code in this repo; their notes and code are in the `ai-eng-learning` repository.
