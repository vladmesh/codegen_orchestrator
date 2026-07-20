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
)
import pipeline_helpers
from pipeline_helpers import (
    _build_server_remote_cleanup_command,
    build_github_cleanup_script,
    build_registry_cleanup_script,
    run_non_llm_qa,
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


def test_registry_cleanup_treats_stale_tag_with_missing_manifest_as_absent(monkeypatch):
    requested_urls = []

    class Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = {}

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected status {self.status_code}")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            requested_urls.append(url)
            if url.endswith("/tags/list"):
                return Response(200, {"tags": ["sha-stale"]})
            return Response(404)

        async def delete(self, url, **kwargs):
            raise AssertionError(f"must not delete missing manifest: {url}")

    monkeypatch.setenv("ORCHESTRATOR_HOSTNAME", "registry.example.com")
    monkeypatch.setenv("REGISTRY_USER", "user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "password")
    monkeypatch.setattr(pipeline_helpers.httpx, "AsyncClient", lambda **kwargs: Client())

    exec(  # noqa: S102
        build_registry_cleanup_script("project-factory-organization/owned-repository-backend"),
        {},
    )

    assert (
        requested_urls.count(
            "https://registry.example.com/v2/"
            "project-factory-organization/owned-repository-backend/manifests/sha-stale"
        )
        == 2
    )


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
            return httpx.Response(201, json={"id": "project", "slug": "live-test-slug"})
        return httpx.Response(500, text="repository unavailable")

    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    monkeypatch.setattr(pipeline_helpers, "cleanup_all", cleanup)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        with pytest.raises(httpx.HTTPStatusError):
            await pipeline_helpers.create_noop_project(api, api)

    assert len(cleanup_contexts) == 1
    manifest = cleanup_contexts[0]["manifest"]
    assert [(resource.kind, resource.identifier) for resource in manifest.resources] == [
        ("project", cleanup_contexts[0]["project_id"])
    ]
    written = json.loads((tmp_path / ".live-manifests" / f"{manifest.run_id}.json").read_text())
    assert written["resources"] == [{"identifier": manifest.run_id, "kind": "project"}]


@pytest.mark.asyncio
async def test_llm_backend_project_uses_real_worker_backend_only_config(monkeypatch, tmp_path):
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/api/projects/":
            return httpx.Response(201, json={"id": "project", "slug": "live-test-llm-slug"})
        return httpx.Response(201, json={"id": "repo-1"})

    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        ctx = await pipeline_helpers.create_llm_backend_project(api, api)

    project_payload = requests[0][1]
    config = project_payload["config"]
    assert project_payload["title"].startswith("live-test-llm-")
    assert config["modules"] == ["backend"]
    assert config["agent_type"] == "claude"
    assert "user-provided secrets" in config["detailed_spec"]
    assert "secrets" not in config
    assert "env_hints" not in config
    assert requests[1][1]["name"] == "live-test-llm-slug"
    assert requests[1][1]["git_url"].endswith("/live-test-llm-slug")
    assert ctx["project_name"] == "live-test-llm-slug"
    assert ctx["repo_name"] == "live-test-llm-slug"
    assert ctx["repo_id"] == "repo-1"
    assert ctx["task_title"] == pipeline_helpers.LLM_BACKEND_TASK_TITLE
    assert ctx["task_description"] == pipeline_helpers.LLM_BACKEND_TASK_DESCRIPTION


@pytest.mark.asyncio
async def test_create_story_and_task_uses_context_task_description():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, body))
        if request.url.path == "/api/stories/":
            return httpx.Response(201, json={"id": "story-1"})
        if request.url.path == "/api/tasks/":
            return httpx.Response(201, json={"id": "task-1"})
        return httpx.Response(200, json={})

    ctx = {
        "project_id": "project-1",
        "task_title": "Implement backend health API",
        "task_description": "Create a real backend health endpoint.",
    }
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        await pipeline_helpers.create_story_and_task(api, ctx)

    task_payload = [body for path, body in requests if path == "/api/tasks/"][0]
    assert task_payload["title"] == "Implement backend health API"
    assert task_payload["description"] == "Create a real backend health endpoint."
    assert ctx["story_id"] == "story-1"
    assert ctx["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_poll_status_fails_loudly_before_parsing_error_bodies():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "internal key required"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        with pytest.raises(httpx.HTTPStatusError):
            await pipeline_helpers.poll_status(
                api,
                "/api/projects/project-1",
                {"active"},
                timeout=3,
            )


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
        fixture = live_conftest.test_project.__wrapped__(api, api)
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
                json=[
                    {
                        "id": "deploy-1",
                        "project_id": "project-1",
                        "status": "running",
                        "type": "deploy",
                    }
                ],
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
        # Second quiescence proof, behind the consumer fence.
        "wait-runs",
        "server",
        "workers",
        "registry",
        "github",
        "database",
    ]


