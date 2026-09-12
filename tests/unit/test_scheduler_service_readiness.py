"""Behavior of the shared production/stand scheduler readiness probe."""

import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).parents[2] / "scripts" / "wait_scheduler_services.sh"


def _fake_docker(tmp_path: Path) -> Path:
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -eu
counter="$(cat "${FAKE_COUNTER_PATH}" 2>/dev/null || echo 0)"
counter=$((counter + 1))
echo "$counter" > "${FAKE_COUNTER_PATH}"
if [ "$1" = "inspect" ]; then
  if [ "${FAKE_SCHEDULER_MODE}" = "down" ]; then
    echo 'false|4'
  elif [ "${FAKE_SCHEDULER_MODE}" = "restart-change" ]; then
    echo "true|${counter}"
  else
    echo 'true|0'
  fi
  exit 0
fi
for arg in "$@"; do
  if [ "$arg" = "ps" ]; then
    if [ "${FAKE_SCHEDULER_MODE}" = "id-change" ]; then
      echo "scheduler-container-${counter}"
    else
      echo 'scheduler-container-id'
    fi
    exit 0
  fi
  if [ "$arg" = "exec" ]; then
    [ "${FAKE_SCHEDULER_MODE}" != "missing-startup" ] || exit 1
    for service in "$@"; do
      case "$service" in
        scheduler-pipeline|scheduler-infrastructure|scheduler-maintenance)
          echo "$service"
          exit 0
          ;;
      esac
    done
  fi
  if [ "$arg" = "logs" ]; then
    echo 'recent diagnostic log'
    exit 0
  fi
done
"""
    )
    docker.chmod(0o755)
    return docker


def _run_probe(
    tmp_path: Path, *, mode: str, timeout_seconds: int = 2
) -> subprocess.CompletedProcess[str]:
    _fake_docker(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_SCHEDULER_MODE": mode,
        "FAKE_COUNTER_PATH": str(tmp_path / "counter"),
        "SCHEDULER_READINESS_POLL_SECONDS": "0.1",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), str(timeout_seconds), "-f", "docker-compose.yml"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_probe_accepts_stable_processes_without_startup_logs(tmp_path):
    # The fake's logs contain no startup marker: readiness is durable process
    # state, so a busy long-running service cannot lose its evidence to log rotation.
    result = _run_probe(tmp_path, mode="healthy", timeout_seconds=5)

    assert result.returncode == 0
    assert "ready and stable" in result.stdout


def test_probe_reports_each_service_when_processes_do_not_stay_up(tmp_path):
    result = _run_probe(tmp_path, mode="down")

    assert result.returncode == 1
    assert "scheduler-pipeline state" in result.stderr
    assert "scheduler-infrastructure state" in result.stderr
    assert "scheduler-maintenance state" in result.stderr


def test_probe_rejects_a_running_container_whose_restart_count_changes(tmp_path):
    assert _run_probe(tmp_path, mode="restart-change").returncode == 1


def test_probe_rejects_replaced_running_containers(tmp_path):
    assert _run_probe(tmp_path, mode="id-change").returncode == 1


def test_probe_rejects_a_running_process_without_startup_readiness(tmp_path):
    assert _run_probe(tmp_path, mode="missing-startup").returncode == 1
