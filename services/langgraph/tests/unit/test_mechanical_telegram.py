"""Fixed stand probe rejects evidence from the wrong chat, owner, clock or callback."""

from datetime import UTC, datetime, timedelta

import pytest

from src.consumers.mechanical_telegram import (
    ProbeFailure,
    check_cancel,
    check_delivery,
    check_receipt,
    selection,
)


def test_malformed_mechanical_selection_cannot_fall_through_to_a_model():
    with pytest.raises(ProbeFailure, match="selection"):
        selection("- GET /health returns 200\n- Stand mechanical reminders: bad marker")


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed_emission", [False, True])
async def test_fixed_conversation_observes_edited_callbacks_and_only_reads_http(  # noqa: C901, PLR0915
    monkeypatch, delayed_emission
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
    from src.consumers import mechanical_telegram as probe

    now = datetime.now(UTC)
    rows, notes, messages, commands, clicks = [], [], [], [], []
    bot = SimpleNamespace(id=123)
    sequence = 0

    def message(text, *, out=False, date=now, buttons=None):
        nonlocal sequence
        sequence += 1
        result = SimpleNamespace(
            id=sequence,
            raw_text=text,
            out=out,
            date=date,
            edit_date=None,
            peer_id=SimpleNamespace(user_id=bot.id),
            buttons=buttons,
        )
        messages.append(result)
        return result

    def make_row(text, seconds):
        item = {
            "id": f"reminder-{len(rows)}",
            "text": text,
            "user_ref": f"telegram:{QA_TEST_TELEGRAM_ID}",
            "state": "scheduled",
            "remind_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
        }
        rows.append(item)
        return item

    async def send(_, text):
        commands.append(text)
        sent = message(text, out=True, date=datetime.now(UTC))
        if text.startswith("/note "):
            notes.append(text.split(" ", 1)[1])
            message("Saved: " + notes[-1])
        elif text == "/notes":
            message("\n".join(notes))
        elif text == "/remind buy milk in 2 minutes":
            first = make_row("buy milk", 120)
            message(probe.receipt(first, probe.TIMEZONE))
            message(
                "Reminder: buy milk",
                date=datetime.fromisoformat(first["remind_at"]) + timedelta(seconds=10),
            )
        elif text.startswith("/remind "):
            choice = message(
                "Choose a time for your reminder:",
                buttons=[
                    [
                        SimpleNamespace(text=label)
                        for label in ("In 5 minutes", "In 1 hour", "Tomorrow at 09:00")
                    ]
                ],
            )

            async def preset(*, text):
                clicks.append(text)
                second = make_row("stand-cancel-mark", 300)
                choice.raw_text = probe.receipt(second, probe.TIMEZONE)
                choice.edit_date = datetime.now(UTC)

            choice.click = preset
        elif text == "/reminders":
            scheduled = [item for item in rows if item["state"] == "scheduled"]
            if not scheduled:
                message("You have no scheduled reminders.")
            else:
                selected = scheduled[0]
                listing = message(
                    "time: " + selected["text"], buttons=[[SimpleNamespace(text="Cancel")]]
                )

                async def cancel(*, text):
                    clicks.append(text)
                    selected["state"] = "cancelled"
                    listing.raw_text = "Cancelled: " + selected["text"]
                    listing.edit_date = datetime.now(UTC)

                listing.click = cancel
        return sent

    async def get(_, *, min_id=None, limit=None, ids=None):
        if ids is not None:
            return next(item for item in messages if item.id == ids)
        inbound = [item for item in messages if item.id > min_id]
        if any(item.raw_text == "Reminder: buy milk" for item in inbound):
            rows[0]["state"] = "due" if delayed_emission else "emitted"
        return list(reversed(inbound))

    async def read(path, *, headers):
        assert path == "/reminders"
        assert headers == {"identity": "runtime-only"}
        snapshot = [dict(item) for item in rows]
        if rows and rows[0]["state"] == "due":
            # Publication already reached the chat; confirmation commits after this read.
            rows[0]["state"] = "emitted"
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: snapshot)

    client = SimpleNamespace(send_message=send, get_messages=get)
    http = SimpleNamespace(get=AsyncMock(side_effect=read))
    # The first get returns confirmation without the later due message.
    first_read = True

    async def delayed_due(*args, **kwargs):
        nonlocal first_read
        values = await get(*args, **kwargs)
        if first_read and any(item.raw_text.startswith("Scheduled for ") for item in values):
            first_read = False
            rows[0]["state"] = "scheduled"
            return [item for item in values if item.raw_text != "Reminder: buy milk"]
        return values

    client.get_messages = delayed_due
    notes.append("stand-note-mark")
    evidence = {}
    await probe.run_conversation(
        client,
        bot,
        http,
        mode="reminders",
        marker="mark",
        headers={"identity": "runtime-only"},
        evidence=evidence,
    )
    assert "/remind buy milk in 2 minutes" in commands
    assert clicks == ["In 5 minutes", "Cancel"]
    assert evidence["preset"]["confirmation"]["id"] == evidence["presets"]["message"]["id"]
    assert evidence["cancellation"]["reply"]["id"] == evidence["cancellation"]["selected"]["id"]
    assert evidence["cancellation"]["row"]["state"] == "cancelled"
    assert "stand-note-mark" in evidence["notes_after"]["text"]
    assert evidence["delivery"]["row"]["state"] == "emitted"
    if delayed_emission:
        observed = evidence["delivery"]["reconciliation_rows"]
        assert [snapshot[0]["state"] for snapshot in observed] == ["due", "emitted"]
        assert all(snapshot[0]["id"] == rows[0]["id"] for snapshot in observed)


