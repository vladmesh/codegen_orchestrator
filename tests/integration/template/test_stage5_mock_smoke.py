"""Stage 5 deterministic smoke for the pinned service-template."""

from pathlib import Path
import stat
import subprocess

import pytest
from stage5_mock_smoke import (
    KIT_PACKAGE,
    CommandTimeout,
    Stage5Smoke,
    load_production_template,
    read_active_packages,
    read_listed_packages,
)

from scripts.template_pin import TEMPLATE_PIN


def test_production_template_is_loaded_from_system_config() -> None:
    """The smoke runs the pin the orchestrator deploys, not a copy typed out here."""
    template = load_production_template()

    assert template.source == TEMPLATE_PIN.source
    assert template.ref == TEMPLATE_PIN.ref


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


def test_a_tag_pin_accepts_its_own_record_and_not_a_bare_short_sha(tmp_path: Path) -> None:
    """A tagged template makes Copier record the tag, which is the pinned ref itself."""
    resolved = "9f3c1ab5e0d24c7a8b61f0d3e2c4a5b6d7e8f901"
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="9.9.9")

    assert smoke._recorded_ref_matches("9.9.9", resolved)
    assert not smoke._recorded_ref_matches("9f3c1ab", resolved)
    assert not smoke._recorded_ref_matches("9f3c1ab5e0d24c7a8b61f0d3e2c4a5b6d7e8f9", resolved)


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
    monkeypatch.setattr(Stage5Smoke, "_exercise_generated_access_lifecycle", lambda *_args: None)
    monkeypatch.setattr(
        Stage5Smoke,
        "_prove_kit_package_install",
        lambda _self, ref: events.append(("package", ref)),
    )
    monkeypatch.setattr(Stage5Smoke, "cleanup", lambda *_args: None)

    assert smoke.run() == pinned_sha
    assert events == [
        ("resolve", "candidate"),
        ("copier", pinned_sha),
        ("recorded", pinned_sha),
        ("package", pinned_sha),
    ]


def test_generated_access_lifecycle_uses_capability_and_bot_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/template", ref="candidate")
    calls: list[tuple[str, str, str]] = []

    def capture_worker(_self: Stage5Smoke, service: str, source: str, *, phase: str) -> None:
        calls.append((service, source, phase))

    monkeypatch.setattr(Stage5Smoke, "_run_service_python", capture_worker)
    smoke._exercise_generated_access_lifecycle()

    assert [service for service, _, _ in calls] == ["backend", "tg_bot"]
    backend_source = calls[0][1]
    assert "X-Grant-Capability" in backend_source
    assert '"/grant"' in backend_source
    assert '"/revoke"' in backend_source
    assert backend_source.count('"/access?channel=telegram&external_id=8202532144"') == 2
    assert "USERS_GRANT_CAPABILITY" in backend_source
    assert "enforce_access" in calls[1][1]
    assert "ApplicationHandlerStop" in calls[1][1]


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


def _write_product(root: Path, *, listed: list[str], generated: list[dict[str, str]]) -> Path:
    (root / "codegen_kit").mkdir(parents=True)
    (root / "codegen_kit/_active_packages.py").write_text(
        '"""Package identities used to generate this product contract."""\n\n'
        f"ACTIVE_PACKAGES: list[dict[str, str]] = {generated!r}\n"
    )
    (root / "services/backend").mkdir(parents=True)
    (root / "services/backend/manifest.yaml").write_text(f"version: 1\npackages: {listed!r}\n")
    return root


def test_generated_and_listed_package_sets_are_read_from_the_product(tmp_path: Path) -> None:
    identity = {"manifest_sha256": "0" * 64, "name": "reminders", "version": "0.1.0"}
    product = _write_product(tmp_path / "product", listed=["reminders"], generated=[identity])

    assert read_active_packages(product) == [identity]
    assert read_listed_packages(product) == ["reminders"]


