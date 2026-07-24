"""Env vars each LLM-backed ReactAgent needs before it can run.

Single source of truth for three consumers: the startup checks in `main.py` and
`consumers/architect.py`, the documented groups in `.env.example`, and the test
that keeps those two in sync. An agent runs only when every var in its group has
a value.
"""

from __future__ import annotations

from typing import Any

AGENT_LLM_ENV: dict[str, tuple[str, str, str]] = {
    "po": ("PO_LLM_MODEL", "PO_LLM_BASE_URL", "PO_LLM_API_KEY"),
    "architect": ("ARCHITECT_LLM_MODEL", "ARCHITECT_LLM_BASE_URL", "ARCHITECT_LLM_API_KEY"),
}


def missing_llm_env(agent: str, settings: Any) -> list[str]:
    """Return the agent's env var names that carry no value.

    Settings field names are the lowercased env var names (pydantic-settings is
    case-insensitive and uses no prefix here).
    """
    return [name for name in AGENT_LLM_ENV[agent] if not getattr(settings, name.lower())]
