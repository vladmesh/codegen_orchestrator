"""Unit tests for worker queue contracts."""

from pydantic import TypeAdapter, ValidationError
import pytest

from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.qa import QAMessage
from shared.contracts.queues.worker import (
    AgentType,
    WorkerCapability,
    WorkerCommand,
    WorkerConfig,
    WorkerLabel,
    WorkerOwnership,
)

# Ownership is required of every worker; these tests are about other fields.
_OWNERSHIP = WorkerOwnership(project_id="proj-1", run_id="live-1", attempt_id="eng-1")


class TestOwnershipIsDerivedFromTheMessageThatAskedForTheWork:
    """The run and the attempt are two identities, and never each other.

    A worker is owned by the run that initiated the work — a live harness run, a
    matrix combination — which one producer wrote onto the message. The
    engineering Run row (or the QA Run row) is one attempt inside that run. Both
    are recorded, and they are recorded in different places, because run-scoped
    cleanup and per-run evidence are only decidable against the initiating run.
    """

    def test_an_engineering_message_owns_its_worker_by_the_initiating_run(self):
        msg = EngineeringMessage(
            task_id="eng-777",
            project_id="proj-1",
            initiating_run_id="live-42",
            telegram_chat_id="",
        )

        ownership = WorkerOwnership.for_engineering(msg)

        assert ownership.run_id == "live-42"
        assert ownership.attempt_id == "eng-777"
        assert ownership.project_id == "proj-1"

    def test_a_qa_message_owns_its_executor_by_the_same_run(self):
        msg = QAMessage(
            project_id="proj-1",
            initiating_run_id="live-42",
            deployed_url="https://example.com",
            application_id=7,
            acceptance_criteria="- it answers",
            run_id="qa-9",
        )

        ownership = WorkerOwnership.for_qa(msg)

        assert ownership.run_id == "live-42"
        assert ownership.attempt_id == "qa-9"
        assert ownership.project_id == "proj-1"

    def test_the_run_and_the_attempt_land_on_different_labels(self):
        labels = WorkerOwnership(
            project_id="proj-1", run_id="live-42", attempt_id="eng-777"
        ).as_labels()

        assert labels[WorkerLabel.RUN.value] == "live-42"
        assert labels[WorkerLabel.ATTEMPT.value] == "eng-777"
        assert labels[WorkerLabel.PROJECT.value] == "proj-1"

    @pytest.mark.parametrize(
        "project_id, run_id, attempt_id",
        [("", "live-1", "eng-1"), ("proj-1", "", "eng-1"), ("proj-1", "live-1", "")],
    )
    def test_no_part_of_ownership_may_be_empty(self, project_id, run_id, attempt_id):
        """An empty label attributes a dead worker to nothing at all."""
        with pytest.raises(ValidationError):
            WorkerOwnership(project_id=project_id, run_id=run_id, attempt_id=attempt_id)


class TestWorkerConfigSerialization:
    def test_codex_worker_config_roundtrip_keeps_auth_profile(self):
        config = WorkerConfig(
            name="dev-codex",
            worker_type="developer",
            agent_type="codex",
            instructions="Read AGENTS.md",
            allowed_commands=["*"],
            capabilities=[WorkerCapability.GIT],
            ownership=_OWNERSHIP,
            host_codex_home="/srv/codex-worker",
        )

        restored = WorkerConfig.model_validate_json(config.model_dump_json())

        assert restored.agent_type is AgentType.CODEX
        assert restored.host_codex_home == "/srv/codex-worker"


class TestQARunsOnAnAssignedSubscriptionAgent:
    """Only Claude Code or Codex may be a `qa` worker.

    The executor contract names two agents, and both are subscription CLIs whose
    session stays on the management host. `factory` runs on a provider API key
    and `noop` performs no testing at all, so a `qa` create carrying either is
    refused at the contract — which is the same validation worker-manager runs
    on every command it takes off the stream, before any container exists.
    """

    def _qa_config(self, agent_type: AgentType) -> WorkerConfig:
        return WorkerConfig(
            name="qa-1",
            worker_type="qa",
            agent_type=agent_type,
            instructions="# QA executor",
            allowed_commands=["*"],
            capabilities=[],
            ownership=_OWNERSHIP,
        )

    @pytest.mark.parametrize("agent_type", [AgentType.CLAUDE, AgentType.CODEX])
    def test_an_assigned_subscription_agent_is_accepted(self, agent_type):
        assert self._qa_config(agent_type).agent_type is agent_type

    @pytest.mark.parametrize("agent_type", [AgentType.FACTORY, AgentType.NOOP])
    def test_no_other_agent_can_be_a_qa_worker(self, agent_type):
        with pytest.raises(ValidationError) as exc:
            self._qa_config(agent_type)

        assert agent_type.value in str(exc.value)

    @pytest.mark.parametrize("agent_type", ["factory", "noop"])
    def test_the_wire_refuses_it_too(self, agent_type):
        """What worker-manager parses off `worker:commands` is this same model.

        A payload that never validates is never dispatched: the consumer logs it
        and ACKs it away, so the refusal happens before a container is built.
        """
        payload = {
            "command": "create",
            "request_id": "req-qa-1",
            "config": {
                "name": "qa-1",
                "worker_type": "qa",
                "agent_type": agent_type,
                "instructions": "# QA executor",
                "allowed_commands": ["*"],
                "capabilities": [],
                "ownership": {"project_id": "proj-1", "run_id": "run-1"},
            },
        }

        with pytest.raises(ValidationError):
            TypeAdapter(WorkerCommand).validate_python(payload)

    @pytest.mark.parametrize("agent_type", [AgentType.FACTORY, AgentType.NOOP])
    def test_a_developer_worker_keeps_the_full_agent_set(self, agent_type):
        """The restriction is on QA, not on the enum: developers are untouched."""
        config = WorkerConfig(
            name="dev-1",
            worker_type="developer",
            agent_type=agent_type,
            instructions="Read TASK.md",
            allowed_commands=["*"],
            capabilities=[WorkerCapability.GIT],
            ownership=_OWNERSHIP,
        )

        assert config.agent_type is agent_type
