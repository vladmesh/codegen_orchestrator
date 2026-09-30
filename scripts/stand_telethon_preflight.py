#!/usr/bin/env python3
"""Prove the stand's QA Telegram session before a paid suite spends anything.

A suite whose QA executor judges a Telegram-bot product needs the QA runtime's
Telethon session, and nothing else in the run proves it: pre-create validation
only sees that three secrets are non-empty, and qa-worker discovers an unusable
session hours later, after the machines, the developer turns and the deploy
were paid for. Paid run 36147402976 blocked exactly there.

This asks the three questions the QA runtime will ask, with the stand's own
secrets, on the GitHub runner before any machine exists:

* the session authorizes (``get_me``) — else ``telethon_session_unauthorized``;
* its user id is the one the QA runtime's ``/start`` probe compares against,
  ``shared.contracts.bot_access.QA_TEST_TELEGRAM_ID`` — else
  ``telethon_identity_mismatch``;
* it resolves the stand product bot, whose username is read from
  ``STAND_PRODUCT_BOT_TOKEN`` with the Bot API's ``getMe``, and can write
  ``/start`` to it — else ``telethon_bot_unreachable``.

The client disconnects before this process exits, so the session is never
open here while qa-worker holds it on the stand. Only non-secret facts are
printed: the user id, the bot username, and the verdict or the reason.

``needs-session`` answers whether a suite needs any of this; it runs on the
runner's bare ``python3``, so this module imports nothing outside the standard
library at import time. ``prove`` runs with Telethon::

    uv run --no-project --with telethon==1.45.0 python -m scripts.stand_telethon_preflight prove
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import json
import os
import sys
from typing import Any
import urllib.error
import urllib.request

from shared import telethon_identity
from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID

TELETHON_ENV_VARS = ("TELETHON_API_ID", "TELETHON_API_HASH", "TELETHON_SESSION")
PRODUCT_BOT_TOKEN_ENV = "STAND_PRODUCT_BOT_TOKEN"  # noqa: S105 - a name, not a secret
# The suites whose QA executor judges a Telegram-bot product: `mega-live` is the
# level-1 lifecycle (a bot) with a real QA executor. `mega-noop` deploys the same
# bot but its QA is deterministic and never opens the session; the Product Brief
# suites run a QA executor on products that are not bots. Pinned against the
# runner's suite table by scripts/tests/test_stand_telethon_preflight.py.
QA_TELETHON_SUITES = frozenset({"mega-live"})
# One bound per Telegram round trip. A hung MTProto connection is a refusal
# with its stage named, never a step that waits for the job timeout.
CALL_TIMEOUT_SECONDS = 30
BOT_API_TIMEOUT_SECONDS = 15
BOT_API_GET_ME = "https://api.telegram.org/bot{token}/getMe"


class Refusal(StrEnum):
    SESSION_UNAUTHORIZED = telethon_identity.SESSION_UNAUTHORIZED
    IDENTITY_MISMATCH = telethon_identity.IDENTITY_MISMATCH
    BOT_UNREACHABLE = "telethon_bot_unreachable"


@dataclass(frozen=True)
class Verdict:
    """What the preflight found. ``refusal`` is None exactly when it passed."""

    refusal: Refusal | None
    detail: str = ""
    user_id: int | None = None
    bot_username: str | None = None

    def line(self) -> str:
        facts = [
            f"user_id={self.user_id if self.user_id is not None else 'unknown'}",
            f"bot=@{self.bot_username}" if self.bot_username else "bot=unknown",
            f"expected_user_id={QA_TEST_TELEGRAM_ID}",
        ]
        if self.refusal is None:
            return "telethon_preflight: pass " + " ".join(facts)
        return (
            f"telethon_preflight: refused reason={self.refusal.value} "
            + " ".join(facts)
            + f" detail={self.detail}"
        )


class _Refused(Exception):
    def __init__(self, refusal: Refusal, detail: str):
        super().__init__(detail)
        self.refusal = refusal
        self.detail = detail


def needs_session(suite: str) -> bool:
    """Whether *suite*'s QA executor opens the QA Telegram session."""
    return suite in QA_TELETHON_SUITES


def _error_name(exc: BaseException) -> str:
    # The class name only: an exception's text may quote what it was handed.
    return type(exc).__name__


