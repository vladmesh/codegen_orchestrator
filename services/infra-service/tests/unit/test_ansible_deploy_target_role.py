"""Regression tests for deploy-target bootstrap ownership."""

from pathlib import Path

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
        assert key_task["ansible.posix.authorized_key"]["user"] == "{{ deploy_user }}"
        assert "ssh_public_key" in key_task["ansible.posix.authorized_key"]["key"]

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
