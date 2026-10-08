"""Scripted transport proves chat correlation and timer waits without wall-clock sleeps."""

from types import SimpleNamespace

import pytest

from src.consumers.stand_conversation import ConversationFailure, run_steps


@pytest.mark.parametrize(
    "contour,criteria",
    [
        ("production", "- GET /health returns 200\n- Stand conversation: platform-module"),
        ("stand", "Stand conversation: ../outside"),
        ("stand", "Stand conversation: platform-module\nStand conversation: another"),
        ("stand", "Stand conversation: platform-module\nStand mechanical notes: marker"),
    ],
)
def test_selection_refuses_foreign_contour_paths_and_multiple_probes(
    monkeypatch, contour, criteria
):
    from src.consumers.stand_conversation import selection

    monkeypatch.setenv("LIVE_CONTOUR", contour)
    with pytest.raises(ConversationFailure, match="selection"):
        selection(criteria)


def test_qa_routes_conversation_to_health_and_native_grant(monkeypatch):
    from src.consumers.qa import _qa_criteria

    monkeypatch.setenv("LIVE_CONTOUR", "stand")
    selected, checks = _qa_criteria(
        "- GET /health returns 200\n- Stand conversation: platform-module"
    )
    assert selected[:2] == ("conversation", "platform-module")
    assert checks is not None


class Transport:
    def __init__(self):
        self.now = 0
        self.messages = []
        self.sends = []
        self.clicks = []
        self.bot = SimpleNamespace(id=42)

    def message(self, text, *, out=False, buttons=None, peer=42):
        item = SimpleNamespace(
            id=len(self.messages) + 1,
            raw_text=text,
            out=out,
            peer_id=SimpleNamespace(user_id=peer),
            buttons=buttons,
        )
        self.messages.append(item)
        return item

    async def send_message(self, bot, text):
        self.sends.append(text)
        sent = self.message(text, out=True)
        self.message("added")
        return sent

    async def get_messages(self, bot, **kwargs):
        if "ids" in kwargs:
            return self.messages[kwargs["ids"] - 1]
        return list(reversed(self.messages))

    async def sleep(self, seconds):
        self.now += seconds
        if self.now == 75:
            self.message("delivery channel body link")


async def test_late_unsolicited_delivery_needs_no_send_and_ignores_old_messages():
    client = Transport()
    client.message("delivery channel body link")
    evidence = {}
    await run_steps(
        client,
        client.bot,
        [
            {"action": "send", "text": "/add", "expect": {"exact": "added"}},
            {
                "action": "wait",
                "expect": {"prefix": "delivery", "contains": ["channel", "body", "link"]},
                "timeout": 150,
            },
        ],
        evidence=evidence,
        clock=lambda: client.now,
        sleep=client.sleep,
    )
    assert client.sends == ["/add"]
    assert evidence["steps"][1]["message"]["id"] == 4
    assert evidence["steps"][1]["elapsed_seconds"] == 75


async def test_unsolicited_timeout_is_a_failure():
    client = Transport()
    with pytest.raises(ConversationFailure, match="deadline"):
        await run_steps(
            client,
            client.bot,
            [
                {"action": "wait", "expect": {"exact": "missing"}, "timeout": 3},
            ],
            evidence={},
            clock=lambda: client.now,
            sleep=client.sleep,
        )
    assert not client.sends


async def test_visible_button_can_edit_the_observed_reply():
    client = Transport()

    async def click():
        client.clicks.append("Remove")
        reply.raw_text = "removed"

    reply = None

    async def send(bot, text):
        nonlocal reply
        sent = client.message(text, out=True)
        reply = client.message("listed", buttons=[[SimpleNamespace(text="Remove", click=click)]])
        return sent

    client.send_message = send
    evidence = {}
    await run_steps(
        client,
        client.bot,
        [
            {"action": "send", "text": "/list", "expect": {"exact": "listed"}},
            {
                "action": "press",
                "message_step": 0,
                "button": "Remove",
                "expect": {"exact": "removed"},
            },
        ],
        evidence=evidence,
        clock=lambda: client.now,
        sleep=client.sleep,
    )
    assert client.clicks == ["Remove"]
    assert evidence["steps"][-1]["message"]["text"] == "removed"


async def test_delivery_received_during_a_reply_stays_eligible_for_passive_wait():
    client = Transport()

    async def send(bot, text):
        sent = client.message(text, out=True)
        client.message("delivery channel body link")
        client.message("digest")
        return sent

    client.send_message = send
    evidence = {}
    await run_steps(
        client,
        client.bot,
        [
            {"action": "send", "text": "/request", "expect": {"exact": "digest"}},
            {"action": "wait", "expect": {"prefix": "delivery"}, "timeout": 150},
        ],
        evidence=evidence,
        clock=lambda: client.now,
        sleep=client.sleep,
    )
    assert evidence["steps"][1]["message"]["id"] == 2
    assert client.now == 0


async def test_incoming_from_another_chat_cannot_prove_acceptance():
    client = Transport()

    async def sleep(seconds):
        client.now += seconds
        client.message("delivered", peer=99)

    with pytest.raises(ConversationFailure, match="another chat"):
        await run_steps(
            client,
            client.bot,
            [
                {"action": "wait", "expect": {"exact": "delivered"}, "timeout": 3},
            ],
            evidence={},
            clock=lambda: client.now,
            sleep=sleep,
        )
