import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

from capability_cleanup import cleanup_owned_capability_messages
import conftest as live_conftest
from conftest import create_test_project_context
import httpx
from live_harness import (
    LIVE_NO_CLEANUP_ENV,
    CleanupError,
    OwnershipManifest,
    cleanup_guard,
    resolve_repo_root,
    run_non_llm_qa,
)
import pipeline_helpers
from pipeline_helpers import (
    _build_server_remote_cleanup_command,
    build_github_cleanup_script,
    build_registry_cleanup_script,
)
import pytest
import structlog

from shared.contracts.queues.deploy import DeployOutcome


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


def _write_fake_docker(tmp_path: Path, body: str) -> Path:
    docker = tmp_path / "bin" / "docker"
    docker.parent.mkdir()
    docker.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
    docker.chmod(0o755)
    return docker


def test_server_cleanup_discovers_underscored_compose_project(tmp_path):
    state = tmp_path / "container-state"
    calls = tmp_path / "docker-calls"
    state.write_text("up\n")
    service_dir = tmp_path / "services" / "live-test-2c3e830f" / "infra"
    service_dir.mkdir(parents=True)
    _write_fake_docker(
        tmp_path,
        """
state=${FAKE_DOCKER_STATE:?}
calls=${FAKE_DOCKER_CALLS:?}
if [ "$1" = "ps" ]; then
  filter=
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--filter" ]; then
      shift
      filter=$1
    fi
    shift || true
  done
  [ -s "$state" ] || exit 0
  case "$filter" in
    label=com.docker.compose.project=live_test_2c3e830f|name=^/live_test_2c3e830f[-_])
      echo c1
      ;;
  esac
elif [ "$1" = "inspect" ]; then
  echo live_test_2c3e830f
elif [ "$1" = "compose" ]; then
  project=
  shift
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "-p" ]; then
      shift
      project=$1
    fi
    shift || true
  done
  echo "compose:$project" >> "$calls"
  if [ "$project" = "live_test_2c3e830f" ]; then
    : > "$state"
  fi
elif [ "$1" = "rm" ]; then
  echo "rm:${*: -1}" >> "$calls"
  : > "$state"
fi
""",
    )
    env = {
        **os.environ,
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_CALLS": str(calls),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [
            "sh",
            "-c",
            _build_server_remote_cleanup_command(
                "live-test-2c3e830f", service_base=str(tmp_path / "services")
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert "compose:live_test_2c3e830f" in calls.read_text().splitlines()
    assert not (tmp_path / "services" / "live-test-2c3e830f").exists()


def test_server_cleanup_removes_labelled_resources_without_infra_directory(tmp_path):
    calls = tmp_path / "docker-calls"
    _write_fake_docker(
        tmp_path,
        """
calls=${FAKE_DOCKER_CALLS:?}
if [ "$1" = "ps" ]; then
  exit 0
elif [ "$1" = "volume" ] || [ "$1" = "network" ]; then
  resource=$1
  command=$2
  if [ "$command" = "ls" ]; then
    state=${FAKE_DOCKER_STATE:?}/$resource
    if [ -s "$state" ]; then
      cat "$state"
    fi
  elif [ "$command" = "rm" ]; then
    echo "$resource:${*: -1}" >> "$calls"
    : > "${FAKE_DOCKER_STATE:?}/$resource"
  fi
fi
""",
    )
    state = tmp_path / "resource-state"
    state.mkdir()
    (state / "volume").write_text("v1\n")
    (state / "network").write_text("n1\n")
    env = {
        **os.environ,
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_CALLS": str(calls),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [
            "sh",
            "-c",
            _build_server_remote_cleanup_command(
                "live-test-2c3e830f", service_base=str(tmp_path / "services")
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["volume:v1", "network:n1"]


def test_server_cleanup_verifies_labelled_volume_residue_without_infra_directory(tmp_path):
    _write_fake_docker(
        tmp_path,
        """
if [ "$1" = "ps" ]; then
  exit 0
elif [ "$1" = "volume" ]; then
  if [ "$2" = "ls" ]; then
    echo v1
  fi
elif [ "$1" = "network" ]; then
  exit 0
fi
""",
    )
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}

    result = subprocess.run(
        [
            "sh",
            "-c",
            _build_server_remote_cleanup_command(
                "live-test-2c3e830f", service_base=str(tmp_path / "services")
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode != 0
    assert "volume:live-test-2c3e830f:v1" in result.stderr


def test_server_cleanup_verifies_underscored_container_name_residue(tmp_path):
    service_dir = tmp_path / "services" / "live-test-2c3e830f" / "infra"
    service_dir.mkdir(parents=True)
    _write_fake_docker(
        tmp_path,
        """
if [ "$1" = "ps" ]; then
  filter=
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--filter" ]; then
      shift
      filter=$1
    fi
    shift || true
  done
  if [ "$filter" = "name=^/live_test_2c3e830f[-_]" ]; then
    echo c1
  fi
elif [ "$1" = "inspect" ]; then
  echo ''
fi
""",
    )
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}

    result = subprocess.run(
        [
            "sh",
            "-c",
            _build_server_remote_cleanup_command(
                "live-test-2c3e830f", service_base=str(tmp_path / "services")
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert result.returncode != 0
    assert "name:live_test_2c3e830f:c1" in result.stderr


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
        return httpx.Response(200, json=responses[url], request=httpx.Request("GET", url))

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    api = SimpleNamespace(get=get)
    await pipeline_helpers.wait_deploy(api, api, ctx, timeout=1)

    assert ctx["allocation_id"] == 8
    assert ctx["port"] == 8010


@pytest.mark.asyncio
async def test_wait_deploy_skips_infra_ports_and_picks_web_module(monkeypatch, tmp_path):
    """Regression: deploy allocates a port per module (web + postgres + redis).

    Only the web module serves HTTP /health. The infra ports come first here to
    prove they are skipped, so deployed_url does not point at postgres/redis and
    the non-LLM QA health gate can reach a real service.
    """
    manifest = OwnershipManifest("project-1")
    ctx = {"project_id": "project-1", "project_name": "run", "manifest": manifest}
    responses = {
        "/api/repositories/": [{"id": "repo-1"}],
        "/api/applications/": [{"id": 21, "status": "running"}],
        "/api/servers/": [{"handle": "server-1", "public_ip": "192.0.2.1"}],
        "/api/servers/server-1/ports": [
            {"id": 8, "port": 8001, "application_id": 21, "service_name": "postgres"},
            {"id": 9, "port": 8002, "application_id": 21, "service_name": "redis"},
            {"id": 10, "port": 8010, "application_id": 21, "service_name": "backend"},
        ],
    }

    async def get(url, **kwargs):
        return httpx.Response(200, json=responses[url], request=httpx.Request("GET", url))

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    api = SimpleNamespace(get=get)
    await pipeline_helpers.wait_deploy(api, api, ctx, timeout=1)

    assert ctx["allocation_id"] == 10
    assert ctx["port"] == 8010
    assert ctx["deployed_url"] == "http://192.0.2.1:8010"


@pytest.mark.asyncio
async def test_wait_deploy_reads_servers_as_internal_service(monkeypatch, tmp_path):
    """Regression: /api/servers/ and its ports are require_internal_or_admin.

    The harness user carries only X-Telegram-ID and is not admin, so those
    endpoints answer 403 unless the request also sends X-Internal-Key. Without
    it wait_deploy would iterate a `{"detail": ...}` error body and raise
    `TypeError: string indices must be integers` before registering the deploy
    in the ownership manifest, so cleanup never removes /opt/services/<project>.

    Assert both auth-gated calls carry X-Internal-Key and, on a valid RUNNING
    application, server_deployment and port_allocation reach the manifest.
    """
    manifest = OwnershipManifest("project-1")
    ctx = {"project_id": "project-1", "project_name": "run", "manifest": manifest}
    responses = {
        "/api/repositories/": [{"id": "repo-1"}],
        "/api/applications/": [{"id": 21, "status": "running"}],
        "/api/servers/": [{"handle": "server-1", "public_ip": "192.0.2.1"}],
        "/api/servers/server-1/ports": [{"id": 8, "port": 8010, "application_id": 21}],
    }
    server_endpoint_headers = {}

    async def get(url, headers=None, **kwargs):
        if url.startswith("/api/servers"):
            server_endpoint_headers[url] = headers
        return httpx.Response(200, json=responses[url], request=httpx.Request("GET", url))

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    api = SimpleNamespace(get=get)

    await pipeline_helpers.wait_deploy(api, api, ctx, timeout=1)

    assert set(server_endpoint_headers) == {"/api/servers/", "/api/servers/server-1/ports"}
    for headers in server_endpoint_headers.values():
        assert headers["X-Internal-Key"] == "test-internal-key"

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


@pytest.mark.asyncio
async def test_wait_deploy_fails_loudly_when_servers_endpoint_rejects(monkeypatch, tmp_path):
    """A non-200 from the auth-gated servers endpoint must surface as a clear
    HTTP error, not a `TypeError` from iterating an error body, and must leave
    the manifest empty."""
    manifest = OwnershipManifest("project-1")
    ctx = {"project_id": "project-1", "project_name": "run", "manifest": manifest}
    responses = {
        "/api/repositories/": [{"id": "repo-1"}],
        "/api/applications/": [{"id": 21, "status": "running"}],
    }

    async def get(url, **kwargs):
        request = httpx.Request("GET", url)
        if url == "/api/servers/":
            return httpx.Response(403, json={"detail": "Admin access required"}, request=request)
        return httpx.Response(200, json=responses[url], request=request)

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    api = SimpleNamespace(get=get)

    with pytest.raises(httpx.HTTPStatusError):
        await pipeline_helpers.wait_deploy(api, api, ctx, timeout=1)

    assert manifest.resources == []


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
async def test_cleanup_guard_runs_when_qa_fails_before_fixture_yield(monkeypatch):
    monkeypatch.delenv(LIVE_NO_CLEANUP_ENV, raising=False)
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    with pytest.raises(RuntimeError, match="QA failed"):
        async with cleanup_guard(cleanup, manifest=OwnershipManifest("run-1")):
            raise RuntimeError("QA failed")

    assert cleaned == [True]


@pytest.mark.asyncio
async def test_cleanup_guard_preserves_run_and_cleanup_failures(monkeypatch):
    monkeypatch.delenv(LIVE_NO_CLEANUP_ENV, raising=False)

    async def cleanup():
        raise CleanupError("residue")

    with pytest.raises(BaseExceptionGroup) as caught:
        async with cleanup_guard(cleanup, manifest=OwnershipManifest("run-1")):
            raise RuntimeError("QA failed")

    assert [str(error) for error in caught.value.exceptions] == ["QA failed", "residue"]


@pytest.mark.asyncio
async def test_cleanup_guard_skips_cleanup_and_still_raises_primary_error(monkeypatch):
    """LIVE_NO_CLEANUP leaves owned resources in place but never masks the failure."""
    monkeypatch.setenv(LIVE_NO_CLEANUP_ENV, "1")
    manifest = OwnershipManifest("run-1")
    manifest.own("project", "project-1")
    manifest.own("github_repository", "org/repo")
    manifest.own("server_deployment", "run", server_handle="server-1")
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(RuntimeError, match="deploy timed out"):
            async with cleanup_guard(cleanup, manifest=manifest):
                raise RuntimeError("deploy timed out")

    assert cleaned == []
    warning = next(entry for entry in logs if entry["log_level"] == "warning")
    assert warning["event"] == "cleanup skipped — resources left for debugging"
    assert warning["run_id"] == "run-1"
    assert warning["manifest_file"] == ".live-manifests/run-1.json"
    assert set(warning["left"]) == {
        "project project-1",
        "github_repository org/repo",
        "server_deployment run",
    }


@pytest.mark.asyncio
async def test_cleanup_guard_skips_cleanup_on_success_when_flag_set(monkeypatch):
    monkeypatch.setenv(LIVE_NO_CLEANUP_ENV, "1")
    manifest = OwnershipManifest("run-1")
    manifest.own("project", "project-1")
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    async with cleanup_guard(cleanup, manifest=manifest):
        pass

    assert cleaned == []


@pytest.mark.asyncio
async def test_cleanup_guard_runs_cleanup_when_flag_unset(monkeypatch):
    monkeypatch.delenv(LIVE_NO_CLEANUP_ENV, raising=False)
    manifest = OwnershipManifest("run-1")
    manifest.own("project", "project-1")
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    async with cleanup_guard(cleanup, manifest=manifest):
        pass

    assert cleaned == [True]


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


# ── Environment contract preflight ───────────────────────────────────────


def _probe_stdout(payload: dict) -> str:
    """Container stdout: structlog noise around the marked probe payload."""
    return (
        "2026-07-16 10:00:00 [info] github_token_issued\n"
        + pipeline_helpers.ENV_CONTRACT_PROBE_MARKER
        + json.dumps(payload)
        + "\n"
    )


def _probe_payload(**overrides) -> dict:
    payload = {
        "ref": "abc123",
        "fragment_paths": sorted(pipeline_helpers.EXPECTED_ENV_CONTRACT_FRAGMENTS),
        "entries": ["APP_ENV", "BACKEND_PORT"],
        "merged_into_main": None,
    }
    payload.update(overrides)
    return payload


def test_env_contract_probe_reads_the_ref_deploy_resolves(monkeypatch):
    """The probe must read the exact ref, not a guessed branch.

    devops.env_contract_loader resolves the contract at the deploy's head SHA,
    so a probe that checked main instead would pass while the deployed tree is
    missing a fragment.
    """
    captured = {}

    def fake_exec(service, script, timeout=30):
        captured["service"] = service
        captured["script"] = script
        return SimpleNamespace(returncode=0, stdout=_probe_stdout(_probe_payload()), stderr="")

    monkeypatch.setattr(pipeline_helpers, "docker_exec", fake_exec)

    probe = pipeline_helpers.probe_env_contract("run-repo", "abc123")

    assert captured["service"] == "langgraph"
    assert "'abc123'" in captured["script"]
    assert "merge_env_contract_fragments" in captured["script"]
    assert probe["fragment_paths"] == sorted(pipeline_helpers.EXPECTED_ENV_CONTRACT_FRAGMENTS)


def test_env_contract_probe_payload_survives_container_log_noise(monkeypatch):
    """Container logs share stdout with the payload, so the marker delimits it."""
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=_probe_stdout(_probe_payload(entries=["APP_ENV"])), stderr=""
        ),
    )

    assert pipeline_helpers.probe_env_contract("run-repo", "abc123")["entries"] == ["APP_ENV"]


def test_env_contract_probe_without_payload_is_loud(monkeypatch):
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="[info] nothing here\n", stderr=""),
    )

    with pytest.raises(RuntimeError, match="printed no payload"):
        pipeline_helpers.probe_env_contract("run-repo", "abc123")


def test_env_contract_probe_failure_is_loud(monkeypatch):
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    with pytest.raises(RuntimeError, match="boom"):
        pipeline_helpers.probe_env_contract("run-repo", "abc123")


def test_record_env_contract_accepts_expected_fragments(monkeypatch):
    stdout = _probe_stdout(_probe_payload())
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )
    ctx = {"repo_name": "run-repo"}

    assert pipeline_helpers.record_env_contract(ctx, "abc123", phase="scaffold") is True
    assert "env_contract_errors" not in ctx
    assert ctx["env_contract_probes"]["scaffold"]["ref"] == "abc123"


def test_record_env_contract_rejects_missing_fragment(monkeypatch):
    """A repo without the backend fragment cannot resolve a typed deploy env."""
    payload = _probe_payload(fragment_paths=["infra/env.contract.yaml"])
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=_probe_stdout(payload), stderr=""),
    )
    ctx = {"repo_name": "run-repo"}

    assert pipeline_helpers.record_env_contract(ctx, "abc123", phase="scaffold") is False
    assert "services/backend/env.contract.yaml" in ctx["env_contract_errors"]["scaffold"]
    # The observed paths still reach the debug dump for the failed phase.
    assert ctx["env_contract_probes"]["scaffold"]["fragment_paths"] == ["infra/env.contract.yaml"]


def test_record_env_contract_rejects_empty_contract(monkeypatch):
    payload = _probe_payload(entries=[])
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=_probe_stdout(payload), stderr=""),
    )
    ctx = {"repo_name": "run-repo"}

    assert pipeline_helpers.record_env_contract(ctx, "abc123", phase="scaffold") is False
    assert "declares no entries" in ctx["env_contract_errors"]["scaffold"]


