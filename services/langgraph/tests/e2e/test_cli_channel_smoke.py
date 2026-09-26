"""Opt-in smoke: one real Architect-style tool-call turn through each subscription CLI.

Runs the real `codex` / `claude` channel, alone in its chain, over a bound tool,
and asserts that the envelope the pinned CLI prints parses into a tool call and
that the chain names the channel in `response_metadata["llm_channel"]`. Each
channel skips on its own without its credential or its CLI.

Run it inside the langgraph image, against the credentials the containers really
use and never against a copy of a Codex profile: a copy that refreshes rotates
the refresh token and breaks the original (docs/coding-agents.md). For example,
on a host whose `.env` configures both channels:

    docker compose run --rm --no-deps -v ./services/langgraph/tests:/app/tests architect \\
        sh -c 'pip install -q pytest pytest-asyncio && \\
               python -m pytest -p no:cacheprovider /app/tests/e2e/test_cli_channel_smoke.py -v'

The service image carries no pytest, so the throwaway container installs it.

It spends one subscription turn per channel (two for a channel whose first
answer needs the corrective re-ask).
"""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
import pytest

from shared.contracts.dto.llm_channel import LLMChannel, LLMChannelConfig
from src.llm import LLMAgent, build_agent_llm

CREDENTIAL = {
    LLMChannel.CODEX: "LLM_CODEX_HOME",
    LLMChannel.CLAUDE: "CLAUDE_CODE_OAUTH_TOKEN",
}


@tool
def get_story(story_id: str) -> str:
    """Return the story text for a story id."""
    return f"Story {story_id}: add a /start command."


def _settings(channel: LLMChannel) -> SimpleNamespace:
    credential = os.getenv(CREDENTIAL[channel])
    if not credential:
        pytest.skip(f"{CREDENTIAL[channel]} required for the {channel.value} smoke")
    if shutil.which(channel.value) is None:
        pytest.skip(f"{channel.value} is not on PATH")
    return SimpleNamespace(
        llm_codex_home=os.getenv("LLM_CODEX_HOME"),
        claude_code_oauth_token=os.getenv("CLAUDE_CODE_OAUTH_TOKEN"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [LLMChannel.CODEX, LLMChannel.CLAUDE])
async def test_a_cli_channel_answers_an_architect_turn_with_a_tool_call(channel):
    settings = _settings(channel)
    llm = build_agent_llm(LLMAgent.ARCHITECT, [LLMChannelConfig(channel=channel)], settings)

    answer = await llm.bind_tools([get_story]).ainvoke(
        [
            SystemMessage(content="You are the Architect. You decompose stories into tasks."),
            HumanMessage(
                content="Decompose story story-1. Start by calling get_story for story-1."
            ),
        ]
    )

    assert isinstance(answer, AIMessage)
    assert answer.response_metadata["llm_channel"] == channel.value
    [call] = [call for call in answer.tool_calls if call["name"] == "get_story"]
    assert call["args"] == {"story_id": "story-1"}
    assert call["id"]