def row():
    return {
        "id": "reminder-1",
        "text": "buy milk",
        "user_ref": "telegram:42",
        "remind_at": "2026-10-05T13:02:20+00:00",
        "state": "scheduled",
    }


def test_receipt_correlates_word_month_and_real_row():
    check_receipt(
        "Scheduled for 5 October 2026 at 13:02 UTC: buy milk",
        row(),
        owner="telegram:42",
        timezone="Etc/UTC",
        sent_at=datetime(2026, 10, 5, 13, 0, 20, tzinfo=UTC),
        seconds=120,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"user_ref": "telegram:99"},
        {"text": "other"},
        {"remind_at": "2026-10-05T15:02:20+00:00"},
        {"remind_at": "2026-10-05T13:02:20"},
        {"state": "cancelled"},
    ],
)
def test_receipt_rejects_wrong_owner_time_text_or_state(change):
    with pytest.raises(ProbeFailure):
        check_receipt(
            "Scheduled for 5 October 2026 at 13:02 UTC: buy milk",
            row() | change,
            owner="telegram:42",
            timezone="Etc/UTC",
            sent_at=datetime(2026, 10, 5, 13, 0, 20, tzinfo=UTC),
            seconds=120,
        )


def test_delivery_requires_actual_inbound_message_and_bounded_aware_arrival():
    instant = datetime.fromisoformat(row()["remind_at"])
    check_delivery(
        {
            "id": 12,
            "out": False,
            "text": "Reminder: buy milk",
            "date": (instant + timedelta(seconds=30)).isoformat(),
        },
        row(),
        after_id=10,
    )
    for change in (
        {"out": True},
        {"id": 9},
        {"text": "buy milk"},
        {"date": (instant + timedelta(seconds=200)).isoformat()},
    ):
        with pytest.raises(ProbeFailure):
            check_delivery(
                {"id": 12, "out": False, "text": "Reminder: buy milk", "date": instant.isoformat()}
                | change,
                row(),
                after_id=10,
            )


async def test_permanently_due_row_times_out_with_delivery_and_row_evidence(monkeypatch):
    from unittest.mock import AsyncMock

    from src.consumers import mechanical_telegram as probe

    original = row()
    arrival = {"id": 12, "out": False, "text": "Reminder: buy milk", "date": original["remind_at"]}
    probe.check_delivery(arrival, original, after_id=10)
    evidence = {"delivery": {"message": arrival}}
    read = AsyncMock(return_value=[original | {"state": "due"}])
    monkeypatch.setattr(probe, "REPLY_TIMEOUT", 0.01)
    with pytest.raises(ProbeFailure, match="emission_reconciliation:.*deadline"):
        await probe.reconcile_emission(read, original, evidence)
    assert evidence["phase"] == "emission_reconciliation"
    assert evidence["delivery"]["message"] == arrival
    assert evidence["delivery"]["reconciliation_rows"][0][0]["state"] == "due"
    assert "row" not in evidence["delivery"]


@pytest.mark.parametrize(
    "change",
    [
        {"user_ref": "telegram:99"},
        {"id": "replaced"},
        {"text": "other"},
        {"remind_at": "2026-10-05T13:02:20"},
        {"state": "cancelled"},
        {"state": "scheduled"},
        {"state": None},
    ],
)
async def test_reconciliation_rejects_changed_identity_content_or_state(change):
    from unittest.mock import AsyncMock

    from src.consumers import mechanical_telegram as probe

    original = row()
    candidate = original | {"state": "emitted"} | change
    evidence = {"delivery": {"message": {"id": 12}}}
    read = AsyncMock(side_effect=[[original | {"state": "due"}], [candidate]])
    with pytest.raises(ProbeFailure, match="emission_reconciliation"):
        await probe.reconcile_emission(read, original, evidence)
    assert read.await_count == 2
    assert evidence["delivery"]["reconciliation_rows"][-1][0] == candidate
    assert "row" not in evidence["delivery"]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [row(), row()],
        [42],
        {"id": "reminder-1"},
        [{}],
        [{key: value for key, value in row().items() if key != "state"}],
    ],
)
async def test_reconciliation_rejects_missing_duplicate_or_malformed_rows(rows):
    from unittest.mock import AsyncMock

    from src.consumers import mechanical_telegram as probe

    evidence = {"delivery": {"message": {"id": 12}}}
    read = AsyncMock(return_value=rows)
    with pytest.raises(ProbeFailure, match="emission_reconciliation"):
        await probe.reconcile_emission(read, row(), evidence)
    assert read.await_count == 1
    assert "row" not in evidence["delivery"]


def test_cancel_requires_same_owner_id_and_terminal_state():
    cancelled = row() | {"state": "cancelled"}
    check_cancel("Cancelled: buy milk", cancelled, row())
    with pytest.raises(ProbeFailure):
        check_cancel("Cancelled: buy milk", cancelled | {"id": "foreign"}, row())
    with pytest.raises(ProbeFailure):
        check_cancel("Access denied", cancelled, row())
