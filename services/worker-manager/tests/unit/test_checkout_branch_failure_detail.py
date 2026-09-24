"""A failed checkout says why: exit code, both streams, and the container's end.

Production recorded `checkout_branch_failed error=b''` four times in a row
(story-39fbe87a). The exec had not failed in git at all: the worker container
had exited under it on a configuration error, Docker reported the exec as
killed with no output, and the one line that named the cause was in the
container's own log, which nothing read.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