@pytest.mark.asyncio
async def test_cleanup_cancels_run_created_after_the_first_runs_snapshot(monkeypatch):
    """A supervisor-created run that appears after the first scan must not escape teardown."""
    events = []
    manifest = OwnershipManifest("project-1")
    manifest.own("project", "project-1")
    manifest.own("github_repository", "org/repo")
    statuses = {"deploy-1": "running"}
    scans = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal scans
        if request.method == "GET" and request.url.path == "/api/runs/":
            scans += 1
            if scans == 2:
                # QA run created by the supervisor after the first snapshot.
                statuses["qa-9"] = "queued"
            return httpx.Response(
                200,
                json=[
                    {"id": run_id, "project_id": "project-1", "status": status}
                    for run_id, status in statuses.items()
                ],
            )
        if request.method == "PATCH" and request.url.path.startswith("/api/runs/"):
            run_id = request.url.path.rsplit("/", 1)[-1]
            statuses[run_id] = "cancelled"
            events.append(f"cancel-{run_id}")
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/api/projects/project-1":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(pipeline_helpers, "cancel_owned_scaffold", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_active_work", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_owned_capability_work", lambda ctx: None)
    monkeypatch.setattr(
        pipeline_helpers, "cleanup_server_container", lambda ctx: events.append("server")
    )
    monkeypatch.setattr(pipeline_helpers, "cleanup_owned_workers", lambda ctx, errors: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_registry_resources", lambda ctx, errors: None)
    monkeypatch.setattr(
        pipeline_helpers, "cleanup_github_repo", lambda repo: events.append("github")
    )
    monkeypatch.setattr(pipeline_helpers, "_cleanup_db", lambda project_id: events.append("db"))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        await pipeline_helpers.cleanup_all(
            api,
            None,
            {"project_id": "project-1", "repo_name": "repo", "manifest": manifest},
        )

    assert events.index("cancel-qa-9") < events.index("server")
    assert statuses["qa-9"] == "cancelled"
    assert "qa-9" in {
        resource.identifier for resource in manifest.resources if resource.kind == "run"
    }
    assert events[-3:] == ["server", "github", "db"]


@pytest.mark.asyncio
async def test_cleanup_fails_closed_when_new_runs_never_go_terminal(monkeypatch):
    """Unprovable quiescence stops teardown before any external or DB deletion."""
    external = []
    manifest = OwnershipManifest("project-1")
    manifest.own("project", "project-1")
    manifest.own("github_repository", "org/repo")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/runs/":
            # The supervisor keeps producing work; nothing ever settles.
            return httpx.Response(
                200,
                json=[{"id": "deploy-1", "project_id": "project-1", "status": "running"}],
            )
        if request.method == "PATCH" and request.url.path == "/api/runs/deploy-1":
            return httpx.Response(200)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(pipeline_helpers, "cancel_owned_scaffold", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_active_work", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_owned_capability_work", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "RUN_CANCELLATION_TIMEOUT", 0.05)
    monkeypatch.setattr(pipeline_helpers, "RUN_CANCELLATION_POLL_INTERVAL", 0)
    monkeypatch.setattr(
        pipeline_helpers, "cleanup_server_container", lambda ctx: external.append("server")
    )
    monkeypatch.setattr(
        pipeline_helpers, "cleanup_github_repo", lambda repo: external.append("github")
    )
    monkeypatch.setattr(pipeline_helpers, "_cleanup_db", lambda project_id: external.append("db"))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        with pytest.raises(CleanupError, match="did not reach terminal state"):
            await pipeline_helpers.cleanup_all(
                api,
                None,
                {"project_id": "project-1", "repo_name": "repo", "manifest": manifest},
            )

    assert external == []
    assert "deploy-1" in {
        resource.identifier for resource in manifest.resources if resource.kind == "run"
    }


def _qa_run(**overrides) -> dict:
    run = {
        "id": "qa-1",
        "story_id": "story-1",
        "status": "completed",
        "result": {"qa_outcome": "passed", "summary": "1 GET check(s) passed"},
    }
    run.update(overrides)
    return run


def _runs_transport(runs: list[dict]) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=runs)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_qa_gate_requires_separate_passed_terminal_run(monkeypatch):
    """The gate reports the pipeline's own QA run, not a health probe of its own."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    async with httpx.AsyncClient(
        transport=_runs_transport([_qa_run()]), base_url="http://test"
    ) as api:
        result = await run_non_llm_qa(api, "story-1", timeout=1, poll_interval=0)

    assert result == {"run_id": "qa-1", "status": "completed", "qa_outcome": "passed"}


@pytest.mark.asyncio
async def test_qa_gate_rejects_non_passing_terminal_outcome(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    runs = [
        _qa_run(
            status="failed",
            result={"qa_outcome": "failed", "summary": "1/1 GET check(s) failed"},
        )
    ]
    async with httpx.AsyncClient(transport=_runs_transport(runs), base_url="http://test") as api:
        with pytest.raises(AssertionError, match="status=failed outcome=failed"):
            await run_non_llm_qa(api, "story-1", timeout=1, poll_interval=0)


@pytest.mark.asyncio
async def test_qa_gate_ignores_runs_of_other_stories(monkeypatch):
    """A project carries QA runs of other stories; only this mega's run counts."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    runs = [_qa_run(id="qa-other", story_id="story-other")]
    async with httpx.AsyncClient(transport=_runs_transport(runs), base_url="http://test") as api:
        with pytest.raises(AssertionError, match="no QA run reached a terminal state"):
            await run_non_llm_qa(api, "story-1", timeout=0.01, poll_interval=0)


