import fakeredis.aioredis
import pytest


@pytest.fixture
def mock_redis():
    server = fakeredis.FakeServer()
    return fakeredis.aioredis.FakeRedis(server=server)


@pytest.fixture
def mock_ansible_runner(mocker):
    """Mock the ansible runner to prevent actual execution."""
    ClassMock = mocker.patch("src.provisioner.ansible_runner.AnsibleRunner")
    return ClassMock.return_value
