# Runbook

How to run the project on your machine, how to switch the model, and the five problems you are
most likely to hit. Every command here was run on `main` (including the analysis endpoint, PR #19)
unless it says "not verified".

## Run it locally

You need Python 3.11, Node 20 and Docker (for Postgres). From the repo root:

```bash
# 1. Database: Postgres 16 with pgvector on localhost:54329 (user app, password app: local only)
docker compose -f infra/docker/docker-compose.yml up -d postgres

# 2. Backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements-dev.txt
cd backend
export DATABASE_URL=postgresql://app:app@localhost:54329/timecards
export LLM_PROVIDER=mock               # no model, no key: see "Switch the model" below
python -m app.ai.ingest                # only if you use RETRIEVER=pgvector (loads the 7 policy clauses)
uvicorn app.main:app --port 8000       # http://localhost:8000/health and /docs
```

Retrieval defaults to `RETRIEVER=memory` (keyword match, no embedding download). For the real
embeddings set `RETRIEVER=pgvector` and run the `ingest` line once (the first run downloads a
64 MB model).

```bash
# 3. Demo data: four known cases, one of each type (API must be running)
scripts/demo_seed.sh --reset           # --reset empties the tables first (needs the compose container)

# 4. Frontend (new terminal)
cd frontend && npm ci
echo "VITE_USE_MOCK=false" > .env.local   # without this the UI shows built-in mock data
npm run dev                            # http://localhost:5173, proxies /api to :8000
```

`ENABLE_DEV_SEED` stays **off** (the default). It only enables `POST /api/dev/seed`, which wipes
all data; with it off that endpoint answers 404. Turn it on only on your own machine:
`export ENABLE_DEV_SEED=true` before starting the API.

Stop: `Ctrl+C` the servers, then `docker compose -f infra/docker/docker-compose.yml down`.

Docker for the whole stack (`make up`) starts Postgres and the frontend (built on mock data by
default); the backend container is behind the `api` profile:
`docker compose -f infra/docker/docker-compose.yml up -d --build` (the backend starts with the rest, because the frontend image proxies `/api` to it; not verified).

## Switch the model

Everything is in environment variables, read at each request. Put them in `backend/.env`
(gitignored; copy `.env.example`), then restart the API.

| You want | Set |
|---|---|
| No model, nothing to break (tests, recording) | `LLM_PROVIDER=mock` (and optionally `LLM_MOCK_MODE=ok\|timeout\|rate_limited\|invalid_json\|bad_citation\|low_confidence` to show a failure on purpose) |
| Groq, small model (default) | `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL=https://api.groq.com/openai/v1`, `LLM_MODEL=openai/gpt-oss-20b`, `LLM_API_KEY=<your key>` |
| Groq, bigger model | same, `LLM_MODEL=openai/gpt-oss-120b` |
| Offline, local model | `LLM_PROVIDER=ollama`, `LLM_BASE_URL=http://localhost:11434/v1`, `LLM_MODEL=qwen2.5:7b-instruct` (Ollama running) |

Keep the real model under its limit with `LLM_MAX_RPM` (default 5 calls a minute per API process;
`0` turns it off; the mock is never limited). If a call would have to wait longer than the 25 s
proposal deadline, that proposal uses the rule-based fix instead. With several replicas the limit
is per replica. The key goes only in `backend/.env`, never in git, never in a message.

How to tell which model answered: `source` on the proposal is `LLM` or `RULES_FALLBACK`, `model`
holds the model name, and `/metrics` shows `proposals_created_total` by source.

## The five most likely problems

**1. The API answers 503 `DB_UNAVAILABLE` or `DB_NOT_CONFIGURED`, or `/health` says degraded.**
The database is not running or `DATABASE_URL` is not set in the shell that started uvicorn.
Fix: `docker compose -f infra/docker/docker-compose.yml up -d postgres`, wait for `healthy`
(`docker ps`), `export DATABASE_URL=postgresql://app:app@localhost:54329/timecards`, restart the
API, check `curl localhost:8000/health`.

**2. The screens show a "Mock data" badge, or new cases never appear.**
The dev server is using the built-in mock API. Fix: `frontend/.env.local` with
`VITE_USE_MOCK=false`, then restart `npm run dev`. The API must be on port 8000.

**3. Every proposal says `RULES_FALLBACK` with a real model.**
The model call failed and the safe fallback worked as designed. Look at the case's audit events
for `LLM_ERROR` and at `llm_errors_total{kind=...}` on `/metrics`: `provider_429` means the
provider's limit (lower `LLM_MAX_RPM`, wait a minute), `local_rate_limit` means our own limit made
a call wait past the deadline, `timeout` means a slow model (raise `LLM_TIMEOUT_SECONDS`), `other`
is usually a wrong `LLM_BASE_URL`, `LLM_MODEL` or a missing/invalid `LLM_API_KEY`. To record a
demo without this risk, use `LLM_PROVIDER=mock`.

**4. A proposal is `ESCALATED` with reason `NO_POLICY_MATCH`, or the first pgvector call is slow.**
With `RETRIEVER=pgvector` the policy table is empty until you run `python -m app.ai.ingest`
(from `backend/`, with `DATABASE_URL` set). The first embedding call downloads a 64 MB model; wait
for it, or use `RETRIEVER=memory`. Check what retrieval returns:
`curl 'localhost:8000/api/policies/search?q=missing+clock+out'`.

**5. A demo call fails with 409 `TIMECARD_EXISTS`, or `/api/dev/seed` gives 404.**
Both are intended. `POST /api/analysis/run` refuses a timecard id that is already saved (so a
repeat cannot create duplicate cases): send new ids, or run `scripts/demo_seed.sh --reset` to
start from an empty list. The 404 means `ENABLE_DEV_SEED` is not `true` on that server, which is
the safe default.

Other quick checks: port 8000 or 5173 busy (`lsof -i :8000`, then stop it or pick another port);
`pip install` fails on a wrong Python (use 3.11); run `scripts/clean_clone_check.sh` to see
whether a fresh clone passes.