def test_record_env_contract_rejects_sha_absent_from_main(monkeypatch):
    """The merged check must prove main contains the SHA deploy resolves.

    Without it the mega would accept a contract that only ever existed on the
    story branch.
    """
    payload = _probe_payload(merged_into_main=False)
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=_probe_stdout(payload), stderr=""),
    )
    ctx = {"repo_name": "run-repo"}

    ok = pipeline_helpers.record_env_contract(
        ctx, "abc123", phase="merged", verify_merged_into_main=True
    )

    assert ok is False
    assert "not contained in main" in ctx["env_contract_errors"]["merged"]


# ── Typed deploy outcome ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_deploy_run_selects_the_run_carrying_the_merged_sha(monkeypatch):
    """Only the pr_poller run records head_sha; the engineering-triggered deploy
    run does not, so it is not the run this mega's contract check must follow."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    seen = {}

    async def get(url, params=None, headers=None):
        seen["params"] = params
        seen["headers"] = headers
        return httpx.Response(
            200,
            json=[
                {"id": "deploy-eng-1", "run_metadata": {}},
                {"id": "deploy-poll-1", "run_metadata": {"head_sha": "abc123"}},
            ],
            request=httpx.Request("GET", "http://test/api/runs/"),
        )

    ctx = {"project_id": "project-1"}
    run = await pipeline_helpers.wait_deploy_run(SimpleNamespace(get=get), ctx, timeout=1)

    assert run["id"] == "deploy-poll-1"
    assert ctx["deploy_run_id"] == "deploy-poll-1"
    assert ctx["deploy_head_sha"] == "abc123"
    assert seen["params"] == {"project_id": "project-1", "run_type": "deploy"}
    assert seen["headers"]["X-Internal-Key"] == "test-internal-key"


@pytest.mark.asyncio
async def test_wait_deploy_run_times_out_without_a_merged_deploy_run(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    async def get(url, params=None, headers=None):
        return httpx.Response(200, json=[], request=httpx.Request("GET", "http://test/api/runs/"))

    ctx = {"project_id": "project-1"}
    run = await pipeline_helpers.wait_deploy_run(
        SimpleNamespace(get=get), ctx, timeout=0.001, poll_interval=0
    )

    assert run is None
    assert "no deploy run with a merged head_sha" in ctx["deploy_run_error"]


@pytest.mark.asyncio
async def test_wait_deploy_outcome_types_the_run_result(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    seen = {}

    async def get(url, headers=None):
        seen["url"] = url
        seen["headers"] = headers
        return httpx.Response(
            200,
            json={
                "id": "deploy-poll-1",
                "status": "completed",
                "result": {"deploy_outcome": "success", "deployed_url": "http://192.0.2.1:8010"},
            },
            request=httpx.Request("GET", url),
        )

    ctx = {"deploy_run_id": "deploy-poll-1"}
    result = await pipeline_helpers.wait_deploy_outcome(SimpleNamespace(get=get), ctx, timeout=1)

    assert result.deploy_outcome is DeployOutcome.SUCCESS
    assert ctx["deploy_outcome"] == "success"
    assert seen["url"] == "/api/runs/deploy-poll-1"
    assert seen["headers"]["X-Internal-Key"] == "test-internal-key"


@pytest.mark.asyncio
async def test_wait_deploy_outcome_reports_a_failed_deploy(monkeypatch):
    """A deployed app can answer while the run itself concluded a failure, so the
    outcome the mega gates on comes from the run, not from ApplicationStatus."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    async def get(url, headers=None):
        return httpx.Response(
            200,
            json={
                "id": "deploy-poll-1",
                "status": "failed",
                "result": {
                    "deploy_outcome": "waiting_for_user_secret",
                    "error_details": "STRIPE_SECRET_KEY missing",
                },
            },
            request=httpx.Request("GET", url),
        )

    ctx = {"deploy_run_id": "deploy-poll-1"}
    result = await pipeline_helpers.wait_deploy_outcome(SimpleNamespace(get=get), ctx, timeout=1)

    assert result.deploy_outcome is DeployOutcome.WAITING_FOR_USER_SECRET
    assert ctx["deploy_outcome"] == "waiting_for_user_secret"
    assert ctx["deploy_error_details"] == "STRIPE_SECRET_KEY missing"


