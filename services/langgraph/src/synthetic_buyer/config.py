"""What one synthetic-buyer operation is told, and nothing it is allowed to guess.

Every identity, credential, endpoint and deadline is named by the operator in one
JSON file. There is no production host, session, token, model or fallback identity
in this package: a value the file does not give is a refusal at load time, before
anything connects.

Credentials are never values in the file. A `SecretHandle` names where a value
lives (an environment variable or a file), and only an execution adapter resolves
it, at the moment it is used. The handle is what evidence records; the value is
held in memory by the adapter that needs it and nowhere else.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from shared.contracts.dto.llm_channel import LLMChannelChain
from shared.diagnostics import safe_validation_errors

#: The only config shape this revision reads; a later shape is a new number.
CONFIG_SCHEMA_VERSION = 1

#: A public Telegram username, the way Telegram constrains it.
TELEGRAM_USERNAME = r"^[A-Za-z][A-Za-z0-9_]{3,31}$"


class ConfigError(ValueError):
    """The config cannot drive an operation. The message never quotes a value."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _http_url(value: str, what: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"{what} is an http(s) URL without credentials")
    return value.rstrip("/")


class SecretHandle(_Strict):
    """Where one secret lives: exactly one of an environment variable or a file."""

    env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    file: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _exactly_one(self) -> SecretHandle:
        if (self.env is None) == (self.file is None):
            raise ValueError("a secret handle names exactly one of env or file")
        return self

    def describe(self) -> str:
        """The handle's name, safe for evidence and diagnostics."""
        return f"env:{self.env}" if self.env is not None else f"file:{self.file}"


class MissingSecret(RuntimeError):  # noqa: N818 - a refusal named by what is missing
    """A handle that resolves to nothing. Names the handle, never a value."""

    def __init__(self, handle: SecretHandle, detail: str) -> None:
        super().__init__(f"{handle.describe()} {detail}")
        self.handle = handle


def resolve_secret(
    handle: SecretHandle,
    environ: Mapping[str, str],
    *,
    read_file: Callable[[str], str] = lambda path: Path(path).read_text(encoding="utf-8"),
) -> str:
    """The handle's value, for an execution adapter only. Empty is missing."""
    if handle.env is not None:
        value = environ.get(handle.env, "")
    else:
        try:
            value = read_file(handle.file or "")
        except OSError as exc:
            raise MissingSecret(handle, f"cannot be read ({type(exc).__name__})") from None
    value = value.strip()
    if not value:
        raise MissingSecret(handle, "is unset or empty")
    return value


class CodegenBot(_Strict):
    """The actual Codegen Telegram bot the buyer orders through."""

    username: str = Field(pattern=TELEGRAM_USERNAME)
    user_id: int = Field(gt=0)


class BuyerIdentity(_Strict):
    """The Telegram account the session must prove before any message is sent."""

    telegram_id: int = Field(gt=0)


class Scenario(_Strict):
    """What the customer wants: a fresh bot posting from these public channels."""

    public_channels: list[str] = Field(min_length=1, max_length=5)
    product_language: Literal["ru"]
    #: The second language the acceptance switches to through core settings.
    switch_language: Literal["en"]

    @field_validator("public_channels")
    @classmethod
    def _channels(cls, channels: list[str]) -> list[str]:
        names = [channel.removeprefix("@").lower() for channel in channels]
        if len(set(names)) != len(names):
            raise ValueError("public channels are listed once each")
        for name in names:
            if re.fullmatch(TELEGRAM_USERNAME, name) is None:
                raise ValueError("a public channel is a Telegram username")
        return names


class Deadlines(_Strict):
    """Every bound of the operation. Policy, so none has a default."""

    #: How long one Codegen reply may take to begin after a buyer message.
    reply_seconds: int = Field(gt=0, le=1800)
    #: Quiet time after the last inbound message before a reply counts as complete.
    settle_seconds: int = Field(gt=0, le=300)
    #: Buyer messages the order conversation may spend before it is stalled.
    conversation_turns: int = Field(gt=0, le=60)
    #: From the first order message until the story exists.
    order_seconds: int = Field(gt=0, le=4 * 3600)
    #: From the order until the story's deploy and QA are settled.
    build_seconds: int = Field(gt=0, le=48 * 3600)
    #: How long to wait for every QA run to leave the shared account.
    qa_quiet_seconds: int = Field(gt=0, le=6 * 3600)
    #: How long one product probe waits for the bot's answer.
    probe_reply_seconds: int = Field(gt=0, le=600)
    #: How long to wait for an unsolicited post from a configured channel.
    post_delivery_seconds: int = Field(gt=0, le=24 * 3600)
    #: How long the teardown may take to report completed.
    teardown_seconds: int = Field(gt=0, le=6 * 3600)
    #: Interval between API observations while waiting.
    poll_seconds: int = Field(gt=0, le=600)
    #: Dialog reads, one poll apart, that look for a send whose receipt was lost
    #: before its delivery is declared unknown. An unknown delivery is never resent.
    delivery_checks: int = Field(gt=0, le=10)
    #: Fixed deferrals the buyer may send before the order's project is proven.
    deferrals: int = Field(gt=0, le=10)


