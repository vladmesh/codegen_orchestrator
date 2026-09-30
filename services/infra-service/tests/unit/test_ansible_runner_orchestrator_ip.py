"""Tests for AnsibleRunner passing orchestrator_ip to playbooks."""

import json
import os
from pathlib import Path
import stat
from unittest.mock import MagicMock, patch

import pytest

# Set required env vars before importing modules that validate at import time
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from src.provisioner.ansible_runner import AnsibleRunner  # noqa: E402

ANSIBLE_PLAYBOOKS = Path(__file__).parents[2] / "ansible" / "playbooks"


def _capture_vars(mock_run):
    captured = {}

    def execute(cmd, **kwargs):
        vars_path = cmd[cmd.index("--extra-vars") + 1]
        assert vars_path.startswith("@")
        captured.update(json.loads(Path(vars_path[1:]).read_text()))
        return MagicMock(returncode=0, stdout="ok", stderr="")

    mock_run.side_effect = execute
    return captured


@pytest.fixture(autouse=True)
def use_repository_ansible_paths(monkeypatch):
    """Use the bundled Ansible directory instead of the container path in unit tests."""
    monkeypatch.setattr(
        "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS", str(ANSIBLE_PLAYBOOKS)
    )


class TestAnsibleRunnerOrchestratorIp:
    """Verify orchestrator_ip is passed as extra var when provided."""

    def setup_method(self):
        self.runner = AnsibleRunner()

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_orchestrator_ip_in_extra_vars(self, mock_run):
        extra_vars = _capture_vars(mock_run)

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            orchestrator_ip="5.6.7.8",
        )

        assert extra_vars["orchestrator_ip"] == "5.6.7.8"

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_no_orchestrator_ip_when_not_provided(self, mock_run):
        extra_vars = _capture_vars(mock_run)

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
        )

        assert "orchestrator_ip" not in extra_vars

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_orchestrator_hostname_in_extra_vars(self, mock_run):
        extra_vars = _capture_vars(mock_run)

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            orchestrator_hostname="orch.example.com",
        )

        assert extra_vars["orchestrator_hostname"] == "orch.example.com"

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_no_orchestrator_hostname_when_not_provided(self, mock_run):
        extra_vars = _capture_vars(mock_run)

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
        )

        assert "orchestrator_hostname" not in extra_vars

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_deploy_user_in_extra_vars(self, mock_run):
        extra_vars = _capture_vars(mock_run)

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            deploy_user="dev",
        )

        assert extra_vars["deploy_user"] == "dev"

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_tags_are_passed_to_ansible(self, mock_run):
        _capture_vars(mock_run)

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            tags=["monitoring"],
        )

        cmd = mock_run.call_args[0][0]
        tags_idx = cmd.index("--tags")
        assert cmd[tags_idx + 1] == "monitoring"

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_key_authentication_uses_server_user_and_ephemeral_private_key(self, mock_run):
        captured = {}

        def capture_inventory(cmd, **kwargs):
            inventory_path = cmd[cmd.index("-i") + 1]
            with open(inventory_path) as inventory_file:
                captured["inventory"] = inventory_file.read()
            key_path = captured["inventory"].split("ansible_ssh_private_key_file=")[1].split()[0]
            captured["key_path"] = key_path
            captured["key"] = open(key_path).read()
            captured["key_mode"] = stat.S_IMODE(os.stat(key_path).st_mode)
            return MagicMock(returncode=0, stdout="ok", stderr="")

        mock_run.side_effect = capture_inventory

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            ssh_user="dev",
            ssh_private_key="private-key-material\n",
        )

        assert "ansible_user=dev" in captured["inventory"]
        assert "ansible_user=root" not in captured["inventory"]
        assert captured["key"] == "private-key-material\n"
        assert captured["key_mode"] == 0o600
        assert not os.path.exists(captured["key_path"])

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_key_authentication_materializes_missing_terminal_newline(self, mock_run):
        captured = {}

        def capture_key(cmd, **kwargs):
            inventory_path = cmd[cmd.index("-i") + 1]
            inventory = Path(inventory_path).read_text()
            key_path = inventory.split("ansible_ssh_private_key_file=")[1].split()[0]
            captured["key"] = Path(key_path).read_text()
            return MagicMock(returncode=0, stdout="ok", stderr="")

        mock_run.side_effect = capture_key

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            ssh_user="dev",
            ssh_private_key="private-key-material",
        )

        assert captured["key"] == "private-key-material\n"

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_password_authentication_remains_root_without_private_key(self, mock_run):
        captured = {}

        def capture_inventory(cmd, **kwargs):
            inventory_path = cmd[cmd.index("-i") + 1]
            with open(inventory_path) as inventory_file:
                captured["inventory"] = inventory_file.read()
            return MagicMock(returncode=0, stdout="ok", stderr="")

        mock_run.side_effect = capture_inventory

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_access.yml",
            root_password="root-password",  # noqa: S106
            ssh_user="dev",
            ssh_private_key="private-key-material",
        )

        assert "ansible_user=root" in captured["inventory"]
        assert "ansible_ssh_pass=root-password" in captured["inventory"]
        assert "ansible_ssh_private_key_file" not in captured["inventory"]

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_key_material_is_redacted_from_ansible_failure_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="failed with private-key-material",
            stderr="private-key-material",
        )

        success, output = self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            ssh_user="dev",
            ssh_private_key="private-key-material",
        )

        assert success is False
        assert "private-key-material" not in output
        assert "[REDACTED SSH PRIVATE KEY]" in output
