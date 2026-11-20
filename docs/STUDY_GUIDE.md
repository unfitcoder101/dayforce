# Study guide (for Harsh)

Read this once top to bottom, then open the files named in it. It describes the code on `main`
(PRs #12 to #17, #19, #20 and #23 are merged). Function names are
given instead of line numbers because line numbers move.

## 1. Words you will need

- **Contract**: a shared data shape (a Pydantic model) that every module agrees on. Pydantic checks the data matches the shape and raises an error if it does not.
- **Exception**: a problem found on a timecard (for example, a clock-in with no clock-out).
- **Baseline fix**: the fix the rules themselves work out. It is the safety net when the model fails.
- **Proposal**: a suggested fix for one exception, from the model or from the baseline.
- **Citation**: a policy clause id (like `POL-TA-01`) the fix relies on.
- **RAG** (retrieval-augmented generation): look up the relevant policy text first, then give it to the model, so it can cite real text.
- **Embedding**: a list of 384 numbers that stands for the meaning of a text. Texts with similar meaning have similar numbers. **Cosine similarity** measures how similar two such lists are (0 to 1 here).
- **Protocol** (Python): "anything with these methods". `Retriever` is a Protocol, so the in-memory and pgvector versions are interchangeable.
- **Escalate**: give the case to a person with no AI fix.
- **Paid time**: time worked minus logged unpaid breaks.

## 2. File map of `backend/app`

| File | What it does, in one line |
|---|---|
| `contracts/models.py` | The shared data shapes: `Timecard`, `Punch`, `Shift`, `DetectedException`, `Fix`, `Proposal`, `Decision`, `AuditEvent`, and the enums. Changes need a `contract-change` issue. |
| `contracts/policy_config.py` | The policy numbers (8 h, 12 h, 38 h, 40 h, 6 h, 30 min, 10 h) in one place. |
| `domain/rules/analysis.py` | Shared helpers: pairs punches into work blocks (`analyze_punches`), finds the scheduled shift (`scheduled_shift_for`), builds each employee's blocks (`employee_blocks`), the week start. |
| `domain/rules/detectors.py` | `detect_exceptions`: the four detectors (missed punch, daily and weekly overtime, schedule conflict, meal break, daily maximum, rest). No AI. |
| `validator/validator.py` | `validate_proposal`: applies a fix to a copy of the timecard, re-runs the detectors, and returns the violations. |
| `ai/settings.py` | Settings read from environment variables, plus `FALLBACK_CONFIDENCE` (0.75), the 2-call budget and the 25 s deadline. |
| `ai/prompts.py` | The prompts, the shape the model must answer in (`LLMProposal`), `sanitize_notes`, `parse_llm_output`. |
| `ai/llm.py` | `OpenAICompatibleLLM` (Groq or any OpenAI-style endpoint) and `MockLLM` (a fake with failure modes), `build_llm`. |
| `ai/retrieval.py` | `build_query`, `InMemoryRetriever`, `PgVectorRetriever`, `get_retriever`. |
| `ai/embeddings.py`, `ai/ingest.py` | The embedding model wrapper, and the command that loads the policy clauses into the database. |
| `ai/workflow.py` | `run_workflow` (the state machine) and `generate_proposal` (the public entry point). |
| `scripts/smoke_llm.py` | Runs a fixed set of cases against one or more models and reports rates and latency. Throttled. |
| `scripts/demo_flow.py` | A narrated run of one case, for the demo video. |
| `scripts/export_mock_data.py` | Builds the example data and TypeScript types for the frontend from the real models. |
| `ai/guard.py` | The LLM rate limit (`RateLimiter`, `RateLimitedLLM`) and the LLM metrics wrapper (`InstrumentedLLM`). Applied in the API only. |
| `api/routes.py`, `api/errors.py` | All the HTTP endpoints and the one error shape. adds `POST /api/analysis/run`, `GET /api/timecards`, `GET /api/policies/search`. |
| `db/` (`connection.py`, `repo.py`, `schema.sql`, `seed.py`) | One connection and transaction per request, plain SQL, the append-only audit trigger, the demo seed. |
| `services/apply_fix.py` | Applies an approved fix to a timecard. |
| `observability/` | JSON logs with request ids, and the Prometheus counters. adds validator rejections, escalations, LLM errors and the LLM latency histogram. |

Outside `backend/app`: `data/policies/time_attendance_policy.md` (the policy text),
`docs/frontend-mock/` (data for the UI), `frontend/` (React screens).

## 3. The path one timecard takes

Take the fixture timecard: someone clocked in at 09:02 and never clocked out, with a 09:00 to
18:00 shift.

1. **The data.** A `Timecard` (`contracts/models.py`) holds a list of `Punch` and `Shift` objects
   and a `notes` string.
2. **Detection.** `detect_exceptions` (`detectors.py`) groups timecards by employee, then calls
   `analyze_punches` (`analysis.py`) to pair clock-ins with clock-outs into `WorkBlock`s. Our
   clock-in has no partner, so `_missed_punch` picks up the unmatched clock-in. It calls
   `scheduled_shift_for` to find the 09:00 to 18:00 shift, checks the shift would be 12 hours or
   less, and builds the baseline `Fix`: add a `punch.OUT` at 18:00. `_make` wraps it into a
   `DetectedException` with an id made from its content, so the same input always gives the same id.
3. **Entry to the AI part.** Something calls `generate_proposal(exc, timecard)` (`workflow.py`). It
   reads settings, picks a retriever with `get_retriever` and a model with `build_llm`, and calls
   `run_workflow` with `validate_proposal` as the validator.
4. **Retrieve policy.** `build_query(exc)` (`retrieval.py`) makes a search text for the rule that
   fired. `retriever.retrieve(query, k=3)` returns the best 3 clauses with scores. Anything under
   the minimum score is dropped. If nothing is left, the case is escalated (`NO_POLICY_MATCH`).
5. **Ask the model.** `build_user_prompt` (`prompts.py`) puts the exception, the timecard facts
   and the clauses into the prompt. It calls `sanitize_notes` so the notes arrive cleaned,
   escaped and wrapped in `<untrusted_notes>`. `llm.complete(...)` sends it, with a timeout equal
   to the time left of the 25 seconds.
6. **Check the answer.** `parse_llm_output` turns the reply into an `LLMProposal` (or fails). Then
   the citation check: every cited id must be among the retrieved clauses. Then
   `validate_proposal` (`validator.py`): it routes to `_check_add_punch`, which checks the field,
   the time (must equal the scheduled end, at most 12 hours, enough rest before the next shift),
   then `_rerun` applies the fix to a copy and calls `detect_exceptions` on the copy to confirm the
   problem is gone and nothing new appeared.
7. **If anything fails.** It counts against the shared budget of 2 model calls. The validator's
   reasons are fed into the second prompt. When the budget is used up, or the model errored, the
   `fallback()` inside `run_workflow` takes `exc.baseline_fix`, validates it too, and uses it
   (source `RULES_FALLBACK`). If there is no baseline, or it fails validation, `escalate()` returns
   a `FLAG_FOR_MANAGER` fix with no changes.
8. **Confidence.** `_finish` escalates if confidence is under 0.5. Otherwise it builds the
   `Proposal` with status `PENDING_REVIEW`.
9. **Result.** `run_workflow` returns `(proposal, events)`: one `AuditEvent` per step
   (`WORKFLOW_STARTED`, `POLICY_RETRIEVED`, `LLM_CALLED`, `VALIDATION_RESULT`, and so on). It stores nothing.
10. **After that.** The API (`POST /api/cases/{id}/propose`) saves the proposal and events. A manager approves or
    rejects. On approval `apply_fix` changes the timecard, and the decision and the change are
    saved together, with audit events. On rejection nothing changes.

## 4. The 12 rule decisions, in plain words

These are the judgement calls in the detectors and validator (PR #11). Each can be argued; know
the reason.

1. **Overtime counts paid time** (breaks taken out). Shift length, meal break and rest use the
   clock span. *Why:* overtime is about hours paid. Rest and shift length are about the clock.
2. **8 to 12 paid hours is a low-severity overtime note with no change needed. Over 12 is a high
   breach with no automatic fix**, and is not also reported as overtime. *Why:* the policy
   says over 12 hours needs a manager.
3. **Weekly risk** = paid hours on days with punches plus scheduled shifts on days with none; 38 or
   more is flagged (high above 40). A day with a missing punch counts as zero worked, and is
   counted in the evidence. *Why:* we need a projection, and we cannot know hours that were never
   recorded. The total can therefore be too low.
4. **Conflicts keep the shift that starts first.** *Why:* the policy says "earliest assigned", but
   the contract has no assignment time. This is a stand-in.
5. **A shift can only be taken off an employee and left open.** *Why:* we cannot check the other
   employee's hours or rest.
6. **Rest is measured between work blocks.** Two clock-in/out pairs in one day are two shifts, so a
   split shift with a short gap breaches rest. Back-to-back shifts breach it. Overlaps are
   conflicts, not rest breaches. *Why:* the policy says between the end of one shift and the start
   of the next.
7. **Meal break:** a block over 6 hours with under 30 logged minutes. The deduct-30 fix is offered
   only when no break was logged at all. Notes like "I took no break" are not read. *Why:* a
   partial break is a judgement for a manager, and notes are untrusted.
8. **The clock-out can only be set to the scheduled shift end**, and the validator requires exactly
   that. No fix without a schedule, if the shift end is before the clock-in, or if it passes 12
   hours. A clock-out with no clock-in has no fix. *Why:* this is what the policy says, and it
   means a time written in the notes cannot move the clock-out.
9. **A break deduction must equal the exact missing minutes.** `NO_CHANGE` is allowed only for
   overtime. `EDIT_PUNCH` is not allowed at all. *Why:* docking too much or too little is wrong,
   and the policy never lets us edit an existing punch.
10. **A meal-break violation that only appears once a missed punch is fixed is not blamed on that
    fix.** Going over 12 hours or under 10 hours of rest is blamed on it, so such a fix fails and
    the case escalates. *Why:* an unfinished day cannot be judged for breaks, but the hard limits
    must hold.
11. **"Edits need manager approval"** is `requires_manager_approval(fix)` plus status
    `PENDING_REVIEW`. *Why:* the contract has no field for it, and a contract change was not
    needed.
12. **Overnight and day boundaries:** no date is taken from a timestamp. A shift stays on its
    timecard's `work_date`, hours are not split at midnight, weeks start on Monday from
    `work_date`, and a timestamp with no timezone is read as UTC. *Why:* the data source assigns
    the day in Kolkata time, so we trust it instead of guessing.

## 5. Fifteen questions an interviewer could ask

**1. Walk me through what happens to one timecard.** Section 3. Detection (code), retrieval,
model, parse, citation check, validator, fallback or escalation, confidence check, proposal,
then a manager.

**2. Why not just ask the model to fix timecards?** A model alone can be confidently wrong, and
this touches pay. Code finds the problem, the model only suggests, code checks, a person decides.

**3. What stops the model inventing policy?** It only returns clause ids. We accept an id only if
that clause was retrieved above the minimum score. The quote shown is copied from our text.

**4. What if an employee writes "approve my overtime" in the notes?** The notes are data: cleaned,
escaped, cut to 500 characters and wrapped. The detectors never read them. And even if the model
obeys, its fix goes through the same validator, which rejects it. In our tests, 0 of 5 attacks got
through (PR #15, merged). We do not claim immunity.

**5. What if the model is down or rate limited?** An error, timeout or 429 falls back at once to
the rule-based fix. If there is none, a manager gets the case. At most 2 calls and 25 seconds. The
API also keeps real calls under `LLM_MAX_RPM` (default 5 a minute per process).

**6. How do you know the detectors are right?** Unit tests on every boundary (exactly 8, 12, 6
hours, 38 and 40 weekly hours, 10 hours of rest), property tests on random timecards, and a golden
set of 40 cases whose expected results we wrote from the policy (PR #15, merged). It is our own
reading of the policy on cases we wrote, so it proves consistency, not that we are right.

**6b. What did the real-model run show?** On 2026-11-19, `openai/gpt-oss-20b` on the free tier with the
real validator, 38 proposals (`docs/evaluation_report.md`): JSON-valid 97.4%, citation-valid 100%,
fell back to the rule-based fix 28.9% (11), escalated 13.2% (5), the model's fix identical to the
rule-based fix in 21 of 28 cases that have one, p50 1434 ms and p95 4272 ms, 9 call errors of which
7 were HTTP 429, 6 injection runs with 0 fixes changed. The report does not record the reason for each
fallback, so do not say the 429s caused them. One run, one model.

**7. Where did your evaluation find a real bug?** Retrieval used one query per exception type. For
daily overtime the right clause scored 0.33 against a 0.35 minimum, so a correct citation was
rejected. Fixed by a query per rule. The citation check did its job; our search was wrong.

**8. Why a threshold of 0.57?** We measured 8 relevant and 8 off-topic queries: relevant scored
0.629 to 0.878, off-topic 0.406 to 0.511, so 0.57 is in the middle of the gap. It is a small
sample, so we would re-check it with real queries.

**9. What does the validator not check?** It sees one timecard at a time. Rest against neighbouring
days and later shifts come from facts the detector stored. A fix that cites anything else is
rejected as unverifiable. It does not check another employee's hours, because we never give a
shift to another employee.

**10. Why did you not use LangGraph?** The flow is short and linear, and a plain state machine was
easy to test on every path. The spec lists LangGraph, so a version is still on the list.

**11. What does the model's confidence number mean?** It is the model's own estimate, uncalibrated.
We use it only as a coarse net: under 0.5 goes to a manager. The 0.75 on rule-based fixes is a
fixed value we chose, not a measurement.

**12. How does it scale?** The workflow keeps no state, so several copies can run side by side.
The limit is the model: the free tier allows about 8,000 tokens a minute, and our rate limit is per
copy, so it adds up across copies. Kubernetes manifests exist for Postgres and the frontend (PR #17);
the backend is not in them yet, so scaling out is not deployed or tested.

**13. What are the privacy risks?** A hosted model sees employee ids, punch times and notes. We
have an offline mode with a local model, but we did no privacy review.

**14. How much was written by an AI assistant?** A lot of the first drafts of code and docs.
The team set the architecture, the contracts, the policy and the safety rules, and every module is
tested. Teammate review of several pull requests is still to be done.

**15. What would you do differently?** Put the evaluation in earlier, because it found the
retrieval bug. Agree the API paths with the contract spec before building it, because the API differed
from the spec (we updated `CLAUDE.md` section 9 to match what was built). And test the real model together with the real validator earlier; we first did it on 2026-11-19
(38 proposals, `gpt-oss-20b`: 11 fell back to the rule-based fix, 5 escalated, 0 injections changed the fix).

## 6. The newer parts (analysis endpoint, rate guard, system test)

Read these files, in this order: `api/routes.py` (the analysis section), `ai/guard.py`,
`observability/metrics.py`, `tests/system/conftest.py`, `tests/system/test_failure_injection.py`.

**Analysis endpoint.** `POST /api/analysis/run` takes a list of `Timecard` (at most 200). It checks
the ids are unique and each punch and shift belongs to the timecard's employee (422), refuses
timecard ids that are already saved (409, nothing saved), runs `detect_exceptions` with the real
`PolicyConfig`, saves the timecards and the exceptions as OPEN cases, writes `TIMECARD_IMPORTED`
and `EXCEPTION_DETECTED` audit events, and returns the new cases. The whole request is one database
transaction, so a failure saves nothing. No AI is involved; the notes are stored and never read.

**Rate guard.** `guard_llm` wraps the model for each propose call. `InstrumentedLLM` (always) times
the call and counts failures by kind (`timeout`, `provider_429`, `local_rate_limit`, `other`).
`RateLimitedLLM` (real providers only) asks the shared `RateLimiter.try_reserve` how long to wait for
a free slot. The workflow passes its remaining deadline as the call's timeout; if the wait would be
longer, the guard raises `LLMRateLimited`, which is an `LLMError`, so the existing fallback runs and
the rule-based fix is used. The limiter lives in the process, so with several replicas the limit is
per replica. `LLM_MAX_RPM=0` turns it off.

**Metrics.** After a proposal the route reads the audit events the workflow returned: a
`VALIDATION_RESULT` with `passed: false` counts a validator rejection, an `ESCALATED` event counts an
escalation by reason. Proposals by source were already counted. `/metrics` is how you see that the
safety nets are firing.

**System test.** `start_api` runs uvicorn as a separate process with `LLM_PROVIDER=mock` and a chosen
`LLM_MOCK_MODE`, and the tests talk to it with HTTP. The database is `DATABASE_URL` or a container the
test starts. It fails, rather than skips, when there is no database, so a green run is real.

## 7. Self-quiz on the new parts

Answer without looking, then check the answers at the bottom.

1. What does `POST /api/analysis/run` return when one of the timecard ids is already saved, and what
   is saved?
2. Why is the analysis endpoint transaction all-or-nothing, and which part of the code gives you that?
3. A real model call would have to wait 40 seconds for a free slot, and 12 seconds of the deadline are
   left. What happens, and why does the manager still get a proposal?
4. With 3 replicas and `LLM_MAX_RPM=5`, how many real calls a minute can the provider receive, and
   why?
5. Why is the rate guard applied in the API and not inside `build_llm`?
6. Which two audit events does a proposal count as a validator rejection and as an escalation, and
   where does the route get them from?
7. `ENABLE_DEV_SEED` is unset. What does `POST /api/dev/seed` return, and what happens to the data?
8. In the system test, the mock LLM is set to `bad_citation`. What does the proposal look like, and
   what is in the audit trail?
9. Why does the system test fail instead of skipping when no database is available?
10. The load test is pointed at an API that is secretly using a real model. How does the script
    protect you, and what is it still not able to tell you?

### Answers

1. HTTP 409 with code `TIMECARD_EXISTS`. Nothing is saved, not even the new timecards in the same
   request: the ids are checked before any insert, and the request is one transaction.
2. The `get_conn` dependency opens one connection and commits only if the handler returns; an
   exception (including the 409/422 `ApiError`) rolls everything back. The audit events are inserted
   in the same transaction as the timecards and cases.
3. `try_reserve(12)` returns `None` because 40 > 12, so `RateLimitedLLM` raises `LLMRateLimited`
   without calling the model. The workflow treats it like any LLM error: `LLM_ERROR` audit event,
   then the rule-based fix, validated again, source `RULES_FALLBACK`. If there is no rule-based fix
   the case is escalated.
4. Up to 15 a minute. The limiter is an in-memory object per process, so each replica allows 5.
   Set `LLM_MAX_RPM` to the provider limit divided by the number of replicas, or use a shared limiter.
5. `build_llm` is also used by the smoke and evaluation scripts, which have their own `--max-rpm`
   throttle. Wrapping it would double-limit them (at 5 a minute instead of 18 for the smoke script)
   and change code that was not part of the API.
6. `VALIDATION_RESULT` with `passed` false, and `ESCALATED` (with a `reason` such as
   `LOW_CONFIDENCE`). They come from the list of audit events that `run_workflow` returns, which the
   propose route loops over after saving them.
7. 404 `NOT_FOUND` in the normal error shape. Nothing is deleted: the flag check runs before the
   database dependency, so the database is not even opened.
8. The first answer cites `POL-TA-99`, which was not retrieved, so a `CITATION_REJECTED` event is
   written; the mock does the same on the retry; the budget runs out (`RETRY_BUDGET_EXHAUSTED`,
   `FALLBACK_USED`) and the proposal is `RULES_FALLBACK`, `PENDING_REVIEW`, validated, citing only
   `POL-TA-01`. The timecard is unchanged until a manager approves.
9. A test that quietly skips proves nothing and would turn a broken environment into a green run. It
   uses `DATABASE_URL` if set, starts its own container if not, and calls `pytest.fail` with a clear
   message if neither works.
10. It refuses non-local addresses (unless `--allow-remote`) and anything with "groq" in the host, and
    it stops at once if an LLM-sourced proposal does not have the mock's exact rationale text. It
    cannot tell you anything about real-model speed or quality, and a real model that returned that
    exact sentence would not be caught; the per-process rate limit also keeps a mistake small.
