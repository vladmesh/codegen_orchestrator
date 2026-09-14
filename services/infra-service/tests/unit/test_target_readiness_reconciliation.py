"""The all-managed reconciliation the production deploy runs after its services are healthy."""

from datetime import UTC, datetime
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from shared.contracts.dto.server import ServerDTO
from shared.server_admission import PROVISIONING_PHASE_COMPLETE, PROVISIONING_PHASE_LABEL
from src.provisioner import operations, target_readiness
from src.provisioner.api_client import TargetReadinessSupersededError
from src.provisioner.target_readiness import main, reconcile_managed_targets

PLAYBOOKS = Path(__file__).parents[2] / "ansible" / "playbooks"
LOGIN = PLAYBOOKS / operations.TARGET_READINESS_LOGIN_PLAYBOOK
PRIVILEGE = PLAYBOOKS / operations.TARGET_READINESS_PRIVILEGE_PLAYBOOK
RETROFIT = PLAYBOOKS / operations.QA_IDENTITY_RETROFIT_PLAYBOOK
REVISION = "b" * 40
COMPLETE = {PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE}


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
        "labels": dict(COMPLETE),
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ServerDTO(**base)


@pytest.fixture
def fleet():
    """A fleet reconciliation can prove, and the retrofit it calls per row."""
    rows: list[ServerDTO] = [
        _server("vps-ready"),
        _server("vps-error", status="error"),
        _server("vps-unreachable", status="unreachable"),
        _server("vps-reserved", status="reserved"),
    ]
    retrofit = AsyncMock(return_value=(True, "ok"))
    with (
        patch.object(target_readiness, "list_managed_servers", new=AsyncMock(return_value=rows)),
        patch.object(target_readiness, "retrofit_qa_identity", new=retrofit),
    ):
        yield rows, retrofit


async def test_every_phase_complete_managed_target_is_reconciled_whatever_its_status(fleet):
    """A target that is `unreachable` or `reserved` today is proved by this revision too."""
    _, retrofit = fleet

    verdicts = await reconcile_managed_targets(MagicMock(), revision=REVISION)

    assert sorted(call.args[0] for call in retrofit.await_args_list) == [
        "vps-error",
        "vps-ready",
        "vps-reserved",
        "vps-unreachable",
    ]
    assert {call.kwargs["revision"] for call in retrofit.await_args_list} == {REVISION}
    assert {verdict.outcome for verdict in verdicts} == {"ready"}


async def test_a_fully_recorded_fleet_exits_zero_even_with_targets_not_ready(fleet, capsys):
    _, retrofit = fleet
    retrofit.side_effect = [
        (False, "QA identity retrofit failed at ssh_key_invalid"),
        (True, "ok"),
        (True, "ok"),
        (True, "ok"),
    ]

    assert await main(["--revision", REVISION]) == 0
    assert '"outcome": "not_ready"' in capsys.readouterr().out


@pytest.mark.parametrize("status", ["pending_setup", "provisioning", "force_rebuild"])
async def test_a_row_provisioning_owns_is_an_explicit_non_success(fleet, capsys, status):
    rows, retrofit = fleet
    rows.append(_server("vps-busy", status=status))

    assert await main(["--revision", REVISION]) == 1
    out = capsys.readouterr().out
    assert '"server_handle": "vps-busy", "outcome": "in_progress"' in out
    assert "vps-busy" not in [call.args[0] for call in retrofit.await_args_list]


async def test_a_managed_row_without_a_complete_software_phase_is_unhandled(fleet, capsys):
    rows, retrofit = fleet
    rows.append(_server("vps-half", status="active", labels={}))

    assert await main(["--revision", REVISION]) == 1
    assert '"server_handle": "vps-half", "outcome": "unhandled"' in capsys.readouterr().out
    assert "vps-half" not in [call.args[0] for call in retrofit.await_args_list]


