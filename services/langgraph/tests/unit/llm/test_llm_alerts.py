"""Operator alerts of the LLM channels and the PO's degraded-mode note.

No network: the CLIs are fakes on PATH, OpenRouter is a fake chat model, Redis
is fakeredis (two clients on one server stand for the langgraph and architect
processes), the balance endpoint is an `httpx.MockTransport`, and the admin
delivery is a recording stand-in for `deliver_to_admins`.
"""

from __future__ import annotations

import asyncio
import json

from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
import httpx
from langchain_core.messages import HumanMessage, SystemMessage
import openai
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.llm_channel import LLMChannel, LLMChannelConfig
from shared.notifications import AdminDeliveryResult
from src.llm import (
    PO_SUBSCRIPTIONS_DOWN_NOTE,
    LLMAgent,
    LLMAlerts,
    alerts as alerts_module,
    build_agent_llm,
)
from src.llm.alerts import AlertOutcome, LLMAlertKind, alert_key
from src.llm.openrouter import check_openrouter_balance, run_openrouter_balance_check
from tests.unit.llm.conftest import OPENROUTER_KEY

DEFAULT = ("codex", "claude", "openrouter")
SIX_HOURS = 6 * 3600
PAYMENT_402 = {"exit": 1, "stderr": "ERROR: unexpected status 402 Payment Required"}
UNAUTHORIZED_401 = {"exit": 1, "stderr": "ERROR: unexpected status 401 Unauthorized"}


def _chain(*names: str) -> list[LLMChannelConfig]:
    return [LLMChannelConfig(channel=LLMChannel(name)) for name in names]


def _status_error(status: int, message: str = "Payment Required") -> openai.APIStatusError:
    request = httpx.Request("POST", "https://openrouter.test/api/v1/chat/completions")
    return openai.APIStatusError(
        message, response=httpx.Response(status, request=request), body=None
    )


class _Config:
    """Stands in for `ConfigStore.get(key, default)`."""

    def __init__(self, values: dict | None = None, error: Exception | None = None):
        self.values = values or {}
        self.error = error

    def get(self, key, default):
        if self.error is not None:
            raise self.error
        return self.values.get(key, default)


class _Admins:
    """Stands in for `deliver_to_admins`: records every message, reports a delivery."""

    def __init__(self, succeeded: int = 1, error: Exception | None = None):
        self.succeeded = succeeded
        self.error = error
        self.messages: list[tuple[str, str]] = []

    async def __call__(self, message: str, level: str = "info") -> AdminDeliveryResult:
        self.messages.append((message, level))
        if self.error is not None:
            raise self.error
        return AdminDeliveryResult(configured=1, succeeded=self.succeeded)


class _BrokenRedis:
    async def exists(self, *_):
        raise ConnectionError("redis is down")

    async def set(self, *_, **__):
        raise ConnectionError("redis is down")

    async def delete(self, *_):
        raise ConnectionError("redis is down")


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def redis(server):
    return FakeRedis(server=server)


def _alerts(redis, admins: _Admins, config: _Config | None = None) -> LLMAlerts:
    store = config or _Config()
    return LLMAlerts(lambda: redis, lambda: store, deliver=admins)


async def _ask(llm, text: str = "I want a bot that tracks my expenses.") -> object:
    answer = await llm.ainvoke([HumanMessage(content=text)])
    await LLMAlerts.drain()
    return answer


def _stdin(cli) -> str:
    return "".join(call["stdin"] for call in cli.calls)


# --- 402 on any channel --------------------------------------------------------


