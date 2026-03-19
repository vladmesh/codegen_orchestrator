"""Tests for the qa_runner Ansible role structure and YAML validity."""

from pathlib import Path

import yaml

ROLES_DIR = Path(__file__).parents[2] / "ansible" / "roles"
QA_RUNNER_DIR = ROLES_DIR / "qa_runner"


class TestQaRunnerRoleStructure:
    """Verify qa_runner role directory structure exists."""

    def test_tasks_main_exists(self):
        assert (QA_RUNNER_DIR / "tasks" / "main.yml").is_file()

    def test_defaults_main_exists(self):
        assert (QA_RUNNER_DIR / "defaults" / "main.yml").is_file()


class TestQaRunnerTasksYaml:
    """Verify tasks/main.yml is valid and has required tasks."""

    def setup_method(self):
        with open(QA_RUNNER_DIR / "tasks" / "main.yml") as f:
            self.tasks = yaml.safe_load(f)

    def test_is_valid_yaml_list(self):
        assert isinstance(self.tasks, list)
        assert len(self.tasks) > 0

    def test_all_tasks_have_name(self):
        for task in self.tasks:
            assert "name" in task, f"Task missing 'name': {task}"

    def test_creates_qa_runner_directory(self):
        names = [t["name"] for t in self.tasks]
        assert any("qa" in n.lower() and "dir" in n.lower() for n in names), (
            f"No task creates QA runner directory. Task names: {names}"
        )

    def test_installs_claude_code_cli(self):
        task_names = " ".join(t["name"].lower() for t in self.tasks)
        assert "claude" in task_names, (
            f"No Claude Code CLI installation task found. Tasks: {task_names}"
        )

    def test_installs_python_packages(self):
        task_names = " ".join(t["name"].lower() for t in self.tasks)
        assert "python" in task_names or "pip" in task_names, (
            f"No Python packages installation task found. Tasks: {task_names}"
        )

    def test_copies_claude_credentials(self):
        task_names = " ".join(t["name"].lower() for t in self.tasks)
        assert "claude" in task_names and "credentials" in task_names, (
            f"No Claude credentials copy task found. Tasks: {task_names}"
        )

    def test_all_tasks_are_idempotent(self):
        """Check that tasks use idempotent modules (apt, npm, pip, file, copy, template)."""
        idempotent_modules = {
            "file",
            "apt",
            "apt_key",
            "apt_repository",
            "npm",
            "pip",
            "copy",
            "template",
            "lineinfile",
            "get_url",
            "shell",
            "command",
            "ansible.builtin.shell",
            "ansible.builtin.command",
            "ansible.builtin.apt_key",
            "ansible.builtin.apt_repository",
        }
        for task in self.tasks:
            # Get the module used (first key that isn't name/when/become/etc)
            meta_keys = {
                "name",
                "when",
                "become",
                "become_user",
                "tags",
                "register",
                "changed_when",
                "failed_when",
                "notify",
                "no_log",
                "ignore_errors",
                "environment",
                "args",
                "block",
                "rescue",
                "always",
            }
            module_keys = set(task.keys()) - meta_keys
            for key in module_keys:
                assert key in idempotent_modules, (
                    f"Task '{task['name']}' uses potentially non-idempotent module: {key}"
                )


class TestQaRunnerDefaults:
    """Verify defaults/main.yml has expected variables."""

    def setup_method(self):
        with open(QA_RUNNER_DIR / "defaults" / "main.yml") as f:
            self.defaults = yaml.safe_load(f)

    def test_is_valid_yaml_dict(self):
        assert isinstance(self.defaults, dict)

    def test_qa_runner_dir_defined(self):
        assert "qa_runner_dir" in self.defaults
        assert self.defaults["qa_runner_dir"] == "/opt/qa-runner"

    def test_no_nodejs_dependency(self):
        """Claude Code installs standalone, no Node.js needed."""
        assert "nodejs_major_version" not in self.defaults


class TestSiteYmlIncludesQaRunner:
    """Verify site.yml includes the qa_runner role."""

    def setup_method(self):
        site_path = Path(__file__).parents[2] / "ansible" / "playbooks" / "site.yml"
        with open(site_path) as f:
            self.playbooks = yaml.safe_load(f)

    def test_qa_runner_role_present(self):
        roles = self.playbooks[0].get("roles", [])
        role_names = [r["role"] if isinstance(r, dict) else r for r in roles]
        assert "qa_runner" in role_names, f"qa_runner not in site.yml roles: {role_names}"
