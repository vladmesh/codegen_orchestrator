"""Unit tests for worker queue contracts — ScaffoldConfig serialization."""

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    ScaffoldConfig,
    WorkerCapability,
    WorkerConfig,
)


class TestScaffoldConfig:
    def test_codex_worker_config_roundtrip_keeps_auth_profile(self):
        config = WorkerConfig(
            name="dev-codex",
            worker_type="developer",
            agent_type="codex",
            instructions="Read AGENTS.md",
            allowed_commands=["*"],
            capabilities=[WorkerCapability.GIT],
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