@pytest.mark.asyncio
async def test_qa_gate_waits_out_a_non_terminal_run(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    runs = [_qa_run(status="running", result=None)]
    async with httpx.AsyncClient(transport=_runs_transport(runs), base_url="http://test") as api:
        with pytest.raises(AssertionError, match="no QA run reached a terminal state"):
            await run_non_llm_qa(api, "story-1", timeout=0.01, poll_interval=0)


@pytest.mark.asyncio
async def test_qa_gate_rejects_a_user_scoped_observer():
    """list_runs hides unowned runs from a user-scoped caller — crash, don't blind-poll."""
    async with httpx.AsyncClient(
        transport=_runs_transport([_qa_run()]),
        base_url="http://test",
        headers={"X-Telegram-ID": "12345"},
    ) as api:
        with pytest.raises(RuntimeError, match="X-Telegram-ID"):
            await run_non_llm_qa(api, "story-1", timeout=1, poll_interval=0)


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

HARNESS_USER_ID = 7


def _deploy_run(
    run_id: str,
    *,
    story_id: str = "story-1",
    head_sha: str | None = "abc123",
    user_id: int | None = None,
) -> dict:
    """One /api/runs/ record shaped as the API returns it.

    ``head_sha=None`` is the engineering-triggered deploy run: pr_poller records
    the merged SHA, engineering does not. ``user_id=None`` is how every deploy
    run is really stored — neither producer attributes it to a user.
    """
    metadata = {"triggered_by": "pr_poll", "head_sha": head_sha} if head_sha else {}
    return {
        "id": run_id,
        "type": "deploy",
        "project_id": "project-1",
        "story_id": story_id,
        "user_id": user_id,
        "status": "completed",
        "run_metadata": metadata,
    }


def _runs_api(runs: list[dict]):
    """A GET /api/runs/ that answers as services/api/src/routers/runs.py does.

    The ownership narrowing is the part that matters: list_runs restricts the
    result to the caller's own runs for any non-admin X-Telegram-ID, and the
    internal key does not lift it. Faking the endpoint without that rule is what
    let the 2026-07-16 blindness through a green contract suite.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        selected = [
            run
            for run in runs
            if ("story_id" not in params or run["story_id"] == params["story_id"])
            and ("project_id" not in params or run["project_id"] == params["project_id"])
            and ("run_type" not in params or run["type"] == params["run_type"])
        ]
        if pipeline_helpers.USER_AUTH_HEADER in request.headers:
            selected = [run for run in selected if run["user_id"] == HARNESS_USER_ID]
        return httpx.Response(200, json=selected)

    return handler


def _runs_client(runs: list[dict], *, as_user: bool = False) -> httpx.AsyncClient:
    """Client for the fake runs API, authenticated the way the harness would."""
    headers = dict(pipeline_helpers.internal_headers())
    if as_user:
        headers.update(pipeline_helpers.AUTH_HEADERS)
    return httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(_runs_api(runs)),
        headers=headers,
    )


@pytest.mark.asyncio
async def test_cleanup_cancels_unowned_project_runs_through_internal_client(monkeypatch, tmp_path):
    """Cleanup must see unowned deploy/QA runs without trusting API narrowing."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    runs = [
        {
            "id": "deploy-owned",
            "type": "deploy",
            "project_id": "project-1",
            "story_id": "story-1",
            "user_id": None,
            "status": "running",
        },
        {
            "id": "qa-owned",
            "type": "qa",
            "project_id": "project-1",
            "story_id": "story-1",
            "user_id": None,
            "status": "queued",
        },
        {
            "id": "deploy-foreign",
            "type": "deploy",
            "project_id": "project-2",
            "story_id": "story-2",
            "user_id": HARNESS_USER_ID,
            "status": "running",
        },
    ]
    cancelled = []
    cancellation_requested = set()
    internal_run_headers = []
    external_teardown = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/runs/":
            # Deliberately ignore project_id: the harness must enforce ownership
            # even if an API implementation returns more than it was asked for.
            selected = list(runs)
            if pipeline_helpers.USER_AUTH_HEADER in request.headers:
                selected = [run for run in selected if run["user_id"] == HARNESS_USER_ID]
            else:
                internal_run_headers.append(request.headers)
                for run in selected:
                    if run["id"] in cancellation_requested:
                        run["status"] = "cancelled"
            return httpx.Response(200, json=selected)
        if request.method == "PATCH" and request.url.path.startswith("/api/runs/"):
            internal_run_headers.append(request.headers)
            run_id = request.url.path.rsplit("/", 1)[-1]
            cancelled.append(run_id)
            cancellation_requested.add(run_id)
            return httpx.Response(200, json=next(run for run in runs if run["id"] == run_id))
        if request.method == "GET" and request.url.path == "/api/projects/project-1":
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(pipeline_helpers, "cancel_owned_scaffold", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cancel_owned_active_work", lambda ctx: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_owned_capability_work", lambda ctx: None)

    def cleanup_server(ctx):
        owned_statuses = {run["status"] for run in runs if run["project_id"] == ctx["project_id"]}
        assert owned_statuses == {"cancelled"}
        external_teardown.append("server")

    monkeypatch.setattr(pipeline_helpers, "cleanup_server_container", cleanup_server)
    monkeypatch.setattr(pipeline_helpers, "cleanup_owned_workers", lambda ctx, errors: None)
    monkeypatch.setattr(pipeline_helpers, "cleanup_registry_resources", lambda ctx, errors: None)
    monkeypatch.setattr(pipeline_helpers, "_cleanup_db", lambda project_id: None)

    manifest = OwnershipManifest("project-1")
    manifest.own("project", "project-1")
    transport = httpx.MockTransport(handler)
    async with (
        httpx.AsyncClient(
            base_url="http://test",
            transport=transport,
            headers={**pipeline_helpers.internal_headers(), **pipeline_helpers.AUTH_HEADERS},
        ) as api_as_user,
        httpx.AsyncClient(
            base_url="http://test",
            transport=transport,
            headers=pipeline_helpers.internal_headers(),
        ) as api_internal,
    ):
        blind = await api_as_user.get("/api/runs/", params={"project_id": "project-1"})
        assert [run["id"] for run in blind.json()] == ["deploy-foreign"]

        with pytest.raises(CleanupError, match=pipeline_helpers.USER_AUTH_HEADER):
            await pipeline_helpers.cleanup_all(
                api_as_user,
                None,
                {"project_id": "project-1", "manifest": manifest},
            )
        assert cancelled == []
        assert external_teardown == []

        await pipeline_helpers.cleanup_all(
            api_internal,
            None,
            {"project_id": "project-1", "manifest": manifest},
        )

    assert cancelled == ["deploy-owned", "qa-owned"]
    assert external_teardown == ["server"]
    assert all(headers["X-Internal-Key"] == "test-internal-key" for headers in internal_run_headers)
    assert all(pipeline_helpers.USER_AUTH_HEADER not in headers for headers in internal_run_headers)


