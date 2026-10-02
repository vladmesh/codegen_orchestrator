"""The one user a central QA run reads the product's package routes as.

Kit core 2.1 (template 0.7.0) verifies the caller of every package route: a
trusted caller presents the product's `USER_IDENTITY_CAPABILITY` in
`X-Identity-Capability` and names an *active* user in `X-User-Channel` and
`X-User-External-Id`, and the route acts for the canonical `user_ref`
`"<channel>:<external_id>"`. There is no `user_ref` in a query any more, and an
anonymous read is 401. So QA reads those routes as a user, and this module
decides which one, once per run, before any executor exists:

* a run testing a bot reads as the QA Telegram account the run already proved
  and the bot already admitted, `telegram:<id>` — what the executor creates
  through the bot as that account is what it reads back;
* any other run reads as the platform's own QA identity, `qa:central-qa`;
* a deployment that holds no `USER_IDENTITY_CAPABILITY` — a product older than
  the caller-identity core — gets no identity, and the run is unchanged.

The identity is made active through the product's own users core with its
`USERS_GRANT_CAPABILITY`, and the run requires the product to report it active.
A grant that is not proved is a typed blocker, never a silent anonymous run.

Both capabilities live in runtime memory and request headers only. The grant
client is built here and nowhere else in QA; the identity's headers are added by
`http_get` in `agents/qa/tools.py`, which scrubs the value from everything it
records or returns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import structlog

from shared.contracts.dto.run_result import QABlocker, QABlockerCategory

from ...clients.users_grant import GeneratedServiceGrantClient

logger = structlog.get_logger(__name__)

#: The product's generated secrets, by the names its env contract gives them.
USER_IDENTITY_CAPABILITY = "USER_IDENTITY_CAPABILITY"
USERS_GRANT_CAPABILITY = "USERS_GRANT_CAPABILITY"

#: The kit core's caller-identity headers (CONTRACTS.md "Core caller identity v1").
IDENTITY_CAPABILITY_HEADER = "X-Identity-Capability"
USER_CHANNEL_HEADER = "X-User-Channel"
USER_EXTERNAL_ID_HEADER = "X-User-External-Id"

#: The platform's QA identity for a run that tests no bot. One stable value, so a
#: product's seeded owner and QA's reads can name the same user ahead of time.
QA_PLATFORM_CHANNEL = "qa"
QA_PLATFORM_EXTERNAL_ID = "central-qa"
TELEGRAM_CHANNEL = "telegram"

REDACTED = "[redacted: QA caller identity capability]"
#: The key on `Run.run_metadata` naming the user this run read package routes as.
QA_CALLER_IDENTITY_KEY = "qa_caller_identity"


def canonical_user_ref(channel: str, external_id: str) -> str:
    """The `user_ref` the product's core gives a route for this identity."""
    return f"{channel}:{external_id}"


#: What a run that tests no bot reads package routes as.
QA_PLATFORM_USER_REF = canonical_user_ref(QA_PLATFORM_CHANNEL, QA_PLATFORM_EXTERNAL_ID)


@dataclass(frozen=True)
class QACallerIdentity:
    """The verified user this run's `http_get` reads the deployed product as.

    `capability` is the deployment's `USER_IDENTITY_CAPABILITY`. It leaves this
    object only as a request header; it is kept out of `repr` so that a log line
    or an error carrying the object carries no value.
    """

    channel: str
    external_id: str
    capability: str = field(repr=False)

    @property
    def user_ref(self) -> str:
        return canonical_user_ref(self.channel, self.external_id)

    def headers(self) -> dict[str, str]:
        """Exactly one of each header the core requires, and nothing else."""
        return {
            IDENTITY_CAPABILITY_HEADER: self.capability,
            USER_CHANNEL_HEADER: self.channel,
            USER_EXTERNAL_ID_HEADER: self.external_id,
        }


def _stored(secrets: Mapping[str, object], name: str) -> str | None:
    value = secrets.get(name)
    return value if isinstance(value, str) and value else None


