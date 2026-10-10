"""Unit test configuration."""

import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.config_store import ConfigStore
from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.run import RunType
from shared.contracts.vocab import AgentType
from shared.contracts.worker_turn import AttemptTurnMetadata

# Add /app to sys.path so that 'src' module can be imported.
# This is needed because the volume mount for tests doesn't include the src module.
app_path = Path("/app")
if app_path.exists() and str(app_path) not in sys.path:
    sys.path.insert(0, str(app_path))

# Provide required env vars for Settings validation in unit tests
os.environ.setdefault("API_BASE_URL", "http://api:8000")


@pytest.fixture(autouse=True)
def isolated_attempt_authority(monkeypatch):
    """Existing units have eligible API authority and no pending Redis output.

    Stop/park tests override these network boundaries with their required facts;
    actual SQL/Redis fence proofs execute in the API service suite in CI.
    """
    from shared import commit_publication
    from src.consumers import engineering

    monkeypatch.setattr(
        engineering, "_engineering_attempt_authority", AsyncMock(return_value="eligible")
    )
    monkeypatch.setattr(commit_publication, "pending_publication", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def isolated_worker_publication_transport(monkeypatch):
    """Simulate the eligible API's native stream write in client envelope units."""
    from shared.queues import WORKER_COMMANDS, worker_input_stream
    from src.clients import worker_spawner, worker_turns

    async def create(redis, command):
        await redis.xadd(WORKER_COMMANDS, {"data": command.model_dump_json()})

    async def turn(redis, worker_id, turn):
        await redis.xadd(
            worker_input_stream(worker_id),
            {"data": turn.model_dump_json(exclude_none=True)},
            maxlen=1000,
            approximate=True,
        )

    monkeypatch.setattr(worker_spawner, "_publish_create_command", create)
    monkeypatch.setattr(worker_turns, "_publish_engineering_turn", turn)


@pytest.fixture(autouse=True)
def mock_deploy_config_store(monkeypatch):
    """Keep deploy-consumer unit tests independent of the system-config API."""
    from src.consumers import deploy

    store = MagicMock(spec=ConfigStore)
    store.get_int.return_value = 3600
    monkeypatch.setattr(deploy, "_config", store)
    return store


@pytest.fixture(autouse=True)
def engineering_commit_without_env_contract(monkeypatch):
    """Keep the engineering success path off GitHub: the commit carries no contract.

    Tests of the derived-key check replace this with the contract they need.
    """
    from src.consumers import engineering_result_handler

    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(engineering_result_handler, "_fetch_env_contract", fetch)
    return fetch


#: Where the unit double says its catalog came from: the pinned kit tooling's own copy.
BUNDLED_KIT_CATALOG_SOURCE = "codegen-kit-tooling:framework/package_catalog.yaml"


@pytest.fixture(scope="session")
def bundled_kit_catalog():
    """The pinned kit's catalog, parsed once: reparsing its YAML per test cost ~65 ms each.

    The answer is the catalog the pinned kit tooling ships (`bundled_catalog`), filtered
    by the real `installable`, so it lists what the live catalog listed at the pin.
    """
    from dataclasses import replace
    from hashlib import sha256

    from framework.catalog import bundled_catalog

    from shared.catalog_activation import CATALOG_ACTIVATION
    from src import kit_catalog

    data = Path(__file__).parent / "fixtures/catalog-install"
    raw = (data / "catalog.yaml").read_text()
    return replace(
        kit_catalog.installable(bundled_catalog(), BUNDLED_KIT_CATALOG_SOURCE),
        raw=raw,
        bindings={"reminders": (data / "default.yaml").read_text()},
        manifests={"reminders": (data / "package.yaml").read_text()},
        # Read at a full commit, as every planning read is: an install names it.
        repository=CATALOG_ACTIVATION.repository,
        commit=CATALOG_ACTIVATION.commit,
        catalog_sha256=sha256(raw.encode()).hexdigest(),
    )


@pytest.fixture(scope="session")
def activated_kit_catalog():
    """The activated snapshot from genuine bytes, parsed once (`tests.unit.activated_catalog`)."""
    from tests.unit.activated_catalog import activated_catalog

    return activated_catalog()


@pytest.fixture(autouse=True)
def kit_catalog_off_github(monkeypatch, bundled_kit_catalog):
    """Keep planning off GitHub: the Architect is briefed with the pinned kit's catalog.

    Each test gets its own reader double over the session's `bundled_kit_catalog`.
    Tests of the reader build their own `KitCatalogReader`; a test of an unavailable
    catalog sets `read.return_value` on the reader this returns.
    """
    from src import catalog_product_settings, kit_catalog
    from src.consumers import architect

    reader = MagicMock(spec=kit_catalog.KitCatalogReader)
    reader.read = AsyncMock(return_value=bundled_kit_catalog)
    monkeypatch.setattr(architect, "get_kit_catalog_reader", lambda: reader)
    monkeypatch.setattr(catalog_product_settings, "get_kit_catalog_reader", lambda: reader)
    return reader


@pytest.fixture(autouse=True)
def paid_run_executor_for_legacy_unit_states(monkeypatch):
    """Keep pre-decision unit fixtures focused on their stated behavior."""
    from src.consumers import engineering
    from src.nodes.developer import DeveloperNode

    engineering_decision = ExecutorDecision(
        attempt_kind=RunType.ENGINEERING,
        agent_type=AgentType.CLAUDE,
        source=ExecutorDecisionSource.API_DEFAULT,
        policy_version="v1",
        reason="Engineering executor selected by API DEFAULT_AGENT_TYPE.",
    )
    original_run = DeveloperNode.run

    async def run_with_decision(self, state):
        state.setdefault("executor_decision", engineering_decision)
        return await original_run(self, state)

    monkeypatch.setattr(DeveloperNode, "run", run_with_decision)
    monkeypatch.setattr(
        engineering,
        "_load_engineering_executor_decision",
        AsyncMock(return_value=engineering_decision),
    )
    monkeypatch.setattr(
        engineering,
        "_recorded_attempt_turn",
        AsyncMock(return_value=AttemptTurnMetadata()),
    )
