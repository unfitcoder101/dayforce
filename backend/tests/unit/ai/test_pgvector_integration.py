"""Real Postgres + real fastembed model. Runs only when DATABASE_URL is set, e.g.

  docker run -d --name pgv -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app \
    -e POSTGRES_DB=timecards -p 54329:5432 pgvector/pgvector:pg16
  DATABASE_URL=postgresql://app:app@localhost:54329/timecards pytest -m integration
"""

import os

import pytest

from app.ai.embeddings import Embedder
from app.ai.ingest import ingest
from app.ai.retrieval import PgVectorRetriever, to_psycopg_dsn

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"),
]


@pytest.fixture(scope="module")
def embedder():
    return Embedder()


@pytest.fixture(scope="module")
def dsn(embedder):
    import psycopg

    url = to_psycopg_dsn(os.environ["DATABASE_URL"])
    with psycopg.connect(url) as conn:
        ingest(conn, embedder)
    return url


def test_missing_clock_out_retrieves_missed_punch(dsn, embedder):
    r = PgVectorRetriever(dsn, embedder, min_score=0.0)
    out = r.retrieve("missing clock out", k=3)
    print([(c.clause_id, c.score) for c in out])
    assert out[0].clause_id == "POL-TA-01"
    assert all(0 <= c.score <= 1 for c in out)


def test_overlapping_shifts_retrieves_schedule_conflict(dsn, embedder):
    r = PgVectorRetriever(dsn, embedder, min_score=0.0)
    out = r.retrieve("overlapping shifts", k=3)
    print([(c.clause_id, c.score) for c in out])
    assert out[0].clause_id == "POL-TA-05"


def test_ingest_is_idempotent(dsn, embedder):
    import psycopg

    with psycopg.connect(dsn) as conn:
        ingest(conn, embedder)
        ingest(conn, embedder)
        n = conn.execute("SELECT count(*) FROM policy_chunks").fetchone()[0]
        dims = conn.execute("SELECT vector_dims(embedding) FROM policy_chunks LIMIT 1").fetchone()[
            0
        ]
    assert n == 7 and dims == 384


def test_min_score_filters_results(dsn, embedder):
    assert PgVectorRetriever(dsn, embedder, min_score=0.999).retrieve("missing clock out") == []


def test_unrelated_query_scores(dsn, embedder):
    """Informational: shows how bge-small scores off-topic text, to tune RETRIEVAL_MIN_SCORE."""
    out = PgVectorRetriever(dsn, embedder, min_score=0.0).retrieve("best pizza toppings", k=3)
    print("unrelated:", [(c.clause_id, c.score) for c in out])
    assert len(out) == 3


RELEVANT = [
    "missing clock out",
    "employee forgot to punch out at the end of their shift",
    "no lunch break was logged on a long shift",
    "worked more than 12 hours in a single day",
    "projected to go over 40 hours this week, overtime risk",
    "overlapping shifts",
    "less than 10 hours of rest between two shifts",
    "manager approval is required before a timecard edit is final",
]
OFF_TOPIC = [
    "best pizza toppings",
    "weather forecast for tomorrow",
    "how to bake sourdough bread",
    "football world cup final score",
    "python list comprehension syntax",
    "best hiking trails in the himalayas",
    "causes of the 1929 stock market crash",
    "how to change a flat car tyre",
]


def test_threshold_calibration(dsn, embedder):
    """The pgvector threshold must separate on-topic from off-topic queries.

    Top-1 cosine score per query, measured with BAAI/bge-small-en-v1.5 on the 7 policy clauses:

      RELEVANT (range 0.629 - 0.878)             OFF-TOPIC (range 0.406 - 0.511)
      0.7337  missing clock out                  0.4206  best pizza toppings
      0.6608  forgot to punch out                0.5108  weather forecast for tomorrow
      0.7572  no lunch break logged              0.5038  how to bake sourdough bread
      0.6290  worked more than 12 hours          0.4948  football world cup final score
      0.8207  projected over 40 hours            0.4683  python list comprehension syntax
      0.7367  overlapping shifts                 0.4055  best hiking trails in the himalayas
      0.8446  less than 10 hours of rest         0.4698  causes of the 1929 stock market crash
      0.8784  manager approval before edit       0.4779  how to change a flat car tyre

    Gap: highest off-topic 0.5108 < lowest relevant 0.6290. A threshold of 0.5 let two
    off-topic queries through, so RETRIEVAL_MIN_SCORE_PGVECTOR is 0.57 (middle of the gap).
    The sample is small (16 queries); re-run this test if the model or policy text changes.
    """
    from app.ai.settings import AISettings

    threshold = AISettings(retriever="pgvector").retrieval_min_score
    r = PgVectorRetriever(dsn, embedder, min_score=0.0)  # no filtering: we want raw scores

    def top(q):
        best = r.retrieve(q, k=1)[0]
        return best.clause_id, best.score

    rel = {q: top(q) for q in RELEVANT}
    off = {q: top(q) for q in OFF_TOPIC}
    print(f"\nthreshold = {threshold}")
    print("RELEVANT (top clause, top score):")
    for q, (cid, sc) in rel.items():
        print(f"  {sc:.4f}  {cid}  {q}")
    print("OFF-TOPIC (top clause, top score):")
    for q, (cid, sc) in off.items():
        print(f"  {sc:.4f}  {cid}  {q}")
    lo = min(sc for _, sc in rel.values())
    hi = max(sc for _, sc in off.values())
    print(f"lowest relevant = {lo:.4f}, highest off-topic = {hi:.4f}")
    assert len(rel) == 8 and len(off) == 8
    assert lo > threshold, f"relevant query scored {lo} <= threshold {threshold}"
    assert hi < threshold, f"off-topic query scored {hi} >= threshold {threshold}"
