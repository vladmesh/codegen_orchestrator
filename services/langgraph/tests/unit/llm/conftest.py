"""Fixtures for the LLM channel chain: fake CLIs on PATH, a fake openrouter model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
import pytest

from tests.unit.llm.fake_cli import FakeCli, install

OPENROUTER_KEY = "sk-or-test-secret-key"


class FakeOpenRouterModel(BaseChatModel):
    """Stands in for `ChatOpenAI`: answers, or raises what the provider SDK raises."""

    outcomes: list[Any]
    seen: list[list] = []
    bound: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake-openrouter"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003 - test stand-in
        self.bound.append(list(tools))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        self.seen.append(list(messages))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        message = outcome if isinstance(outcome, AIMessage) else AIMessage(content=outcome)
        return ChatResult(generations=[ChatGeneration(message=message)])


@dataclass
class Channels:
    codex: FakeCli
    claude: FakeCli
    codex_home: Path
    bin_dir: Path
    openrouter: FakeOpenRouterModel

    def settings(self, **overrides: Any) -> SimpleNamespace:
        values = {
            "llm_codex_home": str(self.codex_home),
            "claude_code_oauth_token": "claude-oauth-test-token",
            "architect_llm_model": "openai/gpt-5.6-sol",
            "architect_llm_base_url": "https://openrouter.test/api/v1",
            "architect_llm_api_key": OPENROUTER_KEY,
            "po_llm_model": "openai/gpt-5.6-sol",
            "po_llm_base_url": "https://openrouter.test/api/v1",
            "po_llm_api_key": OPENROUTER_KEY,
            "summarization_model": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)


@pytest.fixture
def channels(tmp_path, monkeypatch):
    """Fake `codex` and `claude` first on PATH, a logged-in Codex profile, a fake OpenRouter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    (codex_home / "auth.json").write_text('{"tokens": {"access_token": "a"}}')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    openrouter = FakeOpenRouterModel(outcomes=["answer from openrouter"], seen=[], bound=[])
    with patch("src.llm.openrouter.ChatOpenAI", return_value=openrouter):
        yield Channels(
            codex=install(bin_dir, "codex"),
            claude=install(bin_dir, "claude"),
            codex_home=codex_home,
            bin_dir=bin_dir,
            openrouter=openrouter,
        )
