"""Phase 3 shim removal: shared compat aliases and the worker:lifecycle contract are gone."""

import importlib

import pytest

import shared
from shared import models, queues
from shared.contracts.queues.worker import WorkerChannels


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


def test_shared_redis_export_no_longer_silently_none():
    # The old try/except shim left RedisStreamClient = None on import failure.
    assert shared.RedisStreamClient is not None