class TestPaymentRequiredAlert:
    @pytest.mark.parametrize(
        ("failing", "chain"),
        [
            ("codex", ("codex", "claude")),
            ("claude", ("claude", "openrouter")),
            ("openrouter", ("openrouter", "codex")),
        ],
    )
    async def test_a_402_alerts_once_per_channel_within_the_window(
        self, channels, redis, failing, chain
    ):
        if failing == "openrouter":
            channels.openrouter.outcomes = [_status_error(402)]
        else:
            getattr(channels, failing).script(PAYMENT_402)
        admins = _Admins()
        llm = build_agent_llm(
            LLMAgent.ARCHITECT, _chain(*chain), channels.settings(), alerts=_alerts(redis, admins)
        )

        with capture_logs() as logs:
            first = await _ask(llm)
            second = await _ask(llm)

        assert first.response_metadata["llm_channel"] == chain[1]
        assert second.response_metadata["llm_channel"] == chain[1]
        [(message, level)] = admins.messages
        assert f"LLM channel {failing} refused architect (payment_required)" in message
        assert "402" in message
        assert level == "error"
        key = alert_key(LLMAlertKind.PAYMENT_REQUIRED, failing)
        assert 0 < await redis.ttl(key) <= SIX_HOURS
        [sent] = [log for log in logs if log["event"] == "llm_alert_sent"]
        assert (sent["kind"], sent["subject"], sent["agent"]) == (
            "payment_required",
            failing,
            "architect",
        )

    async def test_the_reason_is_redacted_and_bounded(self, channels, redis):
        channels.openrouter.outcomes = [
            _status_error(402, f"Payment Required for {OPENROUTER_KEY} " + "x" * 1000)
        ]
        admins = _Admins()
        llm = build_agent_llm(
            LLMAgent.PO,
            _chain("openrouter", "codex"),
            channels.settings(),
            alerts=_alerts(redis, admins),
        )

        await _ask(llm)

        [(message, _)] = admins.messages
        assert OPENROUTER_KEY not in message
        assert len(message) < 500  # noqa: PLR2004

    async def test_other_failures_are_not_alerted(self, channels, redis):
        channels.codex.script(UNAUTHORIZED_401)
        admins = _Admins()
        llm = build_agent_llm(
            LLMAgent.ARCHITECT, _chain(*DEFAULT), channels.settings(), alerts=_alerts(redis, admins)
        )

        answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "claude"
        assert admins.messages == []

    async def test_a_failed_send_does_not_set_the_key(self, channels, redis):
        channels.codex.script(PAYMENT_402)
        admins = _Admins(succeeded=0)
        llm = build_agent_llm(
            LLMAgent.ARCHITECT,
            _chain("codex", "claude"),
            channels.settings(),
            alerts=_alerts(redis, admins),
        )

        with capture_logs() as logs:
            await _ask(llm)
        assert not await redis.exists(alert_key(LLMAlertKind.PAYMENT_REQUIRED, "codex"))
        assert [log["delivery"] for log in logs if log["event"] == "llm_alert_failed"] == ["failed"]

        admins.succeeded = 1
        await _ask(llm)
        assert len(admins.messages) == 2  # noqa: PLR2004 - the lost alert is sent again
        assert await redis.exists(alert_key(LLMAlertKind.PAYMENT_REQUIRED, "codex"))

    async def test_a_raised_send_does_not_set_the_key(self, channels, redis):
        channels.codex.script(PAYMENT_402)
        admins = _Admins(error=RuntimeError("users API returned HTTP 503"))
        llm = build_agent_llm(
            LLMAgent.ARCHITECT,
            _chain("codex", "claude"),
            channels.settings(),
            alerts=_alerts(redis, admins),
        )

        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.content == "answer from claude"
        assert not await redis.exists(alert_key(LLMAlertKind.PAYMENT_REQUIRED, "codex"))
        [failed] = [log for log in logs if log["event"] == "llm_alert_failed"]
        assert failed["error_type"] == "RuntimeError"

    async def test_dedup_holds_across_processes(self, channels, server):
        channels.codex.script(PAYMENT_402)
        admins = _Admins()
        architect_process = build_agent_llm(
            LLMAgent.ARCHITECT,
            _chain("codex", "claude"),
            channels.settings(),
            alerts=_alerts(FakeRedis(server=server), admins),
        )
        langgraph_process = build_agent_llm(
            LLMAgent.PO,
            _chain("codex", "claude"),
            channels.settings(),
            alerts=_alerts(FakeRedis(server=server), admins),
        )

        await _ask(architect_process)
        await _ask(langgraph_process)

        [(message, _)] = admins.messages
        assert "refused architect" in message

    async def test_the_window_comes_from_system_config(self, channels, redis):
        channels.codex.script(PAYMENT_402)
        config = _Config({"llm.alert_realert_window_hours": 1})
        llm = build_agent_llm(
            LLMAgent.ARCHITECT,
            _chain("codex", "claude"),
            channels.settings(),
            alerts=_alerts(redis, _Admins(), config),
        )

        await _ask(llm)

        assert 0 < await redis.ttl(alert_key(LLMAlertKind.PAYMENT_REQUIRED, "codex")) <= 3600  # noqa: PLR2004

    @pytest.mark.parametrize(
        ("config", "event"),
        [
            (_Config(), "llm_alert_config_missing"),
            (_Config({"llm.alert_realert_window_hours": "soon"}), "llm_alert_config_invalid"),
            (_Config(error=RuntimeError("config API down")), "llm_alert_config_unreadable"),
        ],
    )
    async def test_a_missing_or_bad_window_falls_back_to_six_hours(
        self, channels, redis, config, event
    ):
        channels.codex.script(PAYMENT_402)
        llm = build_agent_llm(
            LLMAgent.ARCHITECT,
            _chain("codex", "claude"),
            channels.settings(),
            alerts=_alerts(redis, _Admins(), config),
        )

        with capture_logs() as logs:
            await _ask(llm)

        ttl = await redis.ttl(alert_key(LLMAlertKind.PAYMENT_REQUIRED, "codex"))
        assert SIX_HOURS - 60 < ttl <= SIX_HOURS
        [warning] = [log for log in logs if log["event"] == event]
        assert warning["default"] == 6.0  # noqa: PLR2004


