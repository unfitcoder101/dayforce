"""Policy retrieval. Retriever is a Protocol so pgvector can replace the in-memory version."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.contracts import DetectedException, ExceptionType, PolicyCitation

from .settings import AISettings

DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "policies" / "time_attendance_policy.md"
)
_HEADING_RE = re.compile(r"^## (POL-TA-\d+) (.+)$", re.MULTILINE)
_STOPWORDS = frozenset(
    "a an and are as at be by can for from has have if in is it of on or per than that the "
    "to with no not each any".split()
)

_QUERY_BY_TYPE = {
    ExceptionType.MISSED_PUNCH: "missed punch clock-in without clock-out, correct clock-out "
    "to scheduled shift end, shift hours",
    ExceptionType.OVERTIME_RISK: "overtime risk, daily overtime hours, weekly overtime beyond "
    "40 hours, projected hours, reduce or reassign upcoming shifts",
    ExceptionType.SCHEDULE_CONFLICT: "schedule conflict, overlapping shifts, reassign shift "
    "with manager approval",
    ExceptionType.POLICY_BREACH: "policy breach, meal break, unpaid break minutes, day over "
    "12 hours needs manager review, minimum rest between shifts",
}


class Retriever(Protocol):
    def retrieve(self, query: str, k: int = 3) -> list[PolicyCitation]: ...


def build_query(exc: DetectedException) -> str:
    return _QUERY_BY_TYPE[exc.type]


def _tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOPWORDS:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


@dataclass(frozen=True)
class PolicyChunk:
    clause_id: str
    title: str
    text: str

    @property
    def full_text(self) -> str:
        """What gets embedded, scored and quoted: title plus body."""
        return f"{self.title}. {self.text}"


def parse_policy_chunks(raw: str) -> list[PolicyChunk]:
    """Split policy markdown on '## POL-TA-0X Title' headings, one chunk per clause."""
    matches = list(_HEADING_RE.finditer(raw))
    chunks = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        chunks.append(PolicyChunk(m.group(1), m.group(2).strip(), raw[m.end() : end].strip()))
    return chunks


class InMemoryRetriever:
    """Splits the policy markdown on '## POL-TA-' headings and scores each clause by the
    fraction of query words found in it (keyword overlap)."""

    def __init__(self, policy_path: Path | str | None = None, text: str | None = None):
        raw = text if text is not None else Path(policy_path or DEFAULT_POLICY_PATH).read_text()
        self.clauses: list[tuple[str, str]] = [
            (c.clause_id, c.full_text) for c in parse_policy_chunks(raw)
        ]

    def retrieve(self, query: str, k: int = 3) -> list[PolicyCitation]:
        q = _tokens(query)
        if not q:
            return []
        scored = [
            PolicyCitation(
                clause_id=cid, quote=text, score=round(len(q & _tokens(text)) / len(q), 4)
            )
            for cid, text in self.clauses
        ]
        scored = [c for c in scored if c.score > 0]
        scored.sort(key=lambda c: (-c.score, c.clause_id))
        return scored[:k]


def to_psycopg_dsn(url: str) -> str:
    """DATABASE_URL in .env.example is SQLAlchemy-style ('postgresql+psycopg://');
    psycopg itself wants plain 'postgresql://'."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorRetriever:
    """Cosine-similarity search over policy_chunks (pgvector). Same Retriever protocol as
    InMemoryRetriever.

    Score = cosine similarity clamped to 0..1 (pgvector `<=>` is cosine distance, so
    sim = 1 - distance). Results below `min_score` are dropped here.
    NOTE: bge-small similarities for unrelated text reached ~0.42 in calibration, so its threshold
    (RETRIEVAL_MIN_SCORE_PGVECTOR, default 0.57) is separate from the keyword retriever's.
    """

    SQL = (
        "SELECT clause_id, title, text, 1 - (embedding <=> %s::vector) AS sim "
        "FROM policy_chunks ORDER BY embedding <=> %s::vector LIMIT %s"
    )

    def __init__(
        self,
        dsn: str,
        embedder: Any,
        min_score: float = 0.35,
        connect: Callable[[str], Any] | None = None,
    ):
        self.dsn = to_psycopg_dsn(dsn)
        self.embedder = embedder
        self.min_score = min_score
        self._connect = connect

    def _conn(self):
        if self._connect is not None:
            return self._connect(self.dsn)
        import psycopg  # lazy: memory mode and CI never need a database

        return psycopg.connect(self.dsn)

    def retrieve(self, query: str, k: int = 3) -> list[PolicyCitation]:
        vec = vector_literal(self.embedder.embed([query])[0])
        with self._conn() as conn:
            rows = conn.execute(self.SQL, (vec, vec, k)).fetchall()
        out = []
        for clause_id, title, text, sim in rows:
            score = round(min(1.0, max(0.0, float(sim))), 4)
            if score >= self.min_score:
                out.append(
                    PolicyCitation(clause_id=clause_id, quote=f"{title}. {text}", score=score)
                )
        return out


def get_retriever(settings: AISettings | None = None) -> Retriever:
    """RETRIEVER=memory (default, light, CI) | pgvector."""
    s = settings or AISettings.from_env()
    if s.retriever == "memory":
        return InMemoryRetriever()
    if s.retriever == "pgvector":
        if not s.database_url:
            raise ValueError("RETRIEVER=pgvector needs DATABASE_URL")
        from .embeddings import Embedder

        return PgVectorRetriever(s.database_url, Embedder(s.embedding_model), s.retrieval_min_score)
    raise ValueError(f"unknown RETRIEVER {s.retriever!r}")
