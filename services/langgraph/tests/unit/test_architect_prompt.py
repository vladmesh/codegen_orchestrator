"""Unit tests for architect system prompt — ensures key directives are present."""

from __future__ import annotations

from src.prompts.architect import SYSTEM_PROMPT


class TestArchitectPromptContent:
    def test_references_scaffolded_project(self):
        assert "scaffold" in SYSTEM_PROMPT.lower()

    def test_references_agents_md(self):
        assert "AGENTS.md" in SYSTEM_PROMPT

    def test_prohibits_infrastructure_tasks(self):
        lower = SYSTEM_PROMPT.lower()
        assert "do not create tasks for" in lower or "do not create" in lower
        assert "infrastructure" in lower or "docker" in lower or "ci" in lower

    def test_mentions_business_logic_focus(self):
        assert "business logic" in SYSTEM_PROMPT.lower()

    def test_mentions_diff(self):
        lower = SYSTEM_PROMPT.lower()
        assert "diff" in lower or "difference" in lower

    def test_mentions_task_count_guidance(self):
        assert "1" in SYSTEM_PROMPT and "2" in SYSTEM_PROMPT
