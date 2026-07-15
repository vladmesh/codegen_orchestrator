"""Regression tests for deploy-target bootstrap ownership."""

import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import uuid

import pytest
import yaml

ANSIBLE_DIR = Path(__file__).parents[2] / "ansible"
ROLE_TASKS = ANSIBLE_DIR / "roles" / "deploy_target" / "tasks" / "main.yml"
SOFTWARE_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "provision_software.yml"


def _task_named(tasks: list[dict], name: str) -> dict:
    return next(task for task in tasks if task["name"] == name)


class TestDeployTargetBootstrap:
    """The deployment user can create only its own project roots."""

    def test_role_creates_configured_deploy_user_and_authorizes_key(self):
        tasks = yaml.safe_load(ROLE_TASKS.read_text())

        user_task = _task_named(tasks, "Ensure configured deploy user exists")
        assert user_task["ansible.builtin.user"]["name"] == "{{ deploy_user }}"

        key_task = _task_named(tasks, "Authorize orchestrator key for deploy user")
        assert key_task["ansible.builtin.lineinfile"]["owner"] == "{{ deploy_user }}"
        assert "ssh_public_key" in key_task["ansible.builtin.lineinfile"]["line"]

    def test_empty_target_root_is_writable_but_existing_root_owned_projects_are_isolated(self):
        tasks = yaml.safe_load(ROLE_TASKS.read_text())
        root_task = _task_named(tasks, "Create isolated project root")
        root = root_task["ansible.builtin.file"]

        assert root["path"] == "{{ services_root }}"
        assert root["owner"] == "root"
        assert root["group"] == "{{ deploy_user }}"
        # Group write permits mkdir for the deploy user. The sticky bit prevents
        # that user from renaming or removing root-owned existing project roots.
        assert root["mode"] == "3770"

    def test_provisioning_path_applies_deploy_target_role(self):
        playbook = yaml.safe_load(SOFTWARE_PLAYBOOK.read_text())
        tasks = playbook[0]["tasks"]

        include = _task_named(tasks, "Prepare deploy target")
        assert include["ansible.builtin.include_role"]["name"] == "deploy_target"


class TestDeployTargetPermissions:
    """Execute the role against a disposable target with a non-root user."""

    def test_empty_target_allows_first_deploy_and_protects_existing_project(self, tmp_path):
        if os.geteuid() != 0 and shutil.which("sudo") is None:
            pytest.skip("requires root or passwordless sudo")

        deploy_user = f"deploy-target-{uuid.uuid4().hex[:8]}"
        services_root = Path(tempfile.mkdtemp(prefix="deploy-target-"))
        services_root.rmdir()
        playbook = tmp_path / "deploy_target.yml"
        playbook.write_text(
            """
---
- hosts: localhost
  connection: local
  become: true
  vars:
    deploy_user: DEPLOY_USER
    services_root: SERVICES_ROOT
    ssh_public_key: ""
  roles:
    - deploy_target
""".replace("DEPLOY_USER", deploy_user).replace("SERVICES_ROOT", str(services_root))
        )

        try:
            self._run_privileged(["mkdir", "-p", str(services_root / "personal_site")])
            self._run_privileged(["chown", "root:root", str(services_root / "personal_site")])
            self._run_privileged(["chmod", "0755", str(services_root / "personal_site")])

            self._apply_role(playbook)
            self._apply_role(playbook)

            deploy_identity = pwd.getpwnam(deploy_user)
            root_stat = services_root.stat()
            assert root_stat.st_uid == 0
            assert root_stat.st_gid == deploy_identity.pw_gid
            assert root_stat.st_mode & 0o7777 == 0o3770

            self._run_as_deploy_user(
                deploy_user, ["mkdir", "-p", str(services_root / "new-project" / "infra")]
            )
            self._run_privileged(["test", "-d", str(services_root / "new-project" / "infra")])

            assert (
                self._run_as_deploy_user(
                    deploy_user,
                    ["touch", str(services_root / "personal_site" / "blocked")],
                    check=False,
                ).returncode
                != 0
            )
            assert (
                self._run_as_deploy_user(
                    deploy_user, ["rmdir", str(services_root / "personal_site")], check=False
                ).returncode
                != 0
            )
        finally:
            self._run_privileged(["userdel", "-r", deploy_user], check=False)
            self._run_privileged(["rm", "-rf", str(services_root)], check=False)

    @staticmethod
    def _apply_role(playbook: Path) -> None:
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(playbook)],
            cwd=ANSIBLE_DIR,
            capture_output=True,
            env={**os.environ, "ANSIBLE_STDOUT_CALLBACK": "default"},
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @staticmethod
    def _run_as_deploy_user(deploy_user: str, command: list[str], check: bool = True):
        return TestDeployTargetPermissions._run_privileged(
            ["runuser", "-u", deploy_user, "--", *command], check=check
        )

    @staticmethod
    def _run_privileged(command: list[str], check: bool = True):
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        return subprocess.run([*prefix, *command], check=check, capture_output=True, text=True)
