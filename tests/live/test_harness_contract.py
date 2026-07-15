import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from capability_cleanup import cleanup_owned_capability_messages
import conftest as live_conftest
from conftest import create_test_project_context
import httpx
from live_harness import (
    CleanupError,
    OwnershipManifest,
    cleanup_guard,
    resolve_repo_root,
    run_non_llm_qa,
)
import pipeline_helpers
from pipeline_helpers import build_github_cleanup_script, build_registry_cleanup_script
import pytest


def test_repo_root_is_derived_from_harness_location(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    harness = root / "tests" / "live" / "live_harness.py"
    harness.parent.mkdir(parents=True)
    (root / "pyproject.toml").touch()
    harness.touch()

    monkeypatch.delenv("ORCHESTRATOR_ROOT", raising=False)

    assert resolve_repo_root(harness) == root.resolve()


def test_repo_root_override_must_identify_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCHESTRATOR_ROOT", str(tmp_path))

    with pytest.raises(RuntimeError, match="pyproject.toml"):
        resolve_repo_root(Path(__file__))


def test_manifest_cleanup_is_fail_closed_and_verifies_residue():
    manifest = OwnershipManifest(run_id="run-1")
    manifest.own("github_repository", "org/repo")
    calls = []

    def delete(resource):
        calls.append(("delete", resource.identifier))

    def exists(resource):
        calls.append(("exists", resource.identifier))
        return True

    with pytest.raises(CleanupError, match="still exists"):
        manifest.teardown(delete=delete, exists=exists)

    assert calls == [("delete", "org/repo"), ("exists", "org/repo")]


def test_manifest_reports_delete_failure_without_skipping_verification():
    manifest = OwnershipManifest(run_id="run-1")
    manifest.own("project", "project-1")
    verified = []

    def delete(resource):
        raise RuntimeError("delete failed")

    def exists(resource):
        verified.append(resource.identifier)
        return False

    with pytest.raises(CleanupError, match="delete failed"):
        manifest.teardown(delete=delete, exists=exists)

    assert verified == ["project-1"]


def test_github_cleanup_script_is_valid_python():
    script = build_github_cleanup_script("owned-repository")

    compile(script, "<github-cleanup>", "exec")
    assert "project-factory-organization/owned-repository" in script


def test_registry_cleanup_script_deletes_only_owned_repository_tags_and_manifests():
    script = build_registry_cleanup_script("project-factory-organization/owned-repository-backend")

    compile(script, "<registry-cleanup>", "exec")
    assert "repository = 'project-factory-organization/owned-repository-backend'" in script
    assert "f'{base}/v2/{repository}/tags/list'" in script
    assert "Docker-Content-Digest" in script
    assert "/v2/_catalog" not in script


def test_compose_routes_public_registry_hostname_to_internal_caddy():
    compose = (pipeline_helpers.ORCHESTRATOR_ROOT / "docker-compose.yml").read_text()
    caddy = compose.split("  caddy:\n", 1)[1].split("\n  registry:\n", 1)[0]

    assert "aliases:" in caddy
    assert "- ${ORCHESTRATOR_HOSTNAME}" in caddy


def test_registry_cleanup_script_uses_https_for_bare_registry_host(monkeypatch):
    requested_urls = []

    class Response:
        status_code = 404

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            requested_urls.append(url)
            return Response()

    monkeypatch.setenv("ORCHESTRATOR_HOSTNAME", "registry.example.com")
    monkeypatch.setenv("REGISTRY_USER", "user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "password")
    monkeypatch.setattr(pipeline_helpers.httpx, "AsyncClient", lambda **kwargs: Client())

    exec(  # noqa: S102
        build_registry_cleanup_script("project-factory-organization/owned-repository-backend"),
        {},
    )

    assert requested_urls == [
        "https://registry.example.com/v2/"
        "project-factory-organization/owned-repository-backend/tags/list"
    ]


def test_db_cleanup_follows_port_allocation_application_relation(monkeypatch):
    executed = []

    def run(*args, **kwargs):
        executed.append(args[0][-1])
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(pipeline_helpers.subprocess, "run", run)
    pipeline_helpers._cleanup_db("project-1")

    sql = executed[0]
    assert "port_allocations WHERE application_id IN" in sql
    assert "port_allocations WHERE project_id" not in sql
    assert sql.index("DELETE FROM port_allocations") < sql.index("DELETE FROM applications")


@pytest.mark.asyncio
async def test_wait_deploy_uses_application_owned_port_allocation(monkeypatch, tmp_path):
    manifest = OwnershipManifest("project-1")
    ctx = {"project_id": "project-1", "project_name": "run", "manifest": manifest}
    responses = {
        "/api/repositories/": [{"id": "repo-1"}],
        "/api/applications/": [{"id": 21, "status": "running"}],
        "/api/servers/": [{"handle": "server-1", "public_ip": "192.0.2.1"}],
        "/api/servers/server-1/ports": [
            {"id": 8, "port": 8010, "application_id": 21},
            {"id": 9, "port": 8011, "application_id": 99},
        ],
    }

    async def get(url, **kwargs):
        return httpx.Response(200, json=responses[url])

    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    api = SimpleNamespace(get=get)
    await pipeline_helpers.wait_deploy(api, api, ctx, timeout=1)

    assert ctx["allocation_id"] == 8
    assert ctx["port"] == 8010


@pytest.mark.asyncio
async def test_wait_deploy_reads_servers_with_authenticated_client(monkeypatch, tmp_path):
    """Regression: /api/servers/ is auth-only.

    Routing it through api_no_auth returns a 401 error body ({"detail": ...}),
    and iterating that dict raises `TypeError: string indices must be integers`
    before the deploy is registered in the ownership manifest. wait_deploy must
    use the X-Telegram-ID client so server_deployment reaches the manifest.
    """
    manifest = OwnershipManifest("project-1")
    ctx = {"project_id": "project-1", "project_name": "run", "manifest": manifest}
    auth_responses = {
        "/api/repositories/": [{"id": "repo-1"}],
        "/api/applications/": [{"id": 21, "status": "running"}],
        "/api/servers/": [{"handle": "server-1", "public_ip": "192.0.2.1"}],
        "/api/servers/server-1/ports": [{"id": 8, "port": 8010, "application_id": 21}],
    }

    async def auth_get(url, **kwargs):
        return httpx.Response(200, json=auth_responses[url])

    no_auth_calls = []

    async def no_auth_get(url, **kwargs):
        no_auth_calls.append(url)
        # What the real unauthenticated client returns for auth-only endpoints.
        return httpx.Response(401, json={"detail": "Authentication required"})

    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    api = SimpleNamespace(get=auth_get)
    api_no_auth = SimpleNamespace(get=no_auth_get)

    await pipeline_helpers.wait_deploy(api, api_no_auth, ctx, timeout=1)

    assert no_auth_calls == []
    assert ctx["server_ip"] == "192.0.2.1"
    assert ctx["port"] == 8010
    assert ctx["allocation_id"] == 8
    assert ctx["application_id"] == 21
    assert ctx["server_handle"] == "server-1"
    assert ctx["deployed_url"] == "http://192.0.2.1:8010"

    owned = {(resource.kind, resource.identifier) for resource in manifest.resources}
    assert ("server_deployment", "run") in owned
    assert ("port_allocation", "8") in owned
    written = json.loads((tmp_path / ".live-manifests" / f"{manifest.run_id}.json").read_text())
    kinds = {resource["kind"] for resource in written["resources"]}
    assert {"server_deployment", "port_allocation"} <= kinds


def test_debug_dump_retains_ci_failure_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    monkeypatch.setattr(
        pipeline_helpers.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=""),
    )
    ctx = {
        "project_id": "project-1",
        "ci_failure_evidence": [
            {
                "fix_task_id": "fix-1",
                "run_id": 42,
                "head_sha": "abc123",
                "fingerprint": "f00baa",
                "failed_jobs": [{"name": "unit", "failed_steps": ["pytest"]}],
            }
        ],
    }

    pipeline_helpers.dump_debug(ctx, "ci-evidence")

    artifact = next((tmp_path / "docs" / "e2e_results").glob("debug-ci-evidence-*.md"))
    text = artifact.read_text()
    assert "fix-1" in text
    assert "run_id: `42`" in text
    assert "head_sha: `abc123`" in text
    assert '"failed_steps": ["pytest"]' in text


@pytest.mark.asyncio
async def test_cleanup_guard_runs_when_qa_fails_before_fixture_yield():
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    with pytest.raises(RuntimeError, match="QA failed"):
        async with cleanup_guard(cleanup):
            raise RuntimeError("QA failed")

    assert cleaned == [True]


@pytest.mark.asyncio
async def test_cleanup_guard_preserves_run_and_cleanup_failures():
    async def cleanup():
        raise CleanupError("residue")

    with pytest.raises(BaseExceptionGroup) as caught:
        async with cleanup_guard(cleanup):
            raise RuntimeError("QA failed")

    assert [str(error) for error in caught.value.exceptions] == ["QA failed", "residue"]


@pytest.mark.asyncio
async def test_partial_project_creation_writes_manifest_and_cleans_up(monkeypatch, tmp_path):
    cleanup_contexts = []

    async def cleanup(api, api_no_auth, ctx):
        cleanup_contexts.append(ctx)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/projects/":
            return httpx.Response(201, json={"id": "project"})
        return httpx.Response(500, text="repository unavailable")

    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    monkeypatch.setattr(pipeline_helpers, "cleanup_all", cleanup)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        with pytest.raises(AssertionError, match="Create repository failed"):
            await pipeline_helpers.create_noop_project(api)

    assert len(cleanup_contexts) == 1
    manifest = cleanup_contexts[0]["manifest"]
    assert [(resource.kind, resource.identifier) for resource in manifest.resources] == [
        ("project", cleanup_contexts[0]["project_id"])
    ]
    written = json.loads((tmp_path / ".live-manifests" / f"{manifest.run_id}.json").read_text())
    assert written["resources"] == [{"identifier": manifest.run_id, "kind": "project"}]


@pytest.mark.asyncio
async def test_common_live_project_gets_persisted_manifest(monkeypatch, tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "common-project"})

    monkeypatch.setattr("conftest.ORCHESTRATOR_ROOT", tmp_path)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        data, ctx = await create_test_project_context(api)

    assert data == {"id": "common-project"}
    resource = ctx["manifest"].resources[0]
    assert (resource.kind, resource.identifier) == ("project", ctx["project_id"])
    assert (tmp_path / ".live-manifests" / f"{ctx['project_id']}.json").is_file()