class ApiEndpoint(_Strict):
    """The Codegen API this operation observes and drives through operator adapters.

    Calls go through the shared internal API transport, which authenticates with the
    runtime's own `INTERNAL_API_KEY` the way every service does.
    """

    base_url: str

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _http_url(value, "the API base URL")


class TelegramCredentials(_Strict):
    """The buyer's Telethon session: the QA account's, in the production environment."""

    api_id: SecretHandle
    api_hash: SecretHandle
    session: SecretHandle


class PromoPolicy(_Strict):
    """The budget a one-time promo code arms when this buyer has to be registered."""

    credits_microusd: int = Field(ge=0)
    attempt_reservation_microusd: int = Field(gt=0)


class ProductToken(_Strict):
    """Where the product bot's token comes from.

    ``handle``: an operator-supplied protected value. ``botfather``: the buyer
    creates one bot owned by this operation, named from the operation id.
    """

    mode: Literal["handle", "botfather"]
    handle: SecretHandle | None = None
    botfather_username: str | None = Field(default=None, pattern=TELEGRAM_USERNAME)
    bot_display_name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _source(self) -> ProductToken:
        if self.mode == "handle" and (
            self.handle is None or self.botfather_username or self.bot_display_name
        ):
            raise ValueError("handle mode takes a handle and nothing else")
        if self.mode == "botfather" and (
            self.handle is not None or not self.botfather_username or not self.bot_display_name
        ):
            raise ValueError("botfather mode takes the BotFather username and a display name")
        return self


class PlatformEvidence(_Strict):
    """The authenticated adapters that read platform facts outside the conversation.

    The auth admin endpoint is the platform's own (`/admin/v1/products/...`). The
    project's stored secrets are decrypted with the runtime's own
    ``SECRETS_ENCRYPTION_KEY``, exactly as the QA runtime reads them.
    """

    auth_admin_url: SecretHandle
    auth_admin_token: SecretHandle
    #: The reader's public base URL the product's environment contract names.
    reader_base_url: str

    @field_validator("reader_base_url")
    @classmethod
    def _reader_url(cls, value: str) -> str:
        return _http_url(value, "the reader base URL")


class ModelConfig(_Strict):
    """The persona's model: an explicit channel chain, never the default one."""

    chain: LLMChannelChain


class BuyerConfig(_Strict):
    """One operation: who buys, from whom, what, under which bounds, kept where."""

    schema_version: Literal[1]
    operation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    codegen_bot: CodegenBot
    buyer: BuyerIdentity
    scenario: Scenario
    model: ModelConfig
    deadlines: Deadlines
    evidence_dir: str = Field(min_length=1, max_length=1024)
    api: ApiEndpoint
    telegram: TelegramCredentials
    registration: PromoPolicy
    product_token: ProductToken
    platform: PlatformEvidence

    def secret_handles(self) -> dict[str, SecretHandle]:
        """Every handle the live mode resolves, by role."""
        handles = {
            "telegram.api_id": self.telegram.api_id,
            "telegram.api_hash": self.telegram.api_hash,
            "telegram.session": self.telegram.session,
            "platform.auth_admin_url": self.platform.auth_admin_url,
            "platform.auth_admin_token": self.platform.auth_admin_token,
        }
        if self.product_token.handle is not None:
            handles["product_token.handle"] = self.product_token.handle
        return handles


def parse_config(raw: object) -> BuyerConfig:
    """A validated config, or `ConfigError` naming the fields, never their values."""
    try:
        return BuyerConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'} ({error['type']})"
            for error in safe_validation_errors(exc)
        )
        raise ConfigError(f"the buyer config does not validate: {problems}") from None


def load_config(path: Path) -> BuyerConfig:
    """Read and validate the operator's JSON config file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"the buyer config cannot be read ({type(exc).__name__})") from None
    except ValueError:
        raise ConfigError("the buyer config is not JSON") from None
    return parse_config(raw)
