"""Regression tests for deploy-target bootstrap ownership.

The permissions proof creates a system user with sudo and applies the role twice with
`ansible-playbook` and `become`, so the whole module is `privileged`: CI runs it on the
throwaway GitHub runner (fast-checks, `make test-privileged`), and neither the control
host's profile nor the infra compose suite (`-m "not privileged"`) collects it.
"""

import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest
import yaml

pytestmark = pytest.mark.privileged

ANSIBLE_DIR = Path(__file__).parents[3] / "services" / "infra-service" / "ansible"
ROLE_TASKS = ANSIBLE_DIR / "roles" / "deploy_target" / "tasks" / "main.yml"
SOFTWARE_PLAYBOOK = ANSIBLE_DIR / "playbooks" / "provision_software.yml"
# Two applies leave 30 seconds for setup, assertions and cleanup under pytest's 90s limit.
ANSIBLE_TIMEOUT_SECONDS = 30
PRIVILEGED_TIMEOUT_SECONDS = 10


def _task_named(tasks: list[dict], name: str) -> dict:
    return next(task for task in tasks if task["name"] == name)


def _run_bounded(command: list[str], timeout: int, **kwargs) -> subprocess.CompletedProcess:
    started = time.monotonic()
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        # TimeoutExpired can contain bytes even when text=True, or None before output.
        stdout = (
            exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        )
        stderr = (
            exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        pytest.fail(
            f"{command[0]} timed out after {elapsed:.2f}s (limit {timeout}s)\n"
            f"command: {shlex.join(command)}\n"
            f"stdout:\n{stdout or ''}\nstderr:\n{stderr or ''}",
            pytrace=False,
        )


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
  gather_facts: false
  vars:
    deploy_user: DEPLOY_USER
    services_root: SERVICES_ROOT
    ssh_public_key: ""
  roles:
    - deploy_target
""".replace("DEPLOY_USER", deploy_user).replace("SERVICES_ROOT", str(services_root))
        )

        try:
            # Keep account creation out of the role's permissions proof. An empty
            # skeleton and no login logs avoid unrelated runner-image setup work.
            skeleton = tmp_path / "empty-skeleton"
            skeleton.mkdir()
            self._run_privileged(
                [
                    "useradd",
                    "--system",
                    "--user-group",
                    "--no-log-init",
                    "--create-home",
                    "--skel",
                    str(skeleton),
                    "--home-dir",
                    f"/home/{deploy_user}",
                    "--shell",
                    "/bin/bash",
                    "--groups",
                    "docker",
                    deploy_user,
                ]
            )
            self._run_privileged(["mkdir", "-p", str(services_root / "personal_site")])
            self._run_privileged(["chown", "root:root", str(services_root / "personal_site")])
            self._run_privileged(["chmod", "0755", str(services_root / "personal_site")])

            # Account setup must finish before Ansible enters the user task.
            assert pwd.getpwnam(deploy_user).pw_shell == "/bin/bash"
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
            try:
                self._run_privileged(["userdel", "-r", deploy_user], check=False)
            finally:
                self._run_privileged(["rm", "-rf", str(services_root)], check=False)

    @staticmethod
    def _apply_role(playbook: Path) -> None:
        result = _run_bounded(
            ["ansible-playbook", "-vvv", "-i", "localhost,", str(playbook)],
            timeout=ANSIBLE_TIMEOUT_SECONDS,
            cwd=ANSIBLE_DIR,
            env={
                **os.environ,
                "ANSIBLE_STDOUT_CALLBACK": "default",
                # Local modules use the tested Python, not runner-image discovery.
                "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
                # Avoid staging modules and repeated local subprocesses on busy runners.
                "ANSIBLE_PIPELINING": "true",
            },
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
        result = _run_bounded([*prefix, *command], timeout=PRIVILEGED_TIMEOUT_SECONDS)
        if check:
            assert result.returncode == 0, (
                f"{shlex.join(command)} exited {result.returncode}\n{result.stdout}{result.stderr}"
            )
        return result


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [(b"partial task output\n", b"sudo diagnostic\n"), ("task output", "diagnostic"), (None, None)],
)
def test_apply_role_timeout_reports_output_and_elapsed_time(tmp_path, mocker, stdout, stderr):
    run = mocker.patch.object(
        subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired("ansible-playbook", 30, output=stdout, stderr=stderr),
    )
    mocker.patch("time.monotonic", side_effect=[100.0, 130.25])

    with pytest.raises(pytest.fail.Exception) as failure:
        TestDeployTargetPermissions._apply_role(tmp_path / "playbook.yml")

    message = str(failure.value)
    assert "ansible-playbook timed out after 30.25s (limit 30s)" in message
    for label, output in (("stdout", stdout), ("stderr", stderr)):
        expected = output.decode() if isinstance(output, bytes) else output or ""
        assert f"{label}:\n{expected}" in message
    assert run.call_args.kwargs["timeout"] == 30


def test_apply_role_uses_test_python_without_discovery(tmp_path, monkeypatch):
    # Runner images can contain Python versions other than the test environment's.
    alternate_python = tmp_path / "python3.14"
    alternate_python.write_text("#!/bin/sh\necho unexpected interpreter discovery >&2\nexit 1\n")
    alternate_python.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    playbook = tmp_path / "interpreter.yml"
    playbook.write_text(
        """
- hosts: localhost
  connection: local
  become: false
  gather_facts: false
  tasks:
    - ansible.builtin.ping:
"""
    )

    TestDeployTargetPermissions._apply_role(playbook)


def test_privileged_setup_timeout_reports_command_output_and_elapsed_time(mocker):
    mocker.patch("os.geteuid", return_value=0)
    run = mocker.patch.object(
        subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(
            "useradd", 10, output=b"setup output\n", stderr=b"account database locked\n"
        ),
    )
    mocker.patch("time.monotonic", side_effect=[100.0, 110.25])

    with pytest.raises(pytest.fail.Exception) as failure:
        TestDeployTargetPermissions._run_privileged(["useradd", "disposable-user"])

    message = str(failure.value)
    assert "useradd timed out after 10.25s (limit 10s)" in message
    assert "disposable-user" in message
    assert "stdout:\nsetup output\n" in message
    assert "stderr:\naccount database locked\n" in message
    assert run.call_args.kwargs["timeout"] == 10