def test_a_product_without_packages_reads_as_empty_on_both_sides(tmp_path: Path) -> None:
    product = _write_product(tmp_path / "product", listed=[], generated=[])

    assert read_active_packages(product) == []
    assert read_listed_packages(product) == []


def test_package_wheel_is_built_from_the_kit_source_at_the_pinned_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = "b" * 40
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/codegen-product-kit", ref="9.9.9")
    commands: list[list[str]] = []

    def record(_self: Stage5Smoke, command: list[str], **_kwargs: object) -> None:
        commands.append(command)
        if command[:2] == ["uv", "build"]:
            wheels = smoke.package_workspace / "wheels"
            wheels.mkdir(parents=True)
            (wheels / "codegen_kit_reminders-0.1.0-py3-none-any.whl").write_text("")

    monkeypatch.setattr(Stage5Smoke, "_run", record)

    wheel = smoke._build_package_wheel(pinned)

    assert wheel.name == "codegen_kit_reminders-0.1.0-py3-none-any.whl"
    assert commands[0][:3] == ["git", "clone", "--quiet"]
    assert commands[1] == [
        "git",
        "-C",
        str(smoke.package_workspace / "kit"),
        "checkout",
        "--quiet",
        pinned,
    ]
    assert commands[2][:3] == ["uv", "build", "--wheel"]
    assert commands[2][3].endswith("packages/codegen-kit-reminders")


def test_package_install_proof_runs_kit_add_on_its_own_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = "c" * 40
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/codegen-product-kit", ref="9.9.9")
    product = smoke.package_workspace / "product"
    wheel = tmp_path / "codegen_kit_reminders-0.1.0-py3-none-any.whl"
    wheel.write_text("")
    identity = {"manifest_sha256": "1" * 64, "name": "reminders", "version": "0.1.0"}
    renders: list[Path] = []
    kit_add: list[list[str]] = []

    def render(_self: Stage5Smoke, ref: str, destination: Path, **_kwargs: object) -> None:
        assert ref == pinned
        renders.append(destination)
        _write_product(destination, listed=[], generated=[])

    def install(_self: Stage5Smoke, command: list[str], **_kwargs: object) -> None:
        kit_add.append(command)
        _write_product(
            smoke.package_workspace / "installed", listed=["reminders"], generated=[identity]
        )
        for name in ("codegen_kit/_active_packages.py", "services/backend/manifest.yaml"):
            (product / name).write_text((smoke.package_workspace / "installed" / name).read_text())
        (product / "services/backend/packages").mkdir(parents=True)
        (product / "services/backend/packages" / wheel.name).write_text("")

    monkeypatch.setattr(Stage5Smoke, "_run_copier", render)
    monkeypatch.setattr(Stage5Smoke, "_run_make", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(Stage5Smoke, "_build_package_wheel", lambda _self, _ref: wheel)
    monkeypatch.setattr(Stage5Smoke, "_run", install)

    smoke._prove_kit_package_install(pinned)

    assert renders == [product]
    assert kit_add == [[str(product / ".venv/bin/kit"), "add", KIT_PACKAGE, "--wheel", str(wheel)]]
    assert smoke.installed_packages == [identity]


def test_package_install_proof_fails_when_the_generated_contract_stays_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = Stage5Smoke.create(tmp_path, source="gh:example/codegen-product-kit", ref="9.9.9")
    wheel = tmp_path / "codegen_kit_reminders-0.1.0-py3-none-any.whl"
    wheel.write_text("")

    monkeypatch.setattr(
        Stage5Smoke,
        "_run_copier",
        lambda _self, _ref, destination, **_kwargs: _write_product(
            destination, listed=[], generated=[]
        ),
    )
    monkeypatch.setattr(Stage5Smoke, "_run_make", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(Stage5Smoke, "_build_package_wheel", lambda _self, _ref: wheel)
    monkeypatch.setattr(Stage5Smoke, "_run", lambda *_args, **_kwargs: None)

    with pytest.raises(AssertionError, match="generated contract does not record the package"):
        smoke._prove_kit_package_install("d" * 40)
