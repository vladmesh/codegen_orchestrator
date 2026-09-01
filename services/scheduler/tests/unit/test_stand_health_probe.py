"""Tests for the Stand one-shot scheduler health probe."""

import os

os.environ.setdefault("HEALTH_CHECK_INTERVAL", "60")

import pytest

from src import stand_health_probe


@pytest.mark.asyncio
async def test_probe_initializes_config_before_loading_and_checking_server(monkeypatch):
    events: list[str] = []
    server = object()

    def init_config():
        events.append("init_config")

    async def get_server(target_handle: str):
        events.append(f"get_server:{target_handle}")
        return server

    async def check_server(received_server):
        assert received_server is server
        events.append("check_server")

    monkeypatch.setattr(stand_health_probe.startup, "init_config", init_config)
    monkeypatch.setattr(stand_health_probe.api_client, "get_server", get_server)
    monkeypatch.setattr(stand_health_probe, "_check_server", check_server)

    await stand_health_probe.probe("bitlaunch-target")

    assert events == ["init_config", "get_server:bitlaunch-target", "check_server"]


def test_main_requires_target_handle(monkeypatch):
    monkeypatch.delenv("TARGET_HANDLE", raising=False)

    with pytest.raises(RuntimeError, match="TARGET_HANDLE is not set"):
        stand_health_probe.main()
