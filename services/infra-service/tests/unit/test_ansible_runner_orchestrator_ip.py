"""Tests for AnsibleRunner passing orchestrator_ip to playbooks."""

import os
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
