"""Tests for spawn_result_from_output — contract validation + SpawnResult mapping."""

import json
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
import pytest

from shared.contracts.queues.worker_result import WorkerResultAdapter
from src.clients.worker_spawner import (
    WorkerOutputDecodeError,
    _safe_validation_errors,
    _wait_for_response,
    spawn_result_from_output,
)


class TestSpawnResultFromOutput:
    def test_completed_maps_to_success(self):
        result = spawn_result_from_output(
            {
                "status": "completed",
                "commit_sha": "abc123",
                "content": "Implemented feature",
                "worker_report": "REPORT",
                "agent_stdout_tail": "tail",
            },
            request_id="req-1",
            worker_id="dev-1",
        )
        assert result.success is True
        assert result.exit_code == 0
        assert result.output == "Implemented feature"
        assert result.commit_sha == "abc123"
        assert result.worker_report == "REPORT"
        assert result.logs_tail == "tail"
        assert result.gave_up_reason is None

    def test_failed_maps_to_error_message(self):
        result = spawn_result_from_output(
            {"status": "failed", "error": "Agent process failed"},
            request_id="req-2",
            worker_id="dev-2",
        )
        assert result.success is False
        assert result.exit_code == 1
        assert result.error_message == "Agent process failed"
        assert result.gave_up_reason is None

    def test_blocked_maps_to_gave_up_reason(self):
        result = spawn_result_from_output(
            {"status": "blocked", "block_reason": "Missing API credentials"},
            request_id="req-3",
            worker_id="dev-3",
        )
        assert result.success is False
        assert result.gave_up_reason == "Missing API credentials"
        assert result.error_message is None

    def test_rejected_maps_to_gave_up_reason(self):
        result = spawn_result_from_output(
            {"status": "rejected", "block_reason": "REGISTRY_PASSWORD empty"},
            request_id="req-4",
            worker_id="dev-4",
        )
        assert result.success is False
        assert result.gave_up_reason == "REGISTRY_PASSWORD empty"

    def test_invalid_payload_is_explicit_failure(self):
        # legacy synonym status — no longer valid on the wire
        result = spawn_result_from_output(
            {"status": "success", "content": "done"},
            request_id="req-5",
            worker_id="dev-5",
        )
        assert result.success is False
        assert result.exit_code == 1
        assert result.error_message == "invalid_worker_result"
        assert result.output == ""
        assert result.worker_id == "dev-5"


class TestValidationErrorSanitization:
    """The invalid-result log must never echo a payload field value (secrets)."""

    def test_invalid_discriminator_value_stripped(self):
        # A secret leaked into the discriminator field must not reach the log.
        secret = "ghp_secret_token"  # noqa: S105
        try:
            WorkerResultAdapter.validate_python({"status": secret, "content": "x"})
        except ValidationError as e:
            safe = _safe_validation_errors(e)
        blob = json.dumps(safe)
        assert secret not in blob
        assert safe  # still structured — type/loc survive
        assert safe[0]["type"] == "union_tag_invalid"

    def test_field_value_not_in_errors(self):
        try:
            WorkerResultAdapter.validate_python(
                {"status": "completed", "commit_sha": "sha", "content": "x", "leak": "s3cr3t"}
            )
        except ValidationError as e:
            safe = _safe_validation_errors(e)
        assert "s3cr3t" not in json.dumps(safe)


class TestMalformedOutputHandling:
    """Blocker 2: undecodable output is an explicit invalid result, not a timeout."""

    @pytest.mark.asyncio
    async def test_malformed_json_raises_decode_error_and_acks(self):
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock()
        mock_redis.xack = AsyncMock()
        mock_redis.xreadgroup = AsyncMock(
            return_value=[(b"worker:dev-1:output", [(b"1-0", {b"data": b"{not valid json"})])]
        )

        with pytest.raises(WorkerOutputDecodeError):
            await _wait_for_response(mock_redis, "grp", "cons", None, 5.0, "worker:dev-1:output")
        # Poison entry ACKed terminally so the reclaim loop is not poisoned.
        mock_redis.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_data_field_raises_decode_error(self):
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock()
        mock_redis.xack = AsyncMock()
        mock_redis.xreadgroup = AsyncMock(
            return_value=[(b"worker:dev-1:output", [(b"1-0", {b"other": b"x"})])]
        )

        with pytest.raises(WorkerOutputDecodeError):
            await _wait_for_response(mock_redis, "grp", "cons", None, 5.0, "worker:dev-1:output")

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.get_settings")
    @patch("src.clients.worker_spawner.redis")
    async def test_send_task_malformed_output_is_invalid_result(
        self, mock_redis_mod, mock_settings
    ):
        mock_settings.return_value.redis_url = "redis://localhost:6379"
        mock_client = AsyncMock()
        mock_redis_mod.from_url.return_value = mock_client
        mock_client.xgroup_create = AsyncMock()
        mock_client.xadd = AsyncMock()
        mock_client.xack = AsyncMock()
        mock_client.xgroup_destroy = AsyncMock()
        mock_client.aclose = AsyncMock()
        mock_client.xreadgroup = AsyncMock(
            return_value=[(b"worker:dev-1:output", [(b"1-0", {b"data": b"{bad json"})])]
        )

        from src.clients.worker_spawner import send_task_to_worker

        result = await send_task_to_worker(worker_id="dev-1", task_content="fix", timeout_seconds=5)

        # Explicit invalid result — NOT execution_timeout.
        assert result.success is False
        assert result.error_message == "invalid_worker_result"