# --- both subscriptions down ---------------------------------------------------


def _subscriptions_down(channels) -> None:
    channels.codex.script(UNAUTHORIZED_401)


class TestSubscriptionsDown:
    async def test_po_on_openrouter_gets_the_note_and_the_operator_one_alert(self, channels, redis):
        _subscriptions_down(channels)
        admins = _Admins()
        # As on prod: the claude channel has no token at all.
        settings = channels.settings(claude_code_oauth_token=None)
        llm = build_agent_llm(
            LLMAgent.PO, _chain(*DEFAULT), settings, alerts=_alerts(redis, admins)
        )

        with capture_logs() as logs:
            first = await _ask(llm)
            second = await _ask(llm)

        assert first.response_metadata["llm_channel"] == "openrouter"
        assert second.response_metadata["llm_channel"] == "openrouter"
        for sent in channels.openrouter.seen:
            notes = [m for m in sent if isinstance(m, SystemMessage)]
            assert [note.content for note in notes] == [PO_SUBSCRIPTIONS_DOWN_NOTE]
            assert sent[-1].content == PO_SUBSCRIPTIONS_DOWN_NOTE
            assert isinstance(sent[0], HumanMessage)
        assert PO_SUBSCRIPTIONS_DOWN_NOTE not in _stdin(channels.codex)
        [(message, level)] = admins.messages
        assert message.startswith("Subscription channels down, po running on OpenRouter")
        assert "codex=unauthorized" in message
        assert "claude=missing_credential" in message
        assert level == "error"
        assert await redis.exists(alert_key(LLMAlertKind.SUBSCRIPTIONS_DOWN, "po"))
        assert len([log for log in logs if log["event"] == "llm_degraded_note_added"]) == 2  # noqa: PLR2004

    def test_the_note_says_what_the_card_requires(self):
        note = PO_SUBSCRIPTIONS_DOWN_NOTE.lower()
        assert "answer the user normally" in note
        assert "collecting their requirements" in note
        assert "once" in note
        assert "engineering capacity is temporarily unavailable" in note
        assert "timeline" in note

    async def test_no_note_and_no_alert_when_codex_answers(self, channels, redis):
        admins = _Admins()
        llm = build_agent_llm(
            LLMAgent.PO, _chain(*DEFAULT), channels.settings(), alerts=_alerts(redis, admins)
        )

        answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "codex"
        assert PO_SUBSCRIPTIONS_DOWN_NOTE not in _stdin(channels.codex)
        assert admins.messages == []

    async def test_no_note_and_no_alert_when_claude_answers(self, channels, redis):
        _subscriptions_down(channels)
        admins = _Admins()
        llm = build_agent_llm(
            LLMAgent.PO, _chain(*DEFAULT), channels.settings(), alerts=_alerts(redis, admins)
        )

        answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "claude"
        assert PO_SUBSCRIPTIONS_DOWN_NOTE not in _stdin(channels.claude)
        assert channels.openrouter.seen == []
        assert admins.messages == []

    async def test_no_note_when_openrouter_answers_without_both_subscriptions_failing(
        self, channels, redis
    ):
        _subscriptions_down(channels)
        admins = _Admins()
        llm = build_agent_llm(
            LLMAgent.PO,
            _chain("codex", "openrouter"),
            channels.settings(),
            alerts=_alerts(redis, admins),
        )

        answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "openrouter"
        [sent] = channels.openrouter.seen
        assert not any(isinstance(message, SystemMessage) for message in sent)
        assert admins.messages == []

    @pytest.mark.parametrize("agent", [LLMAgent.PO_SUMMARIZER, LLMAgent.ARCHITECT])
    async def test_the_summarizer_and_the_architect_get_the_alert_but_no_note(
        self, channels, redis, agent
    ):
        _subscriptions_down(channels)
        admins = _Admins()
        settings = channels.settings(claude_code_oauth_token=None)
        llm = build_agent_llm(agent, _chain(*DEFAULT), settings, alerts=_alerts(redis, admins))

        answer = await _ask(llm)

        assert answer.response_metadata["llm_channel"] == "openrouter"
        [sent] = channels.openrouter.seen
        assert not any(isinstance(message, SystemMessage) for message in sent)
        [(message, _)] = admins.messages
        assert message.startswith(f"Subscription channels down, {agent.value} running on")
        assert await redis.exists(alert_key(LLMAlertKind.SUBSCRIPTIONS_DOWN, agent.value))


