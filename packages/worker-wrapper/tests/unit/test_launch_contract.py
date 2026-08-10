"""The worker-manager launch environment is the wrapper's sole transport contract."""

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).parents[4]
sys.path.insert(0, str(ROOT / "services" / "worker-manager"))

from worker_wrapper.config import WorkerWrapperConfig, validate_agent_config  # noqa: E402
from worker_wrapper.wrapper import WorkerWrapper  # noqa: E402

from src.container_config import WorkerContainerConfig  # noqa: E402


def _launch_environment() -> dict[str, str]:
    return WorkerContainerConfig(
        worker_id="launch-contract-worker",
        worker_type="developer",
        agent_type="noop",
        capabilities=[],
        auth_mode="api_key",
    ).to_env_vars(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
    )


def test_wrapper_starts_from_exact_manager_environment(monkeypatch):
    environment = _launch_environment()
    monkeypatch.setattr(os, "environ", environment)

    config = WorkerWrapperConfig()
    wrapper = WorkerWrapper(config)

    assert config.worker_id == environment["WORKER_ID"]
    assert wrapper.broker._worker_id == environment["WORKER_ID"]


@pytest.mark.parametrize(
    "forbidden",
    ("WORKER_REDIS_URL", "WORKER_API_URL", "WORKER_MANAGER_URL", "SECRETS_ENCRYPTION_KEY"),
)
def test_wrapper_rejects_any_direct_control_plane_variable(monkeypatch, forbidden):
    environment = _launch_environment() | {forbidden: "must-not-reach-worker"}
    monkeypatch.setattr(os, "environ", environment)

    with pytest.raises(RuntimeError, match="direct worker transport is forbidden"):
        validate_agent_config(WorkerWrapperConfig())
