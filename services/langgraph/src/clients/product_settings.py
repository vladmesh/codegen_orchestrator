"""Narrow client for the generated product's core settings write path.

The same shape as `users_grant.py`, one contract along: it calls only the two
documented endpoints of the released settings core (`service-template`,
`docs/CONTRACTS.md`, "Core settings v1"), takes the deployment capability as an
argument, puts it in a request header and nowhere else, and returns a bounded,
credential-safe outcome per setting.

A write is never assumed. Every accepted `POST /settings/set` is followed by
`POST /settings/get`, and the setting counts as seeded only when the product
answers with the value that was just written — the evidence QA rests on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Protocol

import httpx

from shared.contracts.dto.product_brief import InitialSetting
from shared.contracts.dto.settings_seed import (
    CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL,
    CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL,
    SettingsSeedFailureKind,
)

#: Both generated schemas carry it, and the product refuses any other value.
_CONTRACT_VERSION = 1

#: The one header `POST /settings/set` authenticates with. It is deliberately
#: absent from the generated schemas and OpenAPI, and must never be logged,
#: put in a URL, or reach LLM-facing data.
_CAPABILITY_HEADER = "X-Settings-Capability"  # noqa: S105


@dataclass(frozen=True)
class SettingSeedProof:
    """The result of writing one setting and reading it back."""

    written: bool
    failure: SettingsSeedFailureKind | None = None


class _RequestClient(Protocol):
    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response: ...


class GeneratedServiceSettingsClient:
    """Call only the generated product's documented settings endpoints."""

    def __init__(self, base_url: str, *, transport: _RequestClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport or httpx.AsyncClient(timeout=15.0, follow_redirects=False)
        self._owns_transport = transport is None

    async def seed_and_resolve(
        self, settings: Sequence[InitialSetting], *, capability: str
    ) -> list[SettingSeedProof]:
        """Write every setting and prove each one, in the confirmed order.

        One proof per setting, positionally. Settings are independent values,
        so a refusal of one is recorded and the rest are still attempted: the
        run's record then says what the product holds and what it does not.
        """
        try:
            return [await self._seed_one(setting, capability=capability) for setting in settings]
        finally:
            if self._owns_transport:
                await self._transport.aclose()  # type: ignore[attr-defined]

    async def _seed_one(self, setting: InitialSetting, *, capability: str) -> SettingSeedProof:
        identity: dict[str, Any] = {
            "contract_version": _CONTRACT_VERSION,
            "key": setting.key,
            "scope": setting.scope.value,
            "subject_id": setting.subject_id,
        }
        try:
            written = await self._transport.request(
                "POST",
                f"{self._base_url}/settings/set",
                headers={_CAPABILITY_HEADER: capability},
                json={**identity, "value": setting.value},
            )
        except httpx.HTTPError:
            return SettingSeedProof(written=False, failure=SettingsSeedFailureKind.TRANSPORT)
        if not written.is_success:
            return SettingSeedProof(written=False, failure=_set_refusal(written))
        return await self._resolve(setting, identity)

    async def _resolve(self, setting: InitialSetting, identity: dict[str, Any]) -> SettingSeedProof:
        try:
            read = await self._transport.request(
                "POST", f"{self._base_url}/settings/get", json=identity
            )
        except httpx.HTTPError:
            return SettingSeedProof(written=False, failure=SettingsSeedFailureKind.TRANSPORT)
        if not read.is_success:
            return SettingSeedProof(
                written=False, failure=SettingsSeedFailureKind.READBACK_REJECTED
            )
        try:
            payload = read.json()
        except (TypeError, ValueError):
            return SettingSeedProof(
                written=False, failure=SettingsSeedFailureKind.MALFORMED_READBACK
            )
        if not isinstance(payload, dict) or "value" not in payload:
            return SettingSeedProof(
                written=False, failure=SettingsSeedFailureKind.MALFORMED_READBACK
            )
        if (
            payload.get("key") != setting.key
            or payload.get("scope") != setting.scope.value
            or payload.get("subject_id") != setting.subject_id
        ):
            # An answer about another key, scope or subject proves nothing
            # about this one, whatever it says.
            return SettingSeedProof(
                written=False, failure=SettingsSeedFailureKind.MALFORMED_READBACK
            )
        if payload["value"] != setting.value:
            return SettingSeedProof(
                written=False, failure=SettingsSeedFailureKind.READBACK_MISMATCH
            )
        return SettingSeedProof(written=True)


def _set_refusal(response: httpx.Response) -> SettingsSeedFailureKind:
    """Classify only a documented core refusal as deterministic.

    Both an undeclared manifest key and a missing generated route answer 404.
    The former has one exact released contract body; anything else means the
    product did not demonstrate its settings core and must stay on the
    fail-closed path.
    """
    if (
        response.status_code == HTTPStatus.NOT_FOUND
        and _response_detail(response) == CORE_SETTINGS_V1_UNDECLARED_KEY_DETAIL
    ):
        return SettingsSeedFailureKind.KEY_NOT_DECLARED
    if (
        response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        and _response_detail(response) == CORE_SETTINGS_V1_VALUE_REJECTED_DETAIL
    ):
        return SettingsSeedFailureKind.VALUE_REJECTED
    return SettingsSeedFailureKind.SET_REJECTED


def _response_detail(response: httpx.Response) -> str | None:
    """Extract a scalar contract discriminator without retaining the response body."""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) else None
