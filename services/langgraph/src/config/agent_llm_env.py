"""Env vars each LLM-backed ReactAgent reads, and which of them it needs.

Single source of truth for three consumers: the startup checks in `main.py` and
`consumers/architect.py`, the documented groups in `.env.example`, and the test
that keeps those two in sync.

Most groups are requirements: the agent runs only when every var in its group
has a value. `qa` is not one of them. Exploratory QA is performed by the
assigned subscription coding agent, and its triplet is an optional API fallback
that is read only after that executor has actually failed — so an empty QA group
is a valid production configuration and is never checked at startup or at the
beginning of a run. `OPTIONAL_LLM_ENV` is what keeps that distinction visible to
the documentation test instead of leaving it to a comment.
"""

from __future__ import annotations

from typing import Any

AGENT_LLM_ENV: dict[str, tuple[str, str, str]] = {
    "po": ("PO_LLM_MODEL", "PO_LLM_BASE_URL", "PO_LLM_API_KEY"),
    "architect": ("ARCHITECT_LLM_MODEL", "ARCHITECT_LLM_BASE_URL", "ARCHITECT_LLM_API_KEY"),
    "qa": ("QA_LLM_MODEL", "QA_LLM_BASE_URL", "QA_LLM_API_KEY"),
}

# Groups whose absence is a supported configuration rather than a broken one.
OPTIONAL_LLM_ENV = frozenset({"qa"})


def missing_llm_env(agent: str, settings: Any) -> list[str]:
    """Return the agent's env var names that carry no value.

    Settings field names are the lowercased env var names (pydantic-settings is
    case-insensitive and uses no prefix here).
    """
    return [name for name in AGENT_LLM_ENV[agent] if not getattr(settings, name.lower())]
