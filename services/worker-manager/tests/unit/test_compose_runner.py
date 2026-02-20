import pytest
from unittest.mock import MagicMock, patch

from src.compose_runner import ComposeRunner


@pytest.fixture
def workspace(tmp_path):
    """Create a fake workspace directory structure for worker-123."""
    ws = tmp_path / "worker-123" / "workspace"
    ws.mkdir(parents=True)
    # Place a minimal docker-compose.yml
    (ws / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres:16\n")
    return tmp_path


class TestComposeRunner:
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
        # Subprocess should run from the workspace directory
        _, call_kwargs = mock_run.call_args
        ws_path = str(workspace / "worker-123" / "workspace")
        assert call_kwargs["cwd"] == ws_path

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, workspace):
        """run() should raise ValueError on path traversal in cwd."""
        runner = ComposeRunner(str(workspace))

        with pytest.raises(ValueError, match="traversal"):
            await runner.run("worker-123", ["ps"], cwd="../../etc")

    @pytest.mark.asyncio
    async def test_network_override_generated_for_up(self, workspace):
        """run() with 'up' should write .codegen-network.yml and include it in args."""
        runner = ComposeRunner(str(workspace))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["up", "-d"])

        call_args = mock_run.call_args[0][0]
        assert "-f" in call_args
        # Network override should be referenced by absolute path
        override_path = workspace / "worker-123" / "workspace" / ".codegen-network.yml"
        assert str(override_path) in call_args

        # Verify the override file was written with default network pointing to dev network
        assert override_path.exists()
        content = override_path.read_text()
        assert "dev_proj_worker-123" in content
        assert "default:" in content
        assert "external: true" in content

    @pytest.mark.asyncio
    async def test_network_override_with_user_file_flags(self, workspace):
        """When user passes -f, network override should come after user files."""
        runner = ComposeRunner(str(workspace))

        # Create the user-specified compose file
        infra = workspace / "worker-123" / "workspace" / "infra"
        infra.mkdir(parents=True, exist_ok=True)
        (infra / "compose.yml").write_text("services:\n  db:\n    image: postgres:16\n")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["-f", "infra/compose.yml", "up", "-d"])

        call_args = mock_run.call_args[0][0]
        # User file should come before network override
        user_f_idx = call_args.index("infra/compose.yml")
        override_path = str(workspace / "worker-123" / "workspace" / ".codegen-network.yml")
        override_idx = call_args.index(override_path)
        assert user_f_idx < override_idx

    @pytest.mark.asyncio
    async def test_no_network_override_for_ps(self, workspace):
        """run() with 'ps' (non-container-starting cmd) should NOT inject network override."""
        runner = ComposeRunner(str(workspace))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["ps"])

        call_args = mock_run.call_args[0][0]
        assert ".codegen-network.yml" not in " ".join(call_args)

    @pytest.mark.asyncio
    async def test_env_vars_passed(self, workspace):
        """run() should pass HOST_UID, HOST_GID and custom env vars to subprocess."""
        runner = ComposeRunner(str(workspace))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["ps"], env={"MY_VAR": "hello"})

        _, call_kwargs = mock_run.call_args
        env = call_kwargs["env"]
        assert env["HOST_UID"] == "1000"
        assert env["HOST_GID"] == "1000"
        assert env["MY_VAR"] == "hello"

    @pytest.mark.asyncio
    async def test_env_file_injected_when_exists(self, workspace):
        """run() should pass --env-file when .env exists in workspace root."""
        ws = workspace / "worker-123" / "workspace"
        (ws / ".env").write_text("FOO=bar\n")

        runner = ComposeRunner(str(workspace))

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            await runner.run("worker-123", ["ps"])

        call_args = mock_run.call_args[0][0]
        assert "--env-file" in call_args
        env_file_idx = call_args.index("--env-file")
        assert str(ws / ".env") == call_args[env_file_idx + 1]
