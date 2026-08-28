import pytest

from shared.provisioning_policy import (
    TIME4VPS_PROVIDER,
    authorized_provider_id,
    managed_provider_ids,
    provider_ip_matches,
    provider_operation_is_authorized,
    validate_provider_policies,
)

_TIME4VPS_POLICY_ENV = "PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS"


def test_missing_provider_policy_denies_every_server(monkeypatch):
    monkeypatch.delenv(_TIME4VPS_POLICY_ENV, raising=False)

    assert managed_provider_ids(TIME4VPS_PROVIDER) == frozenset()
    assert (
        provider_operation_is_authorized(
            provider=TIME4VPS_PROVIDER, provider_id="1001", is_managed=True
        )
        is False
    )


@pytest.mark.parametrize("value", ["abc", "1001,,2002", "-1", "0", "+1001", "1_001", "١٠٠١"])
def test_malformed_provider_policy_fails_closed(monkeypatch, value):
    monkeypatch.setenv(_TIME4VPS_POLICY_ENV, value)

    with pytest.raises(ValueError, match=_TIME4VPS_POLICY_ENV):
        validate_provider_policies()


def test_time4vps_policy_requires_explicit_identity_managed_row_and_allowed_stable_id(monkeypatch):
    monkeypatch.setenv(_TIME4VPS_POLICY_ENV, "1001")

    assert (
        authorized_provider_id(provider=TIME4VPS_PROVIDER, provider_id="1001", is_managed=True)
        == "1001"
    )
    assert authorized_provider_id(provider=None, provider_id="1001", is_managed=True) is None
    assert authorized_provider_id(provider="unknown", provider_id="1001", is_managed=True) is None
    assert (
        authorized_provider_id(provider=TIME4VPS_PROVIDER, provider_id="1001", is_managed=False)
        is None
    )
    assert (
        authorized_provider_id(provider=TIME4VPS_PROVIDER, provider_id="2002", is_managed=True)
        is None
    )


@pytest.mark.parametrize("value", ["0", "١٠٠١", " 1001", "1001 "])
def test_time4vps_rejects_malformed_stable_ids(monkeypatch, value):
    monkeypatch.setenv(_TIME4VPS_POLICY_ENV, "1001")

    assert (
        authorized_provider_id(provider=TIME4VPS_PROVIDER, provider_id=value, is_managed=True)
        is None
    )


@pytest.mark.parametrize("provider_ip", [None, "", "203.0.113.11"])
def test_provider_identity_requires_present_exact_ip(provider_ip):
    assert provider_ip_matches(expected_ip="203.0.113.10", provider_ip=provider_ip) is False


def test_provider_identity_accepts_exact_ip():
    assert provider_ip_matches(expected_ip="203.0.113.10", provider_ip="203.0.113.10") is True
