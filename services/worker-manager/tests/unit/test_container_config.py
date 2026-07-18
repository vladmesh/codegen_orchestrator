from src.container_config import WorkerContainerConfig


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
        env = config.to_env_vars(redis_url="redis://r", api_url="http://api")

        # All config vars use WORKER_ prefix for pydantic-settings compatibility
        assert env["WORKER_ID"] == "test-1"
        assert env["WORKER_REDIS_URL"] == "redis://r"
        assert env["WORKER_AGENT_TYPE"] == "claude"
        assert env["WORKER_API_URL"] == "http://api"
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

        env = config.to_env_vars(redis_url="redis://r", api_url="http://api")

        assert env["CODEX_HOME"] == "/home/worker/.codex"
        assert "OPENAI_API_KEY" not in env

    def test_codex_api_key_mode_uses_openai_variable(self):
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="codex",
            capabilities=["GIT"],
            auth_mode="api_key",
            api_key="sk-openai-test",
        )

        env = config.to_env_vars(redis_url="redis://r", api_url="http://api")

        assert env["OPENAI_API_KEY"] == "sk-openai-test"

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
        env = config.to_env_vars(redis_url="redis://r", api_url="http://api")
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_to_docker_run_kwargs_defaults_to_host_network(self):
        """Without network_name, should use host networking."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )
        kwargs = config.to_docker_run_kwargs()
        assert kwargs["network_mode"] == "host"
        assert "network" not in kwargs
        assert kwargs["mem_limit"] == "4g"

    def test_noop_worker_keeps_lower_memory_limit(self):
        """Noop workers do not need the real-agent memory budget."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="noop",
            capabilities=[],
        )
        kwargs = config.to_docker_run_kwargs()
        assert kwargs["mem_limit"] == "2g"

    def test_factory_worker_gets_real_agent_memory_limit(self):
        """Factory workers need the same memory budget as other real LLM agents."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="factory",
            capabilities=[],
        )
        kwargs = config.to_docker_run_kwargs()
        assert kwargs["mem_limit"] == "4g"

    def test_codex_worker_gets_real_agent_memory_limit(self):
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="codex",
            capabilities=[],
        )
        assert config.to_docker_run_kwargs()["mem_limit"] == "4g"

    def test_to_docker_run_kwargs_with_network_name(self):
        """With network_name, should attach to that network."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )
        kwargs = config.to_docker_run_kwargs(network_name="test-network")
        assert kwargs["network"] == "test-network"
        assert "network_mode" not in kwargs

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

    def test_worker_manager_url_env_var(self):
        """When worker_manager_url is provided, WORKER_MANAGER_URL should be set."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )
        env = config.to_env_vars(
            redis_url="redis://r",
            api_url="http://api",
            worker_manager_url="http://worker-manager:8000",
        )
        assert env["WORKER_MANAGER_URL"] == "http://worker-manager:8000"

    def test_worker_manager_url_absent_when_not_provided(self):
        """When worker_manager_url is not provided, env var should not be set."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=[],
        )
        env = config.to_env_vars(redis_url="redis://r", api_url="http://api")
        assert "WORKER_MANAGER_URL" not in env

    def test_no_orchestrator_env_vars(self):
        """Env vars should not contain any ORCHESTRATOR_ prefixed keys."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=["GIT"],
        )
        env = config.to_env_vars(
            redis_url="redis://r",
            api_url="http://api",
            worker_manager_url="http://wm:8000",
        )
        orchestrator_keys = [k for k in env if k.startswith("ORCHESTRATOR_")]
        assert orchestrator_keys == [], f"Unexpected ORCHESTRATOR_ env vars: {orchestrator_keys}"
