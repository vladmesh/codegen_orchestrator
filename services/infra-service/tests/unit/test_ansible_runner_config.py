"""Regression coverage for the Ansible configuration used by the provisioner."""

import configparser
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import yaml

# Set required env vars before importing modules that validate at import time.
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from src.provisioner.ansible_runner import AnsibleRunner  # noqa: E402

ANSIBLE_DIR = Path(__file__).parents[2] / "ansible"


class TestAnsibleRunnerConfiguration:
    """The runner must load the configuration that makes playbook roles available."""

    def test_runner_executes_an_include_role_using_its_config(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ansible_playbook = bin_dir / "ansible-playbook"
        ansible_playbook.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "from ansible.cli.playbook import PlaybookCLI\n"
            "PlaybookCLI(sys.argv).run()\n"
        )
        ansible_playbook.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

        ansible_dir = tmp_path / "ansible"
        playbooks_dir = ansible_dir / "playbooks"
        role_tasks_dir = ansible_dir / "roles" / "role_resolution_probe" / "tasks"
        playbooks_dir.mkdir(parents=True)
        role_tasks_dir.mkdir(parents=True)
        (ansible_dir / "ansible.cfg").write_text(
            "[defaults]\nroles_path = roles\nhost_key_checking = False\n"
        )
        (playbooks_dir / "role_resolution.yml").write_text(
            """---
- hosts: target
  gather_facts: false
  tasks:
    - ansible.builtin.include_role:
        name: role_resolution_probe
"""
        )
        (role_tasks_dir / "main.yml").write_text(
            """---
- ansible.builtin.assert:
    that:
      - true
"""
        )
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS", str(playbooks_dir)
        )

        success, output = AnsibleRunner().run_playbook(
            server_ip="localhost ansible_connection=local",
            server_handle="vps-test",
            playbook_name="role_resolution.yml",
        )

        assert success is True, output

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_runner_keeps_known_hosts_disabled_for_reinstalled_servers(self, mock_run, monkeypatch):
        captured_inventory: dict[str, str] = {}
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )

        def capture_inventory(cmd, **_kwargs):
            captured_inventory["content"] = Path(cmd[2]).read_text()
            return MagicMock(returncode=0, stdout="ok", stderr="")

        mock_run.side_effect = capture_inventory

        success, _ = AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_access.yml",
            root_password="password",  # noqa: S106
        )

        assert success is True
        assert "UserKnownHostsFile=/dev/null" in captured_inventory["content"]

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_runner_fails_before_execution_when_config_is_missing(
        self, mock_run, tmp_path, monkeypatch
    ):
        missing_playbooks_dir = tmp_path / "playbooks"
        missing_playbooks_dir.mkdir()
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS", str(missing_playbooks_dir)
        )

        success, output = AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="missing_config.yml",
        )

        assert success is False
        assert output == f"Ansible configuration not found: {tmp_path / 'ansible.cfg'}"
        mock_run.assert_not_called()

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_runner_loads_config_that_resolves_provisioning_roles(self, mock_run, monkeypatch):
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        success, _ = AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
        )

        assert success is True
        run_kwargs = mock_run.call_args.kwargs
        config_path = Path(run_kwargs["env"]["ANSIBLE_CONFIG"])
        assert config_path == ANSIBLE_DIR / "ansible.cfg"

        config = configparser.ConfigParser()
        config.read(config_path)
        assert config["defaults"].getboolean("host_key_checking") is False
        assert config["privilege_escalation"].getboolean("become") is True
        roles_root = config_path.parent / config["defaults"]["roles_path"]
        playbook_path = Path(mock_run.call_args.args[0][3])
        tasks = yaml.safe_load(playbook_path.read_text())[0]["tasks"]
        included_roles = [
            task["ansible.builtin.include_role"]["name"]
            for task in tasks
            if "ansible.builtin.include_role" in task
        ]

        assert {"deploy_target", "monitoring"}.issubset(included_roles)
        assert all((roles_root / role).is_dir() for role in included_roles)