def _grant_blocker(user_ref: str, received: str) -> QABlocker:
    """A QA identity that was not proved active. Names no credential value."""
    return QABlocker(
        category=QABlockerCategory.QA_ACCESS_GRANT_FAILED,
        attempted=f"make the QA caller identity {user_ref} active in the product under test",
        sent=(
            f"POST /users/grant and GET /users/access for {user_ref} with the deployment's "
            f"stored {USERS_GRANT_CAPABILITY}"
        ),
        received=received,
    )


async def resolve_qa_caller_identity(
    *,
    deployed_url: str,
    secrets: Mapping[str, object],
    telegram_account_id: int | None,
    grant_client_factory: Callable[[str], GeneratedServiceGrantClient] = (
        GeneratedServiceGrantClient
    ),
) -> tuple[QACallerIdentity | None, QABlocker | None]:
    """Choose this run's QA identity and prove the product reports it active.

    Args:
        deployed_url: the deployment under test; the grant goes only there.
        secrets: the project's decrypted stored secrets, held in memory.
        telegram_account_id: the QA Telegram account this run proved and the
            bot admitted, for a run testing a bot; ``None`` otherwise.
        grant_client_factory: override for the product's users-core client.

    Returns:
        The identity, or the blocker that stopped it; ``(None, None)`` for a
        deployment with no caller-identity core, which is left unchanged.
    """
    identity_capability = _stored(secrets, USER_IDENTITY_CAPABILITY)
    if identity_capability is None:
        logger.info("qa_caller_identity_not_offered")
        return None, None
    if telegram_account_id is not None:
        channel, external_id = TELEGRAM_CHANNEL, str(telegram_account_id)
    else:
        channel, external_id = QA_PLATFORM_CHANNEL, QA_PLATFORM_EXTERNAL_ID
    user_ref = canonical_user_ref(channel, external_id)
    grant_capability = _stored(secrets, USERS_GRANT_CAPABILITY)
    if grant_capability is None:
        logger.warning("qa_caller_identity_grant_unavailable", user_ref=user_ref)
        return None, _grant_blocker(
            user_ref,
            f"capability_unavailable: the deployment holds {USER_IDENTITY_CAPABILITY} but no "
            f"stored {USERS_GRANT_CAPABILITY}, so no QA identity could be made active",
        )
    proof = await grant_client_factory(deployed_url).grant_and_resolve(
        channel=channel, external_id=external_id, capability=grant_capability
    )
    if not proof.active:
        failure = proof.failure.value if proof.failure is not None else "unverified"
        logger.warning("qa_caller_identity_not_active", user_ref=user_ref, failure=failure)
        return None, _grant_blocker(
            user_ref,
            f"{failure}: the product did not report {user_ref} active, so package routes "
            "cannot be read as a verified user",
        )
    logger.info("qa_caller_identity_active", user_ref=user_ref)
    return QACallerIdentity(channel, external_id, identity_capability), None


def caller_identity_record(identity: QACallerIdentity | None) -> dict | None:
    """What `Run.run_metadata[QA_CALLER_IDENTITY_KEY]` says. Carries no capability."""
    if identity is None:
        return None
    return {"user_ref": identity.user_ref, "active": True}


def caller_identity_facts(identity: QACallerIdentity | None) -> list[str]:
    """What the executor is told about the user its reads act as.

    Nothing for a deployment with no caller-identity core: its routes are read
    exactly as before.
    """
    if identity is None:
        return []
    facts = [
        f"- This run reads the product as the verified user `{identity.user_ref}`. The "
        "platform made that identity active through the product's own users core just "
        "before this run, and every `http_get` of the deployed URL carries it in the "
        "core's caller-identity headers. Package routes, for example `GET /reminders`, "
        f"take their owner from that identity and answer for `{identity.user_ref}` only: "
        "send no `user_ref` in a query, path or body, and read an identity-bearing package "
        "route with `http_get`. `localhost_http_get` carries no identity, so a package "
        "route read through it answers 401, which says nothing about the product.",
    ]
    if identity.channel == TELEGRAM_CHANNEL:
        facts.append(
            f"- `{identity.user_ref}` is the QA Telegram account this run talks to the bot "
            "as, so what you create through the bot as that account is what `http_get` of "
            "a package route reads back."
        )
    return facts


def scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every credential value in `text`; the identity's only redaction rule."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text
