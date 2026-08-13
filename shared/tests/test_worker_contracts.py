"""Unit tests for worker queue contracts — ScaffoldConfig serialization."""

from pydantic import TypeAdapter, ValidationError
import pytest

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    ScaffoldConfig,
    WorkerCapability,
    WorkerCommand,
    WorkerConfig,
    WorkerOwnership,
)

# Ownership is required of every worker; these tests are about other fields.
_OWNERSHIP = WorkerOwnership(project_id="proj-1", run_id="run-1")


class TestScaffoldConfig:
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

    def test_roundtrip_serialization(self):
        """ScaffoldConfig survives JSON round-trip through CreateWorkerCommand."""
        scaffold = ScaffoldConfig(
            template_repo="gh:vladmesh/service-template",
            template_ref="0.3.0",
            project_name="my-project",
            modules="backend,tg_bot",
            task_description="Build a telegram bot",
        )
        config = WorkerConfig(
            name="dev-my-project-abc12345",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="Read TASK.md",
            allowed_commands=["*"],
            capabilities=[WorkerCapability.GIT],
            ownership=_OWNERSHIP,
            scaffold_config=scaffold,
        )
        cmd = CreateWorkerCommand(
            request_id="req-123",
            config=config,
        )

        # Serialize to JSON and back
        json_str = cmd.model_dump_json()
        restored = CreateWorkerCommand.model_validate_json(json_str)

        assert restored.config.scaffold_config is not None
        assert restored.config.scaffold_config.template_repo == "gh:vladmesh/service-template"
        assert restored.config.scaffold_config.project_name == "my-project"
        assert restored.config.scaffold_config.modules == "backend,tg_bot"
        assert restored.config.scaffold_config.task_description == "Build a telegram bot"

    def test_scaffold_config_none_by_default(self):
        """WorkerConfig.scaffold_config is None when not provided."""
        config = WorkerConfig(
            name="dev-test",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="test",
            allowed_commands=["*"],
            capabilities=[],
            ownership=_OWNERSHIP,
        )
        assert config.scaffold_config is None

    def test_scaffold_config_defaults(self):
        """ScaffoldConfig task_description defaults to empty string."""
        scaffold = ScaffoldConfig(
            template_repo="gh:vladmesh/service-template",
            template_ref="0.3.0",
            project_name="test",
            modules="backend",
        )
        assert scaffold.task_description == ""


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
