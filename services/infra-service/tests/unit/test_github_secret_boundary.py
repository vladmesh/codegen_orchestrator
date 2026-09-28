"""Recovery's real execution and notification boundaries, with harmless credentials."""

import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat
import subprocess
from unittest.mock import AsyncMock

import pytest
import yaml

os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from src.clients.api import DeploymentRecord  # noqa: E402
from src.provisioner import ansible_runner, recovery  # noqa: E402

TOKEN = "recovery-harmless-credential-canary"  # noqa: S105 - harmless canary
KEY = "private-key-canary"
ENCODED = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
FAILURE = (
    f"clone denied {TOKEN} https://x-access-token:{TOKEN}@github.com/org/repo "
    f"Authorization: Basic {ENCODED} {KEY}"
)


def test_deploy_git_task_uses_clean_url_native_environment_and_no_log():
    playbook = Path(__file__).parents[2] / "ansible" / "playbooks" / "deploy_project.yml"
    tasks = yaml.safe_load(playbook.read_text())[0]["tasks"]
    task = next(task for task in tasks if "git" in task)
    assert task["git"]["repo"] == "https://github.com/{{ repo_full_name }}.git"
    assert task["no_log"] is True
    assert str(task["environment"]["GIT_CONFIG_COUNT"]) == "1"
    assert task["environment"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "github_token" in task["environment"]["GIT_CONFIG_VALUE_0"]
    assert "b64encode" in task["environment"]["GIT_CONFIG_VALUE_0"]
    assert "github_token=xxx" not in playbook.read_text()


@pytest.fixture(autouse=True)
def bundled_playbooks(monkeypatch):
    monkeypatch.setattr(
        ansible_runner.Paths,
        "ANSIBLE_PLAYBOOKS",
        str(Path(__file__).parents[2] / "ansible" / "playbooks"),
    )


@pytest.mark.parametrize("disposition", ["success", "failure", "timeout", "exception"])
def test_private_json_transport_and_cleanup(disposition, monkeypatch, capsys):
    paths = []

    def execute(cmd, **kwargs):
        assert TOKEN not in str(cmd) and ENCODED not in str(cmd)
        assert KEY not in str(cmd)
        inventory = Path(cmd[cmd.index("-i") + 1])
        paths.append(inventory)
        assert TOKEN not in inventory.read_text()
        assert stat.S_IMODE(inventory.stat().st_mode) == 0o600
        vars_arg = cmd[cmd.index("--extra-vars") + 1]
        assert vars_arg.startswith("@"), "Ansible variables must use a private native JSON file"
        variables = Path(vars_arg[1:])
        paths.append(variables)
        assert variables.is_relative_to(Path("/tmp"))  # noqa: S108 - private tempfile root
        assert not variables.is_relative_to(Path.cwd())
        assert stat.S_IMODE(variables.stat().st_mode) == 0o600
        values = json.loads(variables.read_text())
        assert values["github_token"] == TOKEN
        assert values["project_name"] == "a project 'with quotes'"
        assert values["repo_full_name"] == 'org/repo "literal"'
        assert values["service_port"] == "8123"
        assert values["server_hostname"] == "host with spaces"
        key_path = Path(inventory.read_text().split("ansible_ssh_private_key_file=")[1].split()[0])
        paths.append(key_path)
        assert key_path.read_text() == KEY + "\n"
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert kwargs["env"]["ANSIBLE_CONFIG"].endswith("ansible/ansible.cfg")
        assert kwargs["timeout"] == 17
        if disposition == "timeout":
            raise subprocess.TimeoutExpired(cmd, 17, output=FAILURE, stderr=FAILURE)
        if disposition == "exception":
            raise RuntimeError(FAILURE)
        return subprocess.CompletedProcess(cmd, int(disposition == "failure"), FAILURE, FAILURE)

    monkeypatch.setattr(ansible_runner.subprocess, "run", execute)
    success, output = ansible_runner.AnsibleRunner().run_playbook(
        server_ip="192.0.2.1",
        server_handle="host with spaces",
        playbook_name="deploy_project.yml",
        ssh_user="root",
        ssh_private_key=KEY,
        timeout=17,
        extra_vars={
            "github_token": TOKEN,
            "project_name": "a project 'with quotes'",
            "repo_full_name": 'org/repo "literal"',
            "service_port": "8123",
        },
    )
    assert success is (disposition == "success")
    assert paths and all(not path.exists() for path in paths)
    assert not paths[0].parent.exists()
    captured = capsys.readouterr()
    observable = output + captured.out + captured.err
    for secret in (TOKEN, KEY, ENCODED):
        assert secret not in observable
    assert "clone denied" in observable or "Timeout after 17s" in observable


@pytest.mark.parametrize("failed_file", ["ssh.key", "inventory.ini", "vars.json"])
def test_setup_failure_cleans_files_and_sanitizes_exception(failed_file, monkeypatch, capsys):
    paths = []
    original = os.open

    def open_private(path, *args, **kwargs):
        if args[0] & os.O_CREAT:
            paths.append(Path(path))
            if Path(path).name == failed_file:
                raise OSError(FAILURE)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(ansible_runner.os, "open", open_private)
    success, output = ansible_runner.AnsibleRunner().run_playbook(
        server_ip="192.0.2.1",
        server_handle="host",
        playbook_name="deploy_project.yml",
        ssh_user="root",
        ssh_private_key=KEY,
        extra_vars={"github_token": TOKEN},
    )
    assert success is False
    assert paths and all(not path.exists() for path in paths)
    assert all(not path.parent.exists() for path in paths)
    logs = capsys.readouterr()
    for secret in (TOKEN, KEY, ENCODED):
        assert secret not in output + logs.out + logs.err
    assert "clone denied" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["failure", "timeout", "exception"])
async def test_recovery_failure_reaches_admin_without_credentials(disposition, monkeypatch, capsys):
    service = DeploymentRecord(
        id=1,
        project_id="project",
        server_handle="host",
        service_name="billing",
        port=8123,
        deployment_info={"repo_full_name": "org/repo"},
        result="success",
        deployed_at=datetime.now(UTC),
    )
    monkeypatch.setattr(recovery, "get_services_on_server", AsyncMock(return_value=[service]))
    client = type("GitHubDouble", (), {"get_token": AsyncMock(return_value=TOKEN)})
    monkeypatch.setattr("shared.clients.github.GitHubAppClient", client)
    notify = AsyncMock()
    monkeypatch.setattr(recovery, "notify_admins_best_effort", notify)

    def execute(cmd, **kwargs):
        assert TOKEN not in str(cmd)
        assert cmd[cmd.index("--extra-vars") + 1].startswith("@")
        if disposition == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], stderr=FAILURE)
        if disposition == "exception":
            raise RuntimeError(FAILURE)
        return subprocess.CompletedProcess(
            cmd, 2, "noisy task output\n" * 200 + "PLAY RECAP\nhost failed=1\n", FAILURE
        )

    monkeypatch.setattr(ansible_runner.subprocess, "run", execute)
    result = await recovery.redeploy_all_services("host", "192.0.2.1")
    assert result[:2] == (0, 1)
    notify.assert_awaited_once()
    if disposition != "timeout":
        assert "clone denied" in result[2][0]
        assert "clone denied" in notify.await_args.args[0]
    logs = capsys.readouterr()
    observable = str(result) + str(notify.await_args) + logs.out + logs.err
    for secret in (TOKEN, ENCODED):
        assert secret not in observable
    assert "billing" in observable
    assert "clone denied" in observable or "timeout" in observable.lower()
