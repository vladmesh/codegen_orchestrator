"""The backend fixture supplies the repository contract production requires."""

import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.contracts.queues.worker import AgentType, CreateWorkerCommand, WorkerOwnership
from shared.contracts.worker_evidence import RemovalFact, RemovedWorkerEvidence
from tests.integration.backend import conftest as backend


@pytest.mark.subprocess
def test_scaffolded_repository_matches_worker_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "WORKSPACE_BASE_PATH", str(tmp_path))
    repo_id = backend._create_scaffolded_workspace()
    workspace = tmp_path / repo_id
    origin = subprocess.check_output(
        ["git", "-C", str(workspace), "remote", "get-url", "origin"], text=True
    ).strip()

    extra_env = {"FIXTURE_ASSERTION": "preserved"}
    config = backend.scaffolded_worker_config(
        repo_id,
        name="fixture-contract",
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="Keep these instructions.",
        allowed_commands=[],
        capabilities=[],
        ownership=WorkerOwnership(project_id="project", run_id="run", attempt_id="attempt"),
        env_vars=extra_env,
    )

    assert config.repo_id == repo_id
    assert origin == f"https://github.com/{config.env_vars['REPO_NAME']}.git"
    assert repo_id in config.env_vars["REPO_NAME"]
    assert "GITHUB_TOKEN" not in config.env_vars and "GH_TOKEN" not in config.env_vars
    assert config.env_vars["FIXTURE_ASSERTION"] == "preserved"
    assert extra_env == {"FIXTURE_ASSERTION": "preserved"}
    assert subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"])


@pytest.mark.parametrize("key", ["REPO_NAME"])
def test_scaffolded_config_refuses_to_normalize_invalid_credentials(key):
    with pytest.raises(ValueError, match="Use WorkerConfig directly for invalid repository input"):
        backend.scaffolded_worker_config("repository", env_vars={key: ""})


def test_removed_startup_observation_requires_native_exit_and_logs():
    owner = WorkerOwnership(project_id="project", run_id="run", attempt_id="attempt")
    missed = RemovalFact.missed("unused in this observation")
    evidence = RemovedWorkerEvidence(
        worker_id="worker",
        container="worker-worker",
        ownership=owner,
        removed_at="2026-10-04T00:00:00Z",
        worker_type=missed,
        agent_type=missed,
        image=missed,
        state=RemovalFact.read({"status": "exited"}),
        exit_code=RemovalFact.read(1),
        log_tail=RemovalFact.read("profile not writable"),
        transcript_dir=missed,
    )
    diagnostics = backend.removed_worker_lifecycle_diagnostics(evidence, "worker", owner)
    assert "status=exited" in diagnostics
    assert "exit_code=1" in diagnostics
    assert "not writable" in diagnostics
    for field in ("state", "exit_code", "log_tail"):
        with pytest.raises(AssertionError):
            backend.removed_worker_lifecycle_diagnostics(
                evidence.model_copy(update={field: missed}), "worker", owner
            )
    with pytest.raises(AssertionError):
        backend.removed_worker_lifecycle_diagnostics(evidence, "foreign", owner)


@pytest.mark.asyncio
async def test_cleanup_keeps_foreign_workers_and_shared_streams(monkeypatch):
    owner = WorkerOwnership(project_id="project", run_id="run", attempt_id="attempt")
    neighbour = WorkerOwnership(project_id="other", run_id="other-run", attempt_id="other-attempt")

    def command(name, ownership):
        return CreateWorkerCommand(
            request_id=name,
            config=backend.scaffolded_worker_config(
                "repo",
                name=name,
                worker_type="developer",
                agent_type=AgentType.CLAUDE,
                instructions="test",
                allowed_commands=[],
                capabilities=[],
                ownership=ownership,
            ),
        )

    own = command("own-worker", owner)
    foreign = command("foreign-worker", neighbour)
    entries = [
        ("1-0", {"data": own.model_dump_json()}),
        ("2-0", {"data": foreign.model_dump_json()}),
    ]
    redis = MagicMock()
    redis.xrange = AsyncMock(return_value=entries)
    redis.delete = AsyncMock()
    redis.xdel = AsyncMock()

    async def empty_scan(**kwargs):
        for key in ():
            yield key

    redis.scan_iter = empty_scan
    client = MagicMock()
    client.containers.list.return_value = []
    client.networks.list.return_value = []
    monkeypatch.setattr(backend.docker, "DockerClient", lambda **kwargs: client)
    delete = AsyncMock()
    monkeypatch.setattr(backend, "delete_test_worker", delete)
    await backend.cleanup_owned_worker_resources(redis, [owner])
    delete.assert_awaited_once_with(redis, client, "own-worker")
    assert all(call.args[1] == "1-0" for call in redis.xdel.await_args_list)
    assert all("other-run" not in str(call) for call in redis.delete.await_args_list)
    assert not any("worker:commands" in call.args for call in redis.delete.await_args_list)
    client.containers.list.assert_called_once_with(
        all=True, filters={"label": "com.codegen.run.id=run"}
    )
