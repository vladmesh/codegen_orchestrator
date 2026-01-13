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

    def test_docker_capability_adds_socket_mount(self):
        """DOCKER capability should mount docker.sock."""
        config = WorkerContainerConfig(
            worker_id="test-1",
            worker_type="developer",
            agent_type="claude",
            capabilities=["DOCKER"],
            auth_mode="host_session",
        )
        volumes = config.to_volume_mounts()
        assert "/var/run/docker.sock" in volumes
        assert volumes["/var/run/docker.sock"]["bind"] == "/var/run/docker.sock"

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
