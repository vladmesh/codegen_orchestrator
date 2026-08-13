"""Ownership travels with a spawn request, and a spawn without it never happens.

Both halves are written into the create command the spawner publishes, because
worker-manager stamps them on the container at creation. Nothing downstream can
recover them afterwards: the container may already be dead by the time anybody
looks, and its Redis metadata is deleted with it.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.worker import WorkerOwnership


def _mock_settings():
    """Minimal settings for request_spawn."""
    s = MagicMock()
    s.redis_url = "redis://localhost:6379"
    return s


@pytest.mark.asyncio
@patch("src.clients.worker_spawner.get_settings", return_value=_mock_settings())
@patch("src.prompts.load_developer_instructions", return_value="test instructions")
@patch("redis.asyncio.Redis.from_url")
async def test_request_spawn_includes_ownership_in_command(
    mock_redis_from_url, mock_instructions, mock_settings
):
    """The project and the engineering run both reach worker-manager."""
    # Setup mock redis
    mock_redis = AsyncMock()
    mock_redis_from_url.return_value = mock_redis

    # Make xgroup_create succeed
    mock_redis.xgroup_create = AsyncMock()

    # Capture what xadd receives
    captured_commands = []

    async def capture_xadd(stream, data):
        captured_commands.append((stream, data))
        return "msg-id"

    mock_redis.xadd = capture_xadd

    # Make xreadgroup return a creation response immediately
    mock_redis.xreadgroup = AsyncMock(
        side_effect=[
            # First call: creation response
            [
                (
                    b"worker:responses:developer",
                    [
                        (
                            b"1-0",
                            {
                                b"data": json.dumps(
                                    {
                                        "request_id": None,  # Will be overwritten
                                        "success": True,
                                        "worker_id": "test-worker-id",
                                    }
                                ).encode()
                            },
                        )
                    ],
                )
            ],
            # Second call: worker output
            [
                (
                    b"worker:test-worker-id:output",
                    [
                        (
                            b"2-0",
                            {
                                b"data": json.dumps(
                                    {
                                        "status": "completed",
                                        "content": "done",
                                        "commit_sha": "abc123",
                                    }
                                ).encode()
                            },
                        )
                    ],
                )
            ],
        ]
    )
    mock_redis.xack = AsyncMock()
    mock_redis.xgroup_destroy = AsyncMock()
    mock_redis.aclose = AsyncMock()

    from src.clients.worker_spawner import request_spawn

    # Patch the request_id matching — the mock response won't match,
    # so let's use a simpler approach: just verify the xadd payload
    await request_spawn(
        repo="org/repo",
        github_token="ghs_test",  # noqa: S106
        task_content="build it",
        ownership=WorkerOwnership(project_id="proj-456", run_id="eng-789"),
        timeout_seconds=5,
    )

    # First xadd is the create command
    assert len(captured_commands) >= 1
    stream, data = captured_commands[0]
    assert stream == "worker:commands"

    payload = json.loads(data["data"])
    assert payload["config"]["ownership"] == {"project_id": "proj-456", "run_id": "eng-789"}
    assert payload["context"]["project_id"] == "proj-456"


@pytest.mark.asyncio
async def test_request_spawn_cannot_be_called_without_ownership():
    """There is no unowned spawn to fall back to; the call itself is invalid.

    This replaces the old contract, where `project_id` was optional and a worker
    could be created owning nothing. Such a worker is exactly the one that
    cannot be attributed after it dies.
    """
    from src.clients.worker_spawner import request_spawn

    with pytest.raises(TypeError):
        await request_spawn(
            repo="org/repo",
            github_token="ghs_test",  # noqa: S106
            task_content="build it",
            timeout_seconds=5,
        )
