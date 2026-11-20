# Testing

What is tested at which level, how to run each level, and the real numbers. Every number here was
measured by running the command shown. PRs #12 to #17, #19, #20 and #23 are merged, and anything not run says "not run".

## The levels

| Level | What it proves | Where | State |
|---|---|---|---|
| **Unit** | Each module does what it says, with the mock model | `backend/tests/unit/` | On `main` |
| **Integration (database)** | Real pgvector retrieval: ingest the policy, search it with real embeddings, calibrate the score threshold, and check every detector rule retrieves its own clause (7 tests) | `backend/tests/unit/ai/test_pgvector_integration.py` (marked `integration`) | On `main`. Needs a database, so it is skipped without one |
| **Integration (API)** | Every endpoint against a real Postgres: seed, propose, approve, reject, audit | `backend/tests/integration/test_api_flow.py` (PR #12) | On `main`. Skipped without `DATABASE_URL` |
| **Integration (analysis)** | A missed punch goes in through `/api/analysis/run`, comes out as a case, gets a validated proposal, and can be approved; duplicates and bad input are refused | `backend/tests/integration/test_analysis_flow.py` | On `main` (PR #19) |
| **System** | The real API in a separate process, over HTTP, against a pgvector database: the whole flow, plus LLM failure injection | `backend/tests/system/` | On `main` (PR #19). Never skipped: it fails if there is no database or Docker |
| **Golden set** | 40 timecard cases with expected results: detection, validator, the whole workflow under five model behaviours, prompt injection | `backend/tests/golden/` | On `main` (PR #15) |
| **Property tests** | The same promises on random timecards (never crashes, deterministic, 12 hour and 10 hour rules) | `backend/tests/unit/rules/test_properties.py` | On `main` (PR #15) |
| **Frontend** | Components, pages and the API client (with `fetch` faked) | `frontend/src/**/*.test.ts(x)` | On `main`: 35 tests |
| **Load** | Latency percentiles and error rate under concurrent use, mock model only | `scripts/load_test.py` | On `main` (PR #19). Local only |
| **Smoke (real model)** | The real model returns valid JSON and grounded citations | `backend/scripts/smoke_llm.py`, `docs/llm_smoke_report.md` | On `main`. One real run so far |
| **Real-model evaluation** | The golden set with a real model and the real validator | `python -m scripts.evaluate --real ...` | On `main` (PR #23). One real run so far, 2026-11-19 |
| **Clean clone** | A fresh clone installs, lints, tests and builds | `scripts/clean_clone_check.sh` | Docs PR |

## How to run each level

Backend (Python 3.11, from `backend/`, virtual environment active):

```bash
python -m ruff check .                         # lint
python -m pytest --ignore=tests/system         # no Docker: database tests skip, system tests left out
python -m pytest --cov=app --cov=scripts       # everything, with coverage (needs a database or Docker)
```

The database tests need a pgvector database, and the system tests need one too (they start their
own `pgvector/pgvector:pg16` container if `DATABASE_URL` is not set, and fail if they cannot).
Start a throwaway one and point `DATABASE_URL` at it (the password is a throwaway value for local
use only):

```bash
docker run -d --name pgv-test -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app \
  -e POSTGRES_DB=timecards -p 54329:5432 pgvector/pgvector:pg16
DATABASE_URL=postgresql://app:app@localhost:54329/timecards python -m pytest
docker rm -f pgv-test
```

System tests alone, load test and the clean-clone check:

```bash
python -m pytest tests/system -q               # about 9 s; starts a container if DATABASE_URL is unset
# load test: start the API with LLM_PROVIDER=mock, then (never against Groq):
python ../scripts/load_test.py --base-url http://127.0.0.1:8000 --flows 200 --concurrency 10
../scripts/clean_clone_check.sh                # fresh clone, install, lint, tests, frontend (no Docker)
```

The first run downloads the embedding model (about 64 MB, measured) into a temporary folder.

Golden set, report and property tests:

```bash
python -m pytest tests/golden                  # the golden set as tests
python -m scripts.evaluate                     # offline report, writes docs/evaluation_report.md
PROPERTY_EXPLORE=1 PROPERTY_EXAMPLES=2000 python -m pytest tests/unit/rules/test_properties.py
```

Smoke test and demo walk-through:

```bash
python -m scripts.smoke_llm --mock             # no key, no network
python -m scripts.demo_flow --help             # narrated run of one case
# real model, at most 5 requests a minute, key in backend/.env (never in git):
python -m scripts.smoke_llm --model openai/gpt-oss-20b --max-rpm 5
```

Frontend (Node 20, from `frontend/`):

```bash
npm ci && npm run lint && npm test && npm run build
npm run format:check                           # Prettier (PR #14)
```

## Real numbers

**Backend** (Python 3.11, run with a `pgvector/pgvector:pg16` container):

- **430 tests pass, 0 skipped**, in about 16 seconds. Ruff is clean. It was 381 before PR #19.
- Without a database (`--ignore=tests/system`, from a fresh clone): 375 pass, 36 skip (29 API
  integration and 7 pgvector tests).
- By level: 29 API integration tests (21 for the existing endpoints, 8 for analysis), 19 system
  tests, the rest unit, golden and property tests.
- Coverage of `app/` and `scripts/`: **98%** (2,613 statements, 64 missed); **100%** on
  `domain/rules` and `validator`, above the 90% target. The new `app/ai/guard.py` is at 97%.
  The system tests run the API in another process, so their lines are not counted.

**Frontend** (Node 20, fresh clone): **35 tests pass**; lint, Prettier check and build are clean.

**System test** (19 tests, about 9 seconds including starting a container): timecards in, propose,
validation, approve, reject (a comment is required), approving twice applies the fix once, an
escalated case cannot be approved, a case with no proposal cannot be approved, the audit log is
append-only (API and direct SQL), every decision is audited with who and when, a prompt-injection
note is stored and not obeyed, `ENABLE_DEV_SEED` off gives 404 and deletes nothing. Failure
injection with the mock LLM against the running API: timeout, HTTP 429, invalid JSON (retried once),
an invented citation, all fall back to a validated rule-based fix with the failure in the audit
trail and the counters in `/metrics`; low confidence escalates and the case cannot be approved. In
none of them is a timecard changed before a manager approves.

**Load test** (`scripts/load_test.py`; one uvicorn worker, mock LLM, in-memory retrieval, Postgres
in a local container; 200 flows, 10 at a time; each flow is analysis, propose, approve or reject,
and three reads): **1,200 requests, 0 errors, 252 requests per second**, median 38 ms, 95th
percentile 58 ms, 99th 66 ms (analysis 39 / 57 ms, propose 42 / 55 ms, approve 53 / 68 ms,
median / p95). It measures our code and the database, not a real model. The script refused
`api.groq.com` and a non-local host when tried.

**Clean clone** (`scripts/clean_clone_check.sh` on the PR #19 branch, before it was merged): venv and install,
ruff, 375 tests (36 skipped), the golden-set evaluation, the openapi file, and the frontend
`npm ci`, lint, Prettier, tests and build all passed.

The offline evaluation (mock model, no network; `python -m scripts.evaluate`, PR #15) gives:

- **40 of 40 golden cases pass** every check.
- Detection precision and recall are **100%** on every exception type (38 expected exceptions, 0
  missed, 0 extra). **0 false positives** on the 9 clean timecards.
- **378 of 378** deliberately bad fixes are rejected by the validator.
- In all five mock model behaviours (`ok`, `invalid_json`, `timeout`, `bad_citation`,
  `low_confidence`) the final proposal matched the safe outcome the policy requires, and no
  unvalidated fix ever reached a manager. With a working model, 7.9% of runs used the rule-based
  fix and 7.9% were escalated; these are the three cases whose rule-based fix itself breaks the
  rules.
- A simulated model that obeys hostile notes got **0 of 5** attacks through, with 0 tag breakouts.
- The property tests also passed on 2,000 fresh random examples each (about 14,000 cases).
- The evaluation catches regressions: changing one rule by one character in a scratch copy
  (`>` to `>=` on the 8 hour, 6 hour and 12 hour rules) failed the golden set each time.

**Real model smoke run** (Groq, `openai/gpt-oss-20b` and `openai/gpt-oss-120b`, 9 proposals per
model, 5 requests a minute): **0 call errors, 0 rate-limit responses, 100% valid JSON, 100% valid
citations, no fallbacks or escalations.** Median time per proposal 1.1 s (20b) and 1.6 s (120b);
95th percentile 1.8 s and 4.2 s. One injection note was not followed by either model.
**Caveat:** this run happened before the real validator existed, so it used an always-pass
validator. The real model with the real validator is in the next section.

**Real-model evaluation with the real validator** (2026-11-19, `openai_compatible` /
`openai/gpt-oss-20b`, Groq free tier, 5 requests a minute; full table in
[evaluation_report.md](evaluation_report.md)). Command:
`python -m scripts.evaluate --real --model openai/gpt-oss-20b --max-rpm 5`; it needs a key in
`backend/.env`.

- 38 proposals, 48 LLM requests. JSON-valid rate **97.4%**, citation-valid rate **100%**.
- **Fell back to the rule-based fix: 28.9% (11 of 38). Escalated to a manager: 13.2% (5 of 38).**
- The model's fix was identical to the rule-based fix in **21 of 28** cases that have one.
- Latency p50 **1434 ms**, p95 **4272 ms**.
- LLM call errors **9**, of which **7** were HTTP 429. Injection runs **6**, fixes changed by the
  notes **0**.
- Not shown by the report: the reason for each individual fallback, so we do not say how many came
  from the errors and 429 responses; and which 7 of the 28 model fixes differed from the rule-based
  fix. It is one run of one model, not repeated.

**Retrieval calibration** (pgvector, real embeddings; 8 on-topic and 8 off-topic queries): on-topic
queries scored 0.629 to 0.878, off-topic ones 0.406 to 0.511. The threshold is **0.57**, the
middle of that gap. Sixteen queries is a small sample, so re-run the calibration test if the
model or the policy text changes. The golden-set work also found that the query must be specific to
the rule: with one query per exception type, daily overtime scored 0.33 for its own clause (below
the 0.35 minimum). With a query per rule, every rule retrieves its own clause first at 0.80 to
0.95 with real embeddings.

## What is not tested

- No browser-level test of the screens against the running API (the frontend tests fake `fetch`).
- The golden set is 40 cases we wrote. 100% on it shows the rules do what we meant, not that we
  meant everything right.
- The load test uses the mock model, one worker and a local database: it says nothing about real
  model speed, several replicas, or a shared database under load.
- The rate guard (`LLM_MAX_RPM`) is tested with a fake clock, not against a real provider.
- The load test and the real-model runs are not in CI (see [ci-additions.md](ci-additions.md)).
- No Kubernetes deployment test, and no test with real payroll data or with a model other than the
  two in the smoke run (`gpt-oss-120b` has only had that smoke run).
