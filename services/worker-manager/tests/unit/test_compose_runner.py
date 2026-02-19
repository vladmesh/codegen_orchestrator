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
        """run() should build a command with --project-name and --project-directory."""
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
        assert "--project-directory" in call_args

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
        assert ".codegen-network.yml" in call_args

        # Verify the override file was written
        override_path = workspace / "worker-123" / "workspace" / ".codegen-network.yml"
        assert override_path.exists()
        content = override_path.read_text()
        assert "dev_proj_worker-123" in content
        assert "external: true" in content

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
        assert ".codegen-network.yml" not in call_args

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
