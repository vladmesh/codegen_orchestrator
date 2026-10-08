"""Finite, data-driven Telegram acceptance in the stand's native QA grant window."""

import asyncio
import json
import os
from pathlib import Path
import re
import time

import httpx

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.dto.product_brief import InitialSetting
from shared.stand_conversation import select_conversation
from shared.telegram_access_probe import telethon_env
from shared.telethon_identity import prove_qa_identity

from ..clients.product_settings import GeneratedServiceSettingsClient
from .mechanical_telegram import ProbeFailure

ConversationFailure = ProbeFailure
MAX_STEP_TIMEOUT = 180


def selection(criteria):
    try:
        return select_conversation(criteria, contour=os.environ.get("LIVE_CONTOUR"))
    except ValueError as exc:
        raise ConversationFailure("selection", str(exc)) from None


def matches(text, predicate):
    if not predicate or set(predicate) - {"exact", "prefix", "contains"}:
        raise ValueError("a supported reply predicate is required")
    return (
        ("exact" not in predicate or text == predicate["exact"])
        and ("prefix" not in predicate or text.startswith(predicate["prefix"]))
        and all(part in text for part in predicate.get("contains", []))
    )


def record(message):
    return {"id": message.id, "text": message.raw_text, "out": message.out}


async def run_steps(  # noqa: C901, PLR0912, PLR0915 - finite sequential fixture interpreter
    client, bot, steps, *, evidence, setting=None, clock=time.monotonic, sleep=asyncio.sleep
):
    initial = await client.get_messages(bot, limit=1)
    floor = initial[0].id if initial else 0
    observed = []
    transcript = evidence.setdefault("steps", [])
    # Unmatched incoming messages stay eligible: a timer may publish during a digest reply.
    for index, step in enumerate(steps):
        evidence["phase"] = f"conversation_{index}"
        action = step["action"]
        item = {"action": action, "language": step.get("language")}
        transcript.append(item)
        if action == "setting":
            await setting(step)
            item["status"] = "passed"
            observed.append(None)
            continue
        original = None
        if action == "send":
            sent = await client.send_message(bot, step["text"])
            item["sent"] = record(sent)
            after = sent.id
        elif action == "press":
            original = observed[step["message_step"]]
            visible = await client.get_messages(bot, ids=original.id)
            buttons = [
                button
                for row in (visible.buttons or [])
                for button in row
                if button.text == step["button"]
            ]
            if len(buttons) != 1:
                raise ConversationFailure(evidence["phase"], "expected one visible button")
            await buttons[0].click()
            after = original.id
        elif action == "wait":
            after = floor
        else:
            raise ValueError("unsupported conversation action")
        started = clock()
        timeout = step.get("timeout", 30)
        if not 0 < timeout <= MAX_STEP_TIMEOUT:
            raise ValueError("conversation timeout must be within 180 seconds")
        while True:
            incoming = list(reversed(await client.get_messages(bot, min_id=after, limit=100)))
            if original is not None:
                incoming.append(await client.get_messages(bot, ids=original.id))
            accepted = None
            used = {message.id for message in observed if message is not None}
            for message in incoming:
                if message is None or message.out:
                    continue
                if message.id <= after and (original is None or message.id != original.id):
                    continue
                if message.id in used and (original is None or message.id != original.id):
                    continue
                if getattr(message.peer_id, "user_id", None) != bot.id:
                    raise ConversationFailure(evidence["phase"], "message from another chat")
                if matches(message.raw_text or "", step["expect"]):
                    accepted = message
                    break
            if accepted is not None:
                observed.append(accepted)
                item.update(
                    message=record(accepted), status="passed", elapsed_seconds=clock() - started
                )
                break
            if clock() - started >= timeout:
                raise ConversationFailure(evidence["phase"], "expected message missed its deadline")
            await sleep(min(1, timeout - (clock() - started)))
    evidence["languages"] = {
        language: all(
            item["status"] == "passed" for item in transcript if item["language"] == language
        )
        for language in {item["language"] for item in transcript if item["language"]}
    }


async def run_probe(*, marker, bot_username, deployed_url, evidence, redaction, stored, **kwargs):
    from telethon import TelegramClient  # noqa: PLC0415
    from telethon.sessions import StringSession  # noqa: PLC0415

    if os.environ.get("LIVE_CONTOUR") != "stand" or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,63}", marker
    ):
        raise ConversationFailure("selection", "stand conversation refused")
    fixture = json.loads(
        (Path(os.environ["STAND_CONVERSATION_DIR"]) / f"{marker}.json").read_text()
    )
    credentials = telethon_env()
    redaction.add(credentials["TELETHON_SESSION"], credentials["TELETHON_API_HASH"])
    client = TelegramClient(
        StringSession(credentials["TELETHON_SESSION"]),
        int(credentials["TELETHON_API_ID"]),
        credentials["TELETHON_API_HASH"],
        receive_updates=False,
    )
    try:
        async with asyncio.timeout(300):
            await prove_qa_identity(client)
            evidence["identity"] = QA_TEST_TELEGRAM_ID
            bot = await client.get_entity(f"@{bot_username}")
            async with httpx.AsyncClient(base_url=deployed_url, timeout=15) as http:

                async def setting(step):
                    body = {"contract_version": 1, "key": step["key"], "scope": "product"}
                    if step["action"] == "setting":
                        value = InitialSetting(
                            key=step["key"],
                            scope="product",
                            value=step["value"],
                            description="Stand conversation setting.",
                        )
                        (proof,) = await GeneratedServiceSettingsClient(
                            deployed_url, transport=http
                        ).seed_and_resolve([value], capability=stored["SETTINGS_WRITE_CAPABILITY"])
                        if not proof.written:
                            raise ConversationFailure(
                                "setting", "product setting write/readback failed"
                            )
                    response = await http.post("/settings/get", json=body)
                    response.raise_for_status()
                    readback = response.json()
                    if (
                        readback["value"] != step["value"]
                        or readback["key"] != step["key"]
                        or readback["scope"] != "product"
                        or readback["contract_version"] != 1
                        or readback.get("subject_id") is not None
                    ):
                        raise ConversationFailure("setting", "product setting readback differs")

                await setting({"action": "read", **fixture["initial_setting"]})
                await run_steps(client, bot, fixture["steps"], evidence=evidence, setting=setting)
            evidence.update(status="passed", phase="completed")
    finally:
        await asyncio.wait_for(client.disconnect(), timeout=30)
