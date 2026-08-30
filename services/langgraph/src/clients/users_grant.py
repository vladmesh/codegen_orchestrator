"""Narrow client for the generated service's permanent grant capability."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx


class GrantFailureKind(StrEnum):
    """Bounded, credential-safe reasons a grant proof did not complete."""

    TRANSPORT = "transport"
    GRANT_REJECTED = "grant_rejected"
    ACCESS_REJECTED = "access_rejected"
    MALFORMED_ACCESS = "malformed_access"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class GrantProof:
    """The result of granting one identity and reading its access back."""

    active: bool
    failure: GrantFailureKind | None = None


class _RequestClient(Protocol):
    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response: ...


class GeneratedServiceGrantClient:
    """Call only the generated service's documented grant and access endpoints."""

    def __init__(self, base_url: str, *, transport: _RequestClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport or httpx.AsyncClient(timeout=15.0, follow_redirects=False)

    async def grant_and_resolve(
        self, *, channel: str, external_id: str, capability: str
    ) -> GrantProof:
        """Grant an identity, then require the service to report it active.

        The capability only crosses this in-memory call boundary as a request
        header. It is never put in a URL, error, callback, or event.
        """
        try:
            granted = await self._transport.request(
                "POST",
                f"{self._base_url}/users/grant",
                headers={"X-Grant-Capability": capability},
                json={"channel": channel, "external_id": external_id},
            )
        except httpx.HTTPError:
            return GrantProof(active=False, failure=GrantFailureKind.TRANSPORT)
        if not granted.is_success:
            return GrantProof(active=False, failure=GrantFailureKind.GRANT_REJECTED)
        try:
            access = await self._transport.request(
                "GET",
                f"{self._base_url}/users/access",
                params={"channel": channel, "external_id": external_id},
            )
        except httpx.HTTPError:
            return GrantProof(active=False, failure=GrantFailureKind.TRANSPORT)
        if not access.is_success:
            return GrantProof(active=False, failure=GrantFailureKind.ACCESS_REJECTED)
        try:
            payload = access.json()
        except (TypeError, ValueError):
            return GrantProof(active=False, failure=GrantFailureKind.MALFORMED_ACCESS)
        if not isinstance(payload, dict) or (
            payload.get("channel") != channel or payload.get("external_id") != external_id
        ):
            return GrantProof(active=False, failure=GrantFailureKind.MALFORMED_ACCESS)
        if payload.get("active") is not True:
            return GrantProof(active=False, failure=GrantFailureKind.INACTIVE)
        return GrantProof(active=True)
