"""Tests for the Stand one-shot scheduler health probe."""

import asyncio
import os

os.environ.setdefault("HEALTH_CHECK_INTERVAL", "60")

import pytest

from src import stand_health_probe


@pytest.mark.asyncio
async def test_probe_initializes_config_before_loading_and_checking_server(monkeypatch):
    events: list[str] = []
    server = object()

    class Config:
        async def refresh_forever(self):
            events.append("refreshing")
            await asyncio.Event().wait()

    async def init_config(required_keys):
        assert required_keys == stand_health_probe.startup.INFRASTRUCTURE_REQUIRED_KEYS
        events.append("init_config")
        return Config()

    async def get_server(target_handle: str):
        await asyncio.sleep(0)
        events.append(f"get_server:{target_handle}")
        return server

    async def check_server(received_server):
        assert received_server is server
        events.append("check_server")

    monkeypatch.setattr(stand_health_probe.startup, "init_config", init_config)
    monkeypatch.setattr(stand_health_probe.api_client, "get_server", get_server)
    monkeypatch.setattr(stand_health_probe, "_check_server", check_server)

    await stand_health_probe.probe("bitlaunch-target")

    assert events == ["init_config", "refreshing", "get_server:bitlaunch-target", "check_server"]


def test_main_requires_target_handle(monkeypatch):
    monkeypatch.delenv("TARGET_HANDLE", raising=False)

    with pytest.raises(RuntimeError, match="TARGET_HANDLE is not set"):
        stand_health_probe.main()
