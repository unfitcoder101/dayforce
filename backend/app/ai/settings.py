"""AI module settings, read from env vars (CLAUDE.md section 12). Nothing is hard-coded."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# Fixed trust score attached to rule-based (RULES_FALLBACK) proposals. It is NOT a model
# estimate: rule-based fixes are deterministic and policy-backed but still need manager
# approval, so they get a constant "fairly high, not certain" value. Compared against
# CONFIDENCE_ESCALATE_BELOW like any other confidence.
FALLBACK_CONFIDENCE = 0.75


@dataclass(frozen=True)
class AISettings:
    llm_provider: str = "mock"  # openai_compatible | ollama | mock
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = 20.0
    # Per-retriever relevance thresholds: keyword overlap and cosine similarity are on
    # different scales (calibrated by tests/unit/ai/test_pgvector_integration.py).
    retrieval_min_score_memory: float = 0.35
    retrieval_min_score_pgvector: float = 0.57
    retrieval_min_score_override: float | None = None  # RETRIEVAL_MIN_SCORE; beats both
    confidence_escalate_below: float = 0.5
    llm_mock_mode: str = "ok"  # only used by the mock provider
    retriever: str = "memory"  # memory | pgvector
    database_url: str = ""
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    max_llm_calls: int = 2  # shared budget per proposal, whatever the failure cause
    total_deadline_seconds: float = 25.0  # wall-clock limit for all LLM work per proposal

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AISettings:
        e = os.environ if env is None else env
        return cls(
            llm_provider=e.get("LLM_PROVIDER", "mock"),
            llm_base_url=e.get("LLM_BASE_URL", ""),
            llm_model=e.get("LLM_MODEL", ""),
            llm_api_key=e.get("LLM_API_KEY", ""),
            llm_timeout_seconds=float(e.get("LLM_TIMEOUT_SECONDS", "20")),
            retrieval_min_score_memory=float(e.get("RETRIEVAL_MIN_SCORE_MEMORY", "0.35")),
            retrieval_min_score_pgvector=float(e.get("RETRIEVAL_MIN_SCORE_PGVECTOR", "0.57")),
            retrieval_min_score_override=(
                float(e["RETRIEVAL_MIN_SCORE"]) if e.get("RETRIEVAL_MIN_SCORE") else None
            ),
            confidence_escalate_below=float(e.get("CONFIDENCE_ESCALATE_BELOW", "0.5")),
            llm_mock_mode=e.get("LLM_MOCK_MODE", "ok"),
            retriever=e.get("RETRIEVER", "memory"),
            database_url=e.get("DATABASE_URL", ""),
            embedding_model=e.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        )

    @property
    def retrieval_min_score(self) -> float:
        """Threshold for the selected retriever; a plain RETRIEVAL_MIN_SCORE overrides both."""
        if self.retrieval_min_score_override is not None:
            return self.retrieval_min_score_override
        if self.retriever == "pgvector":
            return self.retrieval_min_score_pgvector
        return self.retrieval_min_score_memory
