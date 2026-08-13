import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.codex_auth import validate_codex_host_session
from src.manager import WorkerManager
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType


# Every worker is created for somebody. These tests are not about who, so they
# use one owner; the tests that are about ownership name their own.
_OWNERSHIP = WorkerOwnership(project_id="proj-test", run_id="eng-test", attempt_id="attempt-eng-test")


def _write_profile(path, *, auth_mode=0o600, config_mode=0o600):
    path.mkdir(mode=0o700)
    auth_path = path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                }
            }
        )
    )
    auth_path.chmod(auth_mode)
    config_path = path / "config.toml"
    config_path.write_text('cli_auth_credentials_store = "file"\n')
    config_path.chmod(config_mode)


def test_valid_codex_host_session(tmp_path):
    profile = tmp_path / "codex-worker"
    _write_profile(profile)

    validate_codex_host_session(str(profile))


def test_missing_codex_home_fails_fast(tmp_path):
    with pytest.raises(RuntimeError, match="HOST_CODEX_HOME"):
        validate_codex_host_session(str(tmp_path / "missing"))


@pytest.mark.asyncio
async def test_manager_rejects_missing_codex_session_before_image_resolution(tmp_path):
    docker = MagicMock()
    docker.image_exists = AsyncMock()
    # Creation records what kind of worker this is before it validates anything
    # about the agent, so the Redis double has to be awaitable. The claim under
    # test is unchanged: no image is resolved for a broken Codex session.
    manager = WorkerManager(redis=AsyncMock(), docker_client=docker)

    with pytest.raises(RuntimeError, match="HOST_CODEX_HOME"):
        await manager.create_worker_with_capabilities(
            worker_id="worker-codex",
            capabilities=[],
            base_image="worker-base-codex:latest",
            ownership=_OWNERSHIP,
            agent_type=AgentType.CODEX,
            host_codex_home=str(tmp_path / "missing"),
        )

    docker.image_exists.assert_not_awaited()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda profile: os.chmod(profile, 0o755), "0700"),
        (lambda profile: os.chmod(profile / "auth.json", 0o644), "0600"),
        (lambda profile: (profile / "auth.json").write_text(""), "auth.json"),
        (
            lambda profile: (profile / "auth.json").write_text(json.dumps({"tokens": {"access_token": "test-access"}})),
            "refresh-capable",
        ),
        (
            lambda profile: (profile / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n'),
            "cli_auth_credentials_store",
        ),
    ],
)
def test_unsuitable_codex_session_fails_fast(tmp_path, mutate, message):
    profile = tmp_path / "codex-worker"
    _write_profile(profile)
    mutate(profile)

    with pytest.raises(RuntimeError, match=message):
        validate_codex_host_session(str(profile))
