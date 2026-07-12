"""Tests for spawn_result_from_output — contract validation + SpawnResult mapping."""

from src.clients.worker_spawner import spawn_result_from_output


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
