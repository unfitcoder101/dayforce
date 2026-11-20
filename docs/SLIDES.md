# Slides (exactly five)

Numbers are measured and come from `docs/TESTING.md`. Real-model numbers come from `docs/evaluation_report.md` (one run, 2026-11-19).

## Slide 1: The problem and who it helps
- Managers hunt for timecard mistakes by hand every week: missed punches, overtime, double-booked shifts, policy breaches.
- It is slow, and people get paid wrong.
- Our user: the manager. They review one clear proposal per problem instead of searching.
- The AI never changes anyone's pay; a person decides.

## Slide 2: The workflow, as you will see it live
- Timecards go in; plain code finds four kinds of problem (no AI).
- The AI suggests a fix and cites the exact policy clause it used.
- A checker validates the fix before the manager sees it.
- The manager approves or rejects; every step lands in the audit log.

## Slide 3: Architecture and the harness
- One FastAPI backend, Postgres with pgvector, React screens, an open-weight model behind a config setting.
- The model is one untrusted step inside ordinary code: typed answer, grounded citations, validator, retry budget, rate limit.
- If it fails, is slow or unsure: rule-based fix, or escalate to a person.
- Append-only audit trail; the demo reset endpoint is off by default.

## Slide 4: Safety, testing and evidence
- 430 backend tests pass against a real pgvector database; 98% coverage, 100% on detectors and validator; 35 frontend tests.
- Golden set: 40 of 40 cases, 378 of 378 bad fixes rejected, 0 of 5 injection attacks got through (simulated obedient model).
- System test on the running API with failure injection (timeout, 429, bad JSON, bad citation): all end safely.
- Real model (`gpt-oss-20b`, free tier, real validator, 38 proposals): 97.4% valid JSON, 100% valid citations; 28.9% fell back to the rule-based fix, 13.2% escalated; p95 4.3 s; 0 of 6 injection runs changed the fix. One run.
- Load test (mock model, one worker): 1,200 requests, 0 errors, p95 58 ms.

## Slide 5: What next and why it matters
- Next: login and roles, privacy review for a hosted model, a seed set of real-shaped timecards, a shared rate limit across replicas.
- Next: repeat the real-model run, try the larger model, and look at why each fallback happened (the report does not record it).
- Impact: less hunting, fewer pay errors, a full audit trail. Hours saved: not measured yet.
- Deployment on Kubernetes is in progress (Satyam); see the status in the README.
