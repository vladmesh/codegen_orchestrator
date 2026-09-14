"""A bootstrap credential opens the host; only the generated key may prove it.

Every provisioning route starts with something that is not the key the platform
will keep: BitLaunch's provider creation key, whatever existing access a
Time4VPS host already had, a reinstall's root password. That credential runs the
access play, which installs the provisioner's public key, and nothing else. A
fresh `admin_login` with the generated private key, as the row's administrative
account, follows it; the proof-bearing software play runs through that same
identity; and only that proof reaches the success handler. A route whose access
succeeds but whose generated key cannot log in never reaches success at all.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from shared.contracts.dto.incident import IncidentType
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.provisioner import operations
from src.provisioner.node import ProvisionerNode
from src.provisioner.operations import TARGET_READINESS_LOGIN_PLAYBOOK, reinstall_and_provision

BOOTSTRAP_KEY = fleet_private_key()
GENERATED_KEY = fleet_private_key()
GENERATED_FINGERPRINT = normalize_admin_private_key(GENERATED_KEY).fingerprint
ROOT_PASSWORD = "reinstall-root-password"  # noqa: S105 — fixture value, not a credential
ACCESS = "provision_access.yml"
LOGIN = TARGET_READINESS_LOGIN_PLAYBOOK
SOFTWARE = "provision_software.yml"
PROOF_OUTPUT = (
    '"qa_identity_proof": "qa-identity-proof: qa-observer login=ok '
    f'qa_target_version={QA_TARGET_PROFILE_VERSION}"'
)


class Plays:
    """An Ansible runner that answers per playbook and records every run."""

    def __init__(self, **answers) -> None:
        self.answers = {
            ACCESS: (True, "PLAY RECAP ok=6"),
            LOGIN: (True, "PLAY RECAP ok=1"),
            SOFTWARE: (True, PROOF_OUTPUT),
            **answers,
        }
        self.runner = MagicMock()
        self.runner.run_playbook.side_effect = lambda **call: self.answers[call["playbook_name"]]

    @property
    def calls(self) -> list[dict]:
        return [call.kwargs for call in self.runner.run_playbook.call_args_list]

    @property
    def names(self) -> list[str]:
        return [call["playbook_name"] for call in self.calls]

    def call(self, playbook: str) -> dict:
        return next(call for call in self.calls if call["playbook_name"] == playbook)


def _manager(private_key: str | None = GENERATED_KEY) -> MagicMock:
    manager = MagicMock()
    manager.get_private_key.return_value = private_key
    manager.get_public_key.return_value = "ssh-ed25519 AAAA generated"
    return manager


@pytest.fixture
def node_env(monkeypatch):
    env = SimpleNamespace(
        success=AsyncMock(return_value={"provisioning_result": {"status": "success"}}),
        status=AsyncMock(),
        incident=AsyncMock(),
    )
    monkeypatch.setattr("src.provisioner.node.handle_provisioning_success", env.success)
    monkeypatch.setattr("src.provisioner.node.update_server_labels", AsyncMock())
    monkeypatch.setattr("src.provisioner.node.update_server_status", env.status)
    monkeypatch.setattr("src.provisioner.node.create_incident", env.incident)
    return env


async def _existing_access(plays: Plays, *, manager=None, **bootstrap) -> dict:
    node = ProvisionerNode(ssh_manager=manager or _manager(), ansible_runner=plays.runner)
    return await node._run_existing_access_path(
        "vps-9", "203.0.113.9", "root", 1, "episode-1", False, {"errors": []}, **bootstrap
    )


class TestTheExistingAccessRoute:
    @pytest.mark.parametrize(
        "bootstrap",
        [{"ssh_user": "root", "ssh_private_key": BOOTSTRAP_KEY}, {}],
        ids=["bitlaunch_creation_key", "existing_host_access"],
    )
    async def test_the_bootstrap_credential_runs_only_the_access_play(self, node_env, bootstrap):
        plays = Plays()

        await _existing_access(plays, **bootstrap)

        assert plays.names == [ACCESS, LOGIN, SOFTWARE]
        assert plays.call(ACCESS).get("ssh_private_key") == bootstrap.get("ssh_private_key")
        for later in plays.calls[1:]:
            assert later["ssh_private_key"] == GENERATED_KEY
            assert later["ssh_user"] == "root"
        assert BOOTSTRAP_KEY not in [call.get("ssh_private_key") for call in plays.calls[1:]]

    async def test_the_proof_names_the_generated_key_that_logged_in(self, node_env):
        plays = Plays()

        await _existing_access(plays, ssh_user="root", ssh_private_key=BOOTSTRAP_KEY)

        proof = node_env.success.await_args.kwargs["qa_target_proof"]
        assert proof.profile_version == QA_TARGET_PROFILE_VERSION
        assert proof.ssh_key_fingerprint == GENERATED_FINGERPRINT
        assert proof.ssh_user == "root"

    async def test_access_succeeds_but_the_generated_key_cannot_log_in(self, node_env):
        """The regression: the creation key works and the installed key is refused."""
        plays = Plays(**{LOGIN: (False, "UNREACHABLE! Permission denied (publickey).")})

        result = await _existing_access(plays, ssh_user="root", ssh_private_key=BOOTSTRAP_KEY)

        assert plays.names == [ACCESS, LOGIN]
        node_env.success.assert_not_awaited()
        assert result["provisioning_result"]["status"] == "failed"
        node_env.status.assert_awaited_once_with("vps-9", "error")
        handle, incident_type, details = node_env.incident.await_args.args
        assert (handle, incident_type) == ("vps-9", IncidentType.PROVISIONING_FAILED)
        assert details["step"] == "credential_cutover"
        assert details["reason"] == "admin_login"
        assert "Permission denied" in details["detail"]

    async def test_an_unusable_generated_key_is_never_tried(self, node_env):
        plays = Plays()

        await _existing_access(
            plays, manager=_manager("PRIVATE-KEY"), ssh_user="root", ssh_private_key=BOOTSTRAP_KEY
        )

        assert plays.names == [ACCESS]
        node_env.success.assert_not_awaited()
        assert node_env.incident.await_args.args[2]["reason"] == "ssh_private_key_invalid"

    async def test_a_proof_that_fails_after_cutover_never_reaches_success(self, node_env):
        plays = Plays(**{SOFTWARE: (False, "qa-identity-proof: refused")})

        await _existing_access(plays, ssh_user="root", ssh_private_key=BOOTSTRAP_KEY)

        assert plays.names == [ACCESS, LOGIN, SOFTWARE]
        node_env.success.assert_not_awaited()


@pytest.fixture
def reinstall_env(monkeypatch):
    monkeypatch.setenv("PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS", "9")
    monkeypatch.setattr(operations, "notify_admins_best_effort", AsyncMock())
    monkeypatch.setattr(operations, "update_server_labels", AsyncMock())
    monkeypatch.setattr(operations, "asyncio", SimpleNamespace(sleep=AsyncMock()))


def _time4vps() -> MagicMock:
    client = MagicMock()
    client.get_server_details = AsyncMock(return_value=SimpleNamespace(ip="203.0.113.9"))
    client.reinstall_server = AsyncMock(return_value=41)
    client.wait_for_task = AsyncMock(return_value=SimpleNamespace(results="Password: x"))
    client.extract_password = MagicMock(return_value=ROOT_PASSWORD)
    return client


async def _reinstall(plays: Plays, manager=None):
    return await reinstall_and_provision(
        time4vps_client=_time4vps(),
        server_handle="vps-9",
        provider="time4vps",
        is_managed=True,
        server_id=9,
        server_ip="203.0.113.9",
        os_template="ubuntu",
        ssh_manager=manager or _manager(),
        ansible_runner=plays.runner,
        ssh_public_key="ssh-ed25519 AAAA generated",
        deploy_user="root",
    )


class TestTheReinstallRoute:
    async def test_the_root_password_runs_only_the_access_play(self, reinstall_env):
        plays = Plays()

        outcome = await _reinstall(plays)

        assert outcome.success is True, outcome.message
        assert plays.names == [ACCESS, LOGIN, SOFTWARE]
        assert plays.call(ACCESS)["root_password"] == ROOT_PASSWORD
        for later in plays.calls[1:]:
            assert not later.get("root_password")
            assert later["ssh_private_key"] == GENERATED_KEY
            assert later["ssh_user"] == "root"
        assert outcome.qa_target_proof.ssh_key_fingerprint == GENERATED_FINGERPRINT
        assert outcome.qa_target_proof.ssh_user == "root"

    async def test_a_generated_key_that_cannot_log_in_fails_the_reinstall(self, reinstall_env):
        plays = Plays(**{LOGIN: (False, "UNREACHABLE! Permission denied (publickey).")})

        outcome = await _reinstall(plays)

        assert outcome.success is False
        assert "admin_login" in outcome.message
        assert outcome.qa_target_proof is None
        assert plays.names == [ACCESS, LOGIN]

    async def test_a_failed_reinstall_cutover_never_invokes_the_success_finalizer(
        self, monkeypatch
    ):
        node = ProvisionerNode(ssh_manager=_manager(), ansible_runner=MagicMock())
        success = AsyncMock()
        monkeypatch.setattr("src.provisioner.node.handle_provisioning_success", success)
        monkeypatch.setattr("src.provisioner.node.update_server_status", AsyncMock())
        monkeypatch.setattr("src.provisioner.node.create_incident", AsyncMock())
        monkeypatch.setattr("src.provisioner.node.notify_admins_best_effort", AsyncMock())
        monkeypatch.setattr(
            "src.provisioner.node.reinstall_and_provision",
            AsyncMock(
                return_value=operations.ReinstallOutcome(
                    False, "Credential cutover failed (admin_login): refused"
                )
            ),
        )

        result = await node._run_reinstall_path(
            time4vps_client=SimpleNamespace(),
            server_handle="vps-9",
            provider="time4vps",
            server_id=9,
            server_ip="203.0.113.9",
            deploy_user="root",
            os_template="ubuntu",
            provisioning_attempts=1,
            provisioning_episode_id="episode-1",
            is_recovery=False,
            state={"errors": []},
        )

        success.assert_not_awaited()
        assert result["provisioning_result"]["status"] == "failed"
