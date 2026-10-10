"""Fixed no-model stand conversation, executed inside the native QA grant window."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import time
from zoneinfo import ZoneInfo

import httpx

from shared.contracts.acceptance import MECHANICAL_PROBE_CRITERION_RE
from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.telegram_access_probe import telethon_env
from shared.telethon_identity import IdentityNotProven, prove_qa_identity

TIMEZONE = "Etc/UTC"
PROBE_TIMEOUT = 280
REPLY_TIMEOUT = 30
DUE_LATE_SECONDS = 75
# Every readback and reply poll waits this long between reads; unit tests set it to 0.
POLL_SECONDS = 1
CLOCK_SKEW_SECONDS = 2
MONTHS = (
    "January February March April May June July August September October November December"
).split()
CRITERION = MECHANICAL_PROBE_CRITERION_RE


class ProbeFailure(RuntimeError):
    def __init__(self, phase, detail, *, cause=None):
        self.phase = phase
        # The inner failure the probe caught, already safe to retain: class name,
        # refusal reason and a detail that never quotes the session.
        self.cause = cause
        super().__init__(f"{phase}: {detail}")


def failure_cause(exc):
    """What failed inside the probe, without quoting anything it was handed.

    `IdentityNotProven` carries a reason code and a detail written to never
    contain a secret; any other exception keeps only its class name, because its
    text may quote the session or the API hash.
    """
    if isinstance(exc, ProbeFailure):
        return exc.cause or {"type": "ProbeFailure", "detail": str(exc)}
    if isinstance(exc, IdentityNotProven):
        return {"type": "IdentityNotProven", "reason": exc.reason, "detail": exc.detail}
    return {"type": type(exc).__name__}


def describe_cause(cause):
    return ": ".join(str(cause[key]) for key in ("type", "reason", "detail") if cause.get(key))


def selection(criteria):
    matches = list(CRITERION.finditer(criteria))
    if not matches:
        if "Stand mechanical" in criteria:
            raise ProbeFailure("selection", "malformed fixed probe criterion")
        return None
    if len(matches) != 1:
        raise ProbeFailure("selection", "exactly one fixed probe is required")
    match = matches[0]
    return match[1], match[2], criteria[: match.start()] + criteria[match.end() :]


def aware(value):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            raise ValueError
        return instant
    except (ValueError, TypeError, AttributeError):
        raise ProbeFailure("timestamp", "an aware timestamp is required") from None


def receipt(row, timezone):
    local = aware(row["remind_at"]).astimezone(ZoneInfo(timezone))
    return (
        f"Scheduled for {local.day} {MONTHS[local.month - 1]} {local.year} "
        f"at {local:%H:%M %Z}: {row['text']}"
    )


def check_receipt(text, row, *, owner, timezone, sent_at, seconds):
    if row["user_ref"] != owner or row["state"] != "scheduled":
        raise ProbeFailure("confirmation", "wrong reminder owner or state")
    delay = (aware(row["remind_at"]) - sent_at).total_seconds()
    if not seconds - 2 <= delay <= seconds + REPLY_TIMEOUT:
        raise ProbeFailure("confirmation", "wrong parsed/preset instant")
    if text != receipt(row, timezone):
        raise ProbeFailure("confirmation", "receipt instant/text differs from stored reminder")


def check_delivery(message, row, *, after_id):
    delay = (aware(message["date"]) - aware(row["remind_at"])).total_seconds()
    if (
        message["out"]
        or message["id"] <= after_id
        or message["text"] != f"Reminder: {row['text']}"
        or not -CLOCK_SKEW_SECONDS <= delay <= DUE_LATE_SECONDS
    ):
        raise ProbeFailure("delivery", "wrong message, direction or arrival time")


def check_cancel(text, cancelled, original):
    if (
        text != f"Cancelled: {original['text']}"
        or cancelled["state"] != "cancelled"
        or any(cancelled[key] != original[key] for key in ("id", "user_ref", "text", "remind_at"))
    ):
        raise ProbeFailure(
            "cancellation", "receipt or cancelled row differs from selected reminder"
        )


def message_record(message):
    return {
        "id": message.id,
        "text": message.raw_text,
        "out": message.out,
        "date": message.date.isoformat(),
        "edit_date": message.edit_date.isoformat() if getattr(message, "edit_date", None) else None,
    }


async def reconcile_emission(read_rows, original, evidence):
    """Publication can reach Telegram before the producer commits its emitted state."""
    phase = "emission_reconciliation"
    evidence["phase"] = phase
    delivery = evidence["delivery"]
    observations = delivery["reconciliation_rows"] = []
    identity = ("id", "user_ref", "text", "remind_at")
    try:
        async with asyncio.timeout(REPLY_TIMEOUT):
            while True:
                rows = await read_rows()
                if not isinstance(rows, list):
                    raise ProbeFailure(phase, "reminder readback is not a row list")
                observations.append(
                    [
                        {key: row.get(key) for key in (*identity, "state")}
                        if isinstance(row, dict)
                        else {"invalid_row_type": type(row).__name__}
                        for row in rows
                    ]
                )
                if any(not isinstance(row, dict) for row in rows):
                    raise ProbeFailure(phase, "malformed reminder row")
                matches = [row for row in rows if row.get("id") == original["id"]]
                if len(matches) != 1 or any(
                    matches[0].get(key) != original[key] for key in identity
                ):
                    raise ProbeFailure(phase, "confirmed reminder missing, replaced or changed")
                current = matches[0]
                if current.get("state") == "emitted":
                    delivery["row"] = current
                    return
                if current.get("state") != "due":
                    raise ProbeFailure(
                        phase, "expected the confirmed reminder to be due or emitted"
                    )
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        raise ProbeFailure(phase, "emitted state was not visible before deadline") from None


async def run_conversation(  # noqa: C901, PLR0915 - sequential fixed chat and correlation steps
    client, bot, http, *, mode, marker, headers, evidence
):
    """Only chat sends/callbacks mutate reminders. HTTP reads correlate the actual rows."""
    owner = f"telegram:{QA_TEST_TELEGRAM_ID}"

    async def read_rows():
        response = await http.get("/reminders", headers=headers)
        response.raise_for_status()
        return response.json()

    async def reply(after_id, *, phase, expected, timeout=REPLY_TIMEOUT):
        evidence["phase"] = phase
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in reversed(await client.get_messages(bot, min_id=after_id, limit=100)):
                if item.out or item.id <= after_id:
                    continue
                if getattr(item.peer_id, "user_id", None) != bot.id:
                    raise ProbeFailure(phase, "reply from a different chat")
                text = item.raw_text or ""
                if any(
                    word in text.casefold()
                    for word in (
                        "access denied",
                        "forbidden",
                        "unauthorized",
                        "not authorized",
                        "доступ запрещ",
                    )
                ):
                    raise ProbeFailure(phase, "bot refused QA identity")
                if expected(text):
                    evidence.setdefault("messages", []).append(message_record(item))
                    return item
            await asyncio.sleep(POLL_SECONDS)
        raise ProbeFailure(phase, "expected bot reply did not arrive before deadline")

    async def send(command, phase, expected):
        evidence["phase"] = phase
        sent = await client.send_message(bot, command)
        evidence.setdefault("sent", []).append(message_record(sent))
        return sent, await reply(sent.id, phase=phase, expected=expected)

    async def edited(original, *, phase, expected):
        evidence["phase"] = phase
        deadline = time.monotonic() + REPLY_TIMEOUT
        while time.monotonic() < deadline:
            message = await client.get_messages(bot, ids=original.id)
            if (
                message
                and not message.out
                and message.id == original.id
                and getattr(message.peer_id, "user_id", None) == bot.id
                and message.edit_date is not None
                and expected(message.raw_text)
            ):
                evidence.setdefault("messages", []).append(message_record(message))
                return message
            await asyncio.sleep(POLL_SECONDS)
        raise ProbeFailure(phase, "callback did not edit its own bot message before deadline")

    note = f"stand-note-{marker}"
    if mode == "notes":
        _, saved = await send(f"/note {note}", "notes_save", lambda text: text == f"Saved: {note}")
        evidence["saved_note"] = message_record(saved)
    _, listed = await send("/notes", "notes_list", lambda text: note in text.splitlines())
    evidence["notes_list"] = message_record(listed)
    if mode == "reminders":
        before = await read_rows()
        if before:
            raise ProbeFailure(
                "parsed_confirmation",
                "fresh installed product already has reminders for QA identity",
            )
        sent, confirmed = await send(
            "/remind buy milk in 2 minutes",
            "parsed_confirmation",
            lambda text: text.startswith("Scheduled for ") and text.endswith(": buy milk"),
        )
        rows = await read_rows()
        created = [row for row in rows if row["id"] not in {one["id"] for one in before}]
        if len(created) != 1 or created[0]["text"] != "buy milk":
            raise ProbeFailure("parsed_confirmation", "expected one chat-created buy milk reminder")
        first = created[0]
        check_receipt(
            confirmed.raw_text,
            first,
            owner=owner,
            timezone=TIMEZONE,
            sent_at=sent.date,
            seconds=120,
        )
        evidence["parsed"] = {
            "row": first,
            "confirmation": message_record(confirmed),
            "request": message_record(sent),
            "user_ref": owner,
        }
        remaining = (aware(first["remind_at"]) - datetime.now(UTC)).total_seconds()
        due = await reply(
            confirmed.id,
            phase="due_delivery",
            expected=lambda text: text == "Reminder: buy milk",
            timeout=max(1, remaining + DUE_LATE_SECONDS),
        )
        evidence["delivery"] = {"message": message_record(due)}
        check_delivery(evidence["delivery"]["message"], first, after_id=confirmed.id)
        await reconcile_emission(read_rows, first, evidence)
        second_text = f"stand-cancel-{marker}"
        before = await read_rows()
        _, presets = await send(
            f"/remind {second_text}",
            "presets",
            lambda text: text == "Choose a time for your reminder:",
        )
        labels = [button.text for line in presets.buttons or [] for button in line]
        if labels != ["In 5 minutes", "In 1 hour", "Tomorrow at 09:00"]:
            raise ProbeFailure("presets", "the three declared time buttons are required")
        evidence["presets"] = {"message": message_record(presets), "labels": labels}
        clicked_at = datetime.now(UTC)
        evidence["phase"] = "preset_confirmation"
        await presets.click(text="In 5 minutes")
        scheduled = await edited(
            presets,
            phase="preset_confirmation",
            expected=lambda text: (
                text.startswith("Scheduled for ") and text.endswith(f": {second_text}")
            ),
        )
        created = [
            row for row in await read_rows() if row["id"] not in {one["id"] for one in before}
        ]
        if len(created) != 1 or created[0]["text"] != second_text:
            raise ProbeFailure("preset_confirmation", "expected one preset-created reminder")
        second = created[0]
        check_receipt(
            scheduled.raw_text,
            second,
            owner=owner,
            timezone=TIMEZONE,
            sent_at=clicked_at,
            seconds=300,
        )
        evidence["preset"] = {
            "row": second,
            "confirmation": message_record(scheduled),
            "clicked_at": clicked_at.isoformat(),
        }
        _, listing = await send(
            "/reminders", "cancel_list", lambda text: text.endswith(f": {second_text}")
        )
        if [button.text for line in listing.buttons or [] for button in line] != ["Cancel"]:
            raise ProbeFailure("cancel_list", "selected row must offer its Cancel button")
        evidence["phase"] = "cancellation"
        await listing.click(text="Cancel")
        cancelled_reply = await edited(
            listing, phase="cancellation", expected=lambda text: text == f"Cancelled: {second_text}"
        )
        cancelled = next(row for row in await read_rows() if row["id"] == second["id"])
        check_cancel(cancelled_reply.raw_text, cancelled, second)
        _, empty = await send(
            "/reminders", "cancel_readback", lambda text: text == "You have no scheduled reminders."
        )
        evidence["cancellation"] = {
            "row": cancelled,
            "reply": message_record(cancelled_reply),
            "list": message_record(empty),
            "selected": message_record(listing),
        }
    after_note = f"stand-after-{mode}-{marker}"
    await send(
        f"/note {after_note}", "notes_save_after", lambda text: text == f"Saved: {after_note}"
    )
    _, final = await send(
        "/notes", "notes_list_after", lambda text: {note, after_note} <= set(text.splitlines())
    )
    evidence["notes_after"] = message_record(final)


async def run_fixed_probe(
    *, mode, marker, bot_username, deployed_url, headers, evidence, redaction
):
    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    credentials = telethon_env()
    redaction.add(credentials["TELETHON_SESSION"], credentials["TELETHON_API_HASH"])
    client = TelegramClient(
        StringSession(credentials["TELETHON_SESSION"]),
        int(credentials["TELETHON_API_ID"]),
        credentials["TELETHON_API_HASH"],
        receive_updates=False,
    )
    evidence.update(mode=mode, marker=marker, phase="identity", status="running")
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            await prove_qa_identity(client)
            evidence["identity"] = QA_TEST_TELEGRAM_ID
            bot = await client.get_entity(f"@{bot_username}")
            async with httpx.AsyncClient(base_url=deployed_url, timeout=15) as http:
                if mode == "reminders":
                    evidence["phase"] = "timezone"
                    response = await http.post(
                        "/settings/get",
                        json={
                            "contract_version": 1,
                            "scope": "product",
                            "key": "timezone",
                        },
                    )
                    response.raise_for_status()
                    evidence["timezone"] = response.json()
                    if (
                        evidence["timezone"]["value"] != TIMEZONE
                        or evidence["timezone"]["key"] != "timezone"
                        or evidence["timezone"]["scope"] != "product"
                        or evidence["timezone"]["contract_version"] != 1
                        or evidence["timezone"].get("subject_id") is not None
                    ):
                        raise ProbeFailure(
                            "timezone", "explicit product timezone differs from confirmed brief"
                        )
                await run_conversation(
                    client, bot, http, mode=mode, marker=marker, headers=headers, evidence=evidence
                )
            evidence.update(status="passed", phase="completed")
    except Exception as exc:
        cause = {key: redaction.text(value) for key, value in failure_cause(exc).items()}
        evidence.update(status="failed", failure_type=cause["type"], failure_cause=cause)
        raise ProbeFailure(evidence["phase"], describe_cause(cause), cause=cause) from None
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=30)
        except Exception as exc:
            if evidence["status"] == "passed":
                evidence.update(
                    status="failed", phase="disconnect", failure_type=type(exc).__name__
                )
            evidence["disconnect"] = "failed"
            raise ProbeFailure(evidence["phase"], "session disconnect failed") from None
        evidence["disconnect"] = "completed"


def report(evidence):
    return json.dumps(evidence, sort_keys=True)
