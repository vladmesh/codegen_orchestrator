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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Protocol

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
    status: int
    channels_used: int | None
    requests_this_minute: int | None
    resolves_today: int | None


class PlatformFacts(Protocol):
    """What the controller asks of the platform, live or faked in a unit test."""

    async def auth(self, project_id: str) -> AuthFacts: ...

    async def usage(self, project_id: str, reader_base_url: str) -> UsageFacts: ...

    async def switch_language(self, project_id: str, deployed_url: str, language: str) -> bool: ...


class LivePlatformFacts:
    """The live adapter over the existing platform and product clients."""

    def __init__(
        self,
        config: PlatformEvidence,
        environ: Mapping[str, str],
        redaction: Redaction,
        *,
        stored_secrets: Callable[[str], Any],
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
            raise RuntimeError("the project holds no stored platform key")
        client = self._http or httpx.AsyncClient(
            timeout=USAGE_TIMEOUT_SECONDS, follow_redirects=False
        )
        try:
            response = await client.get(
                reader_base_url.rstrip("/") + "/v1/usage",
                headers={"Authorization": "Bearer " + key},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"reader usage unreachable: {type(exc).__name__}") from None
        finally:
            if self._http is None:
                await client.aclose()
        body: Any = None
        if response.is_success:
            try:
                body = response.json()
            except ValueError:
                body = None
        used = body.get("used") if isinstance(body, dict) else None
        return UsageFacts(
            status=response.status_code,
            channels_used=_int(used.get("channels")) if isinstance(used, dict) else None,
            requests_this_minute=_int(body.get("requests_this_minute"))
            if isinstance(body, dict)
            else None,
            resolves_today=_int(body.get("resolves_today")) if isinstance(body, dict) else None,
        )

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


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