@pytest.mark.asyncio
async def test_wait_deploy_outcome_rejects_an_untyped_result(monkeypatch):
    """Run.result is a JSON column: a payload that is not a DeployRunResult must
    be reported, not read as if its keys meant anything."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    async def get(url, headers=None):
        return httpx.Response(
            200,
            json={
                "id": "deploy-poll-1",
                "status": "completed",
                "result": {"qa_outcome": "passed"},
            },
            request=httpx.Request("GET", url),
        )

    ctx = {"deploy_run_id": "deploy-poll-1"}
    result = await pipeline_helpers.wait_deploy_outcome(SimpleNamespace(get=get), ctx, timeout=1)

    assert result is None
    assert "not a DeployRunResult" in ctx["deploy_outcome_error"]
    assert "deploy_outcome" not in ctx


@pytest.mark.asyncio
async def test_wait_deploy_outcome_rejects_terminal_run_without_result(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    async def get(url, headers=None):
        return httpx.Response(
            200,
            json={"id": "deploy-poll-1", "status": "completed", "result": None},
            request=httpx.Request("GET", url),
        )

    ctx = {"deploy_run_id": "deploy-poll-1"}
    result = await pipeline_helpers.wait_deploy_outcome(SimpleNamespace(get=get), ctx, timeout=1)

    assert result is None
    assert "carries no result" in ctx["deploy_outcome_error"]


@pytest.mark.asyncio
async def test_wait_deploy_outcome_times_out_on_a_stuck_run(monkeypatch):
    """The wait is bounded: a run that never goes terminal must not hang the mega."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    async def get(url, headers=None):
        return httpx.Response(
            200,
            json={"id": "deploy-poll-1", "status": "running", "result": None},
            request=httpx.Request("GET", url),
        )

    ctx = {"deploy_run_id": "deploy-poll-1"}
    result = await pipeline_helpers.wait_deploy_outcome(
        SimpleNamespace(get=get), ctx, timeout=0.001, poll_interval=0
    )

    assert result is None
    assert "did not reach a terminal state" in ctx["deploy_outcome_error"]


