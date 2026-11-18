"""Unit tests for the pgvector retriever, ingest and embeddings (no DB, no model download)."""

import sys
import types

import pytest

from app.ai import embeddings, ingest
from app.ai.embeddings import EMBEDDING_DIM, Embedder
from app.ai.retrieval import (
    InMemoryRetriever,
    PgVectorRetriever,
    get_retriever,
    parse_policy_chunks,
    to_psycopg_dsn,
    vector_literal,
)
from app.ai.settings import AISettings


class FakeEmbedder:
    def embed(self, texts):
        return [[0.5, 0.25] + [0.0] * (EMBEDDING_DIM - 2) for _ in texts]


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows=None):
        self.rows, self.executed, self.committed = rows or [], [], False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return FakeResult(self.rows)

    def commit(self):
        self.committed = True


def test_parse_policy_chunks_real_file():
    from app.ai.retrieval import DEFAULT_POLICY_PATH

    chunks = parse_policy_chunks(DEFAULT_POLICY_PATH.read_text())
    assert [c.clause_id for c in chunks] == [f"POL-TA-0{i}" for i in range(1, 8)]
    assert chunks[0].title == "Missed punch" and "clock-out" in chunks[0].text
    assert chunks[0].full_text.startswith("Missed punch. ")


def test_memory_retriever_still_matches_chunk_text():
    r = InMemoryRetriever()
    assert r.clauses[0][1].startswith("Missed punch. ")


def test_dsn_and_vector_literal():
    assert to_psycopg_dsn("postgresql+psycopg://a:b@h:5432/d") == "postgresql://a:b@h:5432/d"
    assert to_psycopg_dsn("postgresql://x") == "postgresql://x"
    assert vector_literal([0.5, 1, -2.25]) == "[0.5,1.0,-2.25]"


def test_pgvector_retriever_scores_orders_and_filters():
    rows = [
        ("POL-TA-01", "Missed punch", "body1", 0.82),
        ("POL-TA-03", "Daily overtime", "body3", 0.4),
        ("POL-TA-07", "Edits", "body7", 0.2),  # below min
        ("POL-TA-02", "Meal", "body2", -0.3),  # negative sim clamps to 0, filtered
        ("POL-TA-04", "Weekly", "body4", 1.0000001),  # clamps to 1.0
    ]
    conn = FakeConn(rows)
    seen_dsn = []
    r = PgVectorRetriever(
        "postgresql+psycopg://u:p@h/db",
        FakeEmbedder(),
        min_score=0.35,
        connect=lambda dsn: seen_dsn.append(dsn) or conn,
    )
    out = r.retrieve("missing clock out", k=4)
    assert seen_dsn == ["postgresql://u:p@h/db"]
    assert [(c.clause_id, c.score) for c in out] == [
        ("POL-TA-01", 0.82),
        ("POL-TA-03", 0.4),
        ("POL-TA-04", 1.0),
    ]
    assert out[0].quote == "Missed punch. body1"
    sql, params = conn.executed[0]
    assert "<=>" in sql and "ORDER BY" in sql
    assert params[0] == params[1] and params[0].startswith("[0.5,0.25,") and params[2] == 4


def test_pgvector_retriever_default_connect_uses_psycopg(monkeypatch):
    fake = types.SimpleNamespace(connect=lambda dsn: FakeConn([("POL-TA-01", "T", "b", 0.9)]))
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    r = PgVectorRetriever("postgresql://x", FakeEmbedder())
    assert r.retrieve("q")[0].clause_id == "POL-TA-01"


def test_get_retriever_selection(monkeypatch):
    assert isinstance(get_retriever(AISettings()), InMemoryRetriever)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        get_retriever(AISettings(retriever="pgvector"))
    with pytest.raises(ValueError, match="unknown RETRIEVER"):
        get_retriever(AISettings(retriever="nope"))
    r = get_retriever(AISettings(retriever="pgvector", database_url="postgresql://x"))
    assert isinstance(r, PgVectorRetriever) and r.embedder.model_name == "BAAI/bge-small-en-v1.5"
    assert AISettings.from_env({"RETRIEVER": "pgvector"}).retriever == "pgvector"


