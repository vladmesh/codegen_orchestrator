import pytest

from src.container_config import WorkerContainerConfig


BROKER_ARGS = {
    "broker_url": "http://worker-broker:8001",
    "broker_token": "x" * 43,
}


class TestWorkerContainerConfig:
    def test_to_env_vars_includes_required_fields(self):
        """Config should generate all required env vars with WORKER_ prefix."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=["GIT"],
            auth_mode="host_session",
        )
        env = config.to_env_vars(**BROKER_ARGS)

        # All config vars use WORKER_ prefix for pydantic-settings compatibility
        assert env["WORKER_ID"] == "test-1"
        assert env["WORKER_BROKER_URL"] == "http://worker-broker:8001"
        assert env["WORKER_AGENT_TYPE"] == "claude"
        assert env["WORKER_BROKER_TOKEN"] == "x" * 43
        assert env["WORKER_TYPE"] == "developer"
        assert env["WORKER_CAPABILITIES"] == "GIT"

    def test_host_session_mode_adds_volume_mount(self):
        """Host session auth mode should configure volume mount."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=["GIT"],
            auth_mode="host_session",
            host_claude_dir="/home/user/.claude",
        )
        volumes = config.to_volume_mounts()
        assert "/home/user/.claude" in volumes
        assert volumes["/home/user/.claude"]["bind"] == "/home/worker/.claude"

    def test_claude_config_dir_points_at_the_mounted_host_session(self):
        """The CLI must keep .claude.json inside the mounted host directory.

        Without CLAUDE_CONFIG_DIR the CLI writes ~/.claude.json into the
        container's ephemeral layer while its backups land in the mount, so the
        config never survives a restart.
        """
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=["GIT"],
            auth_mode="host_session",
            host_claude_dir="/home/user/.claude-worker",
        )

        env = config.to_env_vars(**BROKER_ARGS)
        volumes = config.to_volume_mounts()

        assert env["CLAUDE_CONFIG_DIR"] == volumes["/home/user/.claude-worker"]["bind"]
        assert volumes["/home/user/.claude-worker"]["mode"] == "rw"
        # The worker validates that mount against the mode it was created with.
        assert env["WORKER_AUTH_MODE"] == "host_session"
        # The config file itself is never a separate bind mount: a single-file
        # mount is inode-bound and the CLI rewrites the file.
        assert all(not v["bind"].endswith(".claude.json") for v in volumes.values())

    def test_codex_host_session_uses_dedicated_rw_mount(self):
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="codex",
            capabilities=["GIT"],
            auth_mode="host_session",
            host_codex_home="/home/user/.codex-worker",
        )

        volumes = config.to_volume_mounts()

        assert volumes["/home/user/.codex-worker"] == {
            "bind": "/home/worker/.codex",
            "mode": "rw",
        }
        assert all(source != "/home/user/.codex" for source in volumes)

    def test_codex_host_session_exports_container_codex_home(self):
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="codex",
            capabilities=["GIT"],
            auth_mode="host_session",
            host_codex_home="/home/user/.codex-worker",
        )

        env = config.to_env_vars(**BROKER_ARGS)

        assert env["CODEX_HOME"] == "/home/worker/.codex"
        assert "OPENAI_API_KEY" not in env
        assert "CLAUDE_CONFIG_DIR" not in env

    def test_codex_api_key_mode_uses_exec_scoped_variable(self):
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="codex",
            capabilities=["GIT"],
            auth_mode="api_key",
            api_key="sk-openai-test",
        )

        env = config.to_env_vars(**BROKER_ARGS)

        assert env["CODEX_API_KEY"] == "sk-openai-test"
        assert "OPENAI_API_KEY" not in env

    def test_api_key_mode_adds_env_var(self):
        """API key auth mode should add ANTHROPIC_API_KEY."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=["GIT"],
            auth_mode="api_key",
            api_key="sk-ant-test",
        )
        env = config.to_env_vars(**BROKER_ARGS)
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
        # api_key mode keeps no session, and the worker must not demand a mount.
        assert env["WORKER_AUTH_MODE"] == "api_key"

    def test_stand_token_mode_injects_claude_token_without_a_host_mount(self):
        token = "claude-test-token"
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=["GIT"],
            auth_mode="stand_token",
            stand_claude_code_oauth_token=token,
            host_claude_dir="/host/.claude",
        )

        env = config.to_env_vars(**BROKER_ARGS)
        volumes = config.to_volume_mounts()

        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == token
        assert "CLAUDE_CONFIG_DIR" not in env
        assert "/host/.claude" not in volumes

    def test_stand_token_mode_injects_codex_token_without_a_profile_mount(self):
        token = "codex-test-token"
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="codex",
            capabilities=["GIT"],
            auth_mode="stand_token",
            stand_codex_access_token=token,
            host_codex_home="/host/.codex",
        )

        env = config.to_env_vars(**BROKER_ARGS)
        volumes = config.to_volume_mounts()

        assert env["CODEX_ACCESS_TOKEN"] == token
        assert "/host/.codex" not in volumes
        assert all(mount["bind"] != "/home/worker/.codex" for mount in volumes.values())

    def test_to_docker_run_kwargs_requires_a_dedicated_network(self):
        """Coding workers fail closed instead of falling back to host networking."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )

        with pytest.raises(ValueError, match="dedicated Docker network"):
            config.to_docker_run_kwargs()

    def test_noop_worker_keeps_lower_memory_limit(self):
        """Noop workers do not need the real-agent memory budget."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="noop",
            capabilities=[],
        )
        kwargs = config.to_docker_run_kwargs(network_name="test-network")
        assert kwargs["mem_limit"] == "2g"

    def test_factory_worker_gets_real_agent_memory_limit(self):
        """Factory workers need the same memory budget as other real LLM agents."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="factory",
            capabilities=[],
        )
        kwargs = config.to_docker_run_kwargs(network_name="test-network")
        assert kwargs["mem_limit"] == "4g"

    def test_codex_worker_gets_real_agent_memory_limit(self):
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="codex",
            capabilities=[],
        )
        assert config.to_docker_run_kwargs(network_name="test-network")["mem_limit"] == "4g"

    def test_to_docker_run_kwargs_with_network_name(self):
        """Coding workers have fixed native Docker hardening on their network."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )
        kwargs = config.to_docker_run_kwargs(network_name="test-network")
        assert kwargs["network"] == "test-network"
        assert "network_mode" not in kwargs
        assert kwargs["mem_limit"] == "4g"
        assert kwargs["cpu_period"] == 100000
        assert kwargs["cpu_quota"] == 100000
        assert kwargs["pids_limit"] > 0
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["security_opt"] == ["no-new-privileges:true"]

    def test_to_docker_run_kwargs_rejects_host_network(self):
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )

        with pytest.raises(ValueError, match="host networking"):
            config.to_docker_run_kwargs(network_name="host")

    def test_workspace_bind_mount(self):
        """When workspace_host_path is set, should add bind mount to /workspace."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
            workspace_host_path="/tmp/codegen/workspaces/test-1/workspace",
        )
        volumes = config.to_volume_mounts()
        assert "/tmp/codegen/workspaces/test-1/workspace" in volumes
        assert volumes["/tmp/codegen/workspaces/test-1/workspace"]["bind"] == "/workspace"
        assert volumes["/tmp/codegen/workspaces/test-1/workspace"]["mode"] == "rw"

    def test_transcript_bind_mount_is_writable(self):
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
            transcript_host_path="/data/worker-transcripts",
        )

        assert config.to_volume_mounts()["/data/worker-transcripts"] == {
            "bind": "/artifacts/worker-transcripts",
            "mode": "rw",
        }

    def test_no_workspace_when_path_not_set(self):
        """When workspace_host_path is None, no /workspace mount should be added."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )
        volumes = config.to_volume_mounts()
        # No workspace mount
        assert all(v.get("bind") != "/workspace" for v in volumes.values())

    def test_direct_control_plane_variables_are_absent(self):
        """Coding workers receive only broker transport variables."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )
        env = config.to_env_vars(**BROKER_ARGS)
        for forbidden in (
            "WORKER_REDIS_URL",
            "WORKER_API_URL",
            "WORKER_MANAGER_URL",
            "SECRETS_ENCRYPTION_KEY",
        ):
            assert forbidden not in env

    def test_no_orchestrator_env_vars(self):
        """Env vars should not contain any ORCHESTRATOR_ prefixed keys."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=["GIT"],
        )
        env = config.to_env_vars(**BROKER_ARGS)
        orchestrator_keys = [k for k in env if k.startswith("ORCHESTRATOR_")]
        assert orchestrator_keys == [], f"Unexpected ORCHESTRATOR_ env vars: {orchestrator_keys}"
