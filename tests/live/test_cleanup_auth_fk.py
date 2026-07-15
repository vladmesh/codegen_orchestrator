"""Regression for live cleanup auth + FK-order failures (codegen_orchestrator-550).

Reproduces the three teardown failures a live mega run hit:
1. server ssh-key fetch -> 401 (unauthenticated /api/servers/*)
2. port allocation lookup -> 401 (same auth-gated endpoint, no internal key)
3. database project delete -> FK violation (applications removed before its
   dependent rows in application_health_history / service_deployments /
   port_allocations).

These run without a live stack: HTTP is driven through MockTransport and the
SQL paths are captured at the subprocess boundary.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
from live_harness import OwnershipManifest
import pipeline_helpers
from pipeline_helpers import build_server_cleanup_script
import pytest


def _load_clean_live_tests():
    path = Path(__file__).resolve().parents[2] / "scripts" / "clean_live_tests.py"
    spec = importlib.util.spec_from_file_location("clean_live_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_before(sql: str, earlier: str, later: str) -> None:
    e = sql.find(f"DELETE FROM {earlier}")
    ln = sql.find(f"DELETE FROM {later}")
    assert e != -1, f"missing DELETE FROM {earlier}"
    assert ln != -1, f"missing DELETE FROM {later}"
    assert e < ln, f"DELETE {earlier} must precede DELETE {later}"


# ── ssh-key fetch is authenticated (bug #1) ──────────────────────────────


def test_server_cleanup_script_authenticates_ssh_key_fetch():
    script = build_server_cleanup_script("live-test-x", "203.0.113.7", "vps-1")
    # ssh-key fetch must carry the internal key like the real consumers.
    assert "X-Internal-Key" in script
    assert "os.environ['INTERNAL_API_KEY']" in script
    # The internal header must be attached to the client that fetches the key.
    assert "/api/servers/vps-1/ssh-key" in script
    assert "headers=headers" in script


# ── port allocation lookup is authenticated (bug #2) ─────────────────────


@pytest.mark.asyncio
async def test_port_allocation_lookup_uses_internal_auth(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    ports_calls: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.startswith("/api/allocations/"):
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/api/servers/vps-1/ports":
            has_key = request.headers.get("X-Internal-Key") == "test-internal-key"
            ports_calls.append(has_key)
            # The real endpoint 401s without the internal key.
            if not has_key:
                return httpx.Response(401, json={"detail": "unauthorized"})
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/api/projects/project-1":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    # Isolate the port-allocation branch: stub every other cleanup step.
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_scaffold", lambda ctx: None)

    async def _noop_async(*args, **kwargs):
        return []

    monkeypatch.setattr(pipeline_helpers, "cancel_owned_runs", _noop_async)
    monkeypatch.setattr(pipeline_helpers, "wait_for_owned_runs", _noop_async)
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_active_work", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_owned_capability_work", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_server_container", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_owned_workers", lambda ctx, errors: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_registry_resources", lambda ctx, errors: None)
    monkeypatch.setattr(pipeline_helpers, "_cleanup_db", lambda project_id: None)

    manifest = OwnershipManifest("project-1")
    manifest.own("project", "project-1")
    ctx = {
        "project_id": "project-1",
        "manifest": manifest,
        "allocation_id": 42,
        "server_handle": "vps-1",
    }

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        # No CleanupError: the ports lookup carried the internal key and got 200.
        await pipeline_helpers.cleanup_all(api, api, ctx)

    assert ports_calls == [True]


# ── database delete order respects FK constraints (bug #3) ────────────────

_DEPENDENTS = ["application_health_history", "service_deployments", "port_allocations"]


def test_cleanup_db_deletes_dependents_before_applications(monkeypatch):
    captured: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        captured["sql"] = argv[argv.index("-c") + 1]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pipeline_helpers.subprocess, "run", fake_run)

    pipeline_helpers._cleanup_db("11111111-1111-1111-1111-111111111111")

    sql = captured["sql"]
    for dependent in _DEPENDENTS:
        _assert_before(sql, dependent, "applications")
    _assert_before(sql, "applications", "repositories")
    _assert_before(sql, "repositories", "projects")


def test_clean_live_tests_deletes_dependents_before_applications(monkeypatch):
    module = _load_clean_live_tests()
    captured: dict[str, str] = {}

    def fake_run_cmd(cmd, **kwargs):
        if "-c" in cmd:
            captured["sql"] = cmd[cmd.index("-c") + 1]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module, "run_cmd", fake_run_cmd)

    module.clean_database()

    sql = captured["sql"]
    for dependent in _DEPENDENTS:
        _assert_before(sql, dependent, "applications")
    _assert_before(sql, "applications", "repositories")
    _assert_before(sql, "repositories", "projects")
