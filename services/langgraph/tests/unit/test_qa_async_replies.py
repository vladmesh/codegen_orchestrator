"""Async Telegram replies and runner-owned non-product verdicts."""

from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.dto.product_brief import ProductBriefContent
from shared.contracts.dto.run_result import (
    QAFailedCheck,
    QAFailedCheckCause,
    QAOutcome,
    QARunResult,
    QATelegramProbeEvidence,
)
from shared.telegram_bot_probe import (
    build_bot_callback_script,
    build_bot_message_script,
    parse_bot_probe_result,
)
from src.agents.qa.tools import _TelegramCapability
from src.consumers._qa_runner import QAResult, parse_qa_result, settle_unverified_checks
from src.consumers._qa_workspace import QAWorkspace


@pytest.mark.parametrize("wait", [0, 61, -1, True, 1.5, "60"])
@pytest.mark.parametrize("action", ["message", "callback"])
async def test_wait_refusal_does_not_run_or_block(tmp_path, wait, action):
    workspace = QAWorkspace(tmp_path)
    runner = AsyncMock()
    telegram = _TelegramCapability(
        bot_username="test_bot",
        workspace=workspace,
        telethon_env={"session": "fixture"},
        probe_runner=runner,
    )
    if action == "message":
        answer = await telegram.telegram_probe("/start", wait_seconds=wait)
    else:
        answer = await telegram.telegram_click_button(7, "Y2FyZWVy", wait_seconds=wait)
    assert answer["refusal"]["reason"] == "invalid_wait"
    runner.assert_not_called()
    assert workspace.telegram_probe_blocker is None


