import pytest

from shared.provisioning_policy import managed_time4vps_server_ids


def test_missing_allowlist_denies_every_server(monkeypatch):
    monkeypatch.delenv("TIME4VPS_MANAGED_SERVER_IDS", raising=False)

    assert managed_time4vps_server_ids() == frozenset()


def test_allowlist_parses_provider_ids(monkeypatch):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", " 1001,2002,1001 ")

    assert managed_time4vps_server_ids() == frozenset({1001, 2002})


@pytest.mark.parametrize("value", ["abc", "1001,,2002", "-1", "0"])
def test_invalid_allowlist_fails_closed(monkeypatch, value):
    monkeypatch.setenv("TIME4VPS_MANAGED_SERVER_IDS", value)

    with pytest.raises(ValueError, match="TIME4VPS_MANAGED_SERVER_IDS"):
        managed_time4vps_server_ids()