@pytest.mark.asyncio
async def test_wait_deploy_run_selects_the_run_carrying_the_merged_sha(monkeypatch):
    """Only the pr_poller run records head_sha; the engineering-triggered deploy
    run does not, so it is not the run this mega's contract check must follow."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    runs = [
        _deploy_run("deploy-eng-1", head_sha=None),
        _deploy_run("deploy-poll-1", head_sha="abc123"),
    ]
    ctx = {"project_id": "project-1", "story_id": "story-1"}

    async with _runs_client(runs) as api_internal:
        run = await pipeline_helpers.wait_deploy_run(api_internal, ctx, timeout=1)

    assert run["id"] == "deploy-poll-1"
    assert ctx["deploy_run_id"] == "deploy-poll-1"
    assert ctx["deploy_head_sha"] == "abc123"


@pytest.mark.asyncio
async def test_wait_deploy_run_finds_the_unowned_run_the_user_filter_hid(monkeypatch):
    """Regression, 2026-07-16: deploy `deploy-poll-ea0bed35` succeeded while the
    mega waited 420s for a run it could not see.

    list_runs narrows to `Run.user_id == caller` for any non-admin X-Telegram-ID,
    internal key or not, and pr_poller's deploy run has no user_id — so the
    harness user was answered `[]` for the whole wait. Observed as a plain
    internal service, the same run is found, and the request must reach the API
    with the internal key and without the user header.
    """
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    seen = {}
    runs = [_deploy_run("deploy-poll-ea0bed35", head_sha="ea0bed35c0ffee", user_id=None)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["headers"] = request.headers
        return _runs_api(runs)(request)

    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        headers=pipeline_helpers.internal_headers(),
    ) as api_internal:
        ctx = {"project_id": "project-1", "story_id": "story-1"}
        run = await pipeline_helpers.wait_deploy_run(api_internal, ctx, timeout=1)

    assert run["id"] == "deploy-poll-ea0bed35"
    assert ctx["deploy_head_sha"] == "ea0bed35c0ffee"
    assert seen["params"] == {"story_id": "story-1", "run_type": "deploy"}
    assert seen["headers"]["X-Internal-Key"] == "test-internal-key"
    assert pipeline_helpers.USER_AUTH_HEADER not in seen["headers"]


@pytest.mark.asyncio
async def test_wait_deploy_run_rejects_a_user_scoped_client(monkeypatch):
    """The 2026-07-16 client must fail loudly instead of polling a blind endpoint.

    Proven against the same fake API: as a user it sees nothing, so a wait would
    only ever end in a false timeout.
    """
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    runs = [_deploy_run("deploy-poll-ea0bed35")]
    ctx = {"project_id": "project-1", "story_id": "story-1"}

    async with _runs_client(runs, as_user=True) as api_as_user:
        blind = await api_as_user.get(
            "/api/runs/", params={"story_id": "story-1", "run_type": "deploy"}
        )
        assert blind.json() == []

        with pytest.raises(RuntimeError, match=pipeline_helpers.USER_AUTH_HEADER):
            await pipeline_helpers.wait_deploy_run(api_as_user, ctx, timeout=1)

    assert "deploy_run_id" not in ctx


@pytest.mark.asyncio
async def test_wait_deploy_run_ignores_another_storys_deploy_run(monkeypatch):
    """A project outlives one story: an earlier story's deploy run also carries a
    merged head_sha, and deploying that SHA is not what this mega asserts about.

    The run is checked against this story even if the API were to widen its
    filter, so the contract check cannot silently follow a foreign deploy.
    """
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    runs = [_deploy_run("deploy-poll-other", story_id="story-earlier", head_sha="dead00")]
    ctx = {"project_id": "project-1", "story_id": "story-1"}

    async with httpx.AsyncClient(
        base_url="http://test",
        # A deliberately over-wide API: it ignores the story_id filter entirely.
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=runs)),
        headers=pipeline_helpers.internal_headers(),
    ) as api_internal:
        run = await pipeline_helpers.wait_deploy_run(
            api_internal, ctx, timeout=0.001, poll_interval=0
        )

    assert run is None
    assert "deploy_run_id" not in ctx
    assert "story-1" in ctx["deploy_run_error"]


@pytest.mark.asyncio
async def test_wait_deploy_run_rejects_a_deploy_run_without_a_merged_sha(monkeypatch):
    """An engineering-triggered deploy run records no head_sha, so the mega has no
    ref to check the environment contract at and must not accept it."""
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    ctx = {"project_id": "project-1", "story_id": "story-1"}

    async with _runs_client([_deploy_run("deploy-eng-1", head_sha=None)]) as api_internal:
        run = await pipeline_helpers.wait_deploy_run(
            api_internal, ctx, timeout=0.001, poll_interval=0
        )

    assert run is None
    assert "deploy_run_id" not in ctx
    assert "no deploy run with a merged head_sha" in ctx["deploy_run_error"]


@pytest.mark.asyncio
async def test_wait_deploy_run_times_out_without_a_merged_deploy_run(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    ctx = {"project_id": "project-1", "story_id": "story-1"}

    async with _runs_client([]) as api_internal:
        run = await pipeline_helpers.wait_deploy_run(
            api_internal, ctx, timeout=0.001, poll_interval=0
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


def test_record_env_contract_records_unreachable_probe_instead_of_raising(monkeypatch):
    """A probe that cannot run must not lose the mega's debug artifact.

    record_env_contract is called outside the fixture's try block, so an escaping
    exception skips the `yield ctx` + dump_debug path and the early failure leaves
    no artifact behind. GitHub 5xx, a dead container or a non-zero exit must be
    recorded as the phase error and reported like any other contract failure.
    """
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="502 Bad Gateway"),
    )
    ctx = {"repo_name": "run-repo"}

    assert pipeline_helpers.record_env_contract(ctx, "abc123", phase="merged") is False
    assert "could not run" in ctx["env_contract_errors"]["merged"]
    assert "502 Bad Gateway" in ctx["env_contract_errors"]["merged"]


def test_record_env_contract_records_unparseable_probe_output(monkeypatch):
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="[info] no payload\n", stderr=""),
    )
    ctx = {"repo_name": "run-repo"}

    assert pipeline_helpers.record_env_contract(ctx, "abc123", phase="scaffold") is False
    assert "could not run" in ctx["env_contract_errors"]["scaffold"]
    assert "printed no payload" in ctx["env_contract_errors"]["scaffold"]


def test_record_env_contract_records_container_timeout(monkeypatch):
    """A hung container raises TimeoutExpired out of subprocess, not a RuntimeError."""

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker compose exec", timeout=60)

    monkeypatch.setattr(pipeline_helpers, "docker_exec", timeout)
    ctx = {"repo_name": "run-repo"}

    assert pipeline_helpers.record_env_contract(ctx, "abc123", phase="merged") is False
    assert "TimeoutExpired" in ctx["env_contract_errors"]["merged"]


def test_debug_dump_retains_probe_exception_without_a_probe(monkeypatch, tmp_path):
    """The dump for a phase whose probe never returned still names the reason."""
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    monkeypatch.setattr(
        pipeline_helpers.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="")
    )
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="502 Bad Gateway"),
    )
    ctx = {"project_id": "project-1", "repo_name": "run-repo"}

    assert pipeline_helpers.record_env_contract(ctx, "abc123", phase="merged") is False
    pipeline_helpers.dump_debug(ctx, "probe-exception")

    text = next((tmp_path / "docs" / "e2e_results").glob("debug-probe-exception-*.md")).read_text()
    assert "merged FAILED" in text
    assert "502 Bad Gateway" in text
