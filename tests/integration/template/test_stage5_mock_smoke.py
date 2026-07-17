"""Stage 5 deterministic smoke for the pinned service-template."""

from pathlib import Path
import stat
import subprocess

import pytest
from stage5_mock_smoke import (
    CommandTimeout,
    Stage5Smoke,
    load_production_template,
)


def test_production_template_is_loaded_from_system_config() -> None:
    template = load_production_template()

    assert template.source == "gh:vladmesh/service-template"
    assert template.ref == "0.3.5"


def test_stage5_smoke_uses_an_isolated_workspace_and_project_name(tmp_path: Path) -> None:
    smoke = Stage5Smoke.create(
        tmp_path,
        source="gh:example/service-template",
        ref="candidate-sha",
    )

    assert smoke.workspace.parent == tmp_path
    assert smoke.workspace.name.startswith("stage5-template-")
    assert smoke.compose_project_name.startswith("codegen_stage5_")
    assert smoke.template.source == "gh:example/service-template"
    assert smoke.template.ref == "candidate-sha"
    assert smoke.artifact.name == "template-compat-result.json"


def test_stage5_mock_smoke_runs_the_worker_mode_contract(tmp_path: Path) -> None:
    production = load_production_template()
    smoke = Stage5Smoke.create(tmp_path, source=production.source, ref=production.ref)

    smoke.run()


def test_worker_start_failure_includes_compose_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")

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
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")
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
    assert captured_run_kwargs["timeout"] > 0


def test_command_timeout_reports_phase_and_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")

    def time_out(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["make", "worker-start"], 120)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(CommandTimeout, match=r"worker-start.*make worker-start"):
        smoke._run(["make", "worker-start"], phase="worker-start")


def test_resolved_commit_must_be_a_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")
    smoke.workspace.mkdir()
    (smoke.workspace / ".copier-answers.yml").write_text("_commit: wrong-ref\n")
    with pytest.raises(RuntimeError, match="Copier resolved unexpected commit"):
        smoke._read_resolved_commit("a" * 40)


def test_unadvertised_commit_sha_is_resolved_by_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested_sha = "a" * 40
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref=requested_sha)
    commands: list[list[str]] = []

    def run_git(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        stdout = f"{requested_sha}\n" if "rev-parse" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(
        Stage5Smoke, "_run", lambda _self, command, **kwargs: run_git(command, **kwargs)
    )

    assert smoke._resolve_remote_ref(requested_sha) == requested_sha
    assert any(command[-2:] == [smoke._git_source(), requested_sha] for command in commands)


def test_copier_git_describe_value_matches_requested_commit(tmp_path: Path) -> None:
    resolved = "1a077d9c4644666e74953e4963b04efff11ae999"
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref=resolved)

    assert smoke._recorded_ref_matches("0.2.0-78-g1a077d9", resolved)
    assert not smoke._recorded_ref_matches("0.2.0-78-gdeadbee", resolved)


def test_run_pins_moving_tag_before_copier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pinned_sha = "a" * 40
    events: list[tuple[str, str]] = []
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")

    monkeypatch.setattr(
        Stage5Smoke,
        "_resolve_remote_ref",
        lambda _self, ref: events.append(("resolve", ref)) or pinned_sha,
    )
    monkeypatch.setattr(
        Stage5Smoke,
        "_run_copier",
        lambda _self, ref: events.append(("copier", ref)),
    )
    monkeypatch.setattr(
        Stage5Smoke,
        "_read_resolved_commit",
        lambda _self, expected: events.append(("recorded", expected)) or expected,
    )
    monkeypatch.setattr(Stage5Smoke, "_run_make", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(Stage5Smoke, "_make_workspace_readable", lambda *_args: None)
    monkeypatch.setattr(Stage5Smoke, "_run_worker_start", lambda *_args: None)
    monkeypatch.setattr(Stage5Smoke, "cleanup", lambda *_args: None)

    assert smoke.run() == pinned_sha
    assert events == [
        ("resolve", "candidate"),
        ("copier", pinned_sha),
        ("recorded", pinned_sha),
    ]


def test_workspace_is_readable_by_the_generated_non_root_container(tmp_path: Path) -> None:
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")
    generated_directory = smoke.workspace / "shared" / "generated"
    generated_directory.mkdir(parents=True)
    generated_file = generated_directory / "schemas.py"
    generated_file.write_text("schema = {}\n")
    generated_directory.chmod(0o700)
    generated_file.chmod(0o600)

    smoke._make_workspace_readable()

    assert generated_directory.stat().st_mode & stat.S_IROTH
    assert generated_directory.stat().st_mode & stat.S_IXOTH
    assert generated_file.stat().st_mode & stat.S_IROTH


def test_cleanup_fails_when_compose_down_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")
    compose_file = smoke.workspace / "infra" / "compose.base.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.touch()

    def fail_down(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["docker", "compose", "down"],
            returncode=1,
            stdout="",
            stderr="daemon unavailable",
        )

    monkeypatch.setattr(subprocess, "run", fail_down)

    with pytest.raises(RuntimeError, match=r"(?s)Phase cleanup failed \(1\).*daemon unavailable"):
        smoke.cleanup()


def test_cleanup_verification_fails_when_docker_listing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")

    def fail_list(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["docker", "ps"],
            returncode=1,
            stdout="",
            stderr="cannot connect to daemon",
        )

    monkeypatch.setattr(subprocess, "run", fail_list)

    with pytest.raises(
        RuntimeError,
        match=r"(?s)Phase verify cleanup containers failed \(1\).*cannot connect to daemon",
    ):
        smoke._assert_no_compose_resources()
