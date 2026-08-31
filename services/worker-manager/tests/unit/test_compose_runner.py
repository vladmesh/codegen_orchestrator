import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from src.compose_runner import ComposeInvocation, ComposeRunner, _write_snapshot
from src.compose_validator import RESOURCE_IDENTITY_POLICY, validate_effective_compose

SAFE_EFFECTIVE_CONFIG = (
    '{"services":{"db":{"image":"postgres:16","networks":{"default":null},"deploy":{"resources":'
    '{"limits":{"cpus":"1.0","memory":"512M"}}}}},'
    '"networks":{"default":{"name":"dev_proj_worker-123","external":true}}}'
)


def _safe_compose_result():
    result = MagicMock()
    result.returncode = 0
    result.stdout = SAFE_EFFECTIVE_CONFIG
    result.stderr = ""
    return result


def _real_docker_available() -> bool:
    executable = shutil.which("docker")
    if executable is None:
        return False
    return subprocess.run([executable, "info"], capture_output=True, check=False).returncode == 0


@pytest.fixture
def workspace(tmp_path):
    """Create a fake workspace directory structure for worker-123."""
    ws = tmp_path / "worker-123" / "workspace"
    infra = ws / "infra"
    infra.mkdir(parents=True)
    # Place minimal compose files matching service-template layout
    (infra / "compose.base.yml").write_text("services:\n  db:\n    image: postgres:16\n")
    (infra / "compose.dev.yml").write_text("services:\n  db:\n    ports:\n      - '5432:5432'\n")
    return tmp_path


