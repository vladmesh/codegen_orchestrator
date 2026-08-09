"""A Claude worker must refuse to start without a host-backed config directory."""

from pathlib import Path

import pytest
from worker_wrapper.config import WorkerWrapperConfig, validate_agent_config

# A real mount point that is writable without root — the stand-in for the host
# session directory bind-mounted into a worker container.
MOUNTED_DIR = Path("/dev/shm")  # noqa: S108

needs_mount_point = pytest.mark.skipif(
    not MOUNTED_DIR.is_dir() or not MOUNTED_DIR.is_mount(),
    reason=f"{MOUNTED_DIR} is not a writable mount point here",
)


def make_config(
    agent_type: str,
    claude_config_dir: str | None,
    auth_mode: str = "host_session",
) -> WorkerWrapperConfig:
    return WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="test-worker",
        agent_type=agent_type,
        auth_mode=auth_mode,
        claude_config_dir=claude_config_dir,
    )


def test_worker_reads_the_cli_variable_and_the_auth_mode_from_the_container(monkeypatch):
    """worker-manager exports the broker-only worker launch contract."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/worker/.claude")
    monkeypatch.setenv("WORKER_AUTH_MODE", "api_key")
    monkeypatch.setenv("WORKER_AGENT_TYPE", "claude")
    monkeypatch.setenv("WORKER_BROKER_URL", "http://worker-broker:8001")
    monkeypatch.setenv("WORKER_BROKER_TOKEN", "x" * 43)
    monkeypatch.setenv("WORKER_ID", "dev-1")

    config = WorkerWrapperConfig()

    assert config.claude_config_dir == "/home/worker/.claude"
    assert config.auth_mode == "api_key"
    assert config.worker_id == "dev-1"


def test_claude_worker_without_config_dir_fails_at_startup():
    with pytest.raises(RuntimeError) as exc:
        validate_agent_config(make_config("claude", None))

    assert "CLAUDE_CONFIG_DIR" in str(exc.value)
    assert "HOST_CLAUDE_DIR" in str(exc.value)


def test_claude_worker_with_unmounted_config_dir_fails_at_startup(tmp_path):
    missing = tmp_path / "never-mounted"

    with pytest.raises(RuntimeError) as exc:
        validate_agent_config(make_config("claude", str(missing)))

    assert str(missing) in str(exc.value)
    assert "HOST_CLAUDE_DIR" in str(exc.value)


def test_claude_worker_with_container_local_config_dir_fails_at_startup(tmp_path):
    """A directory that exists but is not a mount belongs to the image — it dies with it."""
    with pytest.raises(RuntimeError) as exc:
        validate_agent_config(make_config("claude", str(tmp_path)))

    assert "not a mounted host directory" in str(exc.value)


@needs_mount_point
def test_claude_worker_with_mounted_config_dir_starts():
    validate_agent_config(make_config("claude", str(MOUNTED_DIR)))


def test_claude_api_key_worker_needs_no_session(tmp_path):
    """api_key mode authenticates per call and keeps no session on the host."""
    validate_agent_config(make_config("claude", str(tmp_path), auth_mode="api_key"))


@pytest.mark.parametrize("agent_type", ["codex", "factory", "noop"])
def test_other_agents_are_untouched(agent_type):
    validate_agent_config(make_config(agent_type, None))
