"""Regression for live cleanup SSHing as root instead of Server.ssh_user
(codegen_orchestrator-555).

The orchestrator SSH key is authorized for the server's configured ``ssh_user``
(``dev`` on 5vei), not ``root``. Teardown that hardcoded ``root@<ip>`` failed
``Permission denied (publickey)`` and left deployed stacks/dirs on the target.

Both teardown paths must build the SSH target from ``ssh_user``:
- ``pipeline_helpers.build_server_cleanup_script`` (live harness, runs in-container)
- ``scripts/clean_live_tests.clean_remote_servers`` (standalone sweep)

These run without a live stack: the generated in-container script is executed
with an httpx MockTransport, and the standalone sweep's DB read and ssh
subprocess are captured at their boundaries.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
from pipeline_helpers import build_server_cleanup_script


def _load_clean_live_tests():
    path = Path(__file__).resolve().parents[2] / "scripts" / "clean_live_tests.py"
    spec = importlib.util.spec_from_file_location("clean_live_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── live harness: build_server_cleanup_script ────────────────────────────


def test_pipeline_cleanup_script_has_no_hardcoded_root():
    script = build_server_cleanup_script("live-test-x", "203.0.113.7", "vps-1")
    # No hardcoded root@ SSH target — the failure this card fixes.
    assert "root@" not in script
    # The user comes from the server DTO, fetched with the internal key.
    assert "/api/servers/vps-1" in script
    assert "ssh_user = srv.json()['ssh_user']" in script
    assert "ssh_user + '@203.0.113.7'" in script


def test_pipeline_cleanup_script_ssh_target_uses_server_ssh_user(monkeypatch):
    """Execute the generated script: it must SSH as the DTO's ssh_user."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

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

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    script = build_server_cleanup_script("live-test-x", "203.0.113.7", "vps-1")
    exec(script, {"__name__": "__cleanup__"})  # noqa: S102 - run the generated teardown

    argv = captured["argv"]
    assert "dev@203.0.113.7" in argv
    assert not any(str(a).startswith("root@") for a in argv)


def test_pipeline_cleanup_script_fails_closed_on_missing_server(monkeypatch):
    """A gone/unreachable server surfaces as an error, not a silent success."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    def fake_run(argv, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("SSH must not run when the server fetch fails")

    monkeypatch.setattr("subprocess.run", fake_run)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    script = build_server_cleanup_script("live-test-x", "203.0.113.7", "vps-1")
    try:
        exec(script, {"__name__": "__cleanup__"})  # noqa: S102 - run the generated teardown
    except RuntimeError as exc:
        assert "server fetch failed" in str(exc)
    else:
        raise AssertionError("cleanup must fail closed on a missing server")


# ── standalone sweep: clean_remote_servers ───────────────────────────────


def test_clean_remote_servers_ssh_targets_use_ssh_user(monkeypatch):
    module = _load_clean_live_tests()

    server_rows = [
        {"handle": "vps-1", "ip": "203.0.113.7", "key": "KEY-A", "ssh_user": "dev"},
        {"handle": "vps-2", "ip": "203.0.113.8", "key": "KEY-B", "ssh_user": "runner"},
    ]

    def fake_run_cmd(cmd, **kwargs):
        # Only the servers query goes through run_cmd here.
        return SimpleNamespace(returncode=0, stdout=module.json.dumps(server_rows), stderr="")

    monkeypatch.setattr(module, "run_cmd", fake_run_cmd)

    targets: list[str] = []

    def fake_run(argv, **kwargs):
        # The SSH destination is the sole non-flag, non-command positional.
        targets.append(argv[argv.index("BatchMode=yes") + 1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.clean_remote_servers()

    assert targets == ["dev@203.0.113.7", "runner@203.0.113.8"]
    assert not any(t.startswith("root@") for t in targets)
