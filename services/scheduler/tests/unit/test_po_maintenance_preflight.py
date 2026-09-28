"""Exercise the runbook's count-only admission check without HTTP or production I/O."""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shared.contracts.dto.server import ServerDTO, ServerStatus
from shared.provisioning_policy import TIME4VPS_PROVIDER, provider_operation_is_authorized
from shared.schemas import Time4VPSServer
from shared.server_admission import IN_PROGRESS_TARGET_STATUSES, target_readiness_reconcilable


@pytest.fixture
def counts(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    document = (Path(__file__).resolve().parents[4] / "docs/SECRETS.md").read_text()
    start = document.index("def po_maintenance_counts(")
    end = document.index("\nasync def preflight():", start)
    namespace = {
        "TIME4VPS_PROVIDER": TIME4VPS_PROVIDER,
        "IN_PROGRESS_TARGET_STATUSES": IN_PROGRESS_TARGET_STATUSES,
        "target_readiness_reconcilable": target_readiness_reconcilable,
        "provider_operation_is_authorized": provider_operation_is_authorized,
        "ServerStatus": ServerStatus,
    }
    # Execute the repository-owned operator predicate, never network-supplied code.
    exec(compile(ast.parse(document[start:end]), "SECRETS.md", "exec"), namespace)  # noqa: S102
    return namespace["po_maintenance_counts"]


def server(**changes):
    return ServerDTO.model_validate(
        {
            "handle": "vps-1001",
            "host": "target.example.com",
            "public_ip": "203.0.113.7",
            "ssh_user": "root",
            "is_managed": True,
            "provider": "time4vps",
            "provider_id": "1001",
            "status": "ready",
            "labels": {"provisioning_phase": "complete"},
            "created_at": datetime.now(UTC),
            **changes,
        }
    )


def provider(**changes):
    return Time4VPSServer.model_validate(
        {"server_id": 1001, "ip": "203.0.113.7", "domain": "target.example.com", **changes}
    )


def test_idle_reconciled_inventory_admits_deploy(counts):
    assert not any(counts([server()], [provider()], {"1001"}).values())


@pytest.mark.parametrize("status", list(IN_PROGRESS_TARGET_STATUSES))
def test_provisioning_work_refuses_deploy(counts, status):
    result = counts([server(status=status)], [provider()], {"1001"})
    assert result["scheduled_servers"] == 1
    assert result["unreconcilable_managed"] == 1
    if status == ServerStatus.PENDING_SETUP:
        assert result["authorized_pending_setup"] == 1


def test_even_unauthorized_pending_work_requires_settlement(counts):
    result = counts([server(is_managed=False, status="pending_setup")], [provider()], {"1001"})
    assert result["scheduled_servers"] == 1
    assert result["authorized_pending_setup"] == 0


@pytest.mark.parametrize("changes", [{"labels": {}}, {"host": "", "public_ip": ""}])
def test_incomplete_or_unaddressable_target_refuses_deploy(counts, changes):
    assert counts([server(**changes)], [provider()], {"1001"})["unreconcilable_managed"] == 1


def test_new_allowlisted_machine_refuses_before_discovery_can_create_pending_work(counts):
    result = counts([server()], [provider(), provider(server_id=1002)], {"1001", "1002"})
    assert result["allowlist_without_reconciled_row"] == 1
    assert result["provider_inventory_drift"] == 1


@pytest.mark.parametrize(
    "inventory", [[], [provider(ip="203.0.113.8")], [provider(domain="changed.example.com")]]
)
def test_provider_identity_drift_refuses_deploy(counts, inventory):
    assert counts([server()], inventory, {"1001"})["provider_inventory_drift"] > 0


def test_duplicate_provider_identity_refuses_deploy(counts):
    assert (
        counts([server(), server(handle="duplicate")], [provider()], {"1001"})[
            "provider_inventory_drift"
        ]
        > 0
    )