def product_bot_username(
    token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    """The stand product bot's username, as the Bot API's ``getMe`` reports it.

    The token is placed in the request URL only; nothing it could appear in —
    the URL, an exception's text — is ever returned or printed.
    """
    if not token.strip():
        raise _Refused(Refusal.BOT_UNREACHABLE, f"{PRODUCT_BOT_TOKEN_ENV} is not set")
    request = urllib.request.Request(BOT_API_GET_ME.format(token=token.strip()))  # noqa: S310
    try:
        with opener(request, timeout=BOT_API_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _Refused(Refusal.BOT_UNREACHABLE, f"Bot API getMe answered HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise _Refused(
            Refusal.BOT_UNREACHABLE, f"Bot API getMe failed: {_error_name(exc)}"
        ) from None
    result = payload.get("result") if isinstance(payload, dict) else None
    username = result.get("username") if isinstance(result, dict) else None
    if not (isinstance(payload, dict) and payload.get("ok") is True and username):
        raise _Refused(Refusal.BOT_UNREACHABLE, "Bot API getMe returned no bot username")
    return str(username)


async def _bounded(awaitable: Awaitable[Any], refusal: Refusal, stage: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=CALL_TIMEOUT_SECONDS)
    except TimeoutError:
        raise _Refused(refusal, f"{stage} did not answer in {CALL_TIMEOUT_SECONDS}s") from None
    except Exception as exc:  # noqa: BLE001 - every Telethon failure is this stage's refusal
        raise _Refused(refusal, f"{stage} failed: {_error_name(exc)}") from None


async def prove_session(client: Any, *, bot_username: str) -> Verdict:
    """Ask the session the QA runtime's own questions, then disconnect it.

    *client* is a Telethon ``TelegramClient`` (any object with its async
    ``connect``, ``is_user_authorized``, ``get_me``, ``get_entity``,
    ``send_message`` and ``disconnect``).
    """
    user_id: int | None = None
    try:
        # The identity half is the QA runtime's own check, shared with it: the
        # runtime asks exactly this before it hands the session to a sandbox.
        try:
            user_id = await telethon_identity.prove_qa_identity(
                client, timeout=CALL_TIMEOUT_SECONDS
            )
        except telethon_identity.IdentityNotProven as refused:
            user_id = refused.user_id
            raise _Refused(Refusal(refused.reason), refused.detail) from None
        bot = await _bounded(
            client.get_entity(f"@{bot_username}"), Refusal.BOT_UNREACHABLE, "resolve"
        )
        if not getattr(bot, "bot", False):
            raise _Refused(Refusal.BOT_UNREACHABLE, "the username does not resolve to a bot")
        await _bounded(client.send_message(bot, "/start"), Refusal.BOT_UNREACHABLE, "/start")
    except _Refused as refused:
        return Verdict(refused.refusal, refused.detail, user_id, bot_username)
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=CALL_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001, S110 - the verdict stands; the process exits next
            pass
    return Verdict(None, "", user_id, bot_username)


def _telethon_client(environment: Mapping[str, str]) -> Any:
    """The QA runtime's client, built exactly as its probe builds it."""
    from telethon import TelegramClient  # noqa: PLC0415 - only `prove` needs Telethon
    from telethon.sessions import StringSession  # noqa: PLC0415

    return TelegramClient(
        StringSession(environment["TELETHON_SESSION"]),
        int(environment["TELETHON_API_ID"]),
        environment["TELETHON_API_HASH"],
        receive_updates=False,
    )


def prove(
    environment: Mapping[str, str],
    *,
    client_factory: Callable[[Mapping[str, str]], Any] = _telethon_client,
    bot_lookup: Callable[[str], str] = product_bot_username,
) -> Verdict:
    """The whole preflight: credentials present, bot named, session proven."""
    missing = [name for name in TELETHON_ENV_VARS if not environment.get(name, "").strip()]
    if missing:
        return Verdict(Refusal.SESSION_UNAUTHORIZED, f"missing {' '.join(missing)}")
    if not environment["TELETHON_API_ID"].strip().isdecimal():
        return Verdict(Refusal.SESSION_UNAUTHORIZED, "TELETHON_API_ID is not an integer")
    try:
        bot_username = bot_lookup(environment.get(PRODUCT_BOT_TOKEN_ENV, ""))
    except _Refused as refused:
        return Verdict(refused.refusal, refused.detail)
    try:
        client = client_factory(environment)
    except Exception as exc:  # noqa: BLE001 - a session string Telethon cannot load
        return Verdict(
            Refusal.SESSION_UNAUTHORIZED,
            f"the session could not be loaded: {_error_name(exc)}",
            bot_username=bot_username,
        )
    return asyncio.run(prove_session(client, bot_username=bot_username))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    needs = sub.add_parser("needs-session", help="print true when the suite needs the session")
    needs.add_argument("--suite", required=True)
    sub.add_parser("prove", help="prove the session from the environment's stand secrets")
    args = parser.parse_args(argv)
    if args.command == "needs-session":
        print("true" if needs_session(args.suite) else "false")
        return 0
    verdict = prove(os.environ)
    print(verdict.line(), file=sys.stdout if verdict.refusal is None else sys.stderr)
    return 0 if verdict.refusal is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