def test_debug_dump_retains_env_contract_and_deploy_outcome(monkeypatch, tmp_path):
    """An early contract failure must leave its evidence in the dump."""
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    monkeypatch.setattr(
        pipeline_helpers.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="")
    )
    ctx = {
        "project_id": "project-1",
        "deploy_run_id": "deploy-poll-1",
        "deploy_head_sha": "abc123",
        "deploy_outcome": "environment_contract_invalid",
        "env_contract_probes": {
            "merged": _probe_payload(fragment_paths=["infra/env.contract.yaml"])
        },
        "env_contract_errors": {"merged": "merged: fragments missing at abc123"},
    }

    pipeline_helpers.dump_debug(ctx, "env-contract")

    text = next((tmp_path / "docs" / "e2e_results").glob("debug-env-contract-*.md")).read_text()
    assert "deploy-poll-1" in text
    assert "abc123" in text
    assert "environment_contract_invalid" in text
    assert "infra/env.contract.yaml" in text
    assert "merged: fragments missing at abc123" in text


_INFRA_FRAGMENT = """
version: "1"
owner: infra
entries:
  COMPOSE_PROJECT_NAME:
    source: derived
    environments: [local, production]
    required: false
"""

_BACKEND_FRAGMENT = """
version: "1"
owner: backend
entries:
  BACKEND_PORT:
    source: allocation
    environments: [local, production]
    consumers: [backend]
    required: true
    service: backend
"""


