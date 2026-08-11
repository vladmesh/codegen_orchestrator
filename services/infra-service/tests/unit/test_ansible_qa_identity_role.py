"""The account provisioning creates for QA runs, and the ceiling it lives under.

Two questions are asked here, and only the second one can be asked of a file:

* what the role declares — that the account exists, that it is in no secondary
  group, that its sudo rule is one command, that it gets a read into the
  deployment tree and no write. These are read out of the YAML, because "not in
  the docker group" is a property of what the role says, and a role that stopped
  saying it would be the regression.
* what the target actually refuses. The docker wrapper is a real script, so it is
  run as one: `exec`, `run`, `cp` and friends have to be refused by the script
  itself, before docker is reached, with no help from the orchestrator that calls
  it. That is the half of the boundary the QA runtime cannot be trusted to keep,
  because the whole point is that it holds when the caller misbehaves.
"""

from pathlib import Path
import subprocess

import pytest
import yaml

from shared.qa_identity import QA_SSH_USER

ANSIBLE_DIR = Path(__file__).parents[2] / "ansible"
ROLE = ANSIBLE_DIR / "roles" / "qa_identity"
ROLE_TASKS = ROLE / "tasks" / "main.yml"
ROLE_DEFAULTS = ROLE / "defaults" / "main.yml"
WRAPPER = ROLE / "files" / "qa-docker"
SOFTWARE_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "provision_software.yml"
RETROFIT_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "qa_identity_retrofit.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(ROLE_TASKS.read_text())


def _task_named(tasks: list[dict], name: str) -> dict:
    return next(task for task in tasks if task["name"] == name)


def _defaults() -> dict:
    return yaml.safe_load(ROLE_DEFAULTS.read_text())


class TestTheAccountIsCreatedByProvisioning:
    def test_the_role_creates_the_account_the_runtime_looks_for(self):
        """The role's name for the account and the runtime's must be one name."""
        assert _defaults()["qa_ssh_user"] == QA_SSH_USER

        user = _task_named(_tasks(), "Create the QA observation account")["ansible.builtin.user"]
        assert user["name"] == "{{ qa_ssh_user }}"
        assert user["create_home"] is True

    def test_the_software_phase_is_what_creates_it(self):
        """`provisioning_phase=complete` is written when this playbook succeeds.

        So the role has to be inside it: that is what makes "complete, but with no
        account for QA to borrow" a state the provisioner cannot produce.
        """
        playbook = yaml.safe_load(SOFTWARE_PLAYBOOK.read_text())
        include = _task_named(playbook[0]["tasks"], "Create the QA run identity")
        assert include["ansible.builtin.include_role"]["name"] == "qa_identity"

    def test_the_retrofit_creates_the_same_account_from_the_same_role(self):
        """An old host and a fresh one get one identity, not two that look alike."""
        playbook = yaml.safe_load(RETROFIT_PLAYBOOK.read_text())
        include = _task_named(playbook[0]["tasks"], "Create the QA run identity")
        assert include["ansible.builtin.include_role"]["name"] == "qa_identity"

    def test_authorized_keys_is_opened_with_a_line_that_is_never_a_key(self):
        """The runtime appends and removes; it never creates this file.

        The sentinel is also what keeps the revoke's "an empty filter result is a
        failure" rule true for a file whose only other lines are run keys.
        """
        keys = _task_named(
            _tasks(), "Open the QA account's authorized_keys with a line that is never a key"
        )["ansible.builtin.lineinfile"]
        assert keys["path"] == "{{ qa_ssh_home }}/.ssh/authorized_keys"
        assert keys["owner"] == "{{ qa_ssh_user }}"
        assert keys["mode"] == "0600"
        assert keys["create"] is True
        assert _defaults()["qa_authorized_keys_sentinel"].strip().startswith("#")

    def test_an_identity_that_is_root_or_the_deploy_user_fails_provisioning(self):
        assertion = _task_named(
            _tasks(), "Refuse a QA identity that would not be its own unprivileged account"
        )["ansible.builtin.assert"]
        assert "qa_ssh_user != 'root'" in assertion["that"]
        assert "qa_ssh_user != deploy_user" in assertion["that"]


