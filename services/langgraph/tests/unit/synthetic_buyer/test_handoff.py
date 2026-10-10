"""Buyer Telegram use ends at confirmation and returns only after terminal QA."""

import pytest

from src.synthetic_buyer.persona import PersonaDecision, PersonaTurn
from src.synthetic_buyer.telegram import Button, TransportError
from tests.unit.synthetic_buyer.fakes import CODEGEN, PRODUCT, PROJECT, harness


async def test_delayed_story_after_confirmation_is_waited_for_without_telegram(tmp_path):
    h = harness(tmp_path)
    observe = h.api.stories
    delayed = 0
    stopped_at = None

    async def stories(project_id):
        nonlocal delayed, stopped_at
        if h.world.stage == "ordered":
            if stopped_at is None:
                stopped_at = len(h.telegram.events)
            delayed += 1
            if delayed <= 2:
                return []
            assert h.telegram.events[stopped_at:] == []
            assert not h.telegram.connected
        return await observe(project_id)

    h.api.stories = stories

    outcome = await h.buyer().run()

    assert outcome.exit_code == 0, h.store.record["verdict"]
    assert delayed >= 3
    assert h.world.connects_while_qa_busy == 0
    assert h.world.reads_while_qa_busy == 0
    assert h.world.sends_while_qa_busy == []


@pytest.mark.parametrize("outcome", ["failed", "blocked", "skipped"])
async def test_terminal_qa_without_pass_cannot_reconnect_for_probes(tmp_path, outcome):
    h = harness(tmp_path)
    runs = h.api.runs

    async def failed_qa(**params):
        found = await runs(**params)
        for run in found:
            if run["type"] == "qa":
                run["result"] = {
                    "qa_outcome": outcome,
                    "deployed_url": "https://product.example.test",
                }
        return found

    h.api.runs = failed_qa
    result = await h.buyer().run()

    assert result.verdict == "failed"
    assert result.exit_code == 1
    assert not h.telegram.connected
    assert [e for e in h.telegram.events if e[:2] == ("send", PRODUCT.username)] == []


async def test_unknown_confirmation_stops_once_and_retains_ownership(tmp_path):
    h = harness(tmp_path)
    confirm = "Да, всё верно."
    h.telegram.unconfirmed.add(confirm)

    result = await h.buyer().run()

    assert (result.verdict, result.cleanup) == ("failed", "refused")
    assert h.store.record["verdict"]["reason"] == "delivery_unknown"
    assert h.store.record["ownership"]["project_id"] == PROJECT
    assert h.store.record["pending"]["kind"] == "persona"
    assert len([e for e in h.telegram.events if e == ("send", CODEGEN.username, confirm)]) == 1
    assert "request_teardown" not in h.api.calls
    before = h.store.path.read_text()
    calls = list(h.api.calls)
    events = list(h.telegram.events)

    repeated = await h.buyer().run()

    assert repeated.exit_code == 1
    assert h.store.path.read_text() == before
    assert h.api.calls == calls
    assert h.telegram.events == events


@pytest.mark.parametrize("proven", [True, False])
async def test_visible_brief_confirmation_button_requires_a_receipt(tmp_path, proven):
    h = harness(tmp_path)
    label = "Подтвердить"
    h.persona.overrides["Описание заказа"] = PersonaTurn(
        decision=PersonaDecision.PRESS, button=label
    )
    append = h.world.append
    press = h.telegram.press

    def with_button(peer, text, **kwargs):
        if text.startswith("Описание заказа"):
            kwargs["buttons"] = (Button(label, data=b"confirm"),)
        return append(peer, text, **kwargs)

    async def confirm(peer, message_id, data):
        await press(peer, message_id, data)
        h.world.codegen_answer("Да, всё верно.")
        if not proven:
            raise TransportError("press", "unknown receipt")

    h.world.append = with_button
    h.telegram.press = confirm
    result = await h.buyer().run()

    assert len([e for e in h.telegram.events if e[0] == "press"]) == 1
    if proven:
        assert result.exit_code == 0, h.store.record["verdict"]
    else:
        assert (result.verdict, result.cleanup) == ("failed", "refused")
        assert h.store.record["pending"]["effect"] == "press"
        assert "request_teardown" not in h.api.calls