class _FakeGitHub:
    """Stands in for the GitHub App client inside the probe script."""

    files = {
        "infra/env.contract.yaml": _INFRA_FRAGMENT,
        "services/backend/env.contract.yaml": _BACKEND_FRAGMENT,
        "README.md": "# not a contract",
    }
    requested_refs: list[str] = []

    async def list_repo_files_recursive(self, owner, repo, ref):
        type(self).requested_refs.append(ref)
        return sorted(self.files)

    async def get_file_contents(self, owner, repo, path, ref):
        type(self).requested_refs.append(ref)
        return self.files[path]

    async def get_token(self, owner, repo):
        return "gh-token"


def _run_probe_script(monkeypatch, capsys, *, ref, verify_merged_into_main=False):
    """Execute the generated probe script against a fake repository."""
    import shared.clients.github as github_module

    _FakeGitHub.requested_refs = []
    monkeypatch.setattr(github_module, "GitHubAppClient", _FakeGitHub)
    script = pipeline_helpers.build_env_contract_probe_script(
        "run-repo", ref, verify_merged_into_main=verify_merged_into_main
    )
    compile(script, "<env-contract-probe>", "exec")
    exec(script, {})  # noqa: S102
    return pipeline_helpers.parse_env_contract_probe(capsys.readouterr().out)


