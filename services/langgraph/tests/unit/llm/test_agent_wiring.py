"""The Architect, the PO and the PO summarizer receive their channel chains.

The consumers read each chain from agent configuration, refuse an invalid one
at startup, stop blocking on OpenRouter env when another channel exists, and
the Architect's job log names the channels its planning attempt used.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import HumanMessage
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story_planning import StoryPlanning, StoryPlanningState
from shared.contracts.queues.architect import ArchitectMessage
from src.agents.po.graph import create_po_graph
from src.llm import InvalidChannelChainError
from tests.unit.factories import make_project, make_story
from tests.unit.llm.fake_cli import turn

_NO_SUBSCRIPTION_ENV = {"llm_codex_home": None, "claude_code_oauth_token": None}

_NO_OPENROUTER_ENV = {
    "architect_llm_model": None,
    "architect_llm_base_url": None,
    "architect_llm_api_key": None,
    "po_llm_model": None,
    "po_llm_base_url": None,
    "po_llm_api_key": None,
}


class _Configs:
    """`get_agent_config` as the API answers it: a record, or None for 404."""

    def __init__(self, **records):
        self.records = records
        self.asked: list[str] = []

    async def get_agent_config(self, agent_id):
        self.asked.append(agent_id)
        return self.records.get(agent_id)


def _architect_api(configs: _Configs) -> MagicMock:
    api = MagicMock()
    api.get_agent_config = configs.get_agent_config
    api.get_story = AsyncMock(return_value=make_story(id="story-abc", status="created"))
    api.get_project = AsyncMock(return_value=make_project(status=ProjectStatus.ACTIVE, config={}))
    api.get_tasks_by_story = AsyncMock(return_value=[])
    api.transition_story = AsyncMock()
    api.get_product_brief_by_story = AsyncMock(return_value=None)
    # Every planning attempt reports its outcome on the story.
    api.record_planning_outcome = AsyncMock(
        return_value=StoryPlanning(
            state=StoryPlanningState.PARKED, failed_attempts=1, recorded_at=datetime.now(UTC)
        )
    )
    return api


def _job() -> dict:
    return ArchitectMessage(
        story_id="story-abc", project_id="proj-123", telegram_chat_id="user-1"
    ).model_dump(mode="json")


class TestArchitectConsumer:
    async def test_the_default_chain_plans_without_openrouter_env_and_logs_the_channel(
        self, channels
    ):
        from src.consumers import architect

        channels.codex.script({"answer": turn("Nothing to decompose.")})
        settings = channels.settings(**_NO_OPENROUTER_ENV)
        with (
            patch.object(architect, "api_client", _architect_api(_Configs())),
            patch.object(architect, "get_settings", return_value=settings),
            capture_logs() as logs,
        ):
            result = await architect.process_architect_job(_job(), AsyncMock())

        assert result["status"] == "success"
        [success] = [log for log in logs if log["event"] == "architect_job_success"]
        assert success["llm_channels"] == ["codex"]
        assert success["llm_channel_failures"] == []
        assert channels.codex.calls

    async def test_a_failed_planning_attempt_names_every_channel_it_tried(self, channels):
        from src.consumers import architect

        channels.codex.script({"exit": 1, "stderr": "You've hit your usage limit."})
        channels.claude.script({"exit": 1, "stderr": "API Error: 401 invalid token"})
        settings = channels.settings(**_NO_OPENROUTER_ENV)
        with (
            patch.object(architect, "api_client", _architect_api(_Configs())),
            patch.object(architect, "get_settings", return_value=settings),
            capture_logs() as logs,
        ):
            result = await architect.process_architect_job(_job(), AsyncMock())

        assert result["status"] == "failed"
        assert "every LLM channel of architect failed" in result["error"]
        [failed] = [log for log in logs if log["event"] == "architect_job_failed"]
        assert failed["error_type"] == "LLMChannelsExhausted"
        assert failed["llm_channel_failures"] == [
            "codex:quota_exhausted",
            "claude:unauthorized",
            "openrouter:missing_credential",
        ]

    async def test_an_invalid_stored_chain_fails_the_job_by_name(self, channels):
        from src.consumers import architect

        configs = _Configs(architect={"llm_channels": [{"channel": "gpt"}]})
        with (
            patch.object(architect, "api_client", _architect_api(configs)),
            patch.object(architect, "get_settings", return_value=channels.settings()),
        ):
            result = await architect.process_architect_job(_job(), AsyncMock())

        assert result["status"] == "failed"
        assert "invalid_llm_channel_chain" in result["error"]
        assert not channels.codex.calls

    def test_main_refuses_to_start_on_an_invalid_stored_chain(self, channels):
        from src.consumers import architect

        configs = _Configs(architect={"llm_channels": []})
        with (
            patch.object(architect, "api_client", MagicMock(**{"close": AsyncMock()})) as api,
            patch.object(architect, "get_settings", return_value=channels.settings()),
            patch.object(architect, "start_worker") as start_worker,
        ):
            api.get_agent_config = configs.get_agent_config
            with pytest.raises(InvalidChannelChainError, match=r"agent_configs\[architect\]"):
                architect.main()

        start_worker.assert_not_called()
        api.close.assert_awaited_once()

    def test_main_starts_on_the_default_chain_without_openrouter_env(self, channels):
        from src.consumers import architect

        with (
            patch.object(architect, "api_client", MagicMock(**{"close": AsyncMock()})) as api,
            patch.object(
                architect, "get_settings", return_value=channels.settings(**_NO_OPENROUTER_ENV)
            ),
            patch.object(architect, "start_worker") as start_worker,
        ):
            api.get_agent_config = _Configs().get_agent_config
            architect.main()

        start_worker.assert_called_once()

    def test_main_refuses_to_start_when_no_channel_of_the_chain_is_configured(self, channels):
        """The default chain with no credential at all could only exhaust every channel."""
        from src.consumers import architect

        settings = channels.settings(**_NO_OPENROUTER_ENV, **_NO_SUBSCRIPTION_ENV)
        with (
            patch.object(architect, "api_client", MagicMock(**{"close": AsyncMock()})) as api,
            patch.object(architect, "get_settings", return_value=settings),
            patch.object(architect, "start_worker") as start_worker,
        ):
            api.get_agent_config = _Configs().get_agent_config
            with pytest.raises(RuntimeError, match="architect_llm_not_configured") as refused:
                architect.main()

        start_worker.assert_not_called()
        for name in ("LLM_CODEX_HOME", "CLAUDE_CODE_OAUTH_TOKEN", "ARCHITECT_LLM_API_KEY"):
            assert name in str(refused.value)

    @pytest.mark.parametrize(
        "configured",
        [
            {"claude_code_oauth_token": None},
            {"llm_codex_home": None},
        ],
        ids=["codex-only", "claude-only"],
    )
    def test_one_configured_channel_is_enough_to_start(self, channels, configured):
        from src.consumers import architect

        settings = channels.settings(**_NO_OPENROUTER_ENV, **configured)
        with (
            patch.object(architect, "api_client", MagicMock(**{"close": AsyncMock()})) as api,
            patch.object(architect, "get_settings", return_value=settings),
            patch.object(architect, "start_worker") as start_worker,
        ):
            api.get_agent_config = _Configs().get_agent_config
            architect.main()

        start_worker.assert_called_once()


class TestPoStartup:
    @pytest.mark.parametrize(
        ("records", "missing"),
        [
            ({}, []),
            (
                {"po": {"llm_channels": [{"channel": "openrouter"}]}},
                ["PO_LLM_MODEL", "PO_LLM_BASE_URL", "PO_LLM_API_KEY"],
            ),
            (
                {"po_summarizer": {"llm_channels": [{"channel": "openrouter", "model": "m"}]}},
                ["PO_LLM_BASE_URL", "PO_LLM_API_KEY"],
            ),
        ],
        ids=["default-chains", "po-openrouter-only", "summarizer-openrouter-only"],
    )
    async def test_openrouter_env_blocks_only_an_openrouter_only_chain(
        self, channels, records, missing
    ):
        from src import main

        configs = _Configs(**records)
        with (
            patch.object(main, "api_client", configs),
            patch.object(
                main, "get_settings", return_value=channels.settings(**_NO_OPENROUTER_ENV)
            ),
        ):
            assert await main._po_missing_env() == missing

        assert configs.asked == ["po", "po_summarizer"]

    async def test_no_configured_channel_keeps_the_po_disabled(self, channels):
        """A deployment with no LLM credential at all (the service-test stack) keeps PO off."""
        from src import main

        settings = channels.settings(**_NO_OPENROUTER_ENV, **_NO_SUBSCRIPTION_ENV)
        with (
            patch.object(main, "api_client", _Configs()),
            patch.object(main, "get_settings", return_value=settings),
        ):
            missing = await main._po_missing_env()

        assert missing == [
            "LLM_CODEX_HOME",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "PO_LLM_MODEL",
            "PO_LLM_BASE_URL",
            "PO_LLM_API_KEY",
        ]

    async def test_the_disabled_po_never_needs_a_checkpoint_database(self, channels):
        """No configured channel: run_worker logs po_consumer_disabled instead of refusing."""
        from src import main

        settings = channels.settings(
            **_NO_OPENROUTER_ENV, **_NO_SUBSCRIPTION_ENV, checkpoint_database_url=None
        )

        async def _idle():
            return None

        with (
            patch.object(main, "api_client", _Configs()),
            patch.object(main, "get_settings", return_value=settings),
            patch.object(main, "listen_provisioner_triggers", _idle),
            patch.object(main, "listen_worker_events", _idle),
            capture_logs() as logs,
        ):
            await main.run_worker()

        [disabled] = [log for log in logs if log["event"] == "po_consumer_disabled"]
        assert "LLM_CODEX_HOME" in disabled["missing_env"]

    async def test_an_invalid_stored_po_chain_stops_the_service(self, channels):
        from src import main

        configs = _Configs(po={"llm_channels": [{"channel": "claude"}, {"channel": "claude"}]})
        with (
            patch.object(main, "api_client", configs),
            patch.object(main, "get_settings", return_value=channels.settings()),
            pytest.raises(InvalidChannelChainError, match=r"agent_configs\[po\]"),
        ):
            await main._po_missing_env()


class TestPoGraph:
    async def test_the_summarizer_runs_on_its_own_chain(self, channels):
        from src.consumers import po

        channels.codex.script({"answer": turn("SUMMARY: the user wants an expense bot.")})
        channels.openrouter.outcomes = ["Understood."]
        configs = _Configs(
            po={"llm_channels": [{"channel": "openrouter"}]},
            po_summarizer={"llm_channels": [{"channel": "codex", "model": "gpt-5.5-mini"}]},
        )
        with patch.object(po, "api_client", configs):
            llms = await po.load_po_llms(channels.settings())
        graph = await create_po_graph(
            llm=llms.po,
            summarization_llm=llms.summarizer,
            summarization_max_tokens=256,
            summarization_trigger_tokens=128,
            summarization_max_summary_tokens=64,
        )
        config = {"configurable": {"thread_id": "po-summary", "telegram_chat_id": "42"}}
        long_turn = "I want a bot that tracks my expenses and incomes. " * 40

        with capture_logs() as logs:
            for text in (long_turn, long_turn, "What did I ask for?"):
                await graph.ainvoke({"messages": [HumanMessage(content=text)]}, config=config)

        assert configs.asked == ["po", "po_summarizer"]
        assert channels.codex.calls, "the conversation never reached the summarizer's chain"
        summary_call = channels.codex.calls[0]
        assert "tracks my expenses" in summary_call["stdin"]
        argv = summary_call["argv"]
        assert argv[argv.index("--model") + 1] == "gpt-5.5-mini"
        used = {
            (log["agent"], log["channel"]) for log in logs if log["event"] == "llm_channel_used"
        }
        assert used == {("po", "openrouter"), ("po_summarizer", "codex")}
        # The PO's own model saw the summary, not the raw long turns.
        assert any(
            "SUMMARY: the user wants an expense bot." in str(message.content)
            for message in channels.openrouter.seen[-1]
        )
