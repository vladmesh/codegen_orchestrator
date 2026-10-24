"""Central QA of a confirmed order over the real capability endpoint.

These drive `run_qa_centrally` through the unit module's `central_run` harness:
fake SSH target and Telegram replies, but a real loopback HTTP capability
endpoint, so they run in this service shard rather than the host unit profile.
"""

from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.queues.qa import QAConfirmedOrder
from shared.tests.confirmed_order_cases import channels_brief
from tests.unit.test_qa_central_runtime import RUNTIME, TARGET, central_run as unit_central_run

pytestmark = pytest.mark.asyncio
central_run = unit_central_run


#: The repository keeps the scaffold's health check (`central_run` sends it); the
#: confirmed order says what to judge.
CHANNELS_ORDER = QAConfirmedOrder.from_brief(channels_brief())
ORDER_NAMES = [f"[{row.id}] {row.text}" for row in CHANNELS_ORDER.must_requirements]


def _channels_reply(text: str, reply: str | None):
    async def probe(script, *, env, timeout):
        return SimpleNamespace(
            stdout="telegram_probe_result:"
            + json.dumps(
                {
                    "action": "message",
                    "attempted": f"send {text}",
                    "sent": text,
                    "delivered": True,
                    "message_id": 10,
                    "replies": [{"id": 11, "text": reply}] if reply is not None else [],
                }
            ),
            stderr="",
            exit_status=0,
        )

    return probe


async def _channels_run(central_run, checks: list[dict], *, reply: str | None):
    async def behaviour(harness):
        await harness.tools["telegram_probe"].ainvoke({"message": "/channels"})
        failed = any(check["pass"] is False and check["cause"] == "product" for check in checks)
        return json.dumps({"pass": not failed, "summary": "fixture", "checks": checks})

    runtime = replace(RUNTIME, telethon_env={"TELETHON_SESSION": "fixture"})
    with patch("src.agents.qa.tools.run_probe_script", _channels_reply("/channels", reply)):
        result, _, factory, _ = await central_run(
            behaviour=behaviour,
            runtime=runtime,
            order=CHANNELS_ORDER,
            target=replace(TARGET, bot_username="channels_bot"),
        )
    return result, factory


HEALTH_PASSED = {"name": "GET /health returns 200", "pass": True, "detail": "200"}


async def test_the_executor_is_handed_the_confirmed_order_beside_the_scaffold_criteria(
    central_run,
):
    _, factory = await _channels_run(central_run, [HEALTH_PASSED], reply="Список каналов")

    assert "GET /health returns 200" in factory.prompt
    for text in ("/channels", "/addchannel https://t.me/example", "Канал удалён", "/digest"):
        assert text in factory.prompt
    assert '"language":"ru"' in factory.prompt
    assert '"id":"r5","text":"The bot answers in Russian"' in factory.prompt


async def test_health_alone_never_passes_a_confirmed_order(central_run):
    result, _ = await _channels_run(central_run, [HEALTH_PASSED], reply="Список каналов")

    assert result.passed is False
    assert result.blocker.category is QABlockerCategory.ORDER_UNVERIFIED
    assert result.blocker.sent == "r1; r2; r3; r4; r5"
    assert [check.name for check in result.unverified_checks] == ORDER_NAMES
    assert {check.origin.value for check in result.unverified_checks} == {"executor"}
    # Nothing failed in the product: there is no product check to repair.
    assert all(check["pass"] for check in result.checks)


async def test_a_passing_brief_interaction_passes_and_keeps_the_rest_unverified(central_run):
    checks = [
        HEALTH_PASSED,
        {"name": "[r1] /channels lists channels", "pass": True, "detail": "Список каналов"},
        {"name": "[r5] answers in Russian", "pass": True, "detail": "Список каналов"},
    ]

    result, _ = await _channels_run(central_run, checks, reply="Список каналов")

    assert result.blocker is None
    assert result.passed is True
    assert [check.name for check in result.unverified_checks] == ORDER_NAMES[1:4]
    assert [evidence.sent for evidence in result.telegram_probe_evidence] == ["/channels"]


async def test_a_failing_brief_interaction_stays_a_product_failure(central_run):
    failed = {
        "name": "[r1] /channels lists channels",
        "pass": False,
        "detail": "/channels produced no reply",
        "cause": "product",
    }

    result, _ = await _channels_run(central_run, [HEALTH_PASSED, failed], reply=None)

    assert result.blocker is None
    assert result.passed is False
    assert failed in result.checks
    assert result.unverified_checks == []


async def test_requirements_qa_cannot_check_keep_the_executors_reason_once(central_run):
    checks = [
        HEALTH_PASSED,
        {
            "name": "[r1] /channels lists channels",
            "pass": False,
            "detail": "the bot lists no command QA can send",
            "cause": "qa_capability",
        },
    ]

    result, _ = await _channels_run(central_run, checks, reply=None)

    assert result.blocker.category is QABlockerCategory.ORDER_UNVERIFIED
    assert [(check.name, check.reason) for check in result.unverified_checks][0] == (
        "[r1] /channels lists channels",
        "the bot lists no command QA can send",
    )
    assert [check.name for check in result.unverified_checks][1:] == ORDER_NAMES[1:]
