"""The LLM channel chain: order, switch semantics, CLI turns, recording.

No network and no real CLI: `codex` and `claude` are fake executables on a
temporary PATH (`fake_cli.py`), OpenRouter is a fake chat model standing where
the channel constructs `ChatOpenAI`.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
from pathlib import Path
import re

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
import openai
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.llm_channel import LLMChannel, LLMChannelConfig
from src.llm import (
    ChannelFailureClass,
    InvalidChannelChainError,
    LLMAgent,
    LLMChannelsExhausted,
    build_agent_llm,
    channel_usage,
    load_channel_chain,
)
from src.llm.cli_turn import CODEX_PROFILE_LOCK_NAME
from tests.unit.llm.conftest import OPENROUTER_KEY
from tests.unit.llm.fake_cli import turn

SRC = Path(__file__).resolve().parents[3] / "src"


@tool
def get_story(story_id: str) -> str:
    """Return the story text for a story id."""
    return f"Story {story_id}: add a /start command."


@tool
def create_task(title: str, priority: int) -> str:
    """Create one task."""
    return f"created {title} at {priority}"


def _chain(*names: str, **models: str) -> list[LLMChannelConfig]:
    return [LLMChannelConfig(channel=LLMChannel(name), model=models.get(name)) for name in names]


DEFAULT = ("codex", "claude", "openrouter")


class _Api:
    def __init__(self, records: dict[str, dict | None]):
        self.records = records

    async def get_agent_config(self, agent_id: str):
        record = self.records.get(agent_id)
        if record is None:
            request = httpx.Request("GET", f"http://api/agent-configs/{agent_id}")
            raise httpx.HTTPStatusError(
                "404", request=request, response=httpx.Response(404, request=request)
            )
        return record


def _status_error(status: int, message: str = "provider says no") -> openai.APIStatusError:
    request = httpx.Request("POST", "https://openrouter.test/api/v1/chat/completions")
    return openai.APIStatusError(
        message, response=httpx.Response(status, request=request), body=None
    )


async def _ask(llm, text: str = "Say hello.", tools=None) -> AIMessage:
    model = llm.bind_tools(tools) if tools else llm
    return await model.ainvoke([HumanMessage(content=text)])


# --- chain from agent configuration ------------------------------------------


class TestChainFromConfiguration:
    async def test_no_record_means_the_default_order(self, channels):
        chain = await load_channel_chain(_Api({}), LLMAgent.ARCHITECT)

        assert [entry.channel.value for entry in chain] == list(DEFAULT)
        llm = build_agent_llm(LLMAgent.ARCHITECT, chain, channels.settings())
        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.content == "answer from codex"
        assert answer.response_metadata["llm_channel"] == "codex"
        assert not channels.claude.calls and not channels.openrouter.seen
        [used] = [log for log in logs if log["event"] == "llm_channel_used"]
        assert used["position"] == 1

    async def test_a_record_without_the_field_means_the_default_order(self, channels):
        chain = await load_channel_chain(
            _Api({"po": {"id": "po", "llm_channels": None}}), LLMAgent.PO
        )

        assert [entry.channel.value for entry in chain] == list(DEFAULT)
        assert all(entry.model is None for entry in chain)

    async def test_the_order_and_models_come_from_the_stored_config(self, channels):
        stored = {
            "llm_channels": [
                {"channel": "openrouter", "model": "anthropic/claude-haiku-4-5"},
                {"channel": "claude", "model": "claude-sonnet-5"},
            ]
        }
        chain = await load_channel_chain(_Api({"architect": stored}), LLMAgent.ARCHITECT)
        llm = build_agent_llm(LLMAgent.ARCHITECT, chain, channels.settings())

        assert llm.describe() == ["openrouter:anthropic/claude-haiku-4-5", "claude:claude-sonnet-5"]
        channels.openrouter.outcomes = [_status_error(503)]
        answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "claude"
        [call] = channels.claude.calls
        assert call["argv"][call["argv"].index("--model") + 1] == "claude-sonnet-5"
        assert not channels.codex.calls

    @pytest.mark.parametrize(
        "stored",
        [[], [{"channel": "gemini"}], [{"channel": "codex"}, {"channel": "codex"}], "codex"],
        ids=["empty", "unknown-channel", "duplicate", "not-a-list"],
    )
    async def test_an_invalid_stored_chain_is_a_named_error(self, stored):
        with pytest.raises(InvalidChannelChainError) as error:
            await load_channel_chain(
                _Api({"po_summarizer": {"llm_channels": stored}}), LLMAgent.PO_SUMMARIZER
            )

        assert "agent_configs[po_summarizer].llm_channels" in str(error.value)

    async def test_an_unreachable_api_is_not_a_default_chain(self):
        class _Down:
            async def get_agent_config(self, agent_id):
                request = httpx.Request("GET", "http://api/agent-configs/architect")
                raise httpx.HTTPStatusError(
                    "500", request=request, response=httpx.Response(500, request=request)
                )

        with pytest.raises(httpx.HTTPStatusError):
            await load_channel_chain(_Down(), LLMAgent.ARCHITECT)

    async def test_the_openrouter_model_defaults_to_the_agents_env_model(self, channels):
        settings = channels.settings(summarization_model="anthropic/claude-haiku-4-5")
        chain = _chain(*DEFAULT)

        assert build_agent_llm(LLMAgent.ARCHITECT, chain, settings).describe() == [
            "codex:default",
            "claude:default",
            "openrouter:openai/gpt-5.6-sol",
        ]
        assert build_agent_llm(LLMAgent.PO_SUMMARIZER, chain, settings).describe()[-1] == (
            "openrouter:anthropic/claude-haiku-4-5"
        )
        no_summarizer_model = channels.settings(summarization_model=None)
        assert build_agent_llm(LLMAgent.PO_SUMMARIZER, chain, no_summarizer_model).describe()[
            -1
        ] == ("openrouter:openai/gpt-5.6-sol")


# --- switch semantics --------------------------------------------------------

_CLI_FAILURES = [
    ({"exit": 1, "stderr": "ERROR: unexpected status 401 Unauthorized"}, "unauthorized"),
    ({"exit": 1, "stderr": "ERROR: unexpected status 402 Payment Required"}, "payment_required"),
    ({"exit": 1, "stderr": "ERROR: unexpected status 403 Forbidden"}, "forbidden"),
    ({"exit": 1, "stderr": "ERROR: unexpected status 429 Too Many Requests"}, "rate_limited"),
    ({"exit": 1, "stderr": "ERROR: unexpected status 503 Service Unavailable"}, "server_error"),
    (
        {"exit": 1, "stderr": "You've hit your usage limit. Try again in 3 days."},
        "quota_exhausted",
    ),
    ({"exit": 3, "stderr": "thread 'main' panicked"}, "nonzero_exit"),
    ({"raw": "I think the answer is yes"}, "invalid_output"),
]


class TestSwitchOnChannelFailure:
    @pytest.mark.parametrize(
        ("response", "failure_class"),
        _CLI_FAILURES,
        ids=[failure_class for _, failure_class in _CLI_FAILURES],
    )
    async def test_a_cli_failure_moves_the_call_to_the_next_channel(
        self, channels, response, failure_class
    ):
        channels.codex.script(response)
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())

        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.content == "answer from claude"
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert (failed["channel"], failed["failure_class"]) == ("codex", failure_class)

    async def test_claude_reporting_an_error_in_its_result_is_a_channel_failure(self, channels):
        channels.claude.script(
            {
                "exit": 1,
                "envelope": {
                    "type": "result",
                    "is_error": True,
                    "result": 'API Error: 429 {"type":"rate_limit_error"}',
                },
            }
        )
        llm = build_agent_llm(LLMAgent.PO, _chain("claude", "openrouter"), channels.settings())

        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "openrouter"
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert failed["failure_class"] == "rate_limited"

    @pytest.mark.parametrize(
        ("error", "failure_class"),
        [
            (_status_error(401), "unauthorized"),
            (_status_error(402, "Insufficient credits"), "quota_exhausted"),
            (_status_error(402, "Payment Required"), "payment_required"),
            (_status_error(403), "forbidden"),
            (_status_error(429), "rate_limited"),
            (_status_error(500), "server_error"),
            (_status_error(502), "server_error"),
            (
                openai.APITimeoutError(request=httpx.Request("POST", "https://openrouter.test")),
                "timeout",
            ),
            (
                openai.APIConnectionError(request=httpx.Request("POST", "https://openrouter.test")),
                "unreachable",
            ),
        ],
        ids=[
            "401",
            "402-credits",
            "402",
            "403",
            "429",
            "500",
            "502",
            "sdk-timeout",
            "connection",
        ],
    )
    async def test_an_openrouter_failure_moves_the_call_to_the_next_channel(
        self, channels, error, failure_class
    ):
        channels.openrouter.outcomes = [error]
        llm = build_agent_llm(LLMAgent.PO, _chain("openrouter", "claude"), channels.settings())

        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "claude"
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert (failed["channel"], failed["failure_class"]) == ("openrouter", failure_class)

    async def test_a_channel_past_its_timeout_is_abandoned(self, channels):
        channels.codex.script({"sleep": 30, "answer": turn("too late")})
        chain = [
            LLMChannelConfig(channel=LLMChannel.CODEX, timeout_seconds=0.5),
            LLMChannelConfig(channel=LLMChannel.CLAUDE),
        ]
        llm = build_agent_llm(LLMAgent.ARCHITECT, chain, channels.settings())

        loop = asyncio.get_running_loop()
        started = loop.time()
        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.content == "answer from claude"
        assert loop.time() - started < 10  # noqa: PLR2004 - far below the fake's 30 s
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert failed["failure_class"] == "timeout"

    async def test_an_openrouter_call_past_its_timeout_is_abandoned(self, channels):
        class _Hanging(type(channels.openrouter)):
            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
                await asyncio.sleep(30)

        from unittest.mock import patch

        chain = [
            LLMChannelConfig(channel=LLMChannel.OPENROUTER, timeout_seconds=0.2),
            LLMChannelConfig(channel=LLMChannel.CLAUDE),
        ]
        with patch("src.llm.openrouter.ChatOpenAI", return_value=_Hanging(outcomes=["x"])):
            llm = build_agent_llm(LLMAgent.PO, chain, channels.settings())
        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "claude"
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert failed["failure_class"] == "timeout"

    @pytest.mark.parametrize("missing", ["codex-home", "codex-auth", "claude-token", "openrouter"])
    async def test_a_missing_credential_is_a_channel_failure(self, channels, missing):
        overrides = {
            "codex-home": {"llm_codex_home": None},
            "codex-auth": {},
            "claude-token": {"claude_code_oauth_token": None},
            "openrouter": {"architect_llm_api_key": None},
        }[missing]
        if missing == "codex-auth":
            (channels.codex_home / "auth.json").unlink()
        first = {"codex-home": "codex", "codex-auth": "codex", "claude-token": "claude"}.get(
            missing, "openrouter"
        )
        rest = [name for name in DEFAULT if name != first]
        llm = build_agent_llm(
            LLMAgent.ARCHITECT, _chain(first, *rest), channels.settings(**overrides)
        )

        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == rest[0]
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert (failed["channel"], failed["failure_class"]) == (first, "missing_credential")
        if first == "openrouter":
            assert not channels.openrouter.seen
        else:
            assert not getattr(channels, first).calls

    async def test_an_absent_cli_binary_is_a_channel_failure(self, channels):
        (channels.bin_dir / "codex").unlink()
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())

        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.content == "answer from claude"
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert failed["failure_class"] == "binary_missing"

    async def test_an_error_that_is_not_a_channel_failure_propagates_without_switching(
        self, channels
    ):
        channels.openrouter.outcomes = [ValueError("a bug in our code")]
        llm = build_agent_llm(LLMAgent.PO, _chain("openrouter", *DEFAULT[:2]), channels.settings())

        with pytest.raises(ValueError, match="a bug in our code"):
            await _ask(llm)

        assert not channels.codex.calls and not channels.claude.calls

    async def test_a_provider_bad_request_is_not_a_channel_failure(self, channels):
        channels.openrouter.outcomes = [_status_error(400, "unsupported parameter")]
        llm = build_agent_llm(LLMAgent.PO, _chain("openrouter", "codex"), channels.settings())

        with pytest.raises(openai.APIStatusError):
            await _ask(llm)

        assert not channels.codex.calls

    async def test_cancellation_propagates_and_kills_the_cli(self, channels):
        channels.codex.script({"sleep": 30, "answer": turn("never")})
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())

        task = asyncio.create_task(_ask(llm))
        for _ in range(100):
            if channels.codex.calls:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not channels.claude.calls

    async def test_when_every_channel_fails_the_error_names_each_one(self, channels):
        channels.codex.script({"exit": 1, "stderr": "ERROR: unexpected status 401 Unauthorized"})
        channels.claude.script({"exit": 1, "stderr": "Claude usage limit reached"})
        channels.openrouter.outcomes = [_status_error(502)]
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())

        with pytest.raises(LLMChannelsExhausted) as exhausted:
            await _ask(llm)

        assert exhausted.value.agent == "architect"
        assert [
            (attempt.channel.value, attempt.failure_class.value)
            for attempt in exhausted.value.attempts
        ] == [
            ("codex", "unauthorized"),
            ("claude", "quota_exhausted"),
            ("openrouter", "server_error"),
        ]
        assert "codex=unauthorized" in str(exhausted.value)


# --- the CLI turn ------------------------------------------------------------


class TestCliTurn:
    async def test_a_tool_call_round_trips_through_the_react_graph(self, channels):
        channels.codex.script(
            {"answer": turn("", [("get_story", {"story_id": "story-7"})])},
            {"answer": turn("Planned.")},
        )
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())
        graph = create_react_agent(model=llm, tools=[get_story], prompt="You plan stories.")

        result = await graph.ainvoke({"messages": [HumanMessage(content="Decompose story-7.")]})

        ai_call, tool_result, final = result["messages"][1:]
        [call] = ai_call.tool_calls
        assert call["name"] == "get_story" and call["args"] == {"story_id": "story-7"}
        assert re.fullmatch(r"call_[0-9a-f]{24}", call["id"])
        assert isinstance(tool_result, ToolMessage) and tool_result.tool_call_id == call["id"]
        assert final.content == "Planned."

        first, second = channels.codex.calls
        assert '"name": "get_story"' in first["stdin"]
        assert "Return the story text for a story id." in first["stdin"]
        conversation = json.loads(
            second["stdin"].split("CONVERSATION (JSON, oldest message first):\n")[1]
        )
        assert [entry["role"] for entry in conversation] == ["system", "user", "assistant", "tool"]
        assert conversation[0]["content"] == "You plan stories."
        assert conversation[2]["tool_calls"] == [
            {"id": call["id"], "name": "get_story", "arguments": {"story_id": "story-7"}}
        ]
        assert conversation[3]["tool_call_id"] == call["id"]
        assert conversation[3]["content"] == "Story story-7: add a /start command."

    @pytest.mark.parametrize(
        "bad_call",
        [
            ("create_task", {"title": "x", "priority": "high"}),
            ("create_task", '{"title": "x", '),
            ("delete_everything", {}),
        ],
        ids=["args-fail-schema", "args-not-json", "unbound-tool"],
    )
    async def test_an_invalid_tool_call_gets_one_reask_then_switches(self, channels, bad_call):
        channels.codex.script({"answer": turn("", [bad_call])})
        channels.claude.script(
            {"answer": turn("", [("create_task", {"title": "x", "priority": 1})])}
        )
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())

        with capture_logs() as logs:
            answer = await _ask(llm, tools=[create_task])

        first, reask = channels.codex.calls
        assert "WAS REJECTED" not in first["stdin"]
        assert "YOUR PREVIOUS ANSWER TO THIS TURN WAS REJECTED" in reask["stdin"]
        assert answer.response_metadata["llm_channel"] == "claude"
        assert answer.tool_calls[0]["args"] == {"title": "x", "priority": 1}
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert failed["failure_class"] == "invalid_output"

    async def test_a_corrected_answer_after_the_reask_is_used(self, channels):
        channels.codex.script(
            {"raw": "Sure! Here is the plan."},
            {"answer": turn("Here is the plan.")},
        )
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings())

        answer = await _ask(llm)

        assert answer.content == "Here is the plan."
        assert answer.response_metadata["llm_channel"] == "codex"
        assert "Sure! Here is the plan." in channels.codex.calls[1]["stdin"]
        assert not channels.claude.calls

    @pytest.mark.parametrize("cli", ["codex", "claude"])
    async def test_a_conversation_over_200_kib_goes_in_on_stdin(self, channels, cli):
        big = "expense line; " * 16_000  # ~224 KiB
        chain = _chain(cli, "openrouter")
        llm = build_agent_llm(LLMAgent.PO, chain, channels.settings())

        answer = await llm.ainvoke(
            [SystemMessage(content="You are the PO."), HumanMessage(content=big)]
        )

        assert answer.response_metadata["llm_channel"] == cli
        [call] = getattr(channels, cli).calls
        assert len(call["stdin"]) > 200 * 1024
        assert big in call["stdin"]
        assert all(len(argument) < 4096 for argument in call["argv"])  # noqa: PLR2004

    @pytest.mark.parametrize("cli", ["codex", "claude"])
    async def test_the_cli_gets_an_empty_workdir_no_tools_and_a_minimal_env(
        self, channels, cli, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-leak-openai")
        monkeypatch.setenv("PO_LLM_API_KEY", "sk-leak-po")
        monkeypatch.setenv("REDIS_URL", "redis://secret@redis:6379/0")
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
        monkeypatch.setenv("INTERNAL_API_KEY", "internal-leak")
        monkeypatch.setenv("LANG", "C.UTF-8")
        llm = build_agent_llm(LLMAgent.PO, _chain(cli), channels.settings())

        await _ask(llm)

        [call] = getattr(channels, cli).calls
        env = call["env"]
        assert not [name for name in env if name.endswith("_API_KEY")]
        assert "REDIS_URL" not in env and "DATABASE_URL" not in env
        assert env["LANG"] == "C.UTF-8"
        assert "sk-leak" not in json.dumps(env) and OPENROUTER_KEY not in json.dumps(env)
        assert call["cwd_listing"] == []
        argv = call["argv"]
        if cli == "codex":
            assert set(env) - {"CODEX_HOME", "PWD", "LC_CTYPE"} <= {"HOME", "PATH", "LANG"}
            assert env["CODEX_HOME"] == str(channels.codex_home)
            assert argv[:1] == ["exec"] and argv[-1] == "-"
            for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check"):
                assert flag in argv
            assert argv[argv.index("--sandbox") + 1] == "read-only"
            assert argv[argv.index("--cd") + 1] == call["cwd"]
        else:
            assert set(env) - {"CLAUDE_CODE_OAUTH_TOKEN", "PWD", "LC_CTYPE"} <= {
                "HOME",
                "PATH",
                "LANG",
            }
            assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-test-token"  # noqa: S105
            assert argv[argv.index("--tools") + 1] == ""
            assert "--no-session-persistence" in argv
            assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == [
                "content",
                "tool_calls",
            ]

    async def test_codex_waits_for_the_profile_lock_within_its_timeout(self, channels):
        lock_path = channels.codex_home / CODEX_PROFILE_LOCK_NAME
        chain = [
            LLMChannelConfig(channel=LLMChannel.CODEX, timeout_seconds=0.5),
            LLMChannelConfig(channel=LLMChannel.CLAUDE),
        ]
        llm = build_agent_llm(LLMAgent.ARCHITECT, chain, channels.settings())

        with lock_path.open("a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with capture_logs() as logs:
                answer = await _ask(llm)
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)

        assert answer.response_metadata["llm_channel"] == "claude"
        assert not channels.codex.calls, "codex ran while another turn held the profile"
        [failed] = [log for log in logs if log["event"] == "llm_channel_failed"]
        assert failed["failure_class"] == "timeout"

        released = await _ask(llm)
        assert released.response_metadata["llm_channel"] == "codex"

    async def test_codex_turns_on_one_profile_never_overlap(self, channels):
        channels.codex.script({"sleep": 0.3, "answer": turn("serial")})
        llm = build_agent_llm(LLMAgent.ARCHITECT, _chain("codex"), channels.settings())

        await asyncio.gather(_ask(llm), _ask(llm), _ask(llm))

        starts = sorted(call["started"] for call in channels.codex.calls)
        assert len(starts) == 3  # noqa: PLR2004
        # The lock is held for the whole turn, so each turn starts after the last ended.
        assert all(
            later - earlier >= 0.3 for earlier, later in zip(starts, starts[1:], strict=False)
        )  # noqa: PLR2004
        lock = channels.codex_home / CODEX_PROFILE_LOCK_NAME
        assert lock.exists() and (lock.stat().st_mode & 0o777) == 0o600  # noqa: PLR2004


# --- recording ---------------------------------------------------------------


class TestRecording:
    async def test_used_and_failed_channels_are_logged_without_secrets(self, channels):
        channels.codex.script(
            {
                "exit": 1,
                "stderr": "ERROR: status 401 Unauthorized; Authorization: Bearer eyJcodexaccess",
            }
        )
        channels.claude.script(
            {"exit": 1, "stderr": "API Error: 401 bearer claude-oauth-test-token invalid"}
        )
        llm = build_agent_llm(LLMAgent.PO, _chain(*DEFAULT), channels.settings())

        with capture_logs() as logs, channel_usage() as usage:
            answer = await _ask(llm)

        failed = [log for log in logs if log["event"] == "llm_channel_failed"]
        [used] = [log for log in logs if log["event"] == "llm_channel_used"]
        assert [(log["channel"], log["position"]) for log in failed] == [
            ("codex", 1),
            ("claude", 2),
        ]
        assert {log["agent"] for log in failed} == {"po"}
        assert "claude-oauth-test-token" not in json.dumps(failed)
        assert "eyJcodexaccess" not in json.dumps(failed)
        assert all(len(log["reason"]) <= 300 for log in failed)  # noqa: PLR2004
        assert used["agent"] == "po"
        assert (used["channel"], used["model"], used["position"]) == (
            "openrouter",
            "openai/gpt-5.6-sol",
            3,
        )
        assert isinstance(used["duration_s"], float)
        assert answer.response_metadata["llm_channel"] == "openrouter"
        assert answer.response_metadata["llm_channel_position"] == 3  # noqa: PLR2004
        assert usage.channels() == ["openrouter"]
        assert [attempt.failure_class for attempt in usage.failed] == [
            ChannelFailureClass.UNAUTHORIZED,
            ChannelFailureClass.UNAUTHORIZED,
        ]


def test_chat_openai_is_referenced_only_in_the_openrouter_channel_module():
    referencing = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if re.search(r"\bChatOpenAI\b", path.read_text())
    )

    assert referencing == ["llm/openrouter.py"]


def test_the_openrouter_key_env_is_read_only_in_the_openrouter_channel_module():
    readers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if re.search(r"\b(?:po|architect)_llm_api_key\b|_LLM_API_KEY\b", path.read_text())
    )

    # settings.py declares the fields; agent_llm_env.py names them for .env.example.
    assert readers == ["config/agent_llm_env.py", "config/settings.py", "llm/openrouter.py"]
    assert "AGENT_LLM_ENV" in (SRC / "llm/openrouter.py").read_text()
