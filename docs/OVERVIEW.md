# Timecard Exception Resolution Agent: Overview

Owner: Harsh (AI and harness, rules and validator, evaluation, integration, docs). Written from
the code on `main` (PRs #12 to #17, #19, #20 and #23 are merged). Every number was measured
by running something; where a number would be a guess, this document says it needs measuring.

Related documents: [ARCHITECTURE.md](ARCHITECTURE.md) (diagram and module map),
[SECURITY.md](SECURITY.md) (threat model), [TESTING.md](TESTING.md) (tests and numbers),
[STUDY_GUIDE.md](STUDY_GUIDE.md) (for learning the code), [RUNBOOK.md](RUNBOOK.md) (run it, switch the model),
[DEMO_SCRIPT.md](DEMO_SCRIPT.md), [SLIDES.md](SLIDES.md), [ci-additions.md](ci-additions.md),
[llm_smoke_report.md](llm_smoke_report.md).

## 1. What the system does, in plain words

Companies pay people from timecards. Each week a manager has to find the mistakes by hand:
someone forgot to clock out, someone is scheduled in two places, someone is heading into
overtime. This project finds those problems with ordinary code, asks an AI model to suggest a fix
that cites the exact policy clause it relied on, checks the suggestion against rules, and shows it
to a manager who approves or rejects it. The AI never changes anyone's pay. Every step is written
to an audit trail.

There are four exception types: `MISSED_PUNCH`, `OVERTIME_RISK`, `SCHEDULE_CONFLICT`,
`POLICY_BREACH`. The fictional company is Northwind Retail (`data/policies/time_attendance_policy.md`,
clauses `POL-TA-01` to `POL-TA-07`).

### What exists today

| Piece | State |
|---|---|
| Shared contracts, fixtures, policy file | Done (`backend/app/contracts/`, `data/policies/`) |
| Detectors for all four exception types | Done and unit tested (`backend/app/domain/rules/`) |
| Validator (re-checks every proposed fix with the real rules) | Done and unit tested (`backend/app/validator/`), used by default |
| AI module: retrieval, model client, prompts, workflow, fallback, escalation | Done and tested (`backend/app/ai/`) |
| pgvector retriever and policy ingest | Done, tested against a real `pgvector/pgvector:pg16` container |
| Smoke script, demo script, mock data for the UI | Done (`backend/scripts/`, `docs/frontend-mock/`) |
| Real-model smoke run (Groq) | Done once, before the real validator existed (section 5) |
| Real-model evaluation with the real validator (`gpt-oss-20b`, 38 proposals) | Done once on 2026-11-19, merged (PR #23); numbers in section 5 |
| Frontend screens, on built-in mock data or the real API | Done (`frontend/`, 35 tests; PRs #12 to #14) |
| API, Postgres tables, approve and reject, append-only audit | Done and merged (PR #12) |
| Analysis endpoint (timecards in, cases out), timecards list, policy search | Done and merged (PR #19) |
| LLM rate guard (`LLM_MAX_RPM`) and safety metrics | Done and merged (PR #19) |
| System test with LLM failure injection, load test | Done and merged (PR #19); the system test runs in CI against a pgvector service, the load test is local only |
| Golden set (40 cases), evaluation report, property tests | Done and merged (PR #15) |
| Seed dataset of 60+ timecards with ground truth | Not started |
| Docker Compose, Kubernetes manifests (Postgres, frontend), CI for backend and frontend | Merged (PR #17). The backend Deployment and autoscaler were added in PR #21; the frontend image has no `/api` proxy config yet; nothing is publicly deployed |
| Demo script, slides outline, runbook | Written (`DEMO_SCRIPT.md`, `SLIDES.md`, `RUNBOOK.md`); the video is not recorded |

## 2. Architecture

One Python backend (a modular monolith); modules call each other as plain function calls. The
diagram, the module map and where each safety check sits are in
[ARCHITECTURE.md](ARCHITECTURE.md). In short:

```
timecards -> detect_exceptions -> retrieve policy -> model proposes -> parse -> citation check
          -> validate_proposal -> (fallback | escalate) -> Proposal (PENDING_REVIEW) -> manager
```

`run_workflow` returns the proposal and a list of audit events and stores nothing. The API stores
them and applies an approved fix on approval (PR #12). Timecards get into the system through
`POST /api/analysis/run`, which runs the detectors and saves the cases.

## 3. The safety harness

The model is one untrusted step inside a frame of ordinary code. The checks, in order: code finds
the problem; the model must answer in a fixed JSON shape; every citation must be a clause that was
actually retrieved; the validator re-checks the fix on a copy of the timecard; a shared budget of 2
model calls and 25 seconds, then fall back to the rule-based fix or escalate; low confidence
escalates; only a manager's approval changes a timecard; every step has an audit event. The full
threat model, including what is **not** covered (no login, a fixed demo manager, no limit on the
size of one timecard), is in [SECURITY.md](SECURITY.md). The demo reset endpoint is off by default
(`ENABLE_DEV_SEED`, 404 when off).

## 4. The AI workflow and its failure paths

```
retrieve policy (top 3) -- none above the minimum --> ESCALATED (NO_POLICY_MATCH)
LLM proposes (JSON)     -- error, timeout, 429     --> fall back at once
parse -> citation check -> validator   (these failures share ONE budget: 2 calls, 25 s)
confidence below 0.5 -> ESCALATED
else -> Proposal, PENDING_REVIEW
fall back = the baseline fix, validated, source RULES_FALLBACK (confidence 0.75)
            no baseline -> ESCALATED (NO_BASELINE_FIX)
            baseline fails validation -> ESCALATED (BASELINE_FIX_INVALID)
```

### Failure modes and what happens

| What goes wrong | What the system does | Tested |
|---|---|---|
| **The model is down or times out** | Falls back at once (no retry) to the rule-based fix, validated. No fix, or it fails validation: escalate. | Mock `timeout` mode |
| **Rate limit (HTTP 429)** | Treated as any model failure: immediate fallback, no retry into a rate limit. The smoke and evaluation scripts also throttle to stay under the limit, and the API keeps real calls under `LLM_MAX_RPM`: a call that would wait past the deadline falls back. | Stubbed 429 through the real client code; mock `rate_limited` mode against the running API (system test) |
| **Invalid JSON or schema** | One more try inside the shared budget, then fall back. | Mock `invalid_json` |
| **Made-up citation** | Rejected (a clause that was not retrieved). Counts against the budget, then fall back. | Mock `bad_citation` |
| **Prompt injection in the notes** | Notes are cleaned, escaped and wrapped, and the detectors never read them. If the model obeys anyway, its fix goes through the same validator. | Hostile notes in unit tests; 5 golden cases (PR #15, merged) |
| **A bad fix** (wrong time, over-deduction, wrong shift) | The validator rejects it with a reason, fed into the retry. After the budget, fall back or escalate. | 378 bad fixes all rejected (PR #15, merged) |
| **Low model confidence** | Escalate to a manager with no AI fix. | Mock `low_confidence` |
| **The rule-based fix itself breaks policy** | Escalate (`BASELINE_FIX_INVALID`). The model cannot rescue it by agreeing with it. | Unit and golden tests |

Retry budget (`backend/app/ai/settings.py`): `max_llm_calls = 2` in total, whatever mix of failures
happens, and `total_deadline_seconds = 25`. Each call's timeout is capped by the time left. An
earlier version allowed one retry per failure cause (up to 3 calls); it was changed to this shared
budget. `FALLBACK_CONFIDENCE = 0.75` is a fixed trust score for rule-based fixes, not a model
estimate. The model's own confidence is self-reported and uncalibrated.

### Retrieval, and how the threshold was chosen

Two retrievers share one interface. **Memory** (default; keyword overlap; minimum 0.35) needs no
database. **pgvector** (`BAAI/bge-small-en-v1.5`, 384 numbers, cosine similarity; minimum 0.57).
Top-1 cosine scores, real model and real database (8 on-topic and 8 off-topic queries):

| On-topic query | Score | Off-topic query | Score |
|---|---|---|---|
| missing clock out | 0.7337 | best pizza toppings | 0.4206 |
| employee forgot to punch out at the end of their shift | 0.6608 | weather forecast for tomorrow | 0.5108 |
| no lunch break was logged on a long shift | 0.7572 | how to bake sourdough bread | 0.5038 |
| worked more than 12 hours in a single day | 0.6290 | football world cup final score | 0.4948 |
| projected to go over 40 hours this week, overtime risk | 0.8207 | python list comprehension syntax | 0.4683 |
| overlapping shifts | 0.7367 | best hiking trails in the himalayas | 0.4055 |
| less than 10 hours of rest between two shifts | 0.8446 | causes of the 1929 stock market crash | 0.4698 |
| manager approval is required before a timecard edit is final | 0.8784 | how to change a flat car tyre | 0.4779 |

Lowest on-topic 0.629, highest off-topic 0.5108. A first guess of 0.5 let two off-topic queries
through, so the threshold is 0.57. Sixteen short queries is a small sample, so re-run the test if the
model or the policy text changes. A second finding came from the golden-set work: one query per
exception *type* ranked the weekly-overtime clause above the daily one, so a correct citation
of `POL-TA-03` was rejected. Each detector rule now has its own query (PR #15, merged), and every rule
retrieves its own clause first (0.80 to 0.95 with real embeddings).

## 5. Real results

**Smoke run on a real model** (Groq's free tier through an OpenAI-compatible API,
`openai/gpt-oss-20b` as the default and `openai/gpt-oss-120b`; 9 proposals per model; 5 requests a
minute; [llm_smoke_report.md](llm_smoke_report.md)):

| | gpt-oss-20b | gpt-oss-120b |
|---|---|---|
| LLM call errors / HTTP 429 | 0 / 0 | 0 / 0 |
| Valid JSON / valid citations | 100% / 100% | 100% / 100% |
| Fell back / escalated | 0% / 0% | 0% / 0% |
| Time per proposal, median / 95th percentile | 1.1 s / 1.8 s | 1.6 s / 4.2 s |
| "Approve 40 hours of overtime" note followed? | no | no |

Read this carefully: it is a small run, one injection try per model, and it **happened before the
real validator existed**, so it used an always-pass validator. The evaluation below is the run with the
real validator. Both models are Apache 2.0 licensed (Hugging Face model cards).

**Real-model evaluation with the real validator** (2026-11-19, `openai_compatible` / `openai/gpt-oss-20b` on Groq's free tier, throttled to 5 requests a minute; per-proposal table in [evaluation_report.md](evaluation_report.md)):

| Metric | Value |
|---|---|
| Proposals run / LLM requests sent | 38 / 48 |
| JSON-valid rate / citation-valid rate | 97.4% / 100% |
| Fell back to the rule-based fix | 28.9% (11 of 38) |
| Escalated to a manager | 13.2% (5 of 38) |
| Model's fix identical to the rule-based fix (cases that have one) | 21 of 28 |
| Latency p50 / p95 | 1434 ms / 4272 ms |
| LLM call errors / of which HTTP 429 | 9 / 7 |
| Injection runs / fix changed by the notes | 6 / 0 |

"Fell back" means the rule-based fix was used instead of the model's answer. The report does not
record the reason for each fallback, so we do not claim how many came from the call errors and
HTTP 429 responses and how many from other causes. It is one run of one model.

**Evaluation on the golden set** (mock model, PR #15, merged; 40 cases we wrote, expected results
written from the policy): 40 of 40 pass; detection precision and recall 100% on every type with 0
false positives on 9 clean timecards; 378 of 378 bad fixes rejected; in all five mock model
behaviours the final proposal matched the safe outcome and no unvalidated fix reached a manager;
a simulated model that obeys hostile notes got 0 of 5 attacks through. One-character changes to the
8 hour, 6 hour and 12 hour rules each failed the golden set, so the evaluation does catch
regressions. See [TESTING.md](TESTING.md) for how to run it and the full list.

**Tests:** 430 backend tests pass with a pgvector container (375 pass and 36 skip without one), 98% coverage, 100% on the detectors and the validator; 19 of them are system tests against the running API; 35 frontend tests. See [TESTING.md](TESTING.md).

## 6. How it scales up, and how to scale it down

Deployment: Docker Compose runs Postgres, the frontend and (behind a profile) the backend;
Kubernetes manifests exist for Postgres and the frontend (PR #17) and for the backend with an
autoscaler (PR #21). What is true in the code today:

- `run_workflow` keeps no state between calls, so several copies can run side by side. In the API
  each request uses one database transaction and nothing is kept in memory, and the schema is
  created under a lock so several copies can start together.
- **Model limit:** the free tier allows about 8,000 tokens a minute. The API keeps real
  calls under `LLM_MAX_RPM` (default 5 a minute) with an in-process limiter, so with N copies the
  provider can see N times that: set `LLM_MAX_RPM` to the provider limit divided by the copies, or
  move to a shared limiter (for example Redis, not built). A paid or self-hosted model removes the
  limit.
- **Retrieval:** the query depends only on the rule that fired, so there are eight distinct queries
  and their results can be cached. Not built.
- **Database:** the retriever opens a new connection per query, and the API opens one
  per request. A connection pool is needed before many copies share one Postgres.
- **A queue:** a proposal waits for the model for up to 25 seconds. At volume, proposals should be
  made by background workers. Not built and not in v1.
- **Scaling down:** one copy, `LLM_PROVIDER=mock` or a hosted model, and `RETRIEVER=memory` (no
  database for retrieval and no embedding model download). pgvector needs a 64 MB model download
  (measured), cached in a temporary folder by default.
- **Measured (local):** one uvicorn worker with the mock model and a local Postgres handled
  252 requests per second over 1,200 requests with 0 errors; median 38 ms, 95th percentile 58 ms
  (`scripts/load_test.py`). Memory per copy, several copies, a real model and cost per proposal
  **need measuring**.

## 7. Trade-offs and why

| Decision | Why | Cost |
|---|---|---|
| Postgres with pgvector, not MongoDB or a separate vector database | Timecards, approvals and audit need transactions; the policy fits in the same database; one thing to run | Less search tuning than a dedicated engine, not a concern for 7 clauses |
| A plain Python state machine, not LangGraph (yet) | Short and linear, easy to test on every path | The spec lists LangGraph; a version is still on the list |
| No ORM (psycopg and plain SQL) | Fewer moving parts, SQL stays visible | More hand-written SQL |
| No Redis in v1 | One less service | See section 6 |
| The model returns clause ids only; quotes are copied from retrieved text | It cannot invent a quote | It cannot cite a clause that was not retrieved |
| A shared budget of 2 model calls and 25 seconds | Bounded latency and cost | A model that needs a third try always falls back |
| A rule-specific retrieval query | Found by the golden set: the type query lost the right clause | Eight queries to keep in line with the policy text |
| The validator only lets a shift be left open, never given to someone | We cannot check another employee's hours | A manager does the reassigning |

## 8. What is not done yet

- **Real-model evaluation: one run only.** `gpt-oss-20b`, free tier, 38 proposals, 2026-11-19. Not
  repeated, and not run for `gpt-oss-120b`. The report does not say why each fallback happened, and
  does not say which 7 of the 28 model fixes differed from the rule-based fix.
- **CI** runs the database, integration and system tests against a pgvector service, but has no
  scanner; the load test is local only: see [ci-additions.md](ci-additions.md).
- **Kubernetes:** the backend is in the manifests, but the frontend image has no `/api` proxy
  config and nothing is publicly deployed.
- **A seed dataset** of 60+ timecards with ground truth.
- **LangGraph version** of the workflow, a **shared rate limit** across replicas, **retrieval
  caching**, a **connection pool**.
- **Login and roles:** the manager is a fixed demo user. **Privacy review** for sending employee
  data to a hosted model. No limit on the size of one timecard.
- **Teammate review** of several pull requests, and the **demo video** (the script and slides
  outline are written).

## 9. How we built it

An AI coding assistant (Claude Code) was used for scaffolding and first drafts of code and docs,
including the AI module, the detectors, the validator and this document. The team set the
architecture, the contracts, the policy and the safety rules (`CLAUDE.md`). Our working rule is
that code is read, tested and reviewed by a teammate before it is merged and that everyone can
explain their own part. Review of several pull requests by a teammate is still to do.

## 10. Likely interview questions, with honest answers

**1. Why not just give the timecard to an LLM and let it fix it?** A model alone can be confidently
wrong, and this touches pay. Plain code finds the problem, the model only suggests a fix inside a
typed frame, the fix must cite real policy, code checks it, and a person approves.

**2. How do you stop the model making up policy?** It returns clause ids only. We accept an id only
if that clause was retrieved above the minimum score, and the quote shown is copied from our text.
An invented id is rejected and counts against the retry budget.

**3. What happens if the model is down, slow, or rate limited?** An error, timeout or 429 falls
back at once to the rule-based fix. Garbage, bad citations or a failed validation get one more try
inside 2 calls and 25 seconds, then fall back. No fix, or one that fails validation: a manager gets
the case.

**4. How do you handle prompt injection?** Notes are data: cleaned, escaped, cut to 500 characters,
wrapped in a tag, and the detectors never read them. The real protection is that a model that obeys
the notes still produces a fix the validator rejects. We tested a simulated obedient model (0 of 5
got through) and one real injection try on each of two models. That is evidence, not proof.

**5. Can the AI change someone's pay?** No. It returns a proposal and writes nothing. Only a
manager's approval through the API applies a fix, and an escalated case cannot be
approved (checked by the system test).

**6. How did you choose the retrieval threshold, and how sure are you?** We measured it: on-topic
queries scored 0.629 to 0.878 and off-topic 0.406 to 0.511, so 0.57 is the middle of the gap. A
first guess of 0.5 failed that test. Sixteen queries is small, so we are fairly sure it is sensible
and not sure it is tight.

**7. What did your evaluation find?** A real bug: with one search query per exception type, the
correct clause for daily overtime scored 0.33 against a 0.35 minimum, so a correct citation was
rejected and the workflow fell back for no reason. The citation check worked; our search was wrong.
Each rule now has its own query.

**8. Why Postgres with pgvector instead of MongoDB or a vector database?** We need transactions
and joins for timecards, approvals and audit, and seven policy clauses do not justify a second
system. One database is simpler to run, back up and explain.

**9. Why a hand-written state machine instead of LangGraph?** The flow is short and linear, and
plain Python made every failure path easy to test. The spec lists LangGraph, so a version is still
on the list.

**10. What does the validator check, and what can it not?** It applies the fix to a copy of the
timecard and re-runs the detectors. It rejects wrong-kind fixes, unrelated changes, shifts over 12
hours, rest under 10 hours, the wrong break deduction, a problem left in place and any new
violation. It sees one timecard, so neighbouring days come from facts the detector stored, and it
refuses to give a shift to another employee because that person's hours cannot be checked.

**11. How do you know the detectors are right?** Boundary tests (exactly 8, 12 and 6 hours, 38 and
40 weekly hours, 10 hours of rest), property tests on random timecards, and a golden set whose
expected results we wrote from the policy (PR #15, merged). It is our reading of the policy on cases we
wrote, so it shows consistency, not that we read the policy correctly.

**12. How does it scale?** The workflow keeps no state, so copies can run side by side. The real
limit is the model: the free tier is about 8,000 tokens a minute, and our rate limit is per copy.
A single worker with the mock model did 252 requests a second locally (1,200 requests, 0 errors);
nothing is deployed on Kubernetes yet, and real-model cost and speed at volume need measuring.

**13. What does the model's confidence number mean?** It is the model's own, uncalibrated estimate.
We only use it as a coarse net: under 0.5 goes to a manager. The 0.75 on rule-based fixes is a
value we chose, not a measurement.

**14. What are the privacy risks?** A hosted model sees employee ids, punch times and notes. We
support an offline local model, but we did no privacy review.

**15. How did you test the AI part without a real model?** A deterministic mock with failure modes
(invalid JSON, timeout, invented citation, low confidence), a stubbed HTTP client for the real
client including a 429, and a real Postgres for retrieval. That shows the harness behaves; it does
not show how good a real model's fixes are. The real-model evidence is one evaluation run of
`gpt-oss-20b` (38 proposals) and a small smoke run; neither has been repeated.

**16. How much was written by AI?** A lot of the first drafts of code and docs. The team set the
architecture, contracts, policy and safety rules, and the code is tested. Teammate review of several
pull requests is still to do.

**17. What is not done?** Section 8: the real-model evaluation is a single run of one model; CI has
no scanner; the frontend image has no `/api` proxy config and nothing is publicly deployed; no seed dataset, no login, no
privacy review.

**18. What would you do differently?** Build the evaluation earlier, because it found the retrieval
bug. Agree the API paths with the spec before building the API (we later updated `CLAUDE.md` to match). Calibrate the retrieval threshold
with real exception queries, not short test phrases. And run the real model with the real
validator as soon as the validator existed (we did that late; the first real run is in section 5).

**19. What was the hardest design decision?** Where to draw the line for the validator. It must
reject unsafe fixes but not reject a correct rule-based fix. For example, finishing a day that was
missing a clock-out may reveal a missing break; we do not blame that on the fix, but we do blame it
for pushing the day over 12 hours.

**20. What is the biggest risk now?** The real model. Everything around it is tested end to end with a
mock (the system test). With the real model and the real validator, one run of 38 proposals fell back
to the rule-based fix in 11 cases and escalated 5; we have only that one run. After that, deployment:
the frontend image has no `/api` proxy config yet.
