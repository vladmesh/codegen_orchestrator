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

    def test_runner_executes_an_include_role_using_repository_config(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ansible_playbook = bin_dir / "ansible-playbook"
        ansible_playbook.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "from ansible.cli.playbook import PlaybookCLI\n"
            "sys.exit(PlaybookCLI(sys.argv).run())\n"
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
        probe_playbook = playbooks_dir / "role_resolution_probe.yml"
        probe_playbook.write_text(
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
        monkeypatch.delenv("ANSIBLE_CONFIG", raising=False)
        monkeypatch.delenv("ANSIBLE_ROLES_PATH", raising=False)
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(playbooks_dir),
        )

        success, output = AnsibleRunner().run_playbook(
            server_ip="localhost ansible_connection=local",
            server_handle="vps-test",
            playbook_name=probe_playbook.name,
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


class TestWhatASuccessfulPlaybookLeavesBehind:
    """A provisioning that succeeded used to leave no account of what ran.

    Run 33718999040 recorded a target `complete` and QA then found no
    `authorized_keys` for the account that row said was there. The one question
    the artifact could not answer was whether the `qa_identity` role had run at
    all, because on success the runner logged the first 1000 characters of the
    output at debug and kept nothing else — no recap, and none of the closing
    report the play writes about the identity the host lends.
    """

    OUTPUT = (
        "TASK [Create the QA run identity] ***\n"
        + "filler line\n" * 400
        + 'ok: [1.2.3.4] => {"qa_identity_proof": "qa-identity-proof: qa-observer login=ok"}\n'
        "\nPLAY RECAP ***\n1.2.3.4 : ok=57 changed=12 unreachable=0 failed=0 skipped=1\n"
    )

    @staticmethod
    def _recap(capsys) -> str:
        """The one log line this class is about, as the service log tail sees it.

        The provisioner logs through structlog to the service stream, which is
        what the stand collects and redacts; reading it back the same way is
        what makes this a test of the evidence rather than of a call.
        """
        printed = capsys.readouterr().out
        [line] = [line for line in printed.splitlines() if "ansible_play_recap" in line]
        return line

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_a_successful_play_keeps_its_recap_and_its_closing_report(
        self, mock_run, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=self.OUTPUT, stderr="")

        success, _ = AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
        )

        assert success is True
        recap = self._recap(capsys)

        assert "PLAY RECAP" in recap
        assert "failed=0" in recap
        # The tail is bounded and is where the play states the identity this
        # host lends, which is what makes "did the role run" readable.
        assert "qa-identity-proof: qa-observer login=ok" in recap
        assert "TASK [Create the QA run identity]" not in recap

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_a_play_that_produced_no_recap_says_so_rather_than_nothing(
        self, mock_run, monkeypatch, capsys
    ):
        """An absent recap is a named absence, never a silently empty field."""
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )
        mock_run.return_value = MagicMock(returncode=1, stdout="it never started", stderr="boom")

        AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
        )

        assert "no PLAY RECAP" in self._recap(capsys)

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_the_private_key_never_reaches_the_recap(self, mock_run, monkeypatch, capsys):
        """The recap is a log line like any other: it leaves through the redaction."""
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="PLAY RECAP ***\nkey was -----BEGIN KEY----- here\n",
            stderr="",
        )

        AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            ssh_user="root",
            ssh_private_key="-----BEGIN KEY-----",
        )
        recap = self._recap(capsys)

        assert "-----BEGIN KEY-----" not in recap
        assert "REDACTED SSH PRIVATE KEY" in recap
