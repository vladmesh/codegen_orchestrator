"""Fail-closed provider-owned authorization for destructive server operations."""

import os
from typing import Protocol

TIME4VPS_PROVIDER = "time4vps"
BITLAUNCH_PROVIDER = "bitlaunch"
PROVIDER_LABEL = "provider"
TIME4VPS_MANAGED_IDS_ENV = "PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS"


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


BITLAUNCH_ID_LENGTH = 24
_BITLAUNCH_ID_ALPHABET = frozenset("0123456789abcdef")


def parse_bitlaunch_server_id(value: int | str | None) -> str | None:
    """Normalize BitLaunch's opaque 24-character lowercase hex server IDs.

    The provider issues identifiers like `6a920e74c9c98a452507b09b`; they are
    not decimal, and a decimal-only parser rejects every real machine. That is
    not hypothetical: it refused the target of run 33248356742 outright, with
    `TARGET_ID must be a positive decimal BitLaunch ID`, and it silently denied
    the destructive-operation policy for every BitLaunch row before that.

    BitLaunch access provisioning proves a run-owned database row, its labels,
    and the provider's current IP separately. This parser deliberately only
    validates the stable-ID representation; generic destructive operations do
    not gain BitLaunch authority from a parseable ID.
    """
    normalized = parse_provider_id(value)
    if normalized is None or len(normalized) != BITLAUNCH_ID_LENGTH:
        return None
    return normalized if set(normalized) <= _BITLAUNCH_ID_ALPHABET else None


class Time4VPSDestructiveOperationPolicy:
    """Time4VPS may act only on managed rows with explicitly configured IDs."""

    provider = TIME4VPS_PROVIDER

    def normalize_id(self, provider_id: str | int | None) -> str | None:
        return _parse_time4vps_server_id(provider_id)

    def managed_ids(self) -> frozenset[str]:
        raw = os.getenv(TIME4VPS_MANAGED_IDS_ENV)
        if raw is None or not raw.strip():
            return frozenset()

        parsed = [_parse_time4vps_server_id(part.strip()) for part in raw.split(",")]
        if any(provider_id is None for provider_id in parsed):
            raise ValueError(
                f"{TIME4VPS_MANAGED_IDS_ENV} must contain comma-separated positive integers"
            )
        return frozenset(provider_id for provider_id in parsed if provider_id is not None)

    def authorize(self, *, provider_id: str | int | None, is_managed: bool) -> str | None:
        normalized = self.normalize_id(provider_id)
        if not is_managed or normalized is None or normalized not in self.managed_ids():
            return None
        return normalized


STAND_TARGET_LABELS = {"contour": "stand", "stand_role": "target"}


class RunOwnedServerIdentity(Protocol):
    """The server fields that prove one run owns one BitLaunch machine."""

    provider: str | None
    provider_id: str | None
    is_managed: bool
    labels: dict


def authorize_run_owned_target(
    server: RunOwnedServerIdentity, *, run_tag: str | None
) -> str | None:
    """Authorize only this run's exact BitLaunch target, never a general fleet.

    BitLaunch has no configured ID allowlist and cannot have one: its machines
    are created by the run that destroys them minutes later, so their IDs cannot
    be enumerated in advance. The ownership proof is the run tag the contour
    stamped on the row, checked together with the provider identity — which is a
    narrower authority than the Time4VPS allowlist, not a broader one.

    This lives beside the provider policies because both the provisioner and the
    live-harness cleanup have to reach the same verdict about the same row. When
    only the provisioner knew this rule, cleanup fell back to the provider-wide
    policy, found no BitLaunch entry, and refused every target of its own run.
    """
    labels = server.labels if isinstance(server.labels, dict) else {}
    provider_id = parse_bitlaunch_server_id(server.provider_id)
    if (
        server.provider != BITLAUNCH_PROVIDER
        or not server.is_managed
        or provider_id is None
        or labels.get("provider") != BITLAUNCH_PROVIDER
        or labels.get("provider_id") != provider_id
        or not run_tag
        or labels.get("stand_run_tag") != run_tag
        or any(labels.get(key) != value for key, value in STAND_TARGET_LABELS.items())
    ):
        return None
    return provider_id


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
