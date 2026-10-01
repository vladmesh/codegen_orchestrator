"""Exercise the runbook's count-only admission check without HTTP or production I/O."""

import ast
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.contracts.dto.server import ServerDTO, ServerStatus
from shared.provisioning_policy import (
    TIME4VPS_PROVIDER,
    normalize_provider_id,
    provider_operation_is_authorized,
)
from shared.schemas import Time4VPSServer
from shared.server_admission import IN_PROGRESS_TARGET_STATUSES, target_readiness_reconcilable


@pytest.fixture
def counts(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    document = (
        Path(__file__).resolve().parents[4] / "docs/runbooks/po-redis-and-checkpoints.md"
    ).read_text()
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
    exec(compile(ast.parse(document[start:end]), "po-redis-and-checkpoints.md", "exec"), namespace)  # noqa: S102
    return namespace["po_maintenance_counts"]


@pytest.fixture
def run_preflight(monkeypatch, capsys):
    document = (
        Path(__file__).resolve().parents[4] / "docs/runbooks/po-redis-and-checkpoints.md"
    ).read_text()
    start = document.index("def po_maintenance_counts(")
    end = document.index("\nsys.exit(asyncio.run(preflight()))", start)

    def run(servers, inventory, managed_ids):
        events = []
        monkeypatch.setenv(
            "PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", ",".join(sorted(managed_ids))
        )

        class Provider:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get_servers(self):
                events.append("provider_inventory")
                return inventory

        async def get_servers():
            events.append("database_rows")
            return servers

        async def get_provider():
            return Provider()

        async def close():
            events.append("api_closed")

        namespace = {
            "asyncio": asyncio,
            "json": json,
            "TIME4VPS_PROVIDER": TIME4VPS_PROVIDER,
            "IN_PROGRESS_TARGET_STATUSES": IN_PROGRESS_TARGET_STATUSES,
            "target_readiness_reconcilable": target_readiness_reconcilable,
            "provider_operation_is_authorized": provider_operation_is_authorized,
            "normalize_provider_id": normalize_provider_id,
            "ServerStatus": ServerStatus,
            "api_client": SimpleNamespace(get_servers=get_servers, close=close),
            "get_time4vps_client": get_provider,
            "managed_provider_ids": lambda _: managed_ids,
        }
        exec(  # noqa: S102
            compile(ast.parse(document[start:end]), "po-redis-and-checkpoints.md", "exec"),
            namespace,
        )
        exit_code = asyncio.run(namespace["preflight"]())
        return exit_code, json.loads(capsys.readouterr().out), events

    return run


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


ZERO_COUNTS = {
    "scheduled_servers": 0,
    "authorized_pending_setup": 0,
    "unreconcilable_managed": 0,
    "allowlist_without_reconciled_row": 0,
    "provider_inventory_drift": 0,
}


def historical_rows():
    return [
        server(
            handle=f"vps-{provider_id}",
            provider_id=provider_id,
            is_managed=False,
            status=ServerStatus.UNREACHABLE,
            labels={},
        )
        for provider_id in ("273978", "275928")
    ]


def test_real_preflight_admits_historical_deleted_pair_and_reaches_next_stage(run_preflight):
    rows = [server(), *historical_rows()]
    exit_code, result, events = run_preflight(rows, [provider()], {"1001"})
    assert exit_code == 0
    assert result == ZERO_COUNTS
    assert [row.handle for row in rows[1:]] == ["vps-273978", "vps-275928"]
    assert events == ["database_rows", "provider_inventory", "api_closed"]
    next_stage = []
    if exit_code == 0:
        next_stage.append("backup")
    assert next_stage == ["backup"]


@pytest.mark.parametrize(
    ("rows", "inventory", "managed_ids", "expected"),
    [
        ([], [provider()], set(), {"provider_inventory_drift": 1}),
        (
            [server()],
            [],
            {"1001"},
            {"provider_inventory_drift": 1, "allowlist_without_reconciled_row": 0},
        ),
        ([server(is_managed=False)], [], set(), {"provider_inventory_drift": 1}),
        (
            [server(is_managed=False, status=ServerStatus.UNREACHABLE)],
            [],
            {"1001"},
            {"provider_inventory_drift": 1, "allowlist_without_reconciled_row": 1},
        ),
        (
            [server(is_managed=False, status=ServerStatus.READY)],
            [],
            set(),
            {"provider_inventory_drift": 1},
        ),
        (
            [server(is_managed=False, status=ServerStatus.UNREACHABLE, provider_id=None)],
            [],
            set(),
            {"provider_inventory_drift": 1},
        ),
        (
            [server(is_managed=False, status=ServerStatus.UNREACHABLE, provider_id="invalid")],
            [],
            set(),
            {"provider_inventory_drift": 1},
        ),
        (
            [server(status=ServerStatus.UNREACHABLE)],
            [],
            set(),
            {"provider_inventory_drift": 1},
        ),
        (
            [
                server(),
                *historical_rows(),
                historical_rows()[0].model_copy(update={"handle": "copy"}),
            ],
            [provider()],
            {"1001"},
            {"provider_inventory_drift": 1},
        ),
        (
            [server(is_managed=False, status=ServerStatus.PENDING_SETUP)],
            [],
            set(),
            {"provider_inventory_drift": 1, "scheduled_servers": 1},
        ),
        (
            [server(is_managed=False, status=ServerStatus.UNREACHABLE)],
            [provider()],
            {"1001"},
            {"provider_inventory_drift": 1, "allowlist_without_reconciled_row": 1},
        ),
        ([server()], [provider(ip="203.0.113.8")], {"1001"}, {"provider_inventory_drift": 1}),
        ([server()], [provider(), provider()], {"1001"}, {"provider_inventory_drift": 1}),
        (
            [server()],
            [provider(domain="changed.example.com")],
            {"1001"},
            {"provider_inventory_drift": 1},
        ),
        (
            [server(), server(handle="duplicate")],
            [provider()],
            {"1001"},
            {"provider_inventory_drift": 2, "allowlist_without_reconciled_row": 1},
        ),
        ([server()], [provider()], {"1001", "1002"}, {"allowlist_without_reconciled_row": 1}),
        (
            [server(status=ServerStatus.PENDING_SETUP)],
            [provider()],
            {"1001"},
            {
                "scheduled_servers": 1,
                "authorized_pending_setup": 1,
                "unreconcilable_managed": 1,
                "allowlist_without_reconciled_row": 1,
            },
        ),
        (
            [server(is_managed=False, status=ServerStatus.PENDING_SETUP)],
            [provider()],
            {"1001"},
            {
                "scheduled_servers": 1,
                "allowlist_without_reconciled_row": 1,
                "provider_inventory_drift": 1,
            },
        ),
        (
            [server(status=ServerStatus.PROVISIONING)],
            [provider()],
            {"1001"},
            {
                "scheduled_servers": 1,
                "unreconcilable_managed": 1,
                "allowlist_without_reconciled_row": 1,
            },
        ),
        (
            [server(status=ServerStatus.FORCE_REBUILD)],
            [provider()],
            {"1001"},
            {
                "scheduled_servers": 1,
                "unreconcilable_managed": 1,
                "allowlist_without_reconciled_row": 1,
            },
        ),
        (
            [server(labels={})],
            [provider()],
            {"1001"},
            {"unreconcilable_managed": 1, "allowlist_without_reconciled_row": 1},
        ),
    ],
)
def test_real_preflight_blocks_unsettled_inventory_before_safety_stages(
    run_preflight, rows, inventory, managed_ids, expected
):
    exit_code, result, events = run_preflight(rows, inventory, managed_ids)
    assert exit_code == 1
    assert result == ZERO_COUNTS | expected
    assert events == ["database_rows", "provider_inventory", "api_closed"]
    safety_stages = []
    if exit_code == 0:
        safety_stages.extend(["backup", "pause", "deploy"])
    assert safety_stages == []
