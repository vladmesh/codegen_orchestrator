"""Managed-target admission for final cleanup and write-ahead recovery."""

import asyncio
import importlib.util
from pathlib import Path
import subprocess
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


def _server_transport(monkeypatch) -> list[str]:
    """Answer the server and ssh-key reads a target snapshot needs."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api/servers/":
            return httpx.Response(200, json=[_managed_server()])
        if request.url.path == "/api/servers/vps-allowed/ssh-key":
            return httpx.Response(200, json={"ssh_key": "PRIVATE-KEY"})
        raise AssertionError(f"unexpected request: {request.url.path}")

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        live_harness_cleanup.httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(*args, **{**kwargs, "transport": transport}),
    )
    return requested


@pytest.mark.asyncio
async def test_the_target_snapshot_reads_the_same_managed_target_teardown_removes(
    monkeypatch, tmp_path
):
    """One resolution: a snapshot cannot open a host the teardown may not."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    requested = _server_transport(monkeypatch)
    destinations: list[str] = []

    def fake_run(argv, **kwargs):
        destinations.append(argv[argv.index("BatchMode=yes") + 1])
        return SimpleNamespace(
            returncode=0, stdout="== containers ==\nbackend state=exited\n", out=""
        )

    monkeypatch.setattr(live_harness_cleanup.subprocess, "run", fake_run)
    script = tmp_path / "diagnostics.sh"
    script.write_text("set -u\n")

    snapshot = await live_harness_cleanup.collect_server_diagnostics(
        project_name="live-te-x", api_url="http://test", remote_script_path=script
    )

    assert requested == ["/api/servers/", "/api/servers/vps-allowed/ssh-key"]
    assert destinations == ["deploy@203.0.113.7"]
    assert "server=vps-allowed" in snapshot
    assert "state=exited" in snapshot


@pytest.mark.asyncio
async def test_a_target_that_cannot_be_read_is_named_in_the_snapshot_not_raised(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    _server_transport(monkeypatch)
    monkeypatch.setattr(
        live_harness_cleanup.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=255, stdout="", stderr="ssh: connect to host port 22: Connection refused"
        ),
    )
    script = tmp_path / "diagnostics.sh"
    script.write_text("set -u\n")

    snapshot = await live_harness_cleanup.collect_server_diagnostics(
        project_name="live-te-x", api_url="http://test", remote_script_path=script
    )

    assert "target snapshot unavailable for vps-allowed: ssh exited 255" in snapshot
    assert "Connection refused" in snapshot


def _fake_docker(tmp_path: Path, *, logs_exit: int = 0) -> Path:
    """A docker whose `logs` can be made to fail, on a PATH of its own."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  ps) printf '%s\\n' 'live-te-x-backend state=exited status=Exited (1)';;\n"
        f"  logs) echo 'log read failed' >&2; exit {logs_exit};;\n"
        "  inspect) echo 'status=exited exit_code=1 restarts=0';;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    return bin_dir


def test_the_remote_snapshot_degrades_to_what_it_could_read(tmp_path):
    """One unreadable container must not cost the state of every other one."""
    script = Path(__file__).resolve().parents[2] / "shared" / "live_harness_remote_diagnostics.sh"
    bin_dir = _fake_docker(tmp_path, logs_exit=1)

    result = subprocess.run(
        ["sh", "-s", "--", "live-te-x"],
        input=script.read_text(),
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )

    assert result.returncode == 0
    assert "state=exited" in result.stdout
    assert "status=exited exit_code=1" in result.stdout
    assert "log tail unavailable for live-te-x-backend" in result.stdout


def test_the_remote_snapshot_refuses_an_unsafe_project_name(tmp_path):
    script = Path(__file__).resolve().parents[2] / "shared" / "live_harness_remote_diagnostics.sh"

    result = subprocess.run(
        ["sh", "-s", "--", "live-te-x; rm -rf /"],
        input=script.read_text(),
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": f"{_fake_docker(tmp_path)}:/usr/bin:/bin"},
    )

    assert result.returncode == 1
    assert "unsafe project name" in result.stderr