@pytest.mark.parametrize("wait", [1, 15, 60])
@pytest.mark.parametrize("action", ["message", "callback"])
async def test_wait_derives_child_timeout(tmp_path, wait, action):
    runner = AsyncMock(
        return_value=SimpleNamespace(
            stdout="telegram_probe_result:"
            + json.dumps(
                {
                    "action": action,
                    "attempted": "fixture",
                    "sent": "/start",
                    "delivered": True,
                    "replies": [],
                }
            ),
            stderr="",
        )
    )
    telegram = _TelegramCapability(
        bot_username="test_bot",
        workspace=QAWorkspace(tmp_path),
        telethon_env={"session": "fixture"},
        probe_runner=runner,
    )
    if action == "message":
        await telegram.telegram_probe("/start", wait_seconds=wait)
    else:
        menu = SimpleNamespace(
            stdout="telegram_probe_result:"
            + json.dumps(
                {
                    "action": "message",
                    "attempted": "send /start",
                    "sent": "/start",
                    "delivered": True,
                    "replies": [
                        {
                            "id": 7,
                            "text": "Choose",
                            "reply_markup": {
                                "type": "ReplyInlineMarkup",
                                "buttons": [
                                    {
                                        "row": 0,
                                        "column": 0,
                                        "text": "Career",
                                        "type": "KeyboardButtonCallback",
                                        "callback_data": "Y2FyZWVy",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ),
            stderr="",
        )
        runner.side_effect = [menu, runner.return_value]
        await telegram.telegram_probe("/start")
        assert runner.call_args.kwargs["timeout"] == 45
        await telegram.telegram_click_button(7, "Y2FyZWVy", wait_seconds=wait)
    assert runner.call_args.kwargs["timeout"] == wait + 30


def _fake_bot(monkeypatch, action, wait):
    """Run the actual generated script on a virtual clock, with no process or sleep."""
    clock = [0]

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", advance)
    for key, value in {
        "TELETHON_SESSION": "fixture",
        "TELETHON_API_ID": "123",
        "TELETHON_API_HASH": "fixture",
    }.items():
        monkeypatch.setenv(key, value)

    def message(identifier, text, media=None, markup=None):
        return SimpleNamespace(
            id=identifier, raw_text=text, out=False, media=media, reply_markup=markup
        )

    button = SimpleNamespace(text="Career", data=b"career")
    original = message(
        7, "Choose", markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[button])])
    )
    progress = message(11, "Checking")
    photo = message(12, "The cards", type("MessageMediaPhoto", (), {})())
    final = message(13, "Your answer")

    class Client:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def disconnect(self):
            pass

        def get_entity(self, username):
            return username

        def send_message(self, bot, text):
            return SimpleNamespace(id=10)

        def __call__(self, request):
            return SimpleNamespace(message=None, alert=False, url=None)

        def get_messages(self, bot, *, ids=None, min_id=None, limit=None):
            if ids is not None:
                return original
            if limit == 1:
                return [original]
            return [final, photo, progress] if clock[0] >= 20 else [progress]

    modules = {
        "telethon.sync": {"TelegramClient": Client},
        "telethon.sessions": {"StringSession": lambda value: value},
        "telethon.tl.functions.messages": {"GetBotCallbackAnswerRequest": lambda **kwargs: kwargs},
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    script = (
        build_bot_message_script("test_bot", "/reading", wait_seconds=wait)
        if action == "message"
        else build_bot_callback_script(
            "test_bot", 7, "Y2FyZWVy", button_text="Career", wait_seconds=wait
        )
    )
    output = StringIO()
    with redirect_stdout(output):
        exec(script, {})  # noqa: S102 - exercise the generated child script
    return QATelegramProbeEvidence.model_validate(parse_bot_probe_result(output.getvalue()))


@pytest.mark.parametrize("action", ["message", "callback"])
def test_long_wait_collects_progress_delayed_photo_and_messages(monkeypatch, action):
    evidence = _fake_bot(monkeypatch, action, 60)
    assert [reply.id for reply in evidence.replies] == [11, 12, 13]
    assert evidence.replies[1].caption == "The cards"
    assert evidence.replies[2].text == "Your answer"


def _brief():
    return ProductBriefContent(
        summary="Bot readings",
        must_requirements=[{"id": "reading", "text": "Produce a reading"}],
        usage_examples=[
            {
                "requirement_id": "reading",
                "user_sends": "/reading",
                "product_answers": "Your answer",
            }
        ],
    )


def _failure(name, **fields):
    return QAResult(
        passed=False,
        checks=[
            {
                "name": name,
                "pass": False,
                "detail": f"{name} produced no final reply",
                "cause": "product",
                **fields,
            }
        ],
        summary="Failed",
    )


@pytest.mark.parametrize(
    "text,expected", [("/history", True), ("/reading", False), ("made up input", True)]
)
def test_only_brief_or_visible_input_can_fail_product(tmp_path, text, expected):
    workspace = QAWorkspace(tmp_path)
    workspace.record_telegram_probe(
        QATelegramProbeEvidence(
            action="message",
            attempted=f"send {text}",
            sent=text,
            delivered=True,
        )
    )
    result = settle_unverified_checks(
        _failure(text, telegram_step=1), workspace=workspace, brief=_brief()
    )
    assert result.passed is expected
    assert bool(result.unverified_checks) is expected


def test_visible_help_command_is_a_contract(tmp_path):
    workspace = QAWorkspace(tmp_path)
    workspace.record_telegram_probe(
        QATelegramProbeEvidence(
            action="message",
            attempted="send /start",
            sent="/start",
            delivered=True,
            replies=[{"id": 2, "text": "Commands: /history - list readings"}],
        )
    )
    workspace.record_telegram_probe(
        QATelegramProbeEvidence(
            action="message",
            attempted="send /history",
            sent="/history",
            delivered=True,
        )
    )
    result = settle_unverified_checks(
        _failure("/history", telegram_step=2), workspace=workspace, brief=_brief()
    )
    assert result.passed is False
    assert result.checks[0]["cause"] == "product"


@pytest.mark.parametrize("wait", [15, 60])
def test_callback_cutoff_with_server_sends_is_qa_tooling(monkeypatch, tmp_path, wait):
    workspace = QAWorkspace(tmp_path)
    evidence = _fake_bot(monkeypatch, "callback", wait)
    workspace.record_telegram_probe(evidence)
    workspace.record(
        "container_logs",
        "bot tail=200",
        json.dumps(
            {
                "method": "sendPhoto",
                "status_code": 200,
                "chat_id": QA_TEST_TELEGRAM_ID,
                "reply_to_message_id": 7,
                "message_id": 12,
            }
        ),
    )
    workspace.record_observation("container_logs", "bot")
    result = settle_unverified_checks(
        _failure("Career callback", telegram_step=1), workspace=workspace, brief=_brief()
    )
    assert result.passed is (wait == 15)
    if wait == 15:
        assert result.checks == []
        assert "QA tooling" in result.summary
        assert "QA tooling" in result.unverified_checks[0].reason


def test_unrelated_server_success_does_not_override_product(tmp_path):
    workspace = QAWorkspace(tmp_path)
    workspace.record_telegram_probe(
        QATelegramProbeEvidence(
            action="message",
            attempted="send /reading",
            sent="/reading",
            delivered=True,
            message_id=10,
        )
    )
    workspace.record(
        "container_logs",
        "bot",
        json.dumps(
            {
                "method": "sendPhoto",
                "status_code": 200,
                "chat_id": QA_TEST_TELEGRAM_ID + 1,
                "reply_to_message_id": 10,
                "message_id": 12,
            }
        ),
    )
    workspace.record_observation("container_logs", "bot")
    result = settle_unverified_checks(
        _failure("/reading", telegram_step=1), workspace=workspace, brief=_brief()
    )
    assert result.passed is False
    assert result.checks[0]["cause"] == "product"


def test_empty_probe_despite_server_send_survives_as_owner_verification_gap(tmp_path):
    workspace = QAWorkspace(tmp_path)
    workspace.record_telegram_probe(
        QATelegramProbeEvidence(
            action="message",
            attempted="send /reading",
            sent="/reading",
            delivered=True,
            message_id=10,
        )
    )
    workspace.record(
        "container_logs",
        "bot",
        json.dumps(
            {
                "method": "sendMessage",
                "status_code": 200,
                "chat_id": QA_TEST_TELEGRAM_ID,
                "reply_to_message_id": 10,
                "message_id": 12,
            }
        ),
    )
    workspace.record_observation("container_logs", "bot")
    result = settle_unverified_checks(
        _failure("/reading", telegram_step=1),
        workspace=workspace,
        brief=_brief(),
    )
    stored = QARunResult(
        qa_outcome=QAOutcome.PASSED,
        summary=result.summary,
        unverified_checks=result.unverified_checks,
    )
    assert stored.failed_checks == []
    assert "QA tooling" in stored.verification_facts("qa-fixture").unverified_checks[0].reason


@pytest.mark.parametrize("cause", ["product", "qa_capability", "qa_access", "qa_tooling"])
def test_cause_storage_roundtrip_and_step_verdict(cause):
    check = QAFailedCheck(name="check", detail="actual evidence", cause=cause)
    assert QAFailedCheck.model_validate_json(check.model_dump_json()).cause.value == cause
    verdict = parse_qa_result(
        json.dumps(
            {
                "pass": False,
                "summary": "fixture",
                "checks": [
                    {
                        "name": "check",
                        "pass": False,
                        "detail": "actual evidence",
                        "cause": cause,
                        "telegram_step": 1,
                    }
                ],
            }
        )
    )
    assert verdict.blocker is None
    if cause == "qa_tooling":
        settled = settle_unverified_checks(verdict)
        assert settled.passed is True
        assert "QA tooling" in settled.summary
    assert QAFailedCheck(name="legacy", detail="old record").cause is QAFailedCheckCause.PRODUCT


@pytest.mark.parametrize("step", [0, -1, True, 1.5, "1"])
def test_invalid_step_is_an_invalid_verdict(step):
    verdict = _failure("reading", telegram_step=step)
    result = parse_qa_result(
        json.dumps(
            {
                "pass": False,
                "summary": "fixture",
                "checks": verdict.checks,
            }
        )
    )
    assert result.blocker is not None


def test_http_failure_is_still_a_product_failure_in_a_bot_run(tmp_path):
    result = settle_unverified_checks(
        _failure("HTTP GET /health endpoint"),
        workspace=QAWorkspace(tmp_path),
        brief=_brief(),
    )
    assert result.checks[0]["cause"] == "product"
    assert result.passed is False


def test_omitting_the_input_from_the_verdict_does_not_hide_an_invented_command(tmp_path):
    workspace = QAWorkspace(tmp_path)
    workspace.record_telegram_probe(
        QATelegramProbeEvidence(
            action="message",
            attempted="send /history",
            sent="/history",
            delivered=True,
        )
    )
    result = settle_unverified_checks(
        _failure("Reading completed"),
        workspace=workspace,
        brief=_brief(),
    )
    assert result.passed is True
    assert result.checks == []
    assert "QA tooling" in result.summary


def test_ambiguous_inputs_without_a_step_are_unverified(tmp_path):
    workspace = QAWorkspace(tmp_path)
    for text in ["/reading", "/history"]:
        workspace.record_telegram_probe(
            QATelegramProbeEvidence(
                action="message",
                attempted=f"send {text}",
                sent=text,
                delivered=True,
            )
        )
    result = settle_unverified_checks(
        _failure("Reading completed"),
        workspace=workspace,
        brief=_brief(),
    )
    assert result.passed is True
    assert "uniquely recorded input" in result.unverified_checks[0].reason


def test_invented_command_cannot_borrow_another_steps_product_evidence(tmp_path):
    workspace = QAWorkspace(tmp_path)
    workspace.record_telegram_probe(
        QATelegramProbeEvidence(
            action="message",
            attempted="send /reading",
            sent="/reading",
            delivered=True,
        )
    )
    result = settle_unverified_checks(
        _failure("/history", telegram_step=1),
        workspace=workspace,
        brief=_brief(),
    )
    assert result.passed is True
    assert "recorded input" in result.unverified_checks[0].reason


@pytest.mark.parametrize("wait", [0, 61, True, 1.5])
@pytest.mark.parametrize("action", ["message", "callback"])
def test_script_builder_rejects_invalid_wait(wait, action):
    with pytest.raises(ValueError, match="wait_seconds"):
        if action == "message":
            build_bot_message_script("test_bot", "/reading", wait_seconds=wait)
        else:
            build_bot_callback_script(
                "test_bot", 7, "Y2FyZWVy", button_text="Career", wait_seconds=wait
            )