def test_embedder_is_lazy_and_checks_dimension(monkeypatch):
    created = []

    class FakeTE:
        def __init__(self, model_name):
            created.append(model_name)

        def embed(self, texts):
            return ([0.1] * EMBEDDING_DIM for _ in texts)

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTE))
    monkeypatch.setenv("EMBEDDING_MODEL", "some/model")
    e = Embedder()
    assert created == [] and e.model_name == "some/model"  # nothing loaded yet
    assert len(e.embed(["a", "b"])) == 2 and len(e.embed(["c"])[0]) == EMBEDDING_DIM
    assert created == ["some/model"]  # loaded once

    class WrongDim(FakeTE):
        def embed(self, texts):
            return ([0.1] * 10 for _ in texts)

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=WrongDim))
    with pytest.raises(ValueError, match="384"):
        Embedder("x").embed(["a"])


def test_ingest_upserts_every_clause():
    conn = FakeConn()
    ids = ingest.ingest(conn, FakeEmbedder())
    assert ids == [f"POL-TA-0{i}" for i in range(1, 8)]
    assert "CREATE EXTENSION IF NOT EXISTS vector" in conn.executed[0][0]
    assert (
        "vector(384)" in conn.executed[0][0] and "clause_id TEXT PRIMARY KEY" in conn.executed[0][0]
    )
    upserts = [e for e in conn.executed[1:] if "ON CONFLICT (clause_id)" in e[0]]
    assert len(upserts) == 7 and conn.committed
    assert upserts[0][1][:2] == ("POL-TA-01", "Missed punch")


def test_ingest_rejects_policy_without_clauses(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("# nothing here")
    with pytest.raises(ValueError):
        ingest.ingest(FakeConn(), FakeEmbedder(), p)


def test_ingest_main(monkeypatch, capsys):
    conn = FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=lambda dsn: conn))
    monkeypatch.setattr(ingest, "Embedder", FakeEmbedder)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://a:b@h/d")
    assert ingest.main([]) == 0
    assert "ingested 7 clauses" in capsys.readouterr().out
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(SystemExit):
        ingest.main([])


def test_embeddings_module_constants():
    assert embeddings.EMBEDDING_DIM == 384


def test_thresholds_are_per_retriever():
    mem = AISettings(retriever="memory")
    pg = AISettings(retriever="pgvector")
    assert mem.retrieval_min_score == 0.35 and pg.retrieval_min_score == 0.57
    assert AISettings.from_env({}).retrieval_min_score_pgvector == 0.57


def test_thresholds_from_env_and_override():
    env = {"RETRIEVAL_MIN_SCORE_MEMORY": "0.4", "RETRIEVAL_MIN_SCORE_PGVECTOR": "0.55"}
    assert AISettings.from_env({**env, "RETRIEVER": "memory"}).retrieval_min_score == 0.4
    assert AISettings.from_env({**env, "RETRIEVER": "pgvector"}).retrieval_min_score == 0.55
    for r in ("memory", "pgvector"):  # plain RETRIEVAL_MIN_SCORE beats both
        s = AISettings.from_env({**env, "RETRIEVER": r, "RETRIEVAL_MIN_SCORE": "0.7"})
        assert s.retrieval_min_score == 0.7
    assert AISettings.from_env({"RETRIEVAL_MIN_SCORE": ""}).retrieval_min_score_override is None


def test_get_retriever_uses_pgvector_threshold():
    s = AISettings(retriever="pgvector", database_url="postgresql://x")
    assert get_retriever(s).min_score == 0.57
    s = AISettings(
        retriever="pgvector", database_url="postgresql://x", retrieval_min_score_override=0.7
    )
    assert get_retriever(s).min_score == 0.7