class TestTheAccountCannotBecomeRoot:
    def test_it_is_in_no_secondary_group_at_all(self):
        """Membership of `docker` is root on the host, so there is no group list."""
        user = _task_named(_tasks(), "Create the QA observation account")["ansible.builtin.user"]
        assert user["groups"] == []
        # Not appending is what repairs a host whose account was widened by hand.
        assert user["append"] is False

    def test_no_task_in_the_role_grants_docker_group_or_socket_access(self):
        role_text = ROLE_TASKS.read_text()
        assert "docker.sock" not in role_text
        assert "groups: docker" not in role_text

    def test_sudo_is_one_command_and_that_command_is_the_wrapper(self):
        sudoers = _task_named(_tasks(), "Allow the QA account that one command and nothing else")[
            "ansible.builtin.copy"
        ]
        rules = [
            line
            for line in sudoers["content"].splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        assert "{{ qa_ssh_user }} ALL=(root) NOPASSWD: {{ qa_docker_wrapper }}" in rules
        # One command spec, and it is the wrapper. Anything wider than this is
        # the account being able to become root.
        assert [rule for rule in rules if not rule.startswith("Defaults:")] == [
            "{{ qa_ssh_user }} ALL=(root) NOPASSWD: {{ qa_docker_wrapper }}"
        ]
        # A broken sudoers file is a host nobody can administer, so it is checked
        # before it is installed.
        assert sudoers["validate"] == "visudo -cf %s"
        assert sudoers["mode"] == "0440"
        assert sudoers["owner"] == "root"

    def test_the_wrapper_belongs_to_root(self):
        """An account that can rewrite the wrapper is an account with root."""
        wrapper = _task_named(_tasks(), "Install the read-only docker wrapper")[
            "ansible.builtin.copy"
        ]
        assert wrapper["owner"] == "root"
        assert wrapper["group"] == "root"
        assert wrapper["mode"] == "0755"

    def test_the_deployment_tree_is_readable_and_not_writable(self):
        """A read is granted by name, not by joining the group that can write."""
        acl = _task_named(
            _tasks(), "Let the QA account into the deployment tree without giving it a write"
        )["ansible.posix.acl"]
        assert acl["path"] == "{{ services_root }}"
        assert acl["entity"] == "{{ qa_ssh_user }}"
        assert acl["permissions"] == "rx"
        assert "w" not in acl["permissions"]


class TestTheTargetRefusesWhatWrites:
    """The wrapper, run as the target runs it, against a docker that records."""

    @pytest.fixture
    def docker(self, tmp_path):
        """A stand-in docker on PATH that records the argv it was reached with."""
        log = tmp_path / "docker.log"
        stub = tmp_path / "docker"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\n')
        stub.chmod(0o755)
        return log

    def _wrapper(self, docker_log: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(WRAPPER), *args],
            capture_output=True,
            text=True,
            env={"PATH": f"{docker_log.parent}:/usr/bin:/bin"},
        )

    @pytest.mark.parametrize(
        "argv",
        [
            ["exec", "weather-bot-backend-1", "sh"],
            ["run", "-v", "/:/host", "alpine", "sh"],
            ["cp", "/etc/shadow", "weather-bot-backend-1:/tmp/x"],
            ["compose", "restart"],
            ["build", "-t", "x", "."],
            ["commit", "weather-bot-backend-1"],
            ["save", "alpine"],
            ["rm", "-f", "weather-bot-backend-1"],
            ["network", "create", "escape"],
            ["--host", "tcp://127.0.0.1:2375", "logs", "weather-bot-backend-1"],
        ],
    )
    def test_a_sub_command_that_writes_or_escapes_never_reaches_docker(self, docker, argv):
        result = self._wrapper(docker, *argv)

        assert result.returncode != 0
        assert "refused" in result.stderr
        assert not docker.exists(), f"docker was reached with {argv}"

    def test_a_call_with_no_sub_command_is_refused(self, docker):
        result = self._wrapper(docker)

        assert result.returncode != 0
        assert not docker.exists()

    @pytest.mark.parametrize(
        "argv",
        [
            ["logs", "--tail", "200", "weather-bot-backend-1"],
            ["inspect", "--format", "{{json .State}}", "weather-bot-backend-1"],
            ["diff", "weather-bot-backend-1"],
            ["port", "weather-bot-backend-1"],
            ["top", "weather-bot-backend-1"],
            ["stats", "--no-stream", "weather-bot-backend-1"],
            # The run's capability set is resolved with this one, so it has to
            # pass here; which containers the answer may contain is decided in
            # the orchestrator, not on the host.
            ["ps", "--all", "--filter", "label=com.docker.compose.project=weather-bot"],
        ],
    )
    def test_a_read_reaches_docker_unchanged(self, docker, argv):
        result = self._wrapper(docker, *argv)

        assert result.returncode == 0, result.stderr
        assert docker.read_text().strip() == " ".join(argv)

    def test_the_wrapper_allows_exactly_the_reads_the_runtime_can_ask_for(self):
        """The runtime's container-scoped set must be a subset of the target's."""
        allowed = next(
            line for line in WRAPPER.read_text().splitlines() if line.startswith("ALLOWED=")
        )
        names = set(allowed.split('"')[1].split())
        assert {"diff", "inspect", "logs", "port", "stats", "top"} <= names
        assert "ps" in names, "capability resolution asks docker which containers this project has"
