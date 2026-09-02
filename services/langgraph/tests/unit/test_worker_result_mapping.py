"""Tests for spawn_result_from_output — contract validation + SpawnResult mapping."""

import json
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
import pytest

from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.queues.worker_result import (
    ClaudeResultEvidence,
    FactoryResultEvidence,
    WorkerBlockedResult,
    WorkerCompletedResult,
    WorkerFailedResult,
    WorkerResultAdapter,
    WorkerStopReason,
    parse_worker_result,
)
from shared.contracts.vocab import AgentType
from shared.diagnostics import safe_validation_errors
from src.clients.worker_spawner import (
    WorkerOutputDecodeError,
    _wait_for_response,
    spawn_result_from_output,
)
from src.consumers.engineering_result_handler import _observability_patch
from src.nodes.developer import DeveloperNode


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

    def test_claude_evidence_maps_as_one_typed_payload(self):
        result = spawn_result_from_output(
            {
                "status": "completed",
                "commit_sha": "abc123",
                "content": "Implemented feature",
                "claude_evidence": {
                    "provider": "anthropic",
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                    "cache_read_tokens": 4,
                    "cache_write_tokens": 5,
                    "cost_microusd": 40_001,
                },
            },
            request_id="req-claude",
            worker_id="dev-1",
        )

        assert result.claude_evidence is not None
        assert result.claude_evidence.cost_microusd == 40_001
        assert result.claude_evidence.cache_write_tokens == 5

    @pytest.mark.parametrize(
        "wrapper_result",
        [
            WorkerCompletedResult(commit_sha="abc123", content="Implemented feature"),
            WorkerFailedResult(error="Agent crashed"),
            WorkerFailedResult(
                error="Agent timed out", stop_reason=WorkerStopReason.AGENT_LIMIT_EXCEEDED
            ),
            WorkerFailedResult(
                error="Worker cancellation", stop_reason=WorkerStopReason.TURN_DEADLINE_EXCEEDED
            ),
            WorkerBlockedResult(block_reason="Missing credentials"),
        ],
    )
    def test_factory_evidence_reaches_every_terminal_payload(self, wrapper_result):
        evidence = FactoryResultEvidence(
            model="factory-model",
            input_tokens=12,
            output_tokens=3,
            cache_read_tokens=4,
            cache_write_tokens=5,
        )
        wrapper_result = wrapper_result.model_copy(update={"factory_evidence": evidence})
        wire = wrapper_result.model_dump(mode="json")

        broker_result = parse_worker_result(wire)
        spawn_result = spawn_result_from_output(wire, request_id="req-factory", worker_id="dev-1")
        observability = DeveloperNode._worker_observability(
            broker_result, {"config": {"model_identifier": "configured"}}, AgentType.FACTORY
        )
        terminal_payload = _observability_patch(observability)["engineering_attempt"]
        ledger_input = EngineeringAttemptLedgerInput.model_validate(terminal_payload)

        assert spawn_result.factory_evidence == evidence
        assert terminal_payload == {"factory_evidence": evidence.model_dump(mode="json")}
        assert ledger_input.cost_source == "unknown"
        assert ledger_input.cost_microusd is None

    @pytest.mark.parametrize(
        "wrapper_result",
        [
            WorkerCompletedResult(commit_sha="abc123", content="Implemented feature"),
            WorkerFailedResult(error="Agent crashed"),
            WorkerFailedResult(
                error="Agent timed out", stop_reason=WorkerStopReason.AGENT_LIMIT_EXCEEDED
            ),
            WorkerFailedResult(
                error="Worker cancellation", stop_reason=WorkerStopReason.TURN_DEADLINE_EXCEEDED
            ),
            WorkerBlockedResult(block_reason="Missing credentials"),
        ],
    )
    def test_partial_factory_evidence_is_terminal_safe_and_keeps_configured_model(
        self, wrapper_result
    ):
        evidence = FactoryResultEvidence(input_tokens=10, total_tokens=5)
        wrapper_result = wrapper_result.model_copy(update={"factory_evidence": evidence})
        wire = wrapper_result.model_dump(mode="json")

        broker_result = parse_worker_result(wire)
        observability = DeveloperNode._worker_observability(
            broker_result,
            {"config": {"model_identifier": "configured-factory"}},
            AgentType.FACTORY,
        )
        terminal_payload = _observability_patch(observability)["engineering_attempt"]
        ledger_input = EngineeringAttemptLedgerInput.model_validate(terminal_payload)

        assert terminal_payload["factory_evidence"] == {
            "provider": "factory",
            "model": "configured-factory",
            "input_tokens": 10,
            "output_tokens": None,
            "total_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        }
        assert ledger_input.model == "configured-factory"
        assert ledger_input.total_tokens is None
        assert ledger_input.cost_source == "unknown"
        assert ledger_input.cost_microusd is None

    def test_codex_keeps_configured_model_without_parsing_its_output(self):
        result = spawn_result_from_output(
            {
                "status": "failed",
                "error": "Agent exited",
                "agent_stdout_tail": '{"model":"untrusted-cli-model","usage":{"input_tokens":99}}',
            },
            request_id="req-codex",
            worker_id="dev-1",
        )

        observability = DeveloperNode._worker_observability(
            result,
            {
                "config": {
                    "llm_provider": "openai",
                    "model_identifier": "gpt-5-codex",
                }
            },
            AgentType.CODEX,
        )
        attempt = _observability_patch(observability)["engineering_attempt"]

        assert attempt == {
            "provider": "openai",
            "model": "gpt-5-codex",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "cost_source": "unknown",
        }

    def test_factory_without_usage_keeps_its_configured_profile(self):
        result = spawn_result_from_output(
            {"status": "failed", "error": "Agent exited"},
            request_id="req-factory-profile",
            worker_id="dev-1",
        )

        observability = DeveloperNode._worker_observability(
            result,
            {
                "config": {
                    "llm_provider": "factory",
                    "model_identifier": "factory-configured-model",
                }
            },
            AgentType.FACTORY,
        )
        attempt = _observability_patch(observability)["engineering_attempt"]

        assert attempt == {
            "provider": "factory",
            "model": "factory-configured-model",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "cost_source": "unknown",
        }

    @pytest.mark.parametrize(
        "wrapper_result",
        [
            WorkerCompletedResult(commit_sha="abc123", content="Implemented feature"),
            WorkerFailedResult(error="Agent crashed"),
        ],
    )
    def test_serialized_claude_evidence_reaches_canonical_terminal_payload(self, wrapper_result):
        """The wrapper, broker and LangGraph all retain one typed evidence object."""
        evidence = ClaudeResultEvidence(
            model="claude-sonnet",
            input_tokens=12,
            output_tokens=3,
            cache_read_tokens=4,
            cache_write_tokens=5,
            cost_microusd=40_001,
        )
        wrapper_result = wrapper_result.model_copy(update={"claude_evidence": evidence})
        wire = wrapper_result.model_dump(mode="json")

        broker_result = parse_worker_result(wire)
        spawn_result = spawn_result_from_output(wire, request_id="req-claude", worker_id="dev-1")
        observability = DeveloperNode._worker_observability(
            broker_result, {"config": {}}, AgentType.CLAUDE
        )
        terminal_payload = _observability_patch(observability)["engineering_attempt"]

        assert spawn_result.claude_evidence == evidence
        assert terminal_payload == {"claude_evidence": evidence.model_dump(mode="json")}

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

    def test_rejected_is_no_longer_a_status(self):
        result = spawn_result_from_output(
            {"status": "rejected", "block_reason": "REGISTRY_PASSWORD empty"},
            request_id="req-4",
            worker_id="dev-4",
        )
        assert result.success is False
        assert result.error_message == "invalid_worker_result"

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
            safe = safe_validation_errors(e)
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
            safe = safe_validation_errors(e)
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
        published_inputs = []

        async def capture_input(stream, data, **kwargs):
            published_inputs.append((stream, data, kwargs))

        mock_client.xadd = capture_input
        mock_client.xack = AsyncMock()
        mock_client.xgroup_destroy = AsyncMock()
        mock_client.aclose = AsyncMock()

        async def malformed_matching_output(*_args, **_kwargs):
            request_id = json.loads(published_inputs[0][1]["data"])["request_id"]
            return [
                (
                    b"worker:dev-1:output",
                    [(b"1-0", {b"request_id": request_id.encode(), b"data": b"{bad json"})],
                )
            ]

        mock_client.xreadgroup = malformed_matching_output

        from src.clients.worker_spawner import send_task_to_worker

        with (
            patch("src.clients.worker_spawner.record_worker_on_attempt", new_callable=AsyncMock),
            patch("src.clients.worker_spawner.record_turn_on_attempt", new_callable=AsyncMock),
        ):
            result = await send_task_to_worker(
                worker_id="dev-1",
                task_content="fix",
                timeout_seconds=5,
                ownership=WorkerOwnership(
                    project_id="proj-1", run_id="run-1", attempt_id="eng-attempt-1"
                ),
            )

        # Explicit invalid result — NOT execution_timeout.
        assert result.success is False
        assert result.error_message == "invalid_worker_result"
