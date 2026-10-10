"""Authenticated platform facts the customer cannot see, read by existing clients.

Three facts of the live product are technical and stay outside the persona:

* the product's platform key is registered and not revoked — read with the
  deploy resolver's own `PlatformAuthAdminClient` (`GET /admin/v1/products/{id}`);
* the platform's reader attributes actual requests to the product — read from
  the reader's free self-inspection route `GET /v1/usage` with the product's own
  key, which the deploy resolver persisted in the project's encrypted secrets;
* the product language changes through the core settings write path — written
  and read back with `GeneratedServiceSettingsClient`, the deploy seeder's own
  client, using the project's stored `SETTINGS_WRITE_CAPABILITY`.

Stored secrets are decrypted in this process with the runtime's own
`SECRETS_ENCRYPTION_KEY`, exactly as the QA runtime reads them; every value is
added to the operation's redaction set the moment it is decrypted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import hashlib
import re
from typing import Protocol

import httpx
from pydantic import SecretStr

from shared.contracts.dto.product_brief import InitialSetting

from ..clients.platform_auth import PlatformAuthAdminClient, PlatformAuthError
from ..clients.product_settings import GeneratedServiceSettingsClient
from .config import PlatformEvidence, resolve_secret
from .evidence import Redaction

PLATFORM_KEY = "PLATFORM_KEY"
SETTINGS_WRITE_CAPABILITY = "SETTINGS_WRITE_CAPABILITY"
_KEY_ID = re.compile(r"cps_([a-z2-7]{12})_[A-Za-z0-9_-]{43}")
USAGE_TIMEOUT_SECONDS = 15

#: The project's own encrypted secrets, decrypted in this process (live: API + cipher).
StoredSecrets = Callable[[str], Awaitable[dict]]


def platform_product_id(project_id: str) -> str:
    """The platform product id the deploy resolver registers for this project."""
    return "orch-" + hashlib.sha256(project_id.encode()).hexdigest()[:58]


def key_id(value: str) -> str | None:
    match = _KEY_ID.fullmatch(value)
    return match[1] if match else None


@dataclass(frozen=True)
class AuthFacts:
    product_id: str
    stored_key_id: str | None
    active_key_ids: tuple[str, ...]
    revoked_key_ids: tuple[str, ...]

    @property
    def stored_key_active(self) -> bool:
        return self.stored_key_id is not None and self.stored_key_id in self.active_key_ids


@dataclass(frozen=True)
class UsageFacts:
    """The reader's `GET /v1/usage` answer for the key it was asked with.

    The released response is `{product_id, limits, used: {channels,
    requests_this_minute, resolves_today}}`; every counter lives in `used`.
    """

    product_id: str
    channels: int
    requests_this_minute: int
    resolves_today: int


class ReaderUsageRefused(RuntimeError):  # noqa: N818 - a refusal with its reason
    """The reader did not answer a usable usage document: status or shape, never a body."""


class PlatformFacts(Protocol):
    """What the controller asks of the platform, live or faked in a unit test."""

    async def auth(self, project_id: str) -> AuthFacts: ...

    async def usage(self, project_id: str, reader_base_url: str) -> UsageFacts: ...

    async def switch_language(self, project_id: str, deployed_url: str, language: str) -> bool: ...


def parse_usage(response: httpx.Response) -> UsageFacts:
    """The released usage shape, or a refusal naming what is missing or malformed."""
    try:
        body = response.json()
    except ValueError:
        raise ReaderUsageRefused("reader usage is not JSON") from None
    used = body.get("used") if isinstance(body, dict) else None
    product = body.get("product_id") if isinstance(body, dict) else None
    if not isinstance(used, dict) or not isinstance(product, str) or not product:
        raise ReaderUsageRefused("reader usage has no product_id and used object")
    counters = {}
    for name in ("channels", "requests_this_minute", "resolves_today"):
        value = used.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReaderUsageRefused(f"reader usage used.{name} is missing or malformed")
        counters[name] = value
    return UsageFacts(product_id=product, **counters)


class LivePlatformFacts:
    """The live adapter over the existing platform and product clients."""

    def __init__(
        self,
        config: PlatformEvidence,
        environ: Mapping[str, str],
        redaction: Redaction,
        *,
        stored_secrets: StoredSecrets,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._environ = environ
        self._redaction = redaction
        self._stored_secrets = stored_secrets
        self._http = http

    async def _secrets(self, project_id: str) -> dict[str, str]:
        stored = await self._stored_secrets(project_id)
        self._redaction.add(*(value for value in stored.values() if isinstance(value, str)))
        return stored

    async def auth(self, project_id: str) -> AuthFacts:
        stored = await self._secrets(project_id)
        url = resolve_secret(self._config.auth_admin_url, self._environ)
        token = resolve_secret(self._config.auth_admin_token, self._environ)
        self._redaction.add(token)
        client = PlatformAuthAdminClient(url, SecretStr(token))
        product_id = platform_product_id(project_id)
        try:
            keys = await client.product_keys(product_id)
        except PlatformAuthError as error:
            raise RuntimeError(f"platform auth read refused: {error}") from None
        finally:
            await client.aclose()
        stored_key = stored.get(PLATFORM_KEY)
        return AuthFacts(
            product_id=product_id,
            stored_key_id=key_id(stored_key) if isinstance(stored_key, str) else None,
            active_key_ids=tuple(key.key_id for key in keys if key.revoked_at is None),
            revoked_key_ids=tuple(key.key_id for key in keys if key.revoked_at is not None),
        )

    async def usage(self, project_id: str, reader_base_url: str) -> UsageFacts:
        stored = await self._secrets(project_id)
        key = stored.get(PLATFORM_KEY)
        if not isinstance(key, str) or not key:
            raise ReaderUsageRefused("the project holds no stored platform key")
        client = self._http or httpx.AsyncClient(
            timeout=USAGE_TIMEOUT_SECONDS, follow_redirects=False
        )
        try:
            response = await client.get(
                reader_base_url.rstrip("/") + "/v1/usage",
                headers={"Authorization": "Bearer " + key},
            )
        except httpx.HTTPError as exc:
            raise ReaderUsageRefused(f"reader usage unreachable: {type(exc).__name__}") from None
        finally:
            if self._http is None:
                await client.aclose()
        if response.status_code != httpx.codes.OK:
            raise ReaderUsageRefused(f"reader usage answered HTTP {response.status_code}")
        return parse_usage(response)

    async def switch_language(self, project_id: str, deployed_url: str, language: str) -> bool:
        stored = await self._secrets(project_id)
        capability = stored.get(SETTINGS_WRITE_CAPABILITY)
        if not isinstance(capability, str) or not capability:
            raise RuntimeError("the project holds no settings write capability")
        setting = InitialSetting(
            key="language",
            scope="product",
            value=language,
            description="Synthetic buyer acceptance language switch.",
        )
        (proof,) = await GeneratedServiceSettingsClient(deployed_url).seed_and_resolve(
            [setting], capability=capability
        )
        return proof.written
