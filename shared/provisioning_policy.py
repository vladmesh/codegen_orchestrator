"""Fail-closed provider-owned authorization for destructive server operations."""

import os
from typing import Protocol

TIME4VPS_PROVIDER = "time4vps"
PROVIDER_LABEL = "provider"
_TIME4VPS_MANAGED_IDS_ENV = "PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS"


class DestructiveOperationPolicy(Protocol):
    """One provider's stable-ID authorization contract."""

    provider: str

    def normalize_id(self, provider_id: str | int | None) -> str | None: ...

    def managed_ids(self) -> frozenset[str]: ...

    def authorize(self, *, provider_id: str | int | None, is_managed: bool) -> str | None: ...


def parse_provider_id(value: int | str | None) -> str | None:
    """Normalize a present provider ID without guessing its provider-specific format."""
    if value is None:
        return None
    normalized = str(value)
    return normalized if normalized and normalized.strip() == normalized else None


def _parse_time4vps_server_id(value: int | str | None) -> str | None:
    """Normalize Time4VPS's positive ASCII-decimal stable IDs."""
    normalized = parse_provider_id(value)
    if normalized is None or not normalized.isascii() or not normalized.isdecimal():
        return None
    return normalized if int(normalized) > 0 else None


class Time4VPSDestructiveOperationPolicy:
    """Time4VPS may act only on managed rows with explicitly configured IDs."""

    provider = TIME4VPS_PROVIDER

    def normalize_id(self, provider_id: str | int | None) -> str | None:
        return _parse_time4vps_server_id(provider_id)

    def managed_ids(self) -> frozenset[str]:
        raw = os.getenv(_TIME4VPS_MANAGED_IDS_ENV)
        if raw is None or not raw.strip():
            return frozenset()

        parsed = [_parse_time4vps_server_id(part.strip()) for part in raw.split(",")]
        if any(provider_id is None for provider_id in parsed):
            raise ValueError(
                f"{_TIME4VPS_MANAGED_IDS_ENV} must contain comma-separated positive integers"
            )
        return frozenset(provider_id for provider_id in parsed if provider_id is not None)

    def authorize(self, *, provider_id: str | int | None, is_managed: bool) -> str | None:
        normalized = self.normalize_id(provider_id)
        if not is_managed or normalized is None or normalized not in self.managed_ids():
            return None
        return normalized


_POLICIES: dict[str, DestructiveOperationPolicy] = {
    TIME4VPS_PROVIDER: Time4VPSDestructiveOperationPolicy(),
}


def managed_provider_ids(provider: str | None) -> frozenset[str]:
    """Return one provider's configured managed IDs, or no IDs for an unknown provider."""
    policy = _POLICIES.get(provider or "")
    return policy.managed_ids() if policy is not None else frozenset()


def normalize_provider_id(provider: str | None, provider_id: str | int | None) -> str | None:
    """Validate one stable ID with its provider's format without authorizing it."""
    policy = _POLICIES.get(provider or "")
    return policy.normalize_id(provider_id) if policy is not None else None


def authorized_provider_id(
    *, provider: str | None, provider_id: str | int | None, is_managed: bool
) -> str | None:
    """Return a provider-authorized stable ID, denying absent or unknown identities."""
    policy = _POLICIES.get(provider or "")
    if policy is None:
        return None
    return policy.authorize(provider_id=provider_id, is_managed=is_managed)


def provider_operation_is_authorized(
    *, provider: str | None, provider_id: str | int | None, is_managed: bool
) -> bool:
    """Apply the complete provider-owned destructive-operation admission policy."""
    return (
        authorized_provider_id(provider=provider, provider_id=provider_id, is_managed=is_managed)
        is not None
    )


def validate_provider_policies() -> None:
    """Fail startup on malformed provider policy configuration."""
    for policy in _POLICIES.values():
        policy.managed_ids()


def provider_ip_matches(*, expected_ip: str, provider_ip: str | None) -> bool:
    """Require a provider IP and an exact match to the authorized database target."""
    return bool(provider_ip) and provider_ip == expected_ip
