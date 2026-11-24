# Reading order: all days (branch `main`)

`main` is the latest state of `ai-service`: the code of Days 3 to 8 and the React frontend. Every day also has its own branch, which is the project exactly as it was at the end of that day and has a `READING-ORDER.md` for that day alone.

## To see what was done on one day
1. Switch to that day's branch: `git switch day-N` (or open `tree/day-N` on GitHub).
2. Read its `READING-ORDER.md`: what the day built and measured, then the files in the order to read them.
3. To see only what that day changed: `git diff day-<previous>..day-N` (for example `git diff day-7..day-8`).

## The days
| Day | Branch | What was built | Read first on that branch |
|---|---|---|---|
| 1, 2 | not in this repo | Python fundamentals (`docstore`, retry, timing, fetcher) | `ai-eng-learning/day-1`, `day-2` (README first) |
| 3 | `day-3` | the base service: FastAPI, async SQLAlchemy, tenants, auth, documents, middleware, SSE, tests, Docker | `READING-ORDER.md`, then `src/ai_service/main.py` |
| 4 | not in this repo | LLM fundamentals: tokens, sampling, a terminal chat, hallucination test | `ai-eng-learning/day-4` |
| 5 | `day-5` | `POST /chat` (streaming, stored) and `POST /extract` (structured output) | `READING-ORDER.md`, then `services/llm.py` |
| 6 | `day-6` | the model layer: retry, fallback, limits, cost logging, prompt caching, 50 messy inputs | `READING-ORDER.md`, then `services/llm_types.py` |
| 7 | `day-7` | prompt registry and a measured comparison of four prompt versions | `READING-ORDER.md`, then `services/prompt_registry.py` |
| 8 | `day-8` | intent router, parallel summarise chain, LangGraph router and comparison | `READING-ORDER.md`, then `services/router_service.py` |
| 9, 10 | not in this repo | embeddings (corpus, search, benchmark) and vector database notes | `ai-eng-learning/day-9`, `day-10` |
| extra | `main` | a React frontend in `frontend/`, run by `docker-compose.yml` on port 3000 | see "The frontend" below |

## The whole project, step by step
`notes/00-reading-guide.md` walks the full code from `main.py` through Day 6 (it has not been extended for Days 7 and 8, whose orders are in the branch files above). `notes/README.md` indexes the notes of every day, and each day's questions are in `notes/day-N/questions/`. For why things are built the way they are, read `decisions.md` (63 numbered decisions, by day) and `gotchas.md` (the traps found by running the code).

## The frontend (`frontend/`)
Read in this order: `frontend/README.md`, then `src/api/client.js` (every call to the API, and how the chat stream is read), `src/auth/AuthContext.jsx`, the pages in `src/pages/` (`Login`, `Chat`, `Documents`, `Summarise`, `Extract`, `RouterPlayground`), the shared components in `src/components/` and `src/components/ui/`, and last `Dockerfile` and `nginx.conf` (how it is served and how `/api` is forwarded to the API).

## To run it
```
uv sync
docker compose up -d --build     # postgres, redis, the API (8000) and the frontend (3000)
uv run pytest -q
```
Copy `.env.example` to `.env` and set `LLM_API_KEY` first. The API documentation is at `http://localhost:8000/docs`.
