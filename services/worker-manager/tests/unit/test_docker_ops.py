import pytest
from unittest.mock import MagicMock, patch
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
