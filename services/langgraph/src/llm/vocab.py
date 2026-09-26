"""The LLM-backed service agents that answer through a channel chain."""

from __future__ import annotations

from enum import StrEnum


class LLMAgent(StrEnum):
    """An LLM-backed service agent; the value is its `agent_configs` id."""

    ARCHITECT = "architect"
    PO = "po"
    PO_SUMMARIZER = "po_summarizer"
