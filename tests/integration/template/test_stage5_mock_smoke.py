"""Stage 5 deterministic smoke for the pinned service-template."""

from pathlib import Path
import subprocess

import pytest
from stage5_mock_smoke import Stage5Smoke


def test_stage5_smoke_uses_an_isolated_workspace_and_project_name(tmp_path: Path) -> None:
    smoke = Stage5Smoke.create(tmp_path)

    assert smoke.workspace.parent == tmp_path
    assert smoke.workspace.name.startswith("stage5-template-")
    assert smoke.compose_project_name.startswith("codegen_stage5_")
    assert smoke.template == "gh:vladmesh/service-template"
    assert smoke.template_ref == "0.3.0"


def test_stage5_mock_smoke_runs_the_worker_mode_contract(tmp_path: Path) -> None:
    smoke = Stage5Smoke.create(tmp_path)

    smoke.run()


def test_worker_start_failure_includes_compose_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = Stage5Smoke.create(tmp_path)

    def fail_worker_start(_self: Stage5Smoke, _target: str, *_variables: str) -> None:
        raise RuntimeError("worker-start failed")

    compose_logs = subprocess.CompletedProcess(
        args=["docker", "compose", "logs"],
        returncode=0,
        stdout="backend-1 | startup traceback",
        stderr="",
    )
    monkeypatch.setattr(Stage5Smoke, "_run_make", fail_worker_start)
    monkeypatch.setattr(Stage5Smoke, "_run", lambda *_args, **_kwargs: compose_logs)

    with pytest.raises(RuntimeError, match="startup traceback"):
        smoke._run_worker_start()


def test_commands_use_reproducible_host_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = Stage5Smoke.create(tmp_path)
    captured_environment: dict[str, str] = {}
    captured_run_kwargs: dict[str, object] = {}

    def capture_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_run_kwargs.update(kwargs)
        captured_environment.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", capture_run)

    smoke._run(["true"])

    assert captured_environment["HOST_UID"] == str(tmp_path.stat().st_uid)
    assert captured_environment["HOST_GID"] == str(tmp_path.stat().st_gid)
    assert callable(captured_run_kwargs["preexec_fn"])
