"""Contract tests for the canonical cross-service vocabularies.

Each canonical enum must accept exactly its wire values and reject anything
else at the Pydantic boundary. The historically-mismatched vocabularies
(WorkerCliKind, DeployAction, TaskType) are asserted to stay distinct rather
than being merged into the canonical sets.
"""

from pydantic import BaseModel, ValidationError
import pytest

from shared.contracts.base import BaseResult
from shared.contracts.dto.task import TaskType
from shared.contracts.events import ProgressEvent
from shared.contracts.queues.deploy import DeployAction
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.worker import AgentType as AgentTypeReexport
from shared.contracts.queues.worker_lifecycle import WorkerLifecycleEvent
from shared.contracts.vocab import (
    ActionType,
    AgentType,
    LifecycleEvent,
    ResultStatus,
    WorkerCliKind,
)
from shared.schemas.worker_events import parse_worker_event


class TestCanonicalValues:
    def test_agent_type_values(self):
        assert {a.value for a in AgentType} == {"claude", "factory", "noop"}

    def test_action_type_values(self):
        assert {a.value for a in ActionType} == {"create", "feature", "fix"}

    def test_result_status_values(self):
        # 'error' is intentionally gone — collapsed into the single 'failed'.
        assert {s.value for s in ResultStatus} == {"success", "failed", "timeout"}

    def test_lifecycle_event_values(self):
        assert {e.value for e in LifecycleEvent} == {
            "started",
            "progress",
            "completed",
            "failed",
            "stopped",
        }

    def test_agent_type_is_reexported_identity(self):
        # queues.worker must re-export the canonical enum, not a copy.
        assert AgentType is AgentTypeReexport


class TestAgentType:
    def test_dto_accepts_valid(self):
        class _M(BaseModel):
            type: AgentType

        assert _M(type="factory").type is AgentType.FACTORY

    def test_dto_rejects_unknown(self):
        class _M(BaseModel):
            type: AgentType

        with pytest.raises(ValidationError):
            _M(type="droid")


class TestActionType:
    def test_engineering_message_default_and_valid(self):
        msg = EngineeringMessage(task_id="t", project_id="p", user_id="u")
        assert msg.action is ActionType.CREATE
        assert (
            EngineeringMessage(task_id="t", project_id="p", user_id="u", action="fix").action
            is ActionType.FIX
        )

    def test_engineering_message_rejects_deploy_only_action(self):
        # 'stop'/'undeploy' are deploy operations, not engineering actions.
        with pytest.raises(ValidationError):
            EngineeringMessage(task_id="t", project_id="p", user_id="u", action="stop")

    def test_action_type_is_subset_of_deploy_action(self):
        assert {a.value for a in ActionType} <= {d.value for d in DeployAction}

    def test_deploy_and_task_vocab_stay_distinct(self):
        # DeployAction keeps its deploy-only ops; TaskType keeps 'refactor'.
        assert "stop" in {d.value for d in DeployAction}
        assert "undeploy" in {d.value for d in DeployAction}
        assert "refactor" in {t.value for t in TaskType}
        assert "refactor" not in {a.value for a in ActionType}


class TestResultStatus:
    def test_base_result_accepts_valid(self):
        assert BaseResult(request_id="r", status="timeout").status is ResultStatus.TIMEOUT

    def test_base_result_rejects_error_synonym(self):
        # The old 'error' failure synonym must no longer validate.
        with pytest.raises(ValidationError):
            BaseResult(request_id="r", status="error")

    def test_base_result_rejects_unknown(self):
        with pytest.raises(ValidationError):
            BaseResult(request_id="r", status="done")


class TestLifecycleEvent:
    def test_progress_event_accepts_valid(self):
        assert ProgressEvent(type="progress", request_id="r").type is LifecycleEvent.PROGRESS

    def test_progress_event_rejects_unknown(self):
        with pytest.raises(ValidationError):
            ProgressEvent(type="dead", request_id="r")

    def test_worker_lifecycle_event_accepts_stopped(self):
        ev = WorkerLifecycleEvent(worker_id="w", event="stopped")
        assert ev.event is LifecycleEvent.STOPPED

    def test_worker_lifecycle_event_rejects_unknown(self):
        with pytest.raises(ValidationError):
            WorkerLifecycleEvent(worker_id="w", event="progressing")


class TestWorkerCliKind:
    def test_values_are_the_historical_wire_set(self):
        assert {k.value for k in WorkerCliKind} == {"droid", "claude_code", "codex"}

    def test_distinct_from_agent_type(self):
        # Explicitly NOT merged with AgentType (claude/factory/noop).
        assert {k.value for k in WorkerCliKind}.isdisjoint({a.value for a in AgentType})

    def test_worker_event_parses_cli_kind(self):
        ev = parse_worker_event(
            {
                "request_id": "r",
                "event_type": "completed",
                "timestamp": "2026-07-12T00:00:00Z",
                "worker_type": "claude_code",
                "commit_sha": "abc",
                "branch": "main",
                "files_changed": [],
                "summary": "done",
            }
        )
        assert ev.event_type is LifecycleEvent.COMPLETED
        assert ev.worker_type is WorkerCliKind.CLAUDE_CODE

    def test_worker_event_rejects_agent_type_as_worker_type(self):
        with pytest.raises(ValidationError):
            parse_worker_event(
                {
                    "request_id": "r",
                    "event_type": "started",
                    "timestamp": "2026-07-12T00:00:00Z",
                    "worker_type": "claude",
                    "repo": "x",
                    "task_summary": "y",
                }
            )
