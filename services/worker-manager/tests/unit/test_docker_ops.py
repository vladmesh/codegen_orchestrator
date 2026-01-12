import pytest
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
    client_mock.containers.get.return_value = container_mock

    wrapper = DockerClientWrapper()
    await wrapper.remove_container("test-id")

    client_mock.containers.get.assert_called_once_with("test-id")
    container_mock.remove.assert_called_once()


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
