"""Tests for the qa_runner Ansible role structure and YAML validity."""

import os
from pathlib import Path
import subprocess

import yaml

ROLES_DIR = Path(__file__).parents[2] / "ansible" / "roles"
QA_RUNNER_DIR = ROLES_DIR / "qa_runner"


def _task_named(tasks: list[dict], name: str) -> dict:
    return next(task for task in tasks if task["name"] == name)


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
            "assert",
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


class TestClaudeCodeInstallsForQaUser:
    """QA SSHes in as the deploy user and runs `claude` from that user's home."""

    def setup_method(self):
        with open(QA_RUNNER_DIR / "tasks" / "main.yml") as f:
            self.tasks = yaml.safe_load(f)

    def test_install_runs_as_the_qa_user(self):
        install = _task_named(self.tasks, "Install Claude Code CLI via install script")

        assert install["become_user"] == "{{ qa_runner_user }}"
        assert install["environment"]["HOME"] == "{{ qa_runner_home }}"
        assert install["args"]["creates"] == "{{ qa_runner_claude_bin }}"

    def test_verification_executes_the_binary_the_way_qa_does(self):
        verify = _task_named(self.tasks, "Verify Claude Code CLI runs for the QA user")

        assert verify["become_user"] == "{{ qa_runner_user }}"
        assert verify["environment"]["HOME"] == "{{ qa_runner_home }}"
        # Bare `claude` resolved through PATH, matching the QA consumer's command
        assert 'export PATH="$HOME/.local/bin:$PATH"' in verify["shell"]
        assert "claude --version" in verify["shell"]

    def test_credentials_and_settings_land_in_the_qa_user_home(self):
        creds = _task_named(self.tasks, "Copy Claude Code credentials")
        settings = _task_named(self.tasks, "Configure Claude Code permissions (allowlist for QA)")

        assert creds["copy"]["dest"] == "{{ qa_runner_home }}/.claude/.credentials.json"
        assert creds["copy"]["owner"] == "{{ qa_runner_user }}"
        assert settings["copy"]["dest"] == "{{ qa_runner_home }}/.claude/settings.json"
        assert settings["copy"]["owner"] == "{{ qa_runner_user }}"

    def test_nothing_is_provisioned_under_root_home(self):
        """A /root path means the QA user gets nothing — that was the 127 failure."""
        rendered = yaml.safe_dump(self.tasks)
        assert "/root" not in rendered, rendered

    def test_write_guard_cannot_be_replaced_by_the_qa_user(self):
        guard = _task_named(self.tasks, "Install runner-owned application write guard")
        protect_dir = _task_named(self.tasks, "Prevent the QA user from replacing the write guard")

        assert guard["copy"]["owner"] == "root"
        assert guard["copy"]["group"] == "root"
        assert protect_dir["file"] == {
            "path": "{{ qa_runner_dir }}",
            "owner": "root",
            "group": "root",
            "mode": "0755",
        }


class TestTelethonCredentialsReachTheQaUser:
    """QA needs api_id, api_hash and a StringSession — all three, or nothing works."""

    def setup_method(self):
        with open(QA_RUNNER_DIR / "tasks" / "main.yml") as f:
            self.tasks = yaml.safe_load(f)
        with open(QA_RUNNER_DIR / "defaults" / "main.yml") as f:
            self.defaults = yaml.safe_load(f)

    def test_credentials_come_from_the_orchestrator_environment(self):
        assert self.defaults["telethon_api_id"] == "{{ lookup('env', 'TELETHON_API_ID') }}"
        assert self.defaults["telethon_api_hash"] == "{{ lookup('env', 'TELETHON_API_HASH') }}"
        assert self.defaults["telethon_session"] == "{{ lookup('env', 'TELETHON_SESSION') }}"

    def test_missing_credential_fails_the_role(self):
        check = _task_named(self.tasks, "Require Telethon credentials in the environment")

        assert check["assert"]["that"] == [
            "telethon_api_id | string | length > 0",
            "telethon_api_hash | string | length > 0",
            "telethon_session | string | length > 0",
        ]
        # A `when:` on an undefined variable is what made the old session copy
        # skip silently; the check must always run.
        assert "when" not in check

    def test_env_file_is_written_private_to_the_qa_user(self):
        write = _task_named(self.tasks, "Write Telethon credentials for the QA user")

        assert write["copy"]["dest"] == "{{ qa_runner_telethon_env_file }}"
        assert write["copy"]["owner"] == "{{ qa_runner_user }}"
        assert write["copy"]["mode"] == "0600"
        assert write["no_log"] is True
        content = write["copy"]["content"]
        assert "TELETHON_API_ID={{ telethon_api_id }}" in content
        assert "TELETHON_API_HASH={{ telethon_api_hash }}" in content
        assert "TELETHON_SESSION={{ telethon_session }}" in content

    def test_env_file_lands_in_the_home_qa_ssh_lands_in(self):
        assert self.defaults["qa_runner_telethon_env_file"] == (
            "{{ qa_runner_home }}/.qa-telethon.env"
        )

    def test_session_file_copy_is_gone(self):
        """The file-based session was never provisioned — no fallback left behind."""
        rendered = yaml.safe_dump(self.tasks)
        assert "telethon.session" not in rendered, rendered


class TestFailedInstallIsNotReportedAsSuccess:
    """curl failing must fail the task instead of feeding bash an empty script."""

    def setup_method(self):
        with open(QA_RUNNER_DIR / "tasks" / "main.yml") as f:
            self.install = _task_named(
                yaml.safe_load(f), "Install Claude Code CLI via install script"
            )

    def test_runs_under_bash(self):
        # /bin/sh has no pipefail; the pipeline would keep swallowing curl's exit code
        assert self.install["args"]["executable"] == "/bin/bash"

    def test_install_command_exits_nonzero_when_download_fails(self, tmp_path):
        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        curl_stub = stub_bin / "curl"
        curl_stub.write_text("#!/bin/sh\necho 'curl: could not resolve host' >&2\nexit 6\n")
        curl_stub.chmod(0o755)

        result = subprocess.run(
            ["/bin/bash", "-c", self.install["shell"]],
            env={"PATH": f"{stub_bin}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0, (
            f"Failed download reported success: stdout={result.stdout} stderr={result.stderr}"
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

    def test_qa_user_follows_the_deploy_user(self):
        """The deploy user is the one QA connects as (server.ssh_user)."""
        assert self.defaults["qa_runner_user"] == "{{ deploy_user }}"
        assert self.defaults["qa_runner_home"] == "/home/{{ qa_runner_user }}"

    def test_claude_bin_matches_the_path_qa_builds(self):
        """QA exports $HOME/.local/bin and calls a bare `claude`."""
        assert self.defaults["qa_runner_claude_bin"] == "{{ qa_runner_home }}/.local/bin/claude"


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