# --- alerting never fails the model call -----------------------------------------


class TestAlertingNeverFailsTheCall:
    async def test_a_dead_redis_and_a_raising_delivery_leave_the_answer_alone(self, channels):
        channels.codex.script(PAYMENT_402)
        admins = _Admins(error=RuntimeError("TELEGRAM_BOT_TOKEN is not set"))
        llm = build_agent_llm(
            LLMAgent.PO,
            _chain(*DEFAULT),
            channels.settings(claude_code_oauth_token=None),
            alerts=_alerts(_BrokenRedis(), admins),
        )

        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.content == "answer from openrouter"
        events = [log["event"] for log in logs]
        assert "llm_alert_dedup_unreadable" in events
        assert events.count("llm_alert_failed") == 2  # noqa: PLR2004 - the 402 and the degraded alert

    async def test_a_dead_redis_still_sends_and_never_raises(self, channels):
        channels.codex.script(PAYMENT_402)
        admins = _Admins()
        llm = build_agent_llm(
            LLMAgent.ARCHITECT,
            _chain("codex", "claude"),
            channels.settings(),
            alerts=_alerts(_BrokenRedis(), admins),
        )

        with capture_logs() as logs:
            answer = await _ask(llm)

        assert answer.content == "answer from claude"
        assert len(admins.messages) == 1
        assert "llm_alert_dedup_unrecorded" in [log["event"] for log in logs]

    async def test_a_hanging_delivery_does_not_delay_the_answer(self, channels, redis, monkeypatch):
        monkeypatch.setattr(alerts_module, "ALERT_DEADLINE_SECONDS", 0.2)
        channels.codex.script(PAYMENT_402)
        never = asyncio.Event()

        async def hanging(message, level="info"):
            await never.wait()

        llm = build_agent_llm(
            LLMAgent.ARCHITECT,
            _chain("codex", "claude"),
            channels.settings(),
            alerts=LLMAlerts(lambda: redis, _Config, deliver=hanging),
        )

        with capture_logs() as logs:
            answer = await llm.ainvoke([HumanMessage(content="hello")])
            await LLMAlerts.drain()

        assert answer.content == "answer from claude"

        [failed] = [log for log in logs if log["event"] == "llm_alert_failed"]
        assert failed["error_type"] == "TimeoutError"
        assert not await redis.exists(alert_key(LLMAlertKind.PAYMENT_REQUIRED, "codex"))


# --- the scheduled balance check -------------------------------------------------

BASE_URL = "https://openrouter.test/api/v1"


