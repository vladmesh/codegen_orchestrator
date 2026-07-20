"""Regression for live cleanup SSHing as root instead of Server.ssh_user
(codegen_orchestrator-555).

The orchestrator SSH key is authorized for the server's configured ``ssh_user``
(``dev`` on 5vei), not ``root``. Teardown that hardcoded ``root@<ip>`` failed
``Permission denied (publickey)`` and left deployed stacks/dirs on the target.

Both teardown paths must build the SSH target from ``ssh_user``:
- ``shared.live_harness_cleanup.cleanup_server_deployment`` (live harness module)
- ``scripts/clean_live_tests.clean_remote_servers`` (standalone sweep)

These run without a live stack: the generated in-container script is executed
with an httpx MockTransport, and the standalone sweep's DB read and ssh
subprocess are captured at their boundaries.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from shared import live_harness_cleanup


def _load_clean_live_tests():
    path = Path(__file__).resolve().parents[2] / "scripts" / "clean_live_tests.py"
    spec = importlib.util.spec_from_file_location("clean_live_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── live harness: cleanup_server_deployment ──────────────────────────────


def test_pipeline_cleanup_command_has_no_hardcoded_root():
    argv = live_harness_cleanup.build_remote_cleanup_command("live-test-x").split()
    # No hardcoded root@ SSH target — the failure this card fixes.
    assert "root@" not in " ".join(argv)
    assert argv[:3] == ["sh", "-s", "--"]
    assert "live-test-x" in argv


@pytest.mark.asyncio
async def test_pipeline_cleanup_ssh_target_uses_server_ssh_user(monkeypatch, tmp_path):
    """Execute the cleanup module: it must SSH as the DTO's ssh_user."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(live_harness_cleanup.subprocess, "run", fake_run)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Internal-Key") != "test-internal-key":
            return httpx.Response(401, json={"detail": "unauthorized"})
        if request.url.path == "/api/servers/vps-1":
            return httpx.Response(200, json={"handle": "vps-1", "ssh_user": "dev"})
        if request.url.path == "/api/servers/vps-1/ssh-key":
            return httpx.Response(200, json={"ssh_key": "PRIVATE-KEY"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(live_harness_cleanup.httpx, "AsyncClient", client_factory)

    remote_script = tmp_path / "remote.sh"
    remote_script.write_text("set -eu\n")
    await live_harness_cleanup.cleanup_server_deployment(
        project_name="live-test-x",
        server_ip="203.0.113.7",
        server_handle="vps-1",
        api_url="http://test",
        remote_script_path=remote_script,
    )

    argv = captured["argv"]
    assert "dev@203.0.113.7" in argv
    assert not any(str(a).startswith("root@") for a in argv)
    assert captured["input"] == "set -eu\n"


@pytest.mark.asyncio
async def test_pipeline_cleanup_fails_closed_on_missing_server(monkeypatch, tmp_path):
    """A gone/unreachable server surfaces as an error, not a silent success."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    def fake_run(argv, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("SSH must not run when the server fetch fails")

    monkeypatch.setattr(live_harness_cleanup.subprocess, "run", fake_run)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(live_harness_cleanup.httpx, "AsyncClient", client_factory)

    remote_script = tmp_path / "remote.sh"
    remote_script.write_text("set -eu\n")
    with pytest.raises(RuntimeError, match="server fetch failed"):
        await live_harness_cleanup.cleanup_server_deployment(
            project_name="live-test-x",
            server_ip="203.0.113.7",
            server_handle="vps-1",
            api_url="http://test",
            remote_script_path=remote_script,
        )


# ── standalone sweep: clean_remote_servers ───────────────────────────────


def test_clean_remote_servers_ssh_targets_use_ssh_user(monkeypatch):
    module = _load_clean_live_tests()

    server_rows = [
        {"handle": "vps-1", "public_ip": "203.0.113.7", "ssh_user": "dev"},
        {"handle": "vps-2", "public_ip": "203.0.113.8", "ssh_user": "runner"},
    ]
    keys = {"vps-1": "KEY-A", "vps-2": "KEY-B"}

    monkeypatch.setattr(module, "_fetch_remote_servers", lambda: server_rows)
    monkeypatch.setattr(module, "_fetch_remote_server_key", lambda handle: keys[handle])

    targets: list[str] = []
    remote_commands: list[str] = []
    remote_inputs: list[str | None] = []

    def fake_run(argv, **kwargs):
        # The SSH destination is the sole non-flag, non-command positional.
        targets.append(argv[argv.index("BatchMode=yes") + 1])
        remote_commands.append(argv[-1])
        remote_inputs.append(kwargs.get("input"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.clean_remote_servers(["live-te-11111111111111111111111111111111"])

    assert targets == [
        "dev@203.0.113.7",
        "dev@203.0.113.7",
        "runner@203.0.113.8",
        "runner@203.0.113.8",
    ]
    assert not any(t.startswith("root@") for t in targets)
    assert remote_commands == [
        "sh -s -- live-te-11111111111111111111111111111111 /opt/services",
        "docker network prune -f 2>&1 || true",
        "sh -s -- live-te-11111111111111111111111111111111 /opt/services",
        "docker network prune -f 2>&1 || true",
    ]
    assert remote_inputs[0] == live_harness_cleanup.REMOTE_CLEANUP_SCRIPT.read_text()
    assert remote_inputs[2] == live_harness_cleanup.REMOTE_CLEANUP_SCRIPT.read_text()
    assert remote_inputs[1] is None
    assert remote_inputs[3] is None
