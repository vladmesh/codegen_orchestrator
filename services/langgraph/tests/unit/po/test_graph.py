"""Unit tests for PO graph."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.po.graph import (
    CHARS_PER_TOKEN,
    MAX_CONTEXT_TOKENS,
    _estimate_tokens,
    prompt_with_trimming,
)
from src.po.prompts import SYSTEM_PROMPT


class TestEstimateTokens:
    def test_short_message(self):
        msg = HumanMessage(content="hello")
        tokens = _estimate_tokens(msg)
        assert tokens == len("hello") // CHARS_PER_TOKEN + 1

    def test_empty_message(self):
        msg = HumanMessage(content="")
        assert _estimate_tokens(msg) == 1  # minimum 1

    def test_long_message(self):
        content = "x" * 1000
        msg = HumanMessage(content=content)
        assert _estimate_tokens(msg) == 1000 // CHARS_PER_TOKEN + 1


class TestPromptWithTrimming:
    def test_prepends_system_prompt(self):
        state = {"messages": [HumanMessage(content="hello")]}
        result = prompt_with_trimming(state)

        # system prompt + 1 user message
        assert isinstance(result[0], SystemMessage)
        assert result[0].content == SYSTEM_PROMPT
        assert result[1].content == "hello"
        assert len(result) == len(state["messages"]) + 1

    def test_empty_messages(self):
        state = {"messages": []}
        result = prompt_with_trimming(state)

        assert len(result) == 1
        assert isinstance(result[0], SystemMessage)

    def test_trims_old_messages(self):
        # Create messages that exceed token budget
        big_content = "x" * (MAX_CONTEXT_TOKENS * CHARS_PER_TOKEN)
        messages = [
            HumanMessage(content=big_content),
            HumanMessage(content="recent message"),
        ]
        state = {"messages": messages}
        result = prompt_with_trimming(state)

        # Should have system prompt + only the recent message (big one trimmed)
        assert isinstance(result[0], SystemMessage)
        assert any(m.content == "recent message" for m in result[1:])
        assert not any(m.content == big_content for m in result[1:])

    def test_strips_existing_system_messages(self):
        messages = [
            SystemMessage(content="old system"),
            HumanMessage(content="hello"),
            AIMessage(content="hi"),
        ]
        state = {"messages": messages}
        result = prompt_with_trimming(state)

        # Only our system prompt, not the old one
        system_msgs = [m for m in result if isinstance(m, SystemMessage)]
        assert len(system_msgs) == 1
        assert system_msgs[0].content == SYSTEM_PROMPT

    def test_keeps_all_messages_within_budget(self):
        messages = [
            HumanMessage(content="msg1"),
            AIMessage(content="msg2"),
            HumanMessage(content="msg3"),
        ]
        state = {"messages": messages}
        result = prompt_with_trimming(state)

        # System + all 3 messages should fit
        expected = len(messages) + 1  # +1 for system prompt
        assert len(result) == expected

    def test_missing_messages_key(self):
        result = prompt_with_trimming({})
        assert len(result) == 1
        assert isinstance(result[0], SystemMessage)
