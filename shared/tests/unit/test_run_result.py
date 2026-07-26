"""Contract tests for typed `Run.result` — one shape per `RunType`.

Covers each run type, `result=None`, cross-type rejection, unknown outcome,
unknown field, missing required field, and preservation of optional fields.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError
import pytest

from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.run_result import (
    DeployRunResult,
    EngineeringRunResult,
    QABlocker,
    QABlockerCategory,
    QAFailedCheck,
    QARunResult,
    QAStateChange,
    QAStateChangeCleanup,
)
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome

_NOW = datetime.now(UTC)


def _run(run_type: RunType, result, *, status: RunStatus = RunStatus.COMPLETED) -> RunDTO:
    return RunDTO(
        id="run-1",
        project_id="00000000-0000-0000-0000-000000000001",
        type=run_type,
        status=status,
        result=result,
        created_at=_NOW,
    )


class TestValidPayloads:
    """A well-formed payload for each type parses into its typed model."""

    def test_qa_blocker_has_closed_category_and_evidence(self):
        blocker = QABlocker(
            category=QABlockerCategory.TELEGRAM_ACCESS_DENIED,
            attempted="send access probe",
            sent="/start",
            received="Forbidden: bot was blocked by the user",
        )
        run = _run(
            RunType.QA,
            {"qa_outcome": "blocked", "blocker": blocker.model_dump(mode="json")},
        )
        assert isinstance(run.result, QARunResult)
        assert run.result.blocker == blocker

    def test_engineering(self):
        run = _run(
            RunType.ENGINEERING,
            {
                "engineering_status": "done",
                "commit_sha": "abc123",
                "selected_modules": ["tg_bot"],
            },
        )
        assert isinstance(run.result, EngineeringRunResult)
        assert run.result.engineering_status == "done"
        assert run.result.commit_sha == "abc123"

    def test_deploy(self):
        run = _run(
            RunType.DEPLOY,
            {
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": "https://x.example.com",
                "application_id": 42,
            },
        )
        assert isinstance(run.result, DeployRunResult)
        assert run.result.deploy_outcome is DeployOutcome.SUCCESS
        assert run.result.application_id == 42

    def test_qa(self):
        run = _run(
            RunType.QA,
            {
                "qa_outcome": QAOutcome.FAILED.value,
                "summary": "broken",
                "failed_checks": [{"name": "weather", "detail": "404"}],
            },
        )
        assert isinstance(run.result, QARunResult)
        assert run.result.qa_outcome is QAOutcome.FAILED
        assert run.result.failed_checks == [QAFailedCheck(name="weather", detail="404")]

    def test_qa_state_change_carries_cleanup_evidence(self):
        change = QAStateChange(
            resource="user telegram_id=8202532144",
            operation="created",
            cleanup=QAStateChangeCleanup(
                attempted=True,
                succeeded=True,
                detail="DELETE /users/by-telegram/8202532144 returned 204",
            ),
        )
        run = _run(
            RunType.QA,
            {"qa_outcome": "passed", "state_changes": [change.model_dump(mode="json")]},
        )

        assert run.result.state_changes == [change]

    def test_passed_qa_rejects_residual_state_change(self):
        change = QAStateChange(
            resource="POST /api/items",
            operation="created",
            cleanup=QAStateChangeCleanup(
                attempted=True, succeeded=False, detail="no inverse operation"
            ),
        )

        with pytest.raises(ValidationError, match="uncleaned state change"):
            _run(
                RunType.QA,
                {"qa_outcome": "passed", "state_changes": [change.model_dump(mode="json")]},
            )

    def test_qa_result_directly_rejects_passed_residual_state_change(self):
        """The invariant belongs to QARunResult, not only RunDTO routing."""
        with pytest.raises(ValidationError, match="uncleaned state change"):
            QARunResult.model_validate(
                {
                    "qa_outcome": QAOutcome.PASSED.value,
                    "state_changes": [
                        {
                            "resource": "POST /api/items",
                            "operation": "created",
                            "cleanup": {
                                "attempted": False,
                                "succeeded": False,
                                "detail": "direct write cannot be rolled back",
                            },
                        }
                    ],
                }
            )

    @pytest.mark.parametrize("run_type", list(RunType))
    @pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCELLED])
    def test_result_none_allowed_before_terminal(self, run_type, status):
        """None is valid while no outcome exists yet, and for superseded (CANCELLED) runs."""
        run = _run(run_type, None, status=status)
        assert run.result is None


class TestRejection:
    """Incompatible or malformed payloads fail at the boundary."""

    def test_wrong_type_payload_rejected(self):
        """A valid QA payload on a deploy run is rejected — type binds the shape."""
        with pytest.raises(ValidationError):
            _run(RunType.DEPLOY, {"qa_outcome": QAOutcome.PASSED.value})

    def test_deploy_payload_on_qa_run_rejected(self):
        with pytest.raises(ValidationError):
            _run(RunType.QA, {"deploy_outcome": DeployOutcome.SUCCESS.value})

    def test_unknown_outcome_rejected(self):
        with pytest.raises(ValidationError):
            _run(RunType.DEPLOY, {"deploy_outcome": "teleported"})

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            _run(RunType.DEPLOY, {"deploy_outcome": DeployOutcome.SUCCESS.value, "bogus": 1})

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            _run(RunType.DEPLOY, {"deployed_url": "https://x.example.com"})

    def test_qa_missing_outcome_rejected(self):
        with pytest.raises(ValidationError):
            _run(RunType.QA, {"summary": "no outcome"})

    def test_blocked_qa_outcome_without_blocker_rejected(self):
        with pytest.raises(ValidationError, match="blocker"):
            _run(RunType.QA, {"qa_outcome": QAOutcome.BLOCKED.value})

    @pytest.mark.parametrize("run_type", list(RunType))
    @pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED])
    def test_terminal_status_without_result_rejected(self, run_type, status):
        """A COMPLETED/FAILED run that lost its result is rejected, not silently accepted."""
        with pytest.raises(ValidationError):
            _run(run_type, None, status=status)


class TestOptionalFieldPreservation:
    """Optional fields the scheduler reads survive a serialize/parse round-trip."""

    def test_deploy_fields_round_trip(self):
        wire = DeployRunResult(
            deploy_outcome=DeployOutcome.CODE_FIX,
            error_details="ImportError",
            deploy_fix_attempt=2,
            bot_username="mybot",
        ).model_dump(mode="json")
        run = _run(RunType.DEPLOY, wire, status=RunStatus.FAILED)
        assert run.result.deploy_fix_attempt == 2
        assert run.result.error_details == "ImportError"
        assert run.result.bot_username == "mybot"

    def test_qa_failed_checks_round_trip(self):
        wire = QARunResult(
            qa_outcome=QAOutcome.EXHAUSTED,
            summary="still broken",
            failed_checks=[QAFailedCheck(name="login", detail="500")],
            qa_attempt=2,
        ).model_dump(mode="json")
        run = _run(RunType.QA, wire)
        assert run.result.qa_attempt == 2
        assert run.result.failed_checks[0].name == "login"

    def test_deploy_outcome_is_typed_enum_not_string(self):
        run = _run(RunType.DEPLOY, {"deploy_outcome": DeployOutcome.RETRY.value})
        assert run.result.deploy_outcome is DeployOutcome.RETRY
