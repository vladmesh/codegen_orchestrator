from datetime import UTC, datetime

import pytest

from shared.contracts.dto.server import ServerDTO, ServerStatus
from shared.provisioning_policy import (
    authorized_time4vps_server_id,
    managed_time4vps_server_ids,
    parse_time4vps_server_id,
    provider_ip_matches,
    server_is_provisioning_allowed,
)


def test_missing_allowlist_denies_every_server(monkeypatch):
    monkeypatch.delenv("TIME4VPS_MANAGED_SERVER_IDS", raising=False)

    assert managed_time4vps_server_ids() == frozenset()


def test_allowlist_parses_provider_ids(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", " 1001,2002,1001 ")

    assert managed_time4vps_server_ids() == frozenset({1001, 2002})


@pytest.mark.parametrize("value", ["abc", "1001,,2002", "-1", "0", "+1001", "1_001", "١٠٠١"])
def test_invalid_allowlist_fails_closed(monkeypatch, value):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", value)

    with pytest.raises(ValueError, match="TIME4VPS_MANAGED_SERVER_IDS"):
        managed_time4vps_server_ids()


def test_server_requires_both_managed_flag_and_allowlisted_provider_id(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", "1001")
    server = ServerDTO(
        handle="vps-1001",
        host="example",
        public_ip="203.0.113.10",
        ssh_user="root",
        status=ServerStatus.RESERVED,
        provider_id="1001",
        is_managed=True,
        created_at=datetime.now(UTC),
    )

    assert server_is_provisioning_allowed(server) is True
    assert authorized_time4vps_server_id(server) == 1001
    assert server_is_provisioning_allowed(server.model_copy(update={"is_managed": False})) is False
    assert (
        server_is_provisioning_allowed(server.model_copy(update={"provider_id": "2002"})) is False
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1001, 1001), ("1001", 1001), (None, None), ("0", None), ("١٠٠١", None)],
)
def test_provider_id_parser_has_one_strict_definition(value, expected):
    assert parse_time4vps_server_id(value) == expected


@pytest.mark.parametrize("provider_ip", [None, "", "203.0.113.11"])
def test_provider_identity_requires_present_exact_ip(provider_ip):
    assert provider_ip_matches(expected_ip="203.0.113.10", provider_ip=provider_ip) is False


def test_provider_identity_accepts_exact_ip():
    assert provider_ip_matches(expected_ip="203.0.113.10", provider_ip="203.0.113.10") is True
