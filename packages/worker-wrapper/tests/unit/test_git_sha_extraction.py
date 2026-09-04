"""Unit tests for git branch detection in WorkerWrapper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.wrapper import WorkerWrapper, WorkerWrapperConfig

from shared.contracts.queues.worker_result import WorkerCompletedResult, WorkerResultStatus


@pytest.fixture
def wrapper_config():
    return WorkerWrapperConfig(
        broker_url="http://worker-broker:8001",
        broker_token="x" * 43,
        worker_id="test-worker",
        agent_type="claude",
    )


@pytest.fixture
def wrapper(wrapper_config):
    mock_redis = MagicMock()
    mock_redis.redis = AsyncMock()
    return WorkerWrapper(config=wrapper_config, broker_client=mock_redis)


class TestGetGitBranch:
    def test_returns_branch_name(self, wrapper):
        """_get_git_branch returns current branch name."""
        mock_result = MagicMock()
        mock_result.stdout = "story/story-123\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._get_git_branch()

        assert result == "story/story-123"

    def test_returns_none_for_detached_head(self, wrapper):
        """_get_git_branch returns None when HEAD is detached."""
        mock_result = MagicMock()
        mock_result.stdout = "HEAD\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._get_git_branch()

        assert result is None

    def test_returns_none_on_failure(self, wrapper):
        """_get_git_branch returns None when git command fails."""
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stderr = "fatal: not a git repository"

        with patch("subprocess.run", return_value=mock_result):
            result = wrapper._get_git_branch()

        assert result is None

    def test_returns_none_on_exception(self, wrapper):
        """_get_git_branch returns None on any exception."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = wrapper._get_git_branch()

        assert result is None


class TestCompletedResultPush:
    def test_pushes_local_head_and_canonicalizes_reported_abbreviation(self, wrapper):
        """A successful result is not publishable until its exact local HEAD is remote."""
        full_sha = "a" * 40
        branch = "story/story-123"
        reported = "a" * 7

        def git_result(*, stdout="", returncode=0, stderr=""):
            result = MagicMock()
            result.stdout = stdout
            result.returncode = returncode
            result.stderr = stderr
            return result

        with (
            patch.object(wrapper, "_get_git_branch", return_value=branch),
            patch(
                "subprocess.run",
                side_effect=[
                    git_result(stdout=f"{full_sha}\n"),  # resolve reported SHA
                    git_result(stdout=f"{full_sha}\n"),  # local HEAD
                    git_result(),  # push
                    git_result(stdout=f"{full_sha}\trefs/heads/{branch}\n"),  # remote ref
                ],
            ) as run,
        ):
            result, error = wrapper._pushed_completed_result(
                WorkerCompletedResult(commit_sha=reported, content="Done"), branch
            )

        assert error is None
        assert result == WorkerCompletedResult(commit_sha=full_sha, content="Done")
        assert run.call_args_list[0].args[0][3] == "--end-of-options"
        assert run.call_args_list[2].args[0] == [
            "/usr/bin/git",
            "push",
            "origin",
            f"HEAD:refs/heads/{branch}",
        ]

    def test_refuses_success_when_push_is_rejected(self, wrapper):
        """A local commit cannot become a completed worker result when Git rejects its push."""
        full_sha = "b" * 40
        branch = "story/story-456"

        def git_result(*, stdout="", returncode=0, stderr=""):
            result = MagicMock()
            result.stdout = stdout
            result.returncode = returncode
            result.stderr = stderr
            return result

        with (
            patch.object(wrapper, "_get_git_branch", return_value=branch),
            patch(
                "subprocess.run",
                side_effect=[
                    git_result(stdout=f"{full_sha}\n"),
                    git_result(stdout=f"{full_sha}\n"),
                    git_result(returncode=1, stderr="rejected"),
                ],
            ),
        ):
            result, error = wrapper._pushed_completed_result(
                WorkerCompletedResult(commit_sha=full_sha, content="Done"), branch
            )

        assert result is None
        assert error == f"Worker commit {full_sha} could not be pushed to origin/{branch}."

    @pytest.mark.asyncio
    async def test_publishes_failure_when_commit_push_cannot_be_verified(self, wrapper):
        """The broker receives a typed failure instead of an unpushed completed result."""
        result = WorkerCompletedResult(commit_sha="c" * 40, content="Done")
        wrapper.broker.submit_output = AsyncMock()

        with patch.object(
            wrapper,
            "_pushed_completed_result",
            return_value=(None, "Worker commit could not be verified on origin/story/story-789."),
        ):
            await wrapper._submit_checked_result("lease-1", {"branch": "story/story-789"}, result)

        submitted = wrapper.broker.submit_output.await_args.args[1]
        assert submitted.status == WorkerResultStatus.FAILED
        assert submitted.error == "Worker commit could not be verified on origin/story/story-789."

    @pytest.mark.asyncio
    async def test_publishes_failure_when_completed_result_has_no_branch(self, wrapper):
        """A developer cannot claim success without the configured remote target."""
        wrapper.broker.submit_output = AsyncMock()

        await wrapper._submit_checked_result(
            "lease-2", {}, WorkerCompletedResult(commit_sha="d" * 40, content="Done")
        )

        submitted = wrapper.broker.submit_output.await_args.args[1]
        assert submitted.status == WorkerResultStatus.FAILED
        assert submitted.error == "Worker completed without the configured story branch."
