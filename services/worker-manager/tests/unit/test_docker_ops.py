import pytest
import docker
from dataclasses import dataclass
from unittest.mock import MagicMock, patch
from collections import namedtuple

from src.docker_ops import DockerClientWrapper


@pytest.fixture
def mock_docker():
    with patch("docker.from_env") as mock:
        yield mock


@pytest.mark.asyncio
async def test_run_container(mock_docker):
    client_mock = MagicMock()
    mock_docker.return_value = client_mock
    container_mock = MagicMock()
    client_mock.containers.run.return_value = container_mock

    wrapper = DockerClientWrapper()
    res = await wrapper.run_container("image:latest", detach=True)

    assert res == container_mock
    client_mock.containers.run.assert_called_once_with("image:latest", detach=True)


@pytest.mark.asyncio
async def test_remove_container(mock_docker):
    client_mock = MagicMock()
    mock_docker.return_value = client_mock
    container_mock = MagicMock()
    client_mock.containers.get.side_effect = [container_mock, docker.errors.NotFound("gone")]

    wrapper = DockerClientWrapper()
    await wrapper.remove_container("test-id")

    assert client_mock.containers.get.call_count == 2
    container_mock.remove.assert_called_once()


@pytest.mark.asyncio
async def test_remove_container_waits_for_concurrent_removal(mock_docker):
    client_mock = MagicMock()
    mock_docker.return_value = client_mock
    container_mock = MagicMock()
    container_mock.remove.side_effect = docker.errors.APIError(
        "conflict",
        response=MagicMock(status_code=409),
        explanation="removal is already in progress",
    )
    client_mock.containers.get.side_effect = [container_mock, docker.errors.NotFound("gone")]

    wrapper = DockerClientWrapper()
    await wrapper.remove_container("test-id", poll_interval=0)

    assert client_mock.containers.get.call_count == 2


@pytest.mark.asyncio
async def test_remove_container_fails_when_concurrent_removal_does_not_finish(mock_docker):
    client_mock = MagicMock()
    mock_docker.return_value = client_mock
    container_mock = MagicMock()
    container_mock.remove.side_effect = docker.errors.APIError(
        "conflict",
        response=MagicMock(status_code=409),
        explanation="removal is already in progress",
    )
    client_mock.containers.get.return_value = container_mock

    wrapper = DockerClientWrapper()
    with pytest.raises(RuntimeError, match="still exists"):
        await wrapper.remove_container("test-id", verify_attempts=1, poll_interval=0)


@pytest.mark.asyncio
async def test_remove_container_propagates_operational_error(mock_docker):
    client_mock = MagicMock()
    mock_docker.return_value = client_mock
    container_mock = MagicMock()
    container_mock.remove.side_effect = docker.errors.APIError("daemon unavailable")
    client_mock.containers.get.return_value = container_mock

    wrapper = DockerClientWrapper()
    with pytest.raises(docker.errors.APIError, match="daemon unavailable"):
        await wrapper.remove_container("test-id", poll_interval=0)


@dataclass
class ExecResult:
    exit_code: int
    output: bytes


MockExecResult = namedtuple("MockExecResult", ["exit_code", "output"])


class TestDockerExec:
    @pytest.mark.asyncio
    async def test_exec_in_container_returns_exit_code_and_output(self, mock_docker):
        """exec_in_container should return (exit_code, output)."""
        client_mock = MagicMock()
        mock_docker.return_value = client_mock
        mock_container = MagicMock()
        # Mock exec_run to return object with exit_code and output
        mock_container.exec_run.return_value = MockExecResult(exit_code=0, output=b"hello")

        client_mock.containers.get.return_value = mock_container

        wrapper = DockerClientWrapper()
        exit_code, output = await wrapper.exec_in_container("c-123", "echo hello")

        assert exit_code == 0
        assert "hello" in output.decode()
        mock_container.exec_run.assert_called_once()
        args, kwargs = mock_container.exec_run.call_args
        assert kwargs["cmd"] == "echo hello"
        assert kwargs["user"] == "worker"

    @pytest.mark.asyncio
    async def test_exec_in_container_handles_failure(self, mock_docker):
        """Should handle non-zero exit codes."""
        client_mock = MagicMock()
        mock_docker.return_value = client_mock
        mock_container = MagicMock()
        mock_container.exec_run.return_value = MockExecResult(exit_code=1, output=b"error")
        client_mock.containers.get.return_value = mock_container

        wrapper = DockerClientWrapper()
        exit_code, output = await wrapper.exec_in_container("c-123", "bad command")

        assert exit_code == 1
        assert "error" in output.decode()


class TestDockerNetworks:
    @pytest.mark.asyncio
    async def test_create_network(self, mock_docker):
        """create_network should call networks.create with name and driver."""
        client_mock = MagicMock()
        mock_docker.return_value = client_mock
        network_mock = MagicMock()
        client_mock.networks.create.return_value = network_mock

        wrapper = DockerClientWrapper()
        result = await wrapper.create_network("dev_proj_worker1")

        assert result == network_mock
        client_mock.networks.create.assert_called_once_with("dev_proj_worker1", driver="bridge")

    @pytest.mark.asyncio
    async def test_remove_network(self, mock_docker):
        """remove_network should get and remove the network."""

        client_mock = MagicMock()
        mock_docker.return_value = client_mock
        network_mock = MagicMock()
        client_mock.networks.get.return_value = network_mock

        wrapper = DockerClientWrapper()
        await wrapper.remove_network("dev_proj_worker1")

        client_mock.networks.get.assert_called_once_with("dev_proj_worker1")
        network_mock.remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_network_ignores_not_found(self, mock_docker):
        """remove_network should silently ignore NotFound errors."""
        import docker

        client_mock = MagicMock()
        mock_docker.return_value = client_mock
        client_mock.networks.get.side_effect = docker.errors.NotFound("not found")

        wrapper = DockerClientWrapper()
        # Should not raise
        await wrapper.remove_network("nonexistent")

    @pytest.mark.asyncio
    async def test_connect_network(self, mock_docker):
        """connect_network should get the network and connect the container."""
        client_mock = MagicMock()
        mock_docker.return_value = client_mock
        network_mock = MagicMock()
        client_mock.networks.get.return_value = network_mock

        wrapper = DockerClientWrapper()
        await wrapper.connect_network("dev_proj_worker1", "container-abc")

        client_mock.networks.get.assert_called_once_with("dev_proj_worker1")
        network_mock.connect.assert_called_once_with("container-abc")

    @pytest.mark.asyncio
    async def test_list_networks(self, mock_docker):
        """list_networks should call networks.list and return results."""
        client_mock = MagicMock()
        mock_docker.return_value = client_mock
        net1 = MagicMock()
        net1.name = "dev_proj_abc"
        net2 = MagicMock()
        net2.name = "bridge"
        client_mock.networks.list.return_value = [net1, net2]

        wrapper = DockerClientWrapper()
        result = await wrapper.list_networks()

        assert result == [net1, net2]
        client_mock.networks.list.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_network_ignores_not_found(self, mock_docker):
        """disconnect_network should silently ignore NotFound errors."""
        import docker

        client_mock = MagicMock()
        mock_docker.return_value = client_mock
        client_mock.networks.get.side_effect = docker.errors.NotFound("not found")

        wrapper = DockerClientWrapper()
        # Should not raise
        await wrapper.disconnect_network("nonexistent", "container-abc")
