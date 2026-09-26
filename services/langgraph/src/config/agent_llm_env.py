"""OpenRouter env each LLM-backed ReactAgent's openrouter channel reads.

Single source of truth for three consumers: the openrouter channel
(`src/llm/openrouter.py`, the only reader of these values), the documented
groups in `.env.example`, and the test that keeps those two in sync.

A group is required by the agent's openrouter channel: without every var of it
that channel fails as a missing credential and the chain moves on. Only an
agent whose chain is openrouter alone cannot run without it (see `src/llm`).
Exploratory QA has no group here — it is performed by the assigned subscription
coding agent and never by an LLM this service holds a key for.
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