class TestComposeRunner:
    def test_snapshot_compiler_rejects_retained_loader_directives(self, tmp_path):
        snapshot = tmp_path / "compose.resolved.yml"
        invocation = ComposeInvocation(
            command=[],
            config_command=[],
            cwd=tmp_path,
            env={},
            source_files=[],
            workspace_path=tmp_path,
            project_directory=tmp_path,
            project_name="worker_worker-123",
            snapshot_path=snapshot,
        )

        with pytest.raises(ValueError, match="label_file cannot be retained"):
            _write_snapshot(invocation, {"services": {"app": {"label_file": ["/etc/passwd"]}}}, ["up"])

        assert not snapshot.exists()

    def test_snapshot_compiler_rejects_unadmitted_build_properties(self, tmp_path):
        snapshot = tmp_path / "compose.resolved.yml"
        invocation = ComposeInvocation(
            command=[],
            config_command=[],
            cwd=tmp_path,
            env={},
            source_files=[],
            workspace_path=tmp_path,
            project_directory=tmp_path,
            project_name="worker_worker-123",
            snapshot_path=snapshot,
        )

        with pytest.raises(ValueError, match="build.cache_to is not supported"):
            _write_snapshot(
                invocation,
                {"services": {"app": {"build": {"context": str(tmp_path), "cache_to": ["type=local"]}}}},
                ["build"],
            )

        assert not snapshot.exists()

    def test_snapshot_compiler_rejects_daemon_global_resource_identities(self, tmp_path):
        snapshot = tmp_path / "compose.resolved.yml"
        invocation = ComposeInvocation(
            command=[],
            config_command=[],
            cwd=tmp_path,
            env={},
            source_files=[],
            workspace_path=tmp_path,
            project_directory=tmp_path,
            project_name="worker_worker-123",
            snapshot_path=snapshot,
        )

        with pytest.raises(ValueError, match="container_name"):
            _write_snapshot(
                invocation,
                {"services": {"app": {"container_name": "worker-manager"}}},
                ["up"],
            )

        assert not snapshot.exists()

    @pytest.mark.asyncio
    async def test_builds_correct_command(self, workspace):
        """run() should build a command with --project-name and run from workspace cwd."""
        runner = ComposeRunner(str(workspace))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "done\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            exit_code, stdout, stderr = await runner.run("worker-123", ["ps"])

        assert exit_code == 0
        call_args = mock_run.call_args[0][0]  # first positional arg = cmd list
        assert "--project-name" in call_args
        assert "worker_worker-123" in call_args
        # Recovery runs from the manager-owned plan directory.
        _, call_kwargs = mock_run.call_args
        assert call_kwargs["cwd"] == str(workspace / ".compose-plans" / "worker-123")

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, workspace):
        """run() should raise ValueError on path traversal in cwd."""
        runner = ComposeRunner(str(workspace))

        with pytest.raises(ValueError, match="traversal"):
            await runner.run("worker-123", ["up", "-d"], cwd="../../etc")

    @pytest.mark.asyncio
    async def test_network_override_generated_for_up(self, workspace):
        """run() with 'up' should write .codegen-network.yml and include it in args."""
        runner = ComposeRunner(str(workspace))

        mock_result = _safe_compose_result()

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["up", "-d"])

        config_args = mock_run.call_args_list[0].args[0]
        call_args = mock_run.call_args_list[1].args[0]
        assert "infra/compose.base.yml" in config_args
        assert "infra/compose.dev.yml" in config_args
        override_path = workspace / ".compose-plans" / "worker-123" / ".codegen-network.yml"
        assert str(override_path) in config_args
        snapshot_path = workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml"
        assert str(snapshot_path) in call_args

        # Verify the override file was written with default network pointing to dev network
        assert override_path.exists()
        content = override_path.read_text()
        assert "dev_proj_worker-123" in content
        assert "default:" in content
        assert "external: true" in content
        # No project-db alias or services section (workaround removed in #22)
        assert "project-db" not in content
        assert "services:" not in content

    @pytest.mark.asyncio
    async def test_network_override_with_user_file_flags(self, workspace):
        """When user passes -f, network override should come after user files."""
        runner = ComposeRunner(str(workspace))

        # Create the user-specified compose file
        infra = workspace / "worker-123" / "workspace" / "infra"
        infra.mkdir(parents=True, exist_ok=True)
        (infra / "compose.yml").write_text("services:\n  db:\n    image: postgres:16\n")

        mock_result = _safe_compose_result()

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            args = ["-f", "infra/compose.yml", "up", "-d"]
            await runner.run("worker-123", args)

        call_args = mock_run.call_args[0][0]
        assert str(workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml") in call_args

    @pytest.mark.asyncio
    async def test_no_network_override_for_ps(self, workspace):
        """run() with 'ps' (non-container-starting cmd) should NOT inject network override."""
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run", return_value=_safe_compose_result()) as mock_run:
            await runner.run("worker-123", ["ps"])

        call_args = mock_run.call_args[0][0]
        assert ".codegen-network.yml" not in " ".join(call_args)

    @pytest.mark.asyncio
    async def test_env_vars_passed(self, workspace):
        """run() should pass HOST_UID, HOST_GID and custom env vars to subprocess."""
        runner = ComposeRunner(str(workspace))

        with pytest.raises(ValueError, match="environment"):
            await runner.run("worker-123", ["up", "-d"], env={"MY_VAR": "hello"})

    @pytest.mark.asyncio
    async def test_env_file_injected_when_exists(self, workspace):
        """run() should pass --env-file when .env exists in workspace root."""
        ws = workspace / "worker-123" / "workspace"
        (ws / ".env").write_text("FOO=bar\n")

        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run", return_value=_safe_compose_result()) as mock_run:
            await runner.inspect("worker-123", ["up", "-d"])

        call_args = mock_run.call_args[0][0]
        assert "--env-file" in call_args
        env_file_idx = call_args.index("--env-file")
        assert str(ws / ".env") == call_args[env_file_idx + 1]

    @pytest.mark.asyncio
    async def test_workspace_dir_override(self, tmp_path):
        """run() with workspace_dir should use the given path instead of deriving from worker_id."""
        # Workspace is NOT at base_path/worker-123, it's at a separate location
        actual_ws = tmp_path / "project-uuid" / "workspace"
        infra = actual_ws / "infra"
        infra.mkdir(parents=True)
        (infra / "compose.base.yml").write_text("services:\n  db:\n    image: postgres:16\n")
        (infra / "compose.dev.yml").write_text("services:\n  db:\n    ports:\n      - '5432:5432'\n")

        runner = ComposeRunner(str(tmp_path))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["ps"], workspace_dir=str(actual_ws))

        _, call_kwargs = mock_run.call_args
        assert call_kwargs["cwd"] == str(tmp_path / ".compose-plans" / "worker-123")

    @pytest.mark.asyncio
    async def test_missing_workspace_raises_value_error(self, tmp_path):
        """run() should raise ValueError (not FileNotFoundError) when workspace doesn't exist."""
        runner = ComposeRunner(str(tmp_path))

        with pytest.raises(ValueError, match="[Ww]orkspace.*does not exist"):
            await runner.run("nonexistent-worker", ["up", "-d", "--wait", "db"])

    @pytest.mark.asyncio
    async def test_subprocess_timeout_raises_value_error(self, workspace):
        """run() should raise ValueError (not TimeoutExpired) when compose times out."""
        runner = ComposeRunner(str(workspace))

        with patch(
            "subprocess.run",
            side_effect=[_safe_compose_result(), subprocess.TimeoutExpired(cmd="docker compose", timeout=1)],
        ):
            with pytest.raises(ValueError, match="[Tt]imed? ?out"):
                await runner.run("worker-123", ["up", "-d"], timeout=1)

    @pytest.mark.asyncio
    async def test_ports_override_generated_for_up(self, workspace):
        """run() with 'up' should write .codegen-ports.yml clearing published ports."""
        runner = ComposeRunner(str(workspace))

        mock_result = _safe_compose_result()

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["up", "-d"])

        call_args = mock_run.call_args[0][0]
        snapshot_path = workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml"
        assert str(snapshot_path) in call_args
        assert "ports: []" in snapshot_path.read_text()

    @pytest.mark.asyncio
    async def test_ports_override_not_generated_for_ps(self, workspace):
        """run() with 'ps' should NOT inject ports override."""
        runner = ComposeRunner(str(workspace))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            await runner.run("worker-123", ["ps"])

        override_path = workspace / "worker-123" / "workspace" / ".codegen-ports.yml"
        assert not override_path.exists()

    @pytest.mark.asyncio
    async def test_ports_override_order(self, workspace):
        """Ports override should come after network override (last override wins)."""
        runner = ComposeRunner(str(workspace))

        mock_result = _safe_compose_result()

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["up", "-d"])

        call_args = mock_run.call_args[0][0]
        assert str(workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml") in call_args

    @pytest.mark.asyncio
    async def test_limits_override_is_in_the_inspected_and_executed_invocation(self, workspace):
        runner = ComposeRunner(str(workspace))
        mock_result = _safe_compose_result()

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _, prepared = await runner.inspect("worker-123", ["up", "-d"])
            await runner.run("worker-123", ["up", "-d"], prepared=prepared)

        snapshot_path = workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml"
        assert str(snapshot_path) in mock_run.call_args_list[1].args[0]
        assert "cpus: '1.0'" in snapshot_path.read_text()
        assert "memory: 512M" in snapshot_path.read_text()

    @pytest.mark.asyncio
    async def test_real_service_template_resolution_passes_the_production_validator(self, tmp_path):
        fixture = Path(__file__).parents[4] / (
            "shared/tests/fixtures/service-template-edf54dfb1c323d60480761e06ceb982bd79ac9d2"
        )
        workspace = tmp_path / "workspace"
        shutil.copytree(fixture, workspace)
        (workspace / ".env").write_text("POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=postgres\nPOSTGRES_DB=service\n")
        runner = ComposeRunner(str(tmp_path))

        resolved, _ = await runner.inspect("fixture", ["up", "-d"], workspace_dir=str(workspace))

        result = validate_effective_compose(resolved, "fixture", workspace)
        assert result.valid, result.errors
        assert resolved["services"]["db"]["image"] == "postgres:16"
        assert "name" not in resolved["volumes"]["db_data"]

    def test_service_template_has_no_label_file_compatibility_consumer(self):
        fixture = Path(__file__).parents[4] / (
            "shared/tests/fixtures/service-template-edf54dfb1c323d60480761e06ceb982bd79ac9d2"
        )

        assert all("label_file" not in source.read_text() for source in (fixture / "infra").glob("compose*.yml"))

    @pytest.mark.asyncio
    async def test_real_documented_integration_resolution_passes_the_production_validator(self, tmp_path):
        fixture = Path(__file__).parents[4] / (
            "shared/tests/fixtures/service-template-edf54dfb1c323d60480761e06ceb982bd79ac9d2"
        )
        workspace = tmp_path / "workspace"
        shutil.copytree(fixture, workspace)
        (workspace / ".env").write_text("POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=postgres\nPOSTGRES_DB=service\n")
        (workspace / "infra" / ".env.test").write_text("POSTGRES_PASSWORD=postgres\n")
        runner = ComposeRunner(str(tmp_path))

        resolved, _ = await runner.inspect(
            "fixture",
            ["-f", "infra/compose.tests.integration.yml", "run", "integration-tests"],
            workspace_dir=str(workspace),
        )

        result = validate_effective_compose(resolved, "fixture", workspace)
        assert result.valid, result.errors

    @pytest.mark.asyncio
    async def test_documented_integration_source_flow_is_compiled(self, tmp_path):
        fixture = Path(__file__).parents[4] / (
            "shared/tests/fixtures/service-template-edf54dfb1c323d60480761e06ceb982bd79ac9d2"
        )
        workspace = tmp_path / "workspace"
        shutil.copytree(fixture, workspace)
        (workspace / ".env").write_text("POSTGRES_USER=postgres\n")
        (workspace / "infra" / ".env.test").write_text("POSTGRES_PASSWORD=postgres\n")
        runner = ComposeRunner(str(tmp_path))

        with patch("subprocess.run", return_value=_safe_compose_result()) as mock_run:
            _, plan = await runner.inspect(
                "worker-123",
                ["-f", "infra/compose.tests.integration.yml", "run", "integration-tests"],
                workspace_dir=str(workspace),
            )

        assert plan.snapshot_path is not None
        assert "compose.tests.integration.yml" in " ".join(mock_run.call_args.args[0])

    @pytest.mark.asyncio
    async def test_ports_override_with_redis(self, workspace):
        """Ports override should clear ports for all services that publish them."""
        # Add redis with ports to compose.dev.yml
        infra = workspace / "worker-123" / "workspace" / "infra"
        (infra / "compose.dev.yml").write_text(
            "services:\n"
            "  db:\n"
            "    ports:\n"
            "      - '5432:5432'\n"
            "  redis:\n"
            "    ports:\n"
            "      - '6379:6379'\n"
            "  backend:\n"
            "    command: uvicorn main:app\n"
        )
        runner = ComposeRunner(str(workspace))

        mock_result = _safe_compose_result()

        with patch("subprocess.run", return_value=mock_result):
            await runner.run("worker-123", ["up", "-d"])

        snapshot_path = workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml"
        text = snapshot_path.read_text()
        assert "ports: []" in text

    @pytest.mark.asyncio
    async def test_orchestrator_env_is_not_passed_to_compose(self, workspace, monkeypatch):
        """The agent owns the compose file, and compose interpolates ${VAR} from
        this environment into it, so worker-manager's own secrets must not be in it."""
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "orchestrator-platform-key")
        monkeypatch.setenv("POSTGRES_PASSWORD", "orchestrator-db-password")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        runner = ComposeRunner(str(workspace))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["ps"])

        env = mock_run.call_args[1]["env"]
        assert "SECRETS_ENCRYPTION_KEY" not in env
        assert "POSTGRES_PASSWORD" not in env
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOST_UID"] == "1000"

    @pytest.mark.asyncio
    async def test_project_dot_env_still_reaches_compose(self, workspace, monkeypatch):
        """The project's own .env is what compose is supposed to interpolate."""
        (workspace / "worker-123" / "workspace" / ".env").write_text("# project settings\nAPP_SECRET=project-value\n")
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "orchestrator-platform-key")
        runner = ComposeRunner(str(workspace))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["ps"])

        env = mock_run.call_args[1]["env"]
        assert "APP_SECRET" not in env
        assert "SECRETS_ENCRYPTION_KEY" not in env

    @pytest.mark.asyncio
    async def test_inspect_resolves_the_same_fixed_scope_as_execution(self, workspace):
        """The preflight config command carries the generated network and fixed project name."""
        runner = ComposeRunner(str(workspace))
        config = (
            '{"services":{"db":{"image":"postgres:16","networks":{"default":null},"deploy":{"resources":'
            '{"limits":{"cpus":"1.0","memory":"512M"}}}}},'
            '"networks":{"default":{"name":"dev_proj_worker-123","external":true}}}'
        )
        mock_result = MagicMock(returncode=0, stdout=config, stderr="")

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            resolved, prepared = await runner.inspect("worker-123", ["up", "-d"])
            await runner.run("worker-123", ["up", "-d"], prepared=prepared)

        assert resolved["networks"]["default"]["name"] == "dev_proj_worker-123"
        config_command = mock_run.call_args_list[0].args[0]
        execution_command = mock_run.call_args_list[1].args[0]
        assert config_command[:4] == [config_command[0], "compose", "--project-name", "worker_worker-123"]
        project_directory_index = config_command.index("--project-directory")
        assert config_command[project_directory_index + 1] == str(workspace / "worker-123" / "workspace" / "infra")
        assert ".codegen-network.yml" in " ".join(config_command)
        assert config_command[-3:] == ["config", "--format", "json"]
        assert "--project-directory" in execution_command
        assert "compose.resolved.yml" in " ".join(execution_command)

    @pytest.mark.asyncio
    async def test_direct_container_start_revalidates_before_execution(self, workspace):
        """ComposeRunner has no unvalidated container-creating execution path."""
        runner = ComposeRunner(str(workspace))
        config = (
            '{"services":{"db":{"image":"postgres:16","networks":{"default":null},"privileged":true,"deploy":{"resources":'
            '{"limits":{"cpus":"1.0","memory":"512M"}}}}},'
            '"networks":{"default":{"name":"dev_proj_worker-123","external":true}}}'
        )
        mock_result = MagicMock(returncode=0, stdout=config, stderr="")

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            with pytest.raises(ValueError, match="privileged"):
                await runner.run("worker-123", ["up", "-d"])

        assert mock_run.call_count == 1
        assert mock_run.call_args.args[0][-3:] == ["config", "--format", "json"]

    @pytest.mark.asyncio
    async def test_direct_run_scope_flag_is_rejected_before_resolution(self, workspace):
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="--volume"):
                await runner.run("worker-123", ["run", "--volume=/:/host", "db"])

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_source_only_path_escape_is_rejected_before_compose_reads_it(self, workspace):
        compose = workspace / "worker-123" / "workspace" / "infra" / "compose.dev.yml"
        compose.write_text("services:\n  db:\n    image: postgres:16\n    env_file: /etc/passwd\n")
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="env_file"):
                await runner.run("worker-123", ["up", "-d"])

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label_file", ["/etc/passwd", "../../HOSTSECRET.env", "${HOME}/HOSTSECRET.env"])
    async def test_label_file_is_rejected_before_compose_or_snapshot(self, workspace, label_file):
        root = workspace / "worker-123" / "workspace"
        (root / "infra" / "compose.base.yml").write_text(
            f"services:\n  db:\n    image: postgres:16\n    label_file: {label_file}\n"
        )
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="label_file is not supported"):
                await runner.inspect("worker-123", ["-f", "infra/compose.base.yml", "up", "-d"])

        mock_run.assert_not_called()
        assert not (workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml").exists()

    @pytest.mark.asyncio
    async def test_multi_file_source_paths_use_the_first_file_project_directory(self, workspace):
        root = workspace / "worker-123" / "workspace"
        nested = root / "a" / "b" / "c" / "d" / "e"
        nested.mkdir(parents=True)
        (nested / "override.yml").write_text(
            "services:\n  db:\n    image: postgres:16\n    env_file: ../../../../HOSTSECRET.env\n"
        )
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="env_file"):
                await runner.inspect(
                    "worker-123",
                    ["-f", "infra/compose.base.yml", "-f", "a/b/c/d/e/override.yml", "up", "-d"],
                )

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("env_file", "project_env"),
        [("${EVIL}", "EVIL=../../HOSTSECRET.env\n"), ("${HOME}/HOSTSECRET.env", None)],
    )
    async def test_interpolated_env_file_is_rejected_before_compose_resolution(self, workspace, env_file, project_env):
        root = workspace / "worker-123" / "workspace"
        if project_env:
            (root / ".env").write_text(project_env)
        (root / "infra" / "compose.base.yml").write_text(
            f"services:\n  db:\n    image: postgres:16\n    env_file: {env_file}\n"
        )
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="interpolation"):
                await runner.inspect("worker-123", ["-f", "infra/compose.base.yml", "up", "-d"])

        mock_run.assert_not_called()
        assert not (workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml").exists()

    @pytest.mark.asyncio
    async def test_interpolated_extends_file_is_rejected_before_compose_resolution(self, workspace):
        root = workspace / "worker-123" / "workspace"
        (root / "infra" / "compose.base.yml").write_text(
            "services:\n  db:\n    extends: {file: '${EVIL}', service: db}\n"
        )
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="interpolation"):
                await runner.inspect("worker-123", ["-f", "infra/compose.base.yml", "up", "-d"])

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_extends_source_paths_use_the_extended_file_directory(self, workspace):
        root = workspace / "worker-123" / "workspace"
        (root / "infra" / "compose.base.yml").write_text(
            "services:\n  db:\n    extends: {file: ../evil.yml, service: db}\n"
        )
        (root / "evil.yml").write_text("services:\n  db:\n    image: postgres:16\n    env_file: ../HOSTSECRET.env\n")
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="env_file"):
                await runner.inspect("worker-123", ["-f", "infra/compose.base.yml", "up", "-d"])

        mock_run.assert_not_called()
        assert not (workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml").exists()

    @pytest.mark.asyncio
    async def test_nested_extends_targets_use_each_declaring_directory(self, workspace):
        root = workspace / "worker-123" / "workspace"
        deep = root / "deep"
        deep.mkdir()
        (root / "infra" / "compose.base.yml").write_text(
            "services:\n  db:\n    extends: {file: ../deep/mid.yml, service: db}\n"
        )
        (deep / "mid.yml").write_text("services:\n  db:\n    extends: {file: leaf.yml, service: db}\n")
        (root / "infra" / "leaf.yml").write_text("services:\n  db:\n    image: postgres:16\n")
        (deep / "leaf.yml").write_text("services:\n  db:\n    image: postgres:16\n    env_file: /etc/passwd\n")
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="env_file"):
                await runner.inspect("worker-123", ["-f", "infra/compose.base.yml", "up", "-d"])

        mock_run.assert_not_called()
        assert not (workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [["build"], ["up", "--build"]])
    async def test_build_network_is_rejected_before_create_execution(self, workspace, args):
        compose = workspace / "worker-123" / "workspace" / "infra" / "compose.dev.yml"
        compose.write_text(
            "services:\n  db:\n    build:\n      context: ..\n      dockerfile: Dockerfile\n      network: host\n"
        )
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="build network"):
                await runner.run("worker-123", args)

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [["build"], ["up", "--build"], ["up", "-d"]])
    @pytest.mark.parametrize("build_key", ["cache_from", "cache_to"])
    async def test_build_cache_is_rejected_before_every_create_route(self, workspace, args, build_key):
        victim_snapshot = workspace / ".compose-plans" / "worker-victim" / "compose.resolved.yml"
        victim_snapshot.parent.mkdir(parents=True)
        victim_snapshot.write_text("victim plan\n")
        cache_value = "type=local,src=/etc" if build_key == "cache_from" else f"type=local,dest={victim_snapshot}"
        compose = workspace / "worker-123" / "workspace" / "infra" / "compose.dev.yml"
        compose.write_text(
            f"services:\n  db:\n    build:\n      context: ..\n      {build_key}:\n        - {cache_value}\n"
        )
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match=f"build {build_key} is not supported"):
                await runner.run("worker-123", args)

        mock_run.assert_not_called()
        assert victim_snapshot.is_file()
        assert victim_snapshot.read_text() == "victim plan\n"
        assert not (workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [["build"], ["up", "--build"], ["up", "-d"]])
    async def test_manager_replaces_build_output_tag_for_every_create_route(self, workspace, args):
        compose = workspace / "worker-123" / "workspace" / "infra" / "compose.dev.yml"
        compose.write_text(
            "services:\n  db:\n    image: codegen-orchestrator/victim:latest\n    build:\n      context: ..\n"
        )
        resolved = _safe_compose_result()
        resolved.stdout = json.dumps(
            {
                "services": {
                    "db": {
                        "image": "codegen-orchestrator/victim:latest",
                        "build": {"context": str(workspace / "worker-123" / "workspace")},
                        "networks": {"default": None},
                        "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "512M"}}},
                    }
                },
                "networks": {"default": {"name": "dev_proj_worker-123", "external": True}},
            }
        )
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run", return_value=resolved):
            await runner.run("worker-123", args)

        snapshot = workspace / ".compose-plans" / "worker-123" / "compose.resolved.yml"
        assert RESOURCE_IDENTITY_POLICY.build_image("worker-123", "db") in snapshot.read_text()
        assert "codegen-orchestrator/victim:latest" not in snapshot.read_text()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [
            "services:\n  db:\n    image: postgres:16\n    container_name: worker-manager\n",
            "services:\n  db:\n    image: postgres:16\nvolumes:\n  data:\n    name: victim-data\n",
        ],
    )
    async def test_daemon_global_resource_identities_are_rejected_before_resolution(self, workspace, content):
        compose = workspace / "worker-123" / "workspace" / "infra" / "compose.dev.yml"
        compose.write_text(content)
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="container_name|Volume 'data': name"):
                await runner.run("worker-123", ["up", "-d"])

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_real_daemon_resource_identity_policy_preserves_victim_resources(self, tmp_path):
        if not _real_docker_available():
            pytest.skip("Docker daemon is unavailable")
        token = uuid4().hex
        worker_id = f"probe-{token[:12]}"
        victim_image = f"codegen-1163-victim-{token}:latest"
        victim_volume = f"codegen-1163-volume-{token}"
        victim_container = f"codegen-1163-container-{token}"
        output_image = RESOURCE_IDENTITY_POLICY.build_image(worker_id, "app")
        workspace = tmp_path / "workspace"
        infra = workspace / "infra"
        infra.mkdir(parents=True)
        dockerfile = workspace / "Dockerfile"
        dockerfile.write_text("FROM scratch\nLABEL marker=victim\n")
        docker = shutil.which("docker")
        assert docker is not None

        def docker_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            return subprocess.run([docker, *args], check=check, capture_output=True, text=True)

        docker_run("build", "-t", victim_image, str(workspace))
        victim_before = docker_run("image", "inspect", "--format", "{{.Id}}", victim_image).stdout.strip()
        dockerfile.write_text("FROM scratch\nLABEL marker=worker\n")
        (infra / "compose.base.yml").write_text(
            f"services:\n  app:\n    image: {victim_image}\n    build:\n      context: ..\n"
        )
        (infra / "compose.dev.yml").write_text("services: {}\n")
        runner = ComposeRunner(str(tmp_path))
        try:
            await runner.run(worker_id, ["build"], workspace_dir=str(workspace))
            assert docker_run("image", "inspect", "--format", "{{.Id}}", victim_image).stdout.strip() == victim_before
            assert docker_run("image", "inspect", output_image).returncode == 0

            docker_run("volume", "create", victim_volume)
            docker_run("create", "--name", victim_container, victim_image, "/bin/false")
            (infra / "compose.base.yml").write_text(
                "services:\n"
                "  app:\n"
                f"    image: {victim_image}\n"
                f"    container_name: {victim_container}\n"
                "    volumes:\n"
                "      - data:/data\n"
                "volumes:\n"
                "  data:\n"
                f"    name: {victim_volume}\n"
            )
            with pytest.raises(ValueError, match="container_name.*name"):
                await runner.run(worker_id, ["up", "-d"], workspace_dir=str(workspace))

            assert docker_run("volume", "inspect", victim_volume).returncode == 0
            assert docker_run("container", "inspect", victim_container).returncode == 0
            assert not (tmp_path / ".compose-plans" / "victim").exists()
        finally:
            docker_run("container", "rm", "-f", victim_container, check=False)
            docker_run("volume", "rm", victim_volume, check=False)
            docker_run("image", "rm", "-f", output_image, check=False)
            docker_run("image", "rm", "-f", victim_image, check=False)

    @pytest.mark.asyncio
    async def test_recovery_rejects_worker_selected_compose_files(self, workspace):
        runner = ComposeRunner(str(workspace))

        with patch("subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="Recovery.*file"):
                await runner.run("worker-123", ["-f", "/etc/shadow", "ps"])

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_preserves_logs_follow_and_tail_flags(self, workspace):
        runner = ComposeRunner(str(workspace))
        result = MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", return_value=result) as mock_run:
            await runner.run("worker-123", ["logs", "-f", "--tail", "10"])

        command = mock_run.call_args.args[0]
        assert command[-4:] == ["logs", "-f", "--tail", "10"]
        assert "--project-directory" in command

    @pytest.mark.asyncio
    async def test_execution_uses_immutable_snapshot_after_source_mutation(self, workspace):
        runner = ComposeRunner(str(workspace))
        config_result = _safe_compose_result()
        source = workspace / "worker-123" / "workspace" / "infra" / "compose.dev.yml"

        with patch("subprocess.run", return_value=config_result) as mock_run:
            _, plan = await runner.inspect("worker-123", ["up", "-d"])
            source.write_text("services:\n  db:\n    privileged: true\n")
            await runner.run("worker-123", ["up", "-d"], prepared=plan)

        execution = mock_run.call_args_list[1].args[0]
        assert str(plan.snapshot_path) in execution
        assert "compose.dev.yml" not in execution
        assert plan.snapshot_path is not None
        assert plan.snapshot_path.stat().st_mode & 0o777 == 0o600
