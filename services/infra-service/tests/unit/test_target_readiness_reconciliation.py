"""The all-managed reconciliation the production deploy runs after its services are healthy."""

from datetime import UTC, datetime
import os
from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from shared.contracts.dto.server import ServerDTO
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL
from src.provisioner import operations, target_readiness
from src.provisioner.target_readiness import main, reconcile_managed_targets

PLAYBOOKS = Path(__file__).parents[2] / "ansible" / "playbooks"
PREFLIGHT = PLAYBOOKS / operations.TARGET_READINESS_PREFLIGHT_PLAYBOOK
RETROFIT = PLAYBOOKS / operations.QA_IDENTITY_RETROFIT_PLAYBOOK
REVISION = "b" * 40


def _task_modules(plays: list[dict]) -> list[tuple[str, object]]:
    """Every (module, role-name-or-None) a playbook's plays declare, pre-tasks included."""
    found: list[tuple[str, object]] = []
    for play in plays:
        for section in ("pre_tasks", "tasks", "post_tasks", "roles"):
            for task in play.get(section, []) or []:
                for key, value in task.items():
                    if "." in key:
                        role = value.get("name") if isinstance(value, dict) else None
                        found.append((key, role))
    return found


def _server(handle: str, **overrides) -> ServerDTO:
    base = {
        "handle": handle,
        "host": "203.0.113.20",
        "public_ip": "203.0.113.20",
        "ssh_user": "root",
        "status": "ready",
        "is_managed": True,
        "labels": {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ServerDTO(**base)


@pytest.fixture
def fleet():
    servers = [
        _server("vps-b"),
        _server("vps-a", status="error"),
        _server("vps-new", status="provisioning", labels={}),
    ]
    retrofit = AsyncMock(return_value=(True, "ok"))
    with (
        patch.object(target_readiness, "list_managed_servers", new=AsyncMock(return_value=servers)),
        patch.object(target_readiness, "retrofit_qa_identity", new=retrofit),
    ):
        yield retrofit


async def test_every_reconcilable_target_is_reconciled_with_the_deployed_revision(fleet):
    verdicts = await reconcile_managed_targets(MagicMock(), revision=REVISION)

    reconciled = [call.args[0] for call in fleet.await_args_list]
    # A target reconciliation itself parked is retried; one being provisioned is not its.
    assert reconciled == ["vps-a", "vps-b"]
    assert {call.kwargs["revision"] for call in fleet.await_args_list} == {REVISION}
    assert [(v.server_handle, v.outcome) for v in verdicts] == [
        ("vps-a", "ready"),
        ("vps-b", "ready"),
        ("vps-new", "skipped"),
    ]


async def test_a_target_found_not_ready_is_a_recorded_verdict_and_exits_zero(fleet, capsys):
    fleet.side_effect = [(False, "QA identity retrofit failed at ssh_key_invalid"), (True, "ok")]

    assert await main(["--revision", REVISION]) == 0
    assert '"outcome": "not_ready"' in capsys.readouterr().out


async def test_a_verdict_that_could_not_be_recorded_fails_the_command(fleet, capsys):
    fleet.side_effect = [RuntimeError("API did not answer"), (True, "ok")]

    assert await main(["--revision", REVISION]) == 1
    out = capsys.readouterr().out
    # The rest of the fleet is still reconciled and reported.
    assert '"server_handle": "vps-b", "outcome": "ready"' in out
    assert '"outcome": "unrecorded"' in out


@pytest.mark.parametrize("revision", ["main", "b" * 39, "B" * 40])
async def test_the_command_is_bound_to_an_exact_revision(fleet, revision):
    with pytest.raises(SystemExit):
        await main(["--revision", revision])
    fleet.assert_not_awaited()


class TestItCanOnlyReconcile:
    """No reinstall, no firewall, no QA or stand run: two playbooks, both non-destructive."""

    def test_the_operation_runs_only_the_preflight_and_the_retrofit(self):
        source = Path(operations.__file__).read_text()
        start = source.index("async def retrofit_qa_identity")
        end = source.index("async def reset_server_password")
        body = source[start:end]

        assert re.findall(r"playbook_name=(\w+)", body) == [
            "TARGET_READINESS_PREFLIGHT_PLAYBOOK",
            "QA_IDENTITY_RETROFIT_PLAYBOOK",
        ]
        for destructive in (
            "time4vps_client",
            "reinstall_server",
            "provider_operation_is_authorized",
        ):
            assert destructive not in body

    @pytest.mark.parametrize("playbook", [PREFLIGHT, RETROFIT])
    def test_neither_playbook_touches_the_firewall_or_the_provider(self, playbook):
        modules = _task_modules(yaml.safe_load(playbook.read_text()))

        assert modules, f"{playbook.name} declares no task"
        for module, argument in modules:
            assert "ufw" not in module and "iptables" not in module and "reboot" not in module
            assert argument not in {"firewall", "provision_access", "monitoring"}

    def test_the_preflight_logs_in_before_it_escalates_and_writes_nothing(self):
        login, privilege = yaml.safe_load(PREFLIGHT.read_text())

        assert login["become"] is False
        assert login["tasks"][0]["ansible.builtin.raw"] == "id -un"
        assert privilege["become"] is True
        assert privilege["tasks"][0]["ansible.builtin.command"] == "id -u"
        assert "trim == '0'" in privilege["tasks"][1]["ansible.builtin.assert"]["that"][0]
        for play in (login, privilege):
            for task in play["tasks"]:
                assert not {
                    "ansible.builtin.copy",
                    "ansible.builtin.file",
                    "ansible.builtin.apt",
                } & set(task)
