"""Prove the QA Telegram identity for one run before the sandbox may hold it.

The QA executor's sandbox may run its own Telethon client as the QA account, so
the runtime hands it the account's credentials — but only once it has proven,
for this run, that the session is authorized and is `QA_TEST_TELEGRAM_ID`. The
check is `shared.telethon_identity.prove_qa_identity`, the same one the stand
preflight runs, so the two cannot disagree about what "the QA session" means.

A session that fails the proof is never handed over and is not used by the
runtime's own Telegram tools either: the run continues exactly as a run with no
Telethon credentials, and the structured reason is written to the Run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import structlog

from shared.telethon_identity import (
    CALL_TIMEOUT_SECONDS,
    SESSION_UNAUTHORIZED,
    IdentityNotProven,
    prove_qa_identity,
)

if TYPE_CHECKING:
    from ._qa_runner import QARuntimeConfig

logger = structlog.get_logger(__name__)

# The key on `Run.run_metadata` that says whether this run's sandbox was handed
# the QA Telegram identity, and if not, why. It never carries a credential.
QA_TELEGRAM_IDENTITY_KEY = "qa_telegram_identity"


@dataclass(frozen=True)
class QATelegramIdentityRefusal:
    """Why this run's sandbox holds no Telegram identity. Carries no secret."""

    reason: str
    detail: str

    def describe(self) -> str:
        return f"{self.reason}: {self.detail}"


def _telethon_client(environment: Mapping[str, str]) -> Any:
    from telethon import TelegramClient  # noqa: PLC0415 — only a proof needs Telethon
    from telethon.sessions import StringSession  # noqa: PLC0415

    return TelegramClient(
        StringSession(environment["TELETHON_SESSION"]),
        int(environment["TELETHON_API_ID"]),
        environment["TELETHON_API_HASH"],
        receive_updates=False,
    )


async def _prove(
    environment: Mapping[str, str], client_factory: Callable[[Mapping[str, str]], Any]
) -> QATelegramIdentityRefusal | None:
    try:
        client = client_factory(environment)
    except Exception as exc:  # noqa: BLE001 — a session string Telethon cannot load
        return QATelegramIdentityRefusal(
            SESSION_UNAUTHORIZED, f"the session could not be loaded: {type(exc).__name__}"
        )
    try:
        await prove_qa_identity(client)
    except IdentityNotProven as refused:
        return QATelegramIdentityRefusal(refused.reason, refused.detail)
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=CALL_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — the verdict stands; the client is dropped
            logger.warning("qa_telegram_identity_disconnect_failed", error=type(exc).__name__)
    return None


async def prove_sandbox_telegram_identity(
    runtime: QARuntimeConfig,
    *,
    client_factory: Callable[[Mapping[str, str]], Any] = _telethon_client,
) -> QARuntimeConfig:
    """Return this run's runtime with the Telegram identity proven, or withdrawn.

    No credentials: nothing to prove. Proven: the runtime is marked so the
    capability endpoint may serve the identity to the sandbox. Refused: the
    credentials are dropped from the runtime for the whole run, and the reason
    is kept for the Run record (`identity_record`).
    """
    if not runtime.telethon_env:
        return runtime
    refusal = await _prove(runtime.telethon_env, client_factory)
    if refusal is None:
        logger.info("qa_telegram_identity_proven")
        return replace(runtime, telegram_identity_proven=True)
    logger.warning("qa_telegram_identity_refused", reason=refusal.reason, detail=refusal.detail)
    return replace(runtime, telethon_env=None, telegram_identity_refusal=refusal)


def identity_record(runtime: QARuntimeConfig) -> dict | None:
    """What `Run.run_metadata[QA_TELEGRAM_IDENTITY_KEY]` says; None when nothing was proven."""
    if runtime.telegram_identity_proven:
        return {"handed_over": True}
    refusal = runtime.telegram_identity_refusal
    if refusal is None:
        return None
    return {"handed_over": False, "reason": refusal.reason, "detail": refusal.detail}


REDACTED = "[redacted: QA Telegram credential]"


def handed_over_secrets(runtime: QARuntimeConfig) -> tuple[str, ...]:
    """The credential values this run's sandbox may hold, to be kept out of evidence.

    The sandbox writes them to a private file and the `qa` CLI never prints
    them, but an agent with a shell can still print what it holds. Whatever it
    says is scrubbed of these values before it becomes a transcript, a verdict
    or a report on the Run.
    """
    if not (runtime.telegram_identity_proven and runtime.telethon_env):
        return ()
    return tuple(
        value
        for name in ("TELETHON_SESSION", "TELETHON_API_HASH")
        if (value := runtime.telethon_env.get(name, ""))
    )


def redact(text: str | None, secrets: tuple[str, ...]) -> str | None:
    if not text or not secrets:
        return text
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text
