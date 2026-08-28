"""Tests for applying monitoring to an existing managed server."""

from datetime import UTC, datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from shared.contracts.dto.server import ServerDTO
from src.provisioner.operations import provision_monitoring_baseline


@pytest.mark.asyncio
async def test_monitoring_baseline_runs_only_monitoring_tag_and_marks_server(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    server = ServerDTO(
        handle="adopted-vps",
        host="203.0.113.10",
        public_ip="203.0.113.10",
        ssh_user="dev",
        status="ready",
        provider="time4vps",
        provider_id="1001",
        is_managed=True,
        created_at=datetime.now(UTC),
    )
    runner = MagicMock()
    runner.run_playbook.return_value = (True, "ok")

    with (
        patch("src.provisioner.operations.get_server_info", new=AsyncMock(return_value=server)),
        patch(
            "src.provisioner.operations.get_server_ssh_key",
            new=AsyncMock(return_value="private-key"),
        ),
        patch("src.provisioner.operations.update_server_labels", new=AsyncMock()) as labels,
    ):
        success, message = await provision_monitoring_baseline(
            "adopted-vps", runner, orchestrator_ip="198.51.100.3"
        )

    assert success is True
    assert message == "Monitoring baseline applied successfully"
    assert runner.run_playbook.call_args.kwargs["playbook_name"] == "provision_software.yml"
    assert runner.run_playbook.call_args.kwargs["tags"] == ["monitoring"]
    assert runner.run_playbook.call_args.kwargs["deploy_user"] == "dev"
    assert runner.run_playbook.call_args.kwargs["ssh_user"] == "dev"
    assert runner.run_playbook.call_args.kwargs["ssh_private_key"] == "private-key"
    assert labels.await_args.args[1]["monitoring_baseline_applied_at"]


@pytest.mark.asyncio
async def test_monitoring_baseline_rejects_unmanaged_server(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    server = ServerDTO(
        handle="unmanaged-vps",
        host="203.0.113.11",
        public_ip="203.0.113.11",
        ssh_user="root",
        status="ready",
        provider="time4vps",
        provider_id="1001",
        is_managed=False,
        created_at=datetime.now(UTC),
    )

    with patch("src.provisioner.operations.get_server_info", new=AsyncMock(return_value=server)):
        success, message = await provision_monitoring_baseline("unmanaged-vps", MagicMock())

    assert success is False
    assert message == "Server is not authorized for provisioning"


@pytest.mark.asyncio
async def test_monitoring_baseline_rejects_stale_managed_server_outside_allowlist(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "2002")
    server = ServerDTO(
        handle="stale-vps",
        host="203.0.113.11",
        public_ip="203.0.113.11",
        ssh_user="root",
        status="ready",
        provider="time4vps",
        provider_id="1001",
        is_managed=True,
        created_at=datetime.now(UTC),
    )

    with patch("src.provisioner.operations.get_server_info", new=AsyncMock(return_value=server)):
        success, message = await provision_monitoring_baseline("stale-vps", MagicMock())

    assert success is False
    assert message == "Server is not authorized for provisioning"


@pytest.mark.asyncio
async def test_monitoring_baseline_rejects_server_without_stored_ssh_key(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "1001")
    server = ServerDTO(
        handle="keyless-vps",
        host="203.0.113.12",
        public_ip="203.0.113.12",
        ssh_user="dev",
        status="ready",
        provider="time4vps",
        provider_id="1001",
        is_managed=True,
        created_at=datetime.now(UTC),
    )
    runner = MagicMock()

    with (
        patch("src.provisioner.operations.get_server_info", new=AsyncMock(return_value=server)),
        patch("src.provisioner.operations.get_server_ssh_key", new=AsyncMock(return_value=None)),
    ):
        success, message = await provision_monitoring_baseline("keyless-vps", runner)

    assert success is False
    assert message == "Server has no stored SSH key"
    runner.run_playbook.assert_not_called()
