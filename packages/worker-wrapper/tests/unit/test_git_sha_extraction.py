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
        assert submitted.worker_report == "Done"

    @pytest.mark.asyncio
    async def test_run_34160792874_mismatch_keeps_final_report_without_replacing_report(
        self, wrapper
    ):
        """A refused completion retains the best existing diagnostic surface."""
        wrapper.broker.submit_output = AsyncMock()
        result = WorkerCompletedResult(
            commit_sha="bad-claim",
            content="Useful final explanation from the agent",
            worker_report="More complete REPORT.md evidence",
        )

        with patch.object(
            wrapper,
            "_pushed_completed_result",
            return_value=(None, "Worker reported commit bad-claim does not match its local HEAD."),
        ):
            await wrapper._submit_checked_result(
                "lease-34160792874", {"branch": "story/story-1"}, result
            )

        submitted = wrapper.broker.submit_output.await_args.args[1]
        assert submitted.status == WorkerResultStatus.FAILED
        assert submitted.worker_report == "More complete REPORT.md evidence"
        assert "Useful final explanation" not in submitted.worker_report

    def test_reported_commit_mismatch_stops_before_push(self, wrapper):
        """The completion boundary refuses a stale claim before any remote write."""
        branch = "story/story-1"
        claimed_sha = "1" * 40
        head_sha = "2" * 40
        reported = MagicMock(returncode=0, stdout=f"{claimed_sha}\n", stderr="")
        head = MagicMock(returncode=0, stdout=f"{head_sha}\n", stderr="")

        with (
            patch.object(wrapper, "_get_git_branch", return_value=branch),
            patch("subprocess.run", side_effect=[reported, head]) as run,
        ):
            result, error = wrapper._pushed_completed_result(
                WorkerCompletedResult(commit_sha=claimed_sha, content="diagnosis"), branch
            )

        assert result is None
        assert error == f"Worker reported commit {claimed_sha} does not match its local HEAD."
        assert run.call_count == 2

    def test_wrong_checkout_branch_stops_before_commit_resolution(self, wrapper):
        """No commit or remote operation runs when the checkout is on another branch."""
        expected = "story/story-1"

        with (
            patch.object(wrapper, "_get_git_branch", return_value="story/other"),
            patch("subprocess.run") as run,
        ):
            result, error = wrapper._pushed_completed_result(
                WorkerCompletedResult(commit_sha="5" * 40, content="Done"), expected
            )

        assert result is None
        assert error == "Worker checkout is on story/other, expected story/story-1."
        run.assert_not_called()

    def test_remote_readback_mismatch_refuses_completion_after_non_force_push(self, wrapper):
        """A successful push is not completion until the configured ref reads back exactly."""
        branch = "story/story-1"
        head_sha = "3" * 40
        other_sha = "4" * 40

        def git_result(*, stdout="", returncode=0, stderr=""):
            return MagicMock(stdout=stdout, returncode=returncode, stderr=stderr)

        with (
            patch.object(wrapper, "_get_git_branch", return_value=branch),
            patch(
                "subprocess.run",
                side_effect=[
                    git_result(stdout=f"{head_sha}\n"),
                    git_result(stdout=f"{head_sha}\n"),
                    git_result(),
                    git_result(stdout=f"{other_sha}\trefs/heads/{branch}\n"),
                ],
            ) as run,
        ):
            result, error = wrapper._pushed_completed_result(
                WorkerCompletedResult(commit_sha=head_sha, content="Done"), branch
            )

        assert result is None
        assert error == f"Worker commit {head_sha} could not be verified on origin/{branch}."
        assert run.call_args_list[2].args[0][1:3] == ["push", "origin"]
        assert not any("--force" in call.args[0] for call in run.call_args_list)

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
        assert submitted.worker_report == "Done"

    @pytest.mark.asyncio
    async def test_cleanup_failure_refuses_completion_and_keeps_best_report(self, wrapper):
        """A tree that cannot be sanitized is never published as completed."""
        wrapper.broker.submit_output = AsyncMock()
        result = WorkerCompletedResult(commit_sha="e" * 40, content="Useful agent report")

        with patch.object(
            wrapper,
            "_pushed_completed_result",
            return_value=(None, "Worker commit could not be sanitized for publication."),
        ):
            await wrapper._submit_checked_result("lease-cleanup", {"branch": "story/test"}, result)

        submitted = wrapper.broker.submit_output.await_args.args[1]
        assert submitted.status == WorkerResultStatus.FAILED
        assert submitted.error == "Worker commit could not be sanitized for publication."
        assert submitted.worker_report == "Useful agent report"