async def test_a_row_that_changed_before_it_was_reconciled_is_unhandled(fleet, capsys):
    _, retrofit = fleet
    retrofit.side_effect = [
        (False, operations.NOT_RECONCILABLE),
        (True, "ok"),
        (True, "ok"),
        (True, "ok"),
    ]

    assert await main(["--revision", REVISION]) == 1
    assert '"outcome": "unhandled"' in capsys.readouterr().out


async def test_a_superseded_verdict_is_a_non_success(fleet, capsys):
    _, retrofit = fleet
    retrofit.side_effect = [
        TargetReadinessSupersededError("vps-error: identity changed"),
        (True, "ok"),
        (True, "ok"),
        (True, "ok"),
    ]

    assert await main(["--revision", REVISION]) == 1
    assert '"outcome": "superseded"' in capsys.readouterr().out


async def test_a_verdict_that_could_not_be_recorded_fails_the_command(fleet, capsys):
    _, retrofit = fleet
    retrofit.side_effect = [
        RuntimeError("API did not answer"),
        (True, "ok"),
        (True, "ok"),
        (True, "ok"),
    ]

    assert await main(["--revision", REVISION]) == 1
    out = capsys.readouterr().out
    # The rest of the fleet is still reconciled and reported.
    assert '"server_handle": "vps-ready", "outcome": "ready"' in out
    assert '"outcome": "unrecorded"' in out


@pytest.mark.parametrize("revision", ["main", "b" * 39, "B" * 40])
async def test_the_command_is_bound_to_an_exact_revision(fleet, revision):
    _, retrofit = fleet
    with pytest.raises(SystemExit):
        await main(["--revision", revision])
    retrofit.assert_not_awaited()


class TestItCanOnlyReconcile:
    """No reinstall, no firewall, no QA or stand run: three non-destructive playbooks."""

    def test_the_operation_runs_only_the_two_probes_and_the_retrofit(self):
        assert [playbook for playbook, _ in operations.TARGET_READINESS_PREFLIGHT] == [
            operations.TARGET_READINESS_LOGIN_PLAYBOOK,
            operations.TARGET_READINESS_PRIVILEGE_PLAYBOOK,
        ]
        source = Path(operations.__file__).read_text()
        body = source[
            source.index("async def retrofit_qa_identity") : source.index(
                "async def reset_server_password"
            )
        ]
        assert "playbook_name=preflight_playbook" in body
        assert "playbook_name=QA_IDENTITY_RETROFIT_PLAYBOOK" in body
        assert body.count("playbook_name=") == 2
        for destructive in (
            "time4vps_client",
            "reinstall_server",
            "provider_operation_is_authorized",
        ):
            assert destructive not in body

    @pytest.mark.parametrize("playbook", [LOGIN, PRIVILEGE, RETROFIT])
    def test_no_playbook_touches_the_firewall_or_the_provider(self, playbook):
        modules = _task_modules(yaml.safe_load(playbook.read_text()))

        assert modules, f"{playbook.name} declares no task"
        for module, argument in modules:
            assert "ufw" not in module and "iptables" not in module and "reboot" not in module
            assert argument not in {"firewall", "provision_access", "monitoring"}

    def test_the_login_probe_does_not_escalate_and_writes_nothing(self):
        (login,) = yaml.safe_load(LOGIN.read_text())

        assert login["become"] is False
        assert [task["ansible.builtin.raw"] for task in login["tasks"]] == ["id -un"]

    def test_the_privilege_probe_escalates_to_root_and_writes_nothing(self):
        (privilege,) = yaml.safe_load(PRIVILEGE.read_text())

        assert privilege["become"] is True
        assert privilege["tasks"][0]["ansible.builtin.command"] == "id -u"
        assert "trim == '0'" in privilege["tasks"][1]["ansible.builtin.assert"]["that"][0]
        for task in privilege["tasks"]:
            assert not {
                "ansible.builtin.copy",
                "ansible.builtin.file",
                "ansible.builtin.apt",
            } & set(task)
