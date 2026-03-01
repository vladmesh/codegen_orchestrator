"""Unit tests for git SHA extraction in WorkerWrapper.

Tests _get_git_head(), _extract_git_commit_sha(), and execute_agent()
integration with git-based commit SHA detection.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from conftest import MockProcess
import pytest
from worker_wrapper.wrapper import WorkerWrapper, WorkerWrapperConfig

FAKE_SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
FAKE_SHA_2 = "f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3b2a1f6e5"


@pytest.fixture
def wrapper_config():
    return WorkerWrapperConfig(
        redis_url="redis://localhost",
        input_stream="in",
        output_stream="out",
        consumer_group="grp",
        consumer_name="worker-1",
        agent_type="claude",
    )


@pytest.fixture
def wrapper(wrapper_config):
    mock_redis = MagicMock()
    mock_redis.redis = AsyncMock()
    return WorkerWrapper(config=wrapper_config, redis_client=mock_redis)


class TestGetGitHead:
    def test_returns_sha_when_git_available(self, wrapper):
        """_get_git_head returns SHA string when inside a git repo."""
        mock_result = MagicMock()
        mock_result.stdout = f"{FAKE_SHA}\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = wrapper._get_git_head()

        assert result == FAKE_SHA
        mock_run.assert_called_once()
        # Verify it runs in WORKSPACE_DIR
        assert mock_run.call_args.kwargs["cwd"] == "/workspace"

    def test_returns_none_when_not_a_repo(self, wrapper):
        """_get_git_head returns None when /workspace is not a git repo."""
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stderr = "fatal: not a git repository"

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._get_git_head()

        assert result is None

    def test_returns_none_on_exception(self, wrapper):
        """_get_git_head returns None on any exception (FileNotFoundError, etc)."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = wrapper._get_git_head()

        assert result is None


class TestExtractGitCommitSha:
    def test_returns_new_sha_when_head_changed(self, wrapper):
        """Returns new SHA when HEAD differs from initial_head (new commit detected)."""
        mock_result = MagicMock()
        mock_result.stdout = f"{FAKE_SHA_2}\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._extract_git_commit_sha(initial_head=FAKE_SHA)

        assert result == FAKE_SHA_2

    def test_returns_none_when_head_unchanged(self, wrapper):
        """Returns None when HEAD is same as initial (no new commit)."""
        mock_result = MagicMock()
        mock_result.stdout = f"{FAKE_SHA}\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._extract_git_commit_sha(initial_head=FAKE_SHA)

        assert result is None

    def test_returns_sha_when_initial_head_none_and_commits_exist(self, wrapper):
        """When initial_head=None (empty repo) and agent made first commit, returns SHA."""
        mock_result = MagicMock()
        mock_result.stdout = f"{FAKE_SHA}\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._extract_git_commit_sha(initial_head=None)

        assert result == FAKE_SHA

    def test_returns_none_on_git_failure(self, wrapper):
        """Returns None when git command fails (non-zero exit)."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._extract_git_commit_sha(initial_head=FAKE_SHA)

        assert result is None

    def test_returns_none_on_exception(self, wrapper):
        """Returns None on any exception (timeout, missing binary, etc)."""
        with patch("subprocess.run", side_effect=OSError("timeout")):
            result = wrapper._extract_git_commit_sha(initial_head=FAKE_SHA)

        assert result is None


class TestExecuteAgentGitIntegration:
    """Integration tests: execute_agent() uses git SHA extraction."""

    @pytest.mark.asyncio
    async def test_git_sha_added_when_no_result_tags(self, wrapper):
        """When agent produces no <result> tags, git SHA is added to output."""
        mock_process = MockProcess(
            stdout=b"Agent finished without structured output",
            stderr=b"",
            returncode=0,
        )

        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch.object(wrapper, "_get_git_head", return_value=FAKE_SHA),
            patch.object(wrapper, "_extract_git_commit_sha", return_value=FAKE_SHA_2),
        ):
            mock_exec.return_value = mock_process
            result = await wrapper.execute_agent({"content": "build something"})

        assert result["commit_sha"] == FAKE_SHA_2

    @pytest.mark.asyncio
    async def test_git_sha_overrides_agent_sha(self, wrapper):
        """Git SHA is authoritative — overrides agent's commit_sha if both present."""
        agent_sha = "agent000000000000000000000000000000000000"
        mock_stdout = (
            f'<result>{{"status": "success", "commit_sha": "{agent_sha}"}}</result>'
        ).encode()
        mock_process = MockProcess(stdout=mock_stdout, stderr=b"", returncode=0)

        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch.object(wrapper, "_get_git_head", return_value=FAKE_SHA),
            patch.object(wrapper, "_extract_git_commit_sha", return_value=FAKE_SHA_2),
        ):
            mock_exec.return_value = mock_process
            result = await wrapper.execute_agent({"content": "build something"})

        assert result["commit_sha"] == FAKE_SHA_2

    @pytest.mark.asyncio
    async def test_agent_sha_preserved_when_git_fails(self, wrapper):
        """When git extraction fails (None), agent's commit_sha is preserved."""
        agent_sha = "agent000000000000000000000000000000000000"
        mock_stdout = (
            f'<result>{{"status": "success", "commit_sha": "{agent_sha}"}}</result>'
        ).encode()
        mock_process = MockProcess(stdout=mock_stdout, stderr=b"", returncode=0)

        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch.object(wrapper, "_get_git_head", return_value=None),
            patch.object(wrapper, "_extract_git_commit_sha", return_value=None),
        ):
            mock_exec.return_value = mock_process
            result = await wrapper.execute_agent({"content": "build something"})

        assert result["commit_sha"] == agent_sha

    @pytest.mark.asyncio
    async def test_initial_head_captured_before_subprocess(self, wrapper):
        """_get_git_head is called BEFORE subprocess starts."""
        call_order = []

        def tracking_get_git_head():
            call_order.append("get_git_head")
            return FAKE_SHA

        mock_process = MockProcess(
            stdout=b'<result>{"status": "success"}</result>',
            stderr=b"",
            returncode=0,
        )

        async def tracking_subprocess(*args, **kwargs):
            call_order.append("subprocess")
            return mock_process

        with (
            patch("asyncio.create_subprocess_exec", side_effect=tracking_subprocess),
            patch.object(wrapper, "_get_git_head", side_effect=tracking_get_git_head),
            patch.object(wrapper, "_extract_git_commit_sha", return_value=None),
        ):
            await wrapper.execute_agent({"content": "test"})

        assert call_order.index("get_git_head") < call_order.index("subprocess")