class _Credits:
    """The credits endpoint: answers with the scripted balance or status."""

    def __init__(self, credits: float = 100.0, usage: float = 90.55, status: int = 200):
        self.credits = credits
        self.usage = usage
        self.status = status
        self.error: Exception | None = None
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.status != 200:  # noqa: PLR2004
            return httpx.Response(self.status, text=f"Payment Required, key {OPENROUTER_KEY}")
        body = {"data": {"total_credits": self.credits, "total_usage": self.usage}}
        return httpx.Response(200, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def _check(credits: _Credits, alerts: LLMAlerts) -> list[dict]:
    async with credits.client() as client:
        with capture_logs() as logs:
            await check_openrouter_balance(alerts, client, BASE_URL, OPENROUTER_KEY)
    return logs


def _balance_log(logs: list[dict]) -> dict:
    [record] = [log for log in logs if log["event"] == "openrouter_balance"]
    return record


class TestBalanceCheck:
    async def test_it_reads_the_credits_endpoint_with_the_key(self, redis):
        credits = _Credits(credits=50, usage=10)
        logs = await _check(credits, _alerts(redis, _Admins()))

        [request] = credits.requests
        assert str(request.url) == f"{BASE_URL}/credits"
        assert request.headers["Authorization"] == f"Bearer {OPENROUTER_KEY}"
        assert OPENROUTER_KEY not in json.dumps(logs)

    async def test_above_the_threshold_no_alert(self, redis):
        admins = _Admins()
        logs = await _check(_Credits(credits=50, usage=10), _alerts(redis, admins))

        record = _balance_log(logs)
        assert (record["balance_usd"], record["threshold_usd"], record["alert_sent"]) == (
            40.0,
            20.0,
            False,
        )
        assert admins.messages == []

    async def test_below_alerts_once_and_recovery_rearms(self, redis):
        admins = _Admins()
        alerts = _alerts(redis, admins)
        low = _Credits(credits=100, usage=90.55)

        first = _balance_log(await _check(low, alerts))
        second = _balance_log(await _check(low, alerts))

        assert (first["balance_usd"], first["alert_sent"]) == (9.45, True)
        assert (second["alert_sent"], second["alert_outcome"]) == (False, "deduplicated")
        [(message, level)] = admins.messages
        assert "OpenRouter balance is 9.45 USD, below the 20.00 USD" in message
        assert level == "warning"

        logs = await _check(_Credits(credits=100, usage=50), alerts)
        assert "llm_alert_rearmed" in [log["event"] for log in logs]
        assert not await redis.exists(alert_key(LLMAlertKind.OPENROUTER_LOW_BALANCE, "openrouter"))

        again = _balance_log(await _check(low, alerts))
        assert again["alert_sent"] is True
        assert len(admins.messages) == 2  # noqa: PLR2004

    async def test_the_threshold_comes_from_system_config(self, redis):
        admins = _Admins()
        config = _Config({"llm.openrouter_balance_alert_usd": 5})
        record = _balance_log(await _check(_Credits(100, 90.55), _alerts(redis, admins, config)))

        assert (record["threshold_usd"], record["alert_sent"]) == (5.0, False)
        assert admins.messages == []

    async def test_a_failed_read_is_logged_and_not_alerted(self, redis):
        admins = _Admins()
        broken = _Credits(status=500)
        logs = await _check(broken, _alerts(redis, admins))
        down = _Credits()
        down.error = httpx.ConnectError("no route to host")
        logs += await _check(down, _alerts(redis, admins))
        shape = _Credits()
        shape.credits = None  # type: ignore[assignment]
        logs += await _check(shape, _alerts(redis, admins))

        failed = [log for log in logs if log["event"] == "openrouter_balance_read_failed"]
        assert [log["failure_class"] for log in failed] == [None, None, None]
        assert failed[1]["reason"] == "request failed: ConnectError"
        assert failed[2]["reason"] == "unexpected response shape"
        assert OPENROUTER_KEY not in json.dumps(failed)
        assert admins.messages == []
        assert not [log for log in logs if log["event"] == "openrouter_balance"]

    @pytest.mark.parametrize(
        ("status", "kind"),
        [(402, "payment_required"), (401, "unauthorized"), (403, "forbidden")],
    )
    async def test_a_refused_read_is_alerted_like_a_refused_channel(self, redis, status, kind):
        admins = _Admins()
        alerts = _alerts(redis, admins)

        logs = await _check(_Credits(status=status), alerts)
        await _check(_Credits(status=status), alerts)

        [failed, *_] = [log for log in logs if log["event"] == "openrouter_balance_read_failed"]
        assert (failed["failure_class"], failed["alert_outcome"]) == (kind, "sent")
        [(message, _)] = admins.messages
        assert f"LLM channel openrouter refused openrouter_balance_check ({kind})" in message
        assert OPENROUTER_KEY not in message
        assert await redis.exists(alert_key(LLMAlertKind(kind), "openrouter"))

    async def test_a_402_on_the_read_shares_the_channel_402_dedup(self, redis):
        admins = _Admins()
        alerts = _alerts(redis, admins)
        outcome = await alerts.provider_refused(
            LLMChannel.OPENROUTER,
            "po",
            alerts_module.ChannelFailureClass.PAYMENT_REQUIRED,
            "HTTP 402: Payment Required",
        )
        assert outcome is AlertOutcome.SENT

        logs = await _check(_Credits(status=402), alerts)

        [failed] = [log for log in logs if log["event"] == "openrouter_balance_read_failed"]
        assert failed["alert_outcome"] == "deduplicated"
        assert len(admins.messages) == 1

    async def test_no_key_logs_once_and_stays_idle(self, redis, channels):
        admins = _Admins()
        settings = channels.settings(po_llm_api_key=None)

        with capture_logs() as logs:
            await asyncio.wait_for(
                run_openrouter_balance_check(settings, _alerts(redis, admins)), timeout=1
            )

        [idle] = [log for log in logs if log["event"] == "openrouter_balance_check_idle"]
        assert idle["missing_env"] == ["PO_LLM_API_KEY"]
        assert admins.messages == []
