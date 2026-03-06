"""Tests for multi-turn worker spawner API (Iteration 2: worker-reuse-ci-fix)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_settings():
    """Minimal settings for spawner functions."""
    s = MagicMock()
    s.redis_url = "redis://localhost:6379"
    return s


# ---------- Liveness check ----------


class TestCheckWorkerAlive:
    @pytest.mark.asyncio
    async def test_returns_true_when_running(self):
        """Worker with RUNNING status should be considered alive."""
        from src.clients.worker_spawner import _check_worker_alive

        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value="RUNNING")

        assert await _check_worker_alive(mock_redis, "dev-123") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_dead(self):
        """Worker with DEAD status should be considered not alive."""
        from src.clients.worker_spawner import _check_worker_alive

        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value="DEAD")

        assert await _check_worker_alive(mock_redis, "dev-123") is False

    @pytest.mark.asyncio
    async def test_returns_false_when_key_missing(self):
        """Worker with no status key (cleaned up) should be considered not alive."""
        from src.clients.worker_spawner import _check_worker_alive

        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=None)

        assert await _check_worker_alive(mock_redis, "dev-123") is False

    @pytest.mark.asyncio
    async def test_handles_bytes_status(self):
        """Should handle bytes-encoded status (decode_responses=False)."""
        from src.clients.worker_spawner import _check_worker_alive

        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=b"DEAD")

        assert await _check_worker_alive(mock_redis, "dev-123") is False

    @pytest.mark.asyncio
    async def test_handles_bytes_running_status(self):
        """Should handle bytes-encoded RUNNING status."""
        from src.clients.worker_spawner import _check_worker_alive

        mock_redis = AsyncMock()
        mock_redis.hget = AsyncMock(return_value=b"RUNNING")

        assert await _check_worker_alive(mock_redis, "dev-123") is True


class TestWaitForResponseLiveness:
    @pytest.mark.asyncio
    async def test_returns_none_when_worker_dead(self):
        """_wait_for_response should return None quickly when worker is dead."""
        from src.clients.worker_spawner import _wait_for_response

        mock_redis = AsyncMock()
        mock_redis.xreadgroup = AsyncMock(return_value=[])
        mock_redis.xgroup_create = AsyncMock()
        # Worker is DEAD
        mock_redis.hget = AsyncMock(return_value="DEAD")

        # Patch the interval to 0 so check happens immediately
        with patch("src.clients.worker_spawner.LIVENESS_CHECK_INTERVAL_S", 0):
            result = await _wait_for_response(
                redis_client=mock_redis,
                group_name="test-group",
                consumer_id="test-consumer",
                request_id=None,
                timeout_s=60,
                stream="worker:dev-123:output",
                worker_id="dev-123",
            )

        assert result is None
        # Should have checked liveness
        mock_redis.hget.assert_called()


# ---------- 2.1: send_task_to_worker ----------


class TestSendTaskToWorker:
    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings", return_value=_mock_settings())
    @patch("redis.asyncio.Redis.from_url")
    async def test_sends_prompt_to_input_stream_and_waits_output(
        self, mock_redis_from_url, mock_settings
    ):
        """send_task_to_worker() should XADD to worker:{id}:input and wait for output."""
        mock_redis = AsyncMock()
        mock_redis_from_url.return_value = mock_redis

        captured_xadds = []

        async def capture_xadd(stream, data):
            captured_xadds.append((stream, data))
            return "msg-id"

        mock_redis.xadd = capture_xadd
        mock_redis.xgroup_create = AsyncMock()
        mock_redis.xreadgroup = AsyncMock(
            return_value=[
                (
                    b"worker:dev-123:output",
                    [
                        (
                            b"1-0",
                            {
                                b"data": json.dumps(
                                    {
                                        "status": "success",
                                        "content": "Fixed the test",
                                        "commit_sha": "abc123",
                                    }
                                ).encode()
                            },
                        )
                    ],
                )
            ]
        )
        mock_redis.xack = AsyncMock()
        mock_redis.xgroup_destroy = AsyncMock()
        mock_redis.aclose = AsyncMock()

        from src.clients.worker_spawner import send_task_to_worker

        result = await send_task_to_worker(
            worker_id="dev-123",
            task_content="Fix the CI error in test_foo.py",
            timeout_seconds=10,
        )

        # Verify XADD to input stream
        assert len(captured_xadds) == 1
        stream, data = captured_xadds[0]
        assert stream == "worker:dev-123:input"
        payload = json.loads(data["data"])
        assert payload["prompt"] == "Fix the CI error in test_foo.py"

        # Verify result
        assert result.success is True
        assert result.output == "Fixed the test"
        assert result.commit_sha == "abc123"
        assert result.worker_id == "dev-123"

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings", return_value=_mock_settings())
    @patch("redis.asyncio.Redis.from_url")
    async def test_returns_failure_on_timeout(self, mock_redis_from_url, mock_settings):
        """send_task_to_worker() should return failure SpawnResult on timeout."""
        mock_redis = AsyncMock()
        mock_redis_from_url.return_value = mock_redis

        mock_redis.xadd = AsyncMock(return_value="msg-id")
        mock_redis.xgroup_create = AsyncMock()
        # Return empty results to simulate timeout
        mock_redis.xreadgroup = AsyncMock(return_value=[])
        mock_redis.xack = AsyncMock()
        mock_redis.xgroup_destroy = AsyncMock()
        mock_redis.aclose = AsyncMock()

        from src.clients.worker_spawner import send_task_to_worker

        result = await send_task_to_worker(
            worker_id="dev-123",
            task_content="Fix it",
            timeout_seconds=0,
        )

        assert result.success is False
        assert result.error_message == "execution_timeout"
        assert result.worker_id == "dev-123"

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings", return_value=_mock_settings())
    @patch("redis.asyncio.Redis.from_url")
    async def test_returns_failure_on_worker_error(self, mock_redis_from_url, mock_settings):
        """send_task_to_worker() should return failure when worker reports error."""
        mock_redis = AsyncMock()
        mock_redis_from_url.return_value = mock_redis

        mock_redis.xadd = AsyncMock(return_value="msg-id")
        mock_redis.xgroup_create = AsyncMock()
        mock_redis.xreadgroup = AsyncMock(
            return_value=[
                (
                    b"worker:dev-123:output",
                    [
                        (
                            b"1-0",
                            {
                                b"data": json.dumps(
                                    {
                                        "status": "failed",
                                        "error": "Agent process failed",
                                    }
                                ).encode()
                            },
                        )
                    ],
                )
            ]
        )
        mock_redis.xack = AsyncMock()
        mock_redis.xgroup_destroy = AsyncMock()
        mock_redis.aclose = AsyncMock()

        from src.clients.worker_spawner import send_task_to_worker

        result = await send_task_to_worker(
            worker_id="dev-123",
            task_content="Fix it",
            timeout_seconds=10,
        )

        assert result.success is False
        assert result.error_message == "Agent process failed"


# ---------- 2.2: delete_worker ----------


class TestDeleteWorker:
    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings", return_value=_mock_settings())
    @patch("redis.asyncio.Redis.from_url")
    async def test_publishes_delete_command(self, mock_redis_from_url, mock_settings):
        """delete_worker() should publish DeleteWorkerCommand to worker:commands."""
        mock_redis = AsyncMock()
        mock_redis_from_url.return_value = mock_redis

        captured_xadds = []

        async def capture_xadd(stream, data):
            captured_xadds.append((stream, data))
            return "msg-id"

        mock_redis.xadd = capture_xadd
        mock_redis.aclose = AsyncMock()

        from src.clients.worker_spawner import delete_worker

        await delete_worker("dev-123")

        assert len(captured_xadds) == 1
        stream, data = captured_xadds[0]
        assert stream == "worker:commands"

        payload = json.loads(data["data"])
        assert payload["command"] == "delete"
        assert payload["worker_id"] == "dev-123"


# ---------- 2.3: worker_id in SpawnResult ----------


class TestSpawnResultWorkerId:
    def test_spawn_result_has_worker_id_field(self):
        """SpawnResult should have worker_id field defaulting to None."""
        from src.clients.worker_spawner import SpawnResult

        result = SpawnResult(request_id="req-1", success=True, exit_code=0, output="ok")
        assert result.worker_id is None

    def test_spawn_result_accepts_worker_id(self):
        """SpawnResult should accept worker_id."""
        from src.clients.worker_spawner import SpawnResult

        result = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="ok",
            worker_id="dev-123",
        )
        assert result.worker_id == "dev-123"

    @pytest.mark.asyncio
    @patch(
        "src.clients.worker_spawner.uuid.uuid4",
        return_value=MagicMock(
            hex="aabbccdd",
            __str__=lambda self: "aabbccdd-0000-0000-0000-000000000000",
        ),
    )
    @patch("src.clients.worker_spawner.get_settings", return_value=_mock_settings())
    @patch("src.prompts.load_developer_instructions", return_value="test instructions")
    @patch("redis.asyncio.Redis.from_url")
    async def test_request_spawn_returns_worker_id(
        self, mock_redis_from_url, mock_instructions, mock_settings, mock_uuid
    ):
        """request_spawn() should include worker_id in SpawnResult on success."""
        fixed_request_id = "aabbccdd-0000-0000-0000-000000000000"

        mock_redis = AsyncMock()
        mock_redis_from_url.return_value = mock_redis
        mock_redis.xgroup_create = AsyncMock()

        mock_redis.xadd = AsyncMock(return_value="msg-id")
        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                # Creation response
                [
                    (
                        b"worker:responses:developer",
                        [
                            (
                                b"1-0",
                                {
                                    b"data": json.dumps(
                                        {
                                            "request_id": fixed_request_id,
                                            "success": True,
                                            "worker_id": "dev-myrepo-aabbccdd",
                                        }
                                    ).encode()
                                },
                            )
                        ],
                    )
                ],
                # Worker output
                [
                    (
                        b"worker:dev-myrepo-aabbccdd:output",
                        [
                            (
                                b"2-0",
                                {
                                    b"data": json.dumps(
                                        {
                                            "status": "success",
                                            "content": "done",
                                            "commit_sha": "def456",
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

        result = await request_spawn(
            repo="org/myrepo",
            github_token="ghs_test",  # noqa: S106
            task_content="build it",
            timeout_seconds=5,
        )

        assert result.success is True
        assert result.worker_id == "dev-myrepo-aabbccdd"
