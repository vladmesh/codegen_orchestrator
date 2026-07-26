"""Tests for AnsibleRunner passing orchestrator_ip to playbooks."""

import os
import stat
from unittest.mock import MagicMock, patch

# Set required env vars before importing modules that validate at import time
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.provisioner.ansible_runner import AnsibleRunner  # noqa: E402


class TestAnsibleRunnerOrchestratorIp:
    """Verify orchestrator_ip is passed as extra var when provided."""

    def setup_method(self):
        self.runner = AnsibleRunner()

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_orchestrator_ip_in_extra_vars(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            orchestrator_ip="5.6.7.8",
        )

        cmd = mock_run.call_args[0][0]
        extra_vars_idx = cmd.index("--extra-vars")
        extra_vars = cmd[extra_vars_idx + 1]
        assert "orchestrator_ip=5.6.7.8" in extra_vars

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_no_orchestrator_ip_when_not_provided(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
        )

        cmd = mock_run.call_args[0][0]
        extra_vars_idx = cmd.index("--extra-vars")
        extra_vars = cmd[extra_vars_idx + 1]
        assert "orchestrator_ip" not in extra_vars

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_orchestrator_hostname_in_extra_vars(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            orchestrator_hostname="orch.example.com",
        )

        cmd = mock_run.call_args[0][0]
        extra_vars_idx = cmd.index("--extra-vars")
        extra_vars = cmd[extra_vars_idx + 1]
        assert "orchestrator_hostname=orch.example.com" in extra_vars

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_no_orchestrator_hostname_when_not_provided(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
        )

        cmd = mock_run.call_args[0][0]
        extra_vars_idx = cmd.index("--extra-vars")
        extra_vars = cmd[extra_vars_idx + 1]
        assert "orchestrator_hostname" not in extra_vars

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_deploy_user_in_extra_vars(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        self.runner.run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            deploy_user="dev",
        )

        cmd = mock_run.call_args[0][0]
        extra_vars_idx = cmd.index("--extra-vars")
        extra_vars = cmd[extra_vars_idx + 1]
        assert "deploy_user=dev" in extra_vars

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_tags_are_passed_to_ansible(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

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
            ssh_private_key="private-key-material",
        )

        assert "ansible_user=dev" in captured["inventory"]
        assert "ansible_user=root" not in captured["inventory"]
        assert captured["key"] == "private-key-material"
        assert captured["key_mode"] == 0o600
        assert not os.path.exists(captured["key_path"])

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