def test_env_contract_probe_script_merges_real_fragments_at_one_ref(monkeypatch, capsys):
    """Execute the generated script, not just compile it.

    It must read only env.contract.yaml files, merge them through the same
    schema deploy uses, and report the merged keys for the requested ref.
    """
    probe = _run_probe_script(monkeypatch, capsys, ref="abc123")

    assert probe["ref"] == "abc123"
    assert probe["fragment_paths"] == [
        "infra/env.contract.yaml",
        "services/backend/env.contract.yaml",
    ]
    assert probe["entries"] == ["BACKEND_PORT", "COMPOSE_PROJECT_NAME"]
    assert probe["merged_into_main"] is None
    # Every read is pinned to the deploy ref; none silently fall back to main.
    assert set(_FakeGitHub.requested_refs) == {"abc123"}


def test_env_contract_probe_script_reports_merge_into_main(monkeypatch, capsys):
    """A SHA already contained in main compares as identical or behind."""
    requested = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            requested.append(url)
            return httpx.Response(200, json={"status": "behind"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())

    probe = _run_probe_script(monkeypatch, capsys, ref="abc123", verify_merged_into_main=True)

    assert probe["merged_into_main"] is True
    assert requested == [
        "https://api.github.com/repos/project-factory-organization/run-repo/compare/main...abc123"
    ]


def test_env_contract_probe_script_reports_sha_outside_main(monkeypatch, capsys):
    """A story-branch SHA main never took compares as diverged or ahead."""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return httpx.Response(
                200, json={"status": "diverged"}, request=httpx.Request("GET", url)
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())

    probe = _run_probe_script(monkeypatch, capsys, ref="abc123", verify_merged_into_main=True)

    assert probe["merged_into_main"] is False


def test_env_contract_probe_script_reports_a_repo_without_fragments(monkeypatch, capsys):
    """A repo carrying no contract is the missing-contract case deploy rejects."""
    monkeypatch.setattr(_FakeGitHub, "files", {"README.md": "# no contract"})

    probe = _run_probe_script(monkeypatch, capsys, ref="abc123")

    assert probe["fragment_paths"] == []
    assert probe["entries"] == []