@pytest.mark.asyncio
async def test_common_live_project_fixture_uses_verified_cleanup(monkeypatch, tmp_path):
    cleaned = []

    async def cleanup(api, api_no_auth, ctx):
        cleaned.append(ctx)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "common-project"})

    monkeypatch.setattr(live_conftest, "ORCHESTRATOR_ROOT", tmp_path)
    monkeypatch.setattr(live_conftest, "cleanup_all", cleanup)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        fixture = live_conftest.test_project.__wrapped__(api)
        assert await anext(fixture) == {"id": "common-project"}
        with pytest.raises(StopAsyncIteration):
            await anext(fixture)

    assert len(cleaned) == 1
    assert cleaned[0]["manifest"].resources[0].kind == "project"


def test_scaffold_fence_waits_for_claimed_work_to_finish():
    calls = []
    active = iter(["2", "1", "0"])

    def command(*args):
        calls.append(args)
        return next(active) if args[0] == "EVAL" else "OK"

    pipeline_helpers.cancel_and_wait_for_scaffold(
        "project-1", command=command, timeout=1, poll_interval=0
    )

    assert calls[0] == (
        "SET",
        "live:scaffold:cancelled:project-1",
        "1",
        "EX",
        "900",
    )
    assert len(calls[1:]) == 3
    assert all(call[0] == "EVAL" for call in calls[1:])
    assert all("live:scaffold:leases:project-1" in call for call in calls[1:])


