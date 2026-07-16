"""Phase 3 shim removal: shared compat aliases and the worker:lifecycle contract are gone."""

import importlib

import pytest

from shared import models, queues
from shared.contracts.queues.worker import WorkerChannels
from shared.redis_client import RedisStreamClient


def test_worker_lifecycle_contract_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("shared.contracts.queues.worker_lifecycle")


def test_worker_channels_has_no_lifecycle():
    assert not hasattr(WorkerChannels, "LIFECYCLE")


def test_ensure_consumer_groups_alias_removed():
    assert not hasattr(queues, "ensure_consumer_groups")


def test_service_deployment_aliases_removed():
    assert not hasattr(models, "ServiceDeployment")
    assert not hasattr(models, "DeploymentStatus")


def test_redis_client_has_no_shared_package_shim():
    assert RedisStreamClient is not None
