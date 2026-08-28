"""Managed-target admission for final cleanup and write-ahead recovery."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from shared import live_harness_cleanup

pytestmark = pytest.mark.needs_no_api_credential


def _load_clean_live_tests():
    path = Path(__file__).resolve().parents[2] / "scripts" / "clean_live_tests.py"
    spec = importlib.util.spec_from_file_location("clean_live_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _managed_server() -> dict[str, object]:
    return {
        "handle": "vps-allowed",
        "ssh_user": "deploy",
        "public_ip": "203.0.113.7",
        "is_managed": True,
        "provider": "time4vps",
        "provider_id": "1001",
    }


def _unrelated_server() -> dict[str, object]:
    return {
        "handle": "vps-273036",
        "ssh_user": "secretary",
        "public_ip": "203.0.113.8",
        "is_managed": False,
        "provider_id": "273036",
    }


def test_final_cleanup_skips_unrelated_inventory_before_key_or_ssh(monkeypatch):
    module = _load_clean_live_tests()
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    requested: list[str] = []
    ssh_destinations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api/servers/":
            return httpx.Response(200, json=[_managed_server(), _unrelated_server()])
        if request.url.path == "/api/servers/vps-allowed/ssh-key":
            return httpx.Response(200, json={"ssh_key": "PRIVATE-KEY"})
        raise AssertionError(f"unrelated target was requested: {request.url.path}")

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: original_client(*args, **{**kwargs, "transport": transport}),
    )

    def fake_run(argv, **kwargs):
        ssh_destinations.append(argv[argv.index("BatchMode=yes") + 1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.clean_remote_servers(["live-te-" + "1" * 32])

    assert requested == ["/api/servers/", "/api/servers/vps-allowed/ssh-key"]
    assert ssh_destinations == ["deploy@203.0.113.7"] * 3


@pytest.mark.asyncio
async def test_write_ahead_cleanup_skips_unrelated_inventory_before_key_or_ssh(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    requested: list[str] = []
    ssh_destinations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api/servers/":
            return httpx.Response(200, json=[_managed_server(), _unrelated_server()])
        if request.url.path == "/api/servers/vps-allowed/ssh-key":
            return httpx.Response(200, json={"ssh_key": "PRIVATE-KEY"})
        raise AssertionError(f"unrelated target was requested: {request.url.path}")

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        live_harness_cleanup.httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(*args, **{**kwargs, "transport": transport}),
    )
    monkeypatch.setattr(
        live_harness_cleanup.subprocess,
        "run",
        lambda argv, **kwargs: (
            ssh_destinations.append(argv[argv.index("BatchMode=yes") + 1]),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        )[1],
    )
    remote_script = tmp_path / "remote.sh"
    remote_script.write_text("set -eu\n")

    await live_harness_cleanup.cleanup_server_deployment(
        project_name="live-te-x", api_url="http://test", remote_script_path=remote_script
    )

    assert requested == ["/api/servers/", "/api/servers/vps-allowed/ssh-key"]
    assert ssh_destinations == ["deploy@203.0.113.7"]


@pytest.mark.asyncio
async def test_managed_target_without_key_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    ssh_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/servers/":
            return httpx.Response(200, json=[_managed_server()])
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(200, json={"ssh_key": "  "})
        raise AssertionError(f"unexpected request: {request.url.path}")

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        live_harness_cleanup.httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(*args, **{**kwargs, "transport": transport}),
    )

    def fake_run(*args, **kwargs):
        nonlocal ssh_called
        ssh_called = True
        raise AssertionError("SSH must not run without a key")

    monkeypatch.setattr(live_harness_cleanup.subprocess, "run", fake_run)
    remote_script = tmp_path / "remote.sh"
    remote_script.write_text("set -eu\n")

    with pytest.raises(RuntimeError, match="empty ssh_key"):
        await live_harness_cleanup.cleanup_server_deployment(
            project_name="live-te-x", api_url="http://test", remote_script_path=remote_script
        )

    assert ssh_called is False


def test_final_cleanup_fails_when_inventory_has_no_managed_target(monkeypatch):
    module = _load_clean_live_tests()
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[_unrelated_server()]))
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: original_client(*args, **{**kwargs, "transport": transport}),
    )

    with pytest.raises(module.CleanupFailure, match="no managed cleanup target"):
        module.clean_remote_servers([])


def test_final_cleanup_fails_closed_when_managed_target_has_no_key(monkeypatch):
    module = _load_clean_live_tests()
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api/servers/":
            return httpx.Response(200, json=[_managed_server()])
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(200, json={"ssh_key": ""})
        raise AssertionError(f"unexpected request: {request.url.path}")

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: original_client(*args, **{**kwargs, "transport": transport}),
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("SSH must not run without a key"),
    )

    with pytest.raises(module.CleanupFailure, match="empty ssh_key"):
        module.clean_remote_servers([])

    assert requested == ["/api/servers/", "/api/servers/vps-allowed/ssh-key"]


def test_explicit_untrusted_handle_is_not_a_cleanup_target(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_unrelated_server()))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        live_harness_cleanup.httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(*args, **{**kwargs, "transport": transport}),
    )
    remote_script = tmp_path / "remote.sh"
    remote_script.write_text("set -eu\n")

    with pytest.raises(RuntimeError, match="not a managed cleanup target"):
        asyncio.run(
            live_harness_cleanup.cleanup_server_deployment(
                project_name="live-te-x",
                server_handle="vps-273036",
                api_url="http://test",
                remote_script_path=remote_script,
            )
        )