def test_scaffold_fence_makes_unterminated_claim_red():
    def command(*args):
        return "1" if args[0] == "EVAL" else "OK"

    with pytest.raises(CleanupError, match="did not terminate"):
        pipeline_helpers.cancel_and_wait_for_scaffold(
            "project-1", command=command, timeout=0.001, poll_interval=0
        )


def test_active_work_fence_makes_ack_failure_red():
    def command(*args):
        if args[0] == "GET":
            return "ack_failed"
        return "OK"

    with pytest.raises(CleanupError, match="could not settle: ack_failed"):
        pipeline_helpers.cancel_and_wait_for_active_work(
            "project-1", command=command, timeout=0.001, poll_interval=0
        )


@pytest.mark.asyncio
async def test_unproven_workflow_cancellation_marker_fences_external_cleanup(monkeypatch):
    """A workflow_cancellation_unproven fence must stop cleanup before GitHub deletion."""
    manifest = OwnershipManifest("project-1")
    manifest.own("project", "project-1")
    manifest.own("github_repository", "org/repo")
    github_cleanup = []

    def command(*args):
        if args[0] == "GET" and args[1].startswith("live:work:failed:"):
            return "workflow_cancellation_unproven"
        return "OK"

    def fence(ctx):
        pipeline_helpers.cancel_and_wait_for_active_work(
            ctx["project_id"], command=command, timeout=0.001, poll_interval=0
        )

    monkeypatch.setattr(pipeline_helpers, "cancel_owned_scaffold", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_runs", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline_helpers, "wait_for_owned_runs", AsyncMock())
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_active_work", fence)
    monkeypatch.setattr(pipeline_helpers, "cleanup_github_repo", github_cleanup.append)

    async with httpx.AsyncClient(base_url="http://test") as api:
        with pytest.raises(CleanupError, match="workflow_cancellation_unproven"):
            await pipeline_helpers.cleanup_all(
                api,
                None,
                {"project_id": "project-1", "repo_name": "repo", "manifest": manifest},
            )

    assert github_cleanup == []


def test_capability_cleanup_removes_only_owned_queued_and_pending_entries():
    commands = []

    def command(*args):
        commands.append(args)
        if args[0] == "EVAL":
            if sum(call[0] == "EVAL" for call in commands) == 2:
                return "[]"
            return (
                '[{"stream":"engineering:queue","id":"1-0","groups":["capability-workers"]},'
                '{"stream":"deploy:queue","id":"2-0","groups":["capability-workers"]}]'
            )
        return "1"

    residue = cleanup_owned_capability_messages("project-1", {"run-1"}, command=command)

    assert residue == []
    assert commands[0][0] == "EVAL"
    assert "engineering:queue" in commands[0]
    assert "deploy:queue" in commands[0]
    assert "qa:queue" in commands[0]
    assert ("XACK", "engineering:queue", "capability-workers", "1-0") in commands
    assert ("XDEL", "engineering:queue", "1-0") in commands
    assert ("XACK", "deploy:queue", "capability-workers", "2-0") in commands
    assert ("XDEL", "deploy:queue", "2-0") in commands


def test_capability_cleanup_fails_closed_when_owned_residue_cannot_be_deleted():
    calls = 0

    def command(*args):
        nonlocal calls
        if args[0] == "EVAL":
            calls += 1
            return '[{"stream":"qa:queue","id":"3-0","groups":["qa-consumers"]}]'
        return "0"

    with pytest.raises(CleanupError, match="capability stream residue"):
        cleanup_owned_capability_messages("project-1", {"run-1"}, command=command)

    assert calls == 2


@pytest.mark.asyncio
async def test_cleanup_stops_before_external_cleanup_when_capability_ack_fails(monkeypatch):
    manifest = OwnershipManifest("project-1")
    manifest.own("project", "project-1")
    manifest.own("github_repository", "org/repo")
    github_cleanup = []

    monkeypatch.setattr(pipeline_helpers, "cancel_owned_scaffold", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_active_work", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_runs", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline_helpers, "wait_for_owned_runs", AsyncMock())
    monkeypatch.setattr(
        pipeline_helpers,
        "cleanup_owned_capability_work",
        lambda ctx: (_ for _ in ()).throw(RuntimeError("temporary Redis ACK failure")),
    )
    monkeypatch.setattr(pipeline_helpers, "cleanup_github_repo", github_cleanup.append)

    async with httpx.AsyncClient(base_url="http://test") as api:
        with pytest.raises(CleanupError, match="temporary Redis ACK failure"):
            await pipeline_helpers.cleanup_all(
                api,
                None,
                {"project_id": "project-1", "repo_name": "repo", "manifest": manifest},
            )

    assert github_cleanup == []


def test_scaffold_fence_prunes_crashed_execution_after_lease_expiry():
    calls = []

    def command(*args):
        calls.append(args)
        return "0" if args[0] == "EVAL" else "OK"

    pipeline_helpers.cancel_and_wait_for_scaffold(
        "project-1", command=command, timeout=1, poll_interval=0
    )

    prune_script = calls[1][1]
    assert "ZREMRANGEBYSCORE" in prune_script
    assert "ZCARD" in prune_script


@pytest.mark.asyncio
async def test_cleanup_does_not_verify_residue_before_claimed_work_stops(monkeypatch):
    github_cleanup = []
    manifest = OwnershipManifest("project-1")
    manifest.own("project", "project-1")
    manifest.own("github_repository", "org/repo")

    def fence(ctx):
        raise CleanupError("claimed scaffold still active")

    def github(repo_name):
        github_cleanup.append(repo_name)

    monkeypatch.setattr(pipeline_helpers, "cancel_owned_scaffold", fence)
    monkeypatch.setattr(pipeline_helpers, "cleanup_github_repo", github)

    async with httpx.AsyncClient(base_url="http://test") as api:
        with pytest.raises(CleanupError, match="claimed scaffold still active"):
            await pipeline_helpers.cleanup_all(
                api,
                None,
                {"project_id": "project-1", "repo_name": "repo", "manifest": manifest},
            )

    assert github_cleanup == []


@pytest.mark.asyncio
async def test_cleanup_cancels_active_runs_before_external_and_database_cleanup(monkeypatch):
    events = []
    manifest = OwnershipManifest("project-1")
    manifest.own("project", "project-1")
    manifest.own("github_repository", "org/repo")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/runs/":
            events.append("list-runs")
            return httpx.Response(
                200,
                json=[{"id": "deploy-1", "status": "running", "type": "deploy"}],
            )
        if request.method == "PATCH" and request.url.path == "/api/runs/deploy-1":
            events.append("cancel-run")
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/api/projects/project-1":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(
        pipeline_helpers, "cancel_owned_scaffold", lambda ctx: events.append("scaffold")
    )
    monkeypatch.setattr(
        pipeline_helpers, "cancel_owned_active_work", lambda ctx: events.append("active-work")
    )
    monkeypatch.setattr(
        pipeline_helpers,
        "cleanup_owned_capability_work",
        lambda ctx: events.append("capability-streams"),
    )

    async def wait_for_runs(*args, **kwargs):
        events.append("wait-runs")

    monkeypatch.setattr(pipeline_helpers, "wait_for_owned_runs", wait_for_runs)
    monkeypatch.setattr(
        pipeline_helpers, "cleanup_server_container", lambda ctx: events.append("server")
    )
    monkeypatch.setattr(
        pipeline_helpers, "cleanup_owned_workers", lambda ctx, errors: events.append("workers")
    )
    monkeypatch.setattr(
        pipeline_helpers,
        "cleanup_registry_resources",
        lambda ctx, errors: events.append("registry"),
    )
    monkeypatch.setattr(
        pipeline_helpers, "cleanup_github_repo", lambda repo: events.append("github")
    )
    monkeypatch.setattr(
        pipeline_helpers, "_cleanup_db", lambda project_id: events.append("database")
    )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        await pipeline_helpers.cleanup_all(
            api,
            None,
            {"project_id": "project-1", "repo_name": "repo", "manifest": manifest},
        )

    assert events == [
        "scaffold",
        "list-runs",
        "cancel-run",
        "wait-runs",
        "active-work",
        "capability-streams",
        "server",
        "workers",
        "registry",
        "github",
        "database",
    ]


@pytest.mark.asyncio
async def test_qa_gate_requires_separate_passed_terminal_run():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        result = await run_non_llm_qa(api, "http://deployed", timeout=1, poll_interval=0)

    assert result["status"] == "completed"
    assert result["qa_outcome"] == "passed"


@pytest.mark.asyncio
async def test_qa_gate_rejects_non_passing_terminal_outcome():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        with pytest.raises(AssertionError, match="status=failed outcome=failed"):
            await run_non_llm_qa(api, "http://deployed", timeout=0.001, poll_interval=0)
