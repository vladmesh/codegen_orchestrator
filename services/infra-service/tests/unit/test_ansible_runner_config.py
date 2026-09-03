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

from src.provisioner.ansible_runner import (  # noqa: E402
    QA_IDENTITY_MARKER,
    AnsibleRunner,
)

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

    # What run 33729987635 actually produced: the role's report, then the roles
    # after it printing more than the tail keeps. The proof is in the output and
    # not in its last 3000 characters.
    BURIED = (
        "TASK [Report the QA identity this host lends] ***\n"
        'ok: [1.2.3.4] => {"msg": {"qa_identity_proof": '
        '"qa-identity-proof: qa-observer login=ok uid=1001 sudo=wrapper-only"}}\n'
        + "TASK [monitoring : something noisy] ***\nchanged: [1.2.3.4]\n" * 300
        + "\nPLAY RECAP ***\n1.2.3.4 : ok=61 changed=12 unreachable=0 failed=0 skipped=2\n"
    )

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_the_qa_identity_report_survives_a_tail_it_no_longer_fits_in(
        self, mock_run, monkeypatch, capsys
    ):
        """The closing line is cut out by name, not left to the tail's luck.

        `codegen-orchestrator-1254` exists to make the target state this line,
        and the artifact of the paid run that produced it still could not show
        the text, because the roles that run after the report print more than
        the tail keeps. Evidence that only survives when the output is short
        enough is not evidence.
        """
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=self.BURIED, stderr="")

        AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
        )

        recap = self._recap(capsys)

        assert "qa-identity-proof: qa-observer login=ok" in recap
        assert "failed=0" in recap
        # And the field is its own, so the tail is still just the tail.
        assert "qa_identity=" in recap

    # The retrofit play repairs a pre-existing host with the same role and the
    # same proof, and reports it under the same key — which is the whole reason
    # one marker can find either. It named the key `identity_proof` until this
    # was noticed, so a retrofit that proved a seat read as a play that proved
    # none: a false negative in exactly the evidence this field exists for.
    RETROFIT = (
        "TASK [Report what this host changed and what it left] ***\n"
        'ok: [1.2.3.4] => {"msg": {"qa_ssh_user": "qa-observer", "qa_identity_proof": '
        '"qa-identity-proof: qa-observer login=ok uid=1001 sudo=wrapper-only", '
        '"removed_paths": [], "left_in_place": ["/swapfile"]}}\n'
        "\nPLAY RECAP ***\n1.2.3.4 : ok=24 changed=3 unreachable=0 failed=0 skipped=0\n"
    )

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_the_retrofit_play_reports_its_seat_under_the_same_key(
        self, mock_run, monkeypatch, capsys
    ):
        """One marker has to find both plays that create the seat."""
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=self.RETROFIT, stderr="")

        AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="qa_identity_retrofit.yml",
        )

        recap = self._recap(capsys)

        assert "qa-identity-proof: qa-observer login=ok" in recap
        assert "no qa_identity_proof" not in recap

    def test_both_plays_that_create_the_seat_report_it_under_the_marker(self):
        """Asked of the playbooks themselves, so a rename cannot pass silently.

        One marker finds either play only for as long as both name the key the
        same way, and nothing about a `debug:` key makes that self-evident.
        """
        for name, task_name in (
            ("provision_software.yml", "Report the QA identity this host lends"),
            ("qa_identity_retrofit.yml", "Report what this host changed and what it left"),
        ):
            play = yaml.safe_load((ANSIBLE_DIR / "playbooks" / name).read_text())[0]
            [task] = [t for t in play["tasks"] if t.get("name") == task_name]

            assert QA_IDENTITY_MARKER in task["ansible.builtin.debug"]["msg"], name

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_a_play_that_never_reported_an_identity_says_so_rather_than_nothing(
        self, mock_run, monkeypatch, capsys
    ):
        """A named absence, like the recap's: the field is never silently empty."""
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )
        mock_run.return_value = MagicMock(
            returncode=0, stdout="PLAY RECAP ***\n1.2.3.4 : ok=1 failed=0\n", stderr=""
        )

        AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_access.yml",
        )

        assert "no qa_identity_proof" in self._recap(capsys)

    @patch("src.provisioner.ansible_runner.subprocess.run")
    def test_the_private_key_is_redacted_out_of_the_identity_report_too(
        self, mock_run, monkeypatch, capsys
    ):
        """Every path out of this function goes through the same redaction."""
        monkeypatch.setattr(
            "src.provisioner.ansible_runner.Paths.ANSIBLE_PLAYBOOKS",
            str(ANSIBLE_DIR / "playbooks"),
        )
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f'ok: [1.2.3.4] => {{"qa_identity_proof": "took the seat with {key}"}}\n',
            stderr="",
        )

        AnsibleRunner().run_playbook(
            server_ip="1.2.3.4",
            server_handle="vps-test",
            playbook_name="provision_software.yml",
            ssh_user="root",
            ssh_private_key=key,
        )

        # Read whole: a redaction test must look at every byte the service
        # stream received, not at one line of it.
        printed = capsys.readouterr().out

        assert "BEGIN OPENSSH PRIVATE KEY" not in printed
        assert "REDACTED SSH PRIVATE KEY" in printed
        assert "qa_identity=" in printed

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
