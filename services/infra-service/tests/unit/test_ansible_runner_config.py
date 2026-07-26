"""Regression coverage for the Ansible configuration used by the provisioner."""

import configparser
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

# Set required env vars before importing modules that validate at import time.
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.provisioner.ansible_runner import AnsibleRunner  # noqa: E402

ANSIBLE_DIR = Path(__file__).parents[2] / "ansible"


class TestAnsibleRunnerConfiguration:
    """The runner must load the configuration that makes playbook roles available."""

    def test_runner_executes_an_include_role_using_its_config(self, tmp_path, monkeypatch):
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
