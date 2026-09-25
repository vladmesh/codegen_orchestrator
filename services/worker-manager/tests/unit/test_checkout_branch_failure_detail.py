"""A failed checkout says why: exit code, both streams, and the container's end.

Production recorded `checkout_branch_failed error=b''` four times in a row
(story-39fbe87a). The exec had not failed in git at all: the worker container
had exited under it on a configuration error, Docker reported the exec as
killed with no output, and the one line that named the cause was in the
container's own log, which nothing read.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import structlog.testing

from src import git_ops

_BRANCH = "story/story-39fbe87a"


def _docker(exit_code, stdout=b"", stderr=b"", *, state=None, logs=""):
    docker = MagicMock()
    docker.exec_capture = AsyncMock(return_value=(exit_code, stdout, stderr))
    docker.inspect_container = AsyncMock(return_value={"State": state or {"Running": True}})
    docker.read_container_logs = AsyncMock(return_value=logs)
    return docker


async def test_stderr_and_exit_code_are_logged_and_returned():
    docker = _docker(128, b"", b"fatal: unable to access 'https://github.com/o/r/': 403")

    with structlog.testing.capture_logs() as logs:
        result = await git_ops.checkout_branch(docker, "cid", _BRANCH, "w-1")

    assert not result
    assert "exit_code=128" in result.detail
    assert "403" in result.detail
    failed = next(entry for entry in logs if entry["event"] == "checkout_branch_failed")
    assert failed["exit_code"] == 128
    assert "403" in failed["stderr"]
    # A script that spoke is not blamed on the container.
    docker.inspect_container.assert_not_awaited()


async def test_a_silent_failure_reads_the_dead_containers_log():
    reason = "CLAUDE_CONFIG_DIR is not writable by the worker user: /home/worker/.claude."
    docker = _docker(
        137,
        state={"Running": False, "Status": "exited", "ExitCode": 1},
        logs=f"[critical ] configuration_error error='{reason}'\n",
    )

    with structlog.testing.capture_logs() as logs:
        result = await git_ops.checkout_branch(docker, "cid", _BRANCH, "w-1")

    assert not result
    assert "exit_code=137" in result.detail
    assert "status=exited" in result.detail
    assert reason in result.detail
    failed = next(entry for entry in logs if entry["event"] == "checkout_branch_failed")
    assert reason in failed["container"]


async def test_a_silent_failure_in_a_live_container_says_so():
    docker = _docker(1)

    result = await git_ops.checkout_branch(docker, "cid", _BRANCH, "w-1")

    assert not result
    assert "no output" in result.detail
    assert "still running" in result.detail


async def test_an_uninspectable_container_is_still_an_account():
    docker = _docker(137)
    docker.inspect_container = AsyncMock(side_effect=RuntimeError("No such container"))

    result = await git_ops.checkout_branch(docker, "cid", _BRANCH, "w-1")

    assert not result
    assert "could not be inspected" in result.detail
    assert "No such container" in result.detail


async def test_a_token_in_git_output_is_redacted():
    docker = _docker(
        128,
        b"",
        b"fatal: repository 'https://x-access-token:ghs_SECRET@github.com/o/r/' not found",
    )

    result = await git_ops.checkout_branch(docker, "cid", _BRANCH, "w-1")

    assert "ghs_SECRET" not in result.detail
    assert "https://***@github.com/o/r/" in result.detail


async def test_repository_not_found_twice_then_checkout_completes():
    missing = (128, b"", b"remote: Repository not found.\n")
    docker = _docker(0)
    docker.exec_capture.side_effect = [missing, missing, (0, b"origin/story/x", b"")]
    sleep = AsyncMock()

    with patch("src.git_ops.asyncio.sleep", sleep), structlog.testing.capture_logs() as logs:
        result = await git_ops.checkout_branch(docker, "cid", _BRANCH, "w-1")

    assert result.ok
    assert docker.exec_capture.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2]
    retries = [entry for entry in logs if entry["event"] == "checkout_branch_retry"]
    assert [entry["attempt"] for entry in retries] == [1, 2]
    assert not any(entry["event"] == "checkout_branch_failed" for entry in logs)


async def test_repository_not_found_exhausts_the_shared_schedule():
    docker = _docker(128, b"", b"remote: Repository not found.\n")
    sleep = AsyncMock()

    with patch("src.git_ops.asyncio.sleep", sleep), structlog.testing.capture_logs() as logs:
        result = await git_ops.checkout_branch(docker, "cid", _BRANCH, "w-1")

    assert not result
    assert "exit_code=128" in result.detail
    assert "stderr: remote: Repository not found." in result.detail
    assert "attempts=6" in result.detail
    assert docker.exec_capture.await_count == 6
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4, 8, 15]
    assert [entry["event"] for entry in logs].count("checkout_branch_retry") == 5
    assert [entry["event"] for entry in logs].count("checkout_branch_failed") == 1


async def test_authentication_failure_does_not_retry():
    docker = _docker(128, b"", b"fatal: Authentication failed")
    sleep = AsyncMock()

    with patch("src.git_ops.asyncio.sleep", sleep), structlog.testing.capture_logs() as logs:
        result = await git_ops.checkout_branch(docker, "cid", _BRANCH, "w-1")

    assert not result
    assert "Authentication failed" in result.detail
    assert docker.exec_capture.await_count == 1
    sleep.assert_not_awaited()
    assert not any(entry["event"] == "checkout_branch_retry" for entry in logs)


async def test_exec_capture_keeps_streams_apart():
    from src.docker_ops import DockerClientWrapper

    container = MagicMock()
    container.exec_run = MagicMock(return_value=(2, (None, b"boom")))
    wrapper = DockerClientWrapper.__new__(DockerClientWrapper)
    wrapper.get_container = AsyncMock(return_value=container)

    async def run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    wrapper._run = run

    assert await wrapper.exec_capture("cid", "false") == (2, b"", b"boom")
    assert container.exec_run.call_args.kwargs["demux"] is True
