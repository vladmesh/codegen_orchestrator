"""Run the shipped recovery Git task against an authenticated offline remote."""

import base64
import copy
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from shared.tests.git_http_fixture import GitHTTPFixture
from src.provisioner.ansible_runner import AnsibleRunner, Paths


def _assert_remote_file_cleanup(disposition, remote, raw_output, deployment):
    directories = set(
        re.findall(r"/tmp/codegen-deploy-git-[\w]+", raw_output)  # noqa: S108 - inspect private paths
    )
    if disposition != "tempfile_failure":
        assert directories, raw_output
    assert all(not Path(directory).exists() for directory in directories)
    if disposition in ("copy_failure", "tempfile_failure"):
        assert not remote.transport_path.exists() and not remote.headers
        return
    assert "PUT " in raw_output  # Native copy transfer, never a shell echo.
    transports = remote.transports()
    assert transports
    for transport in transports:
        path = Path(transport["path"])
        assert transport["present"]
        assert transport["mode"] == 0o600 and transport["parent_mode"] == 0o700
        assert path.parent.parent == Path("/tmp")  # noqa: S108 - verify outside deployment
        assert not path.is_relative_to(deployment)
        assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize(
    ("reused", "disposition"),
    [
        (False, "success"),
        (True, "success"),
        (False, "git_failure"),
        (False, "copy_failure"),
        (False, "tempfile_failure"),
    ],
)
def test_recovery_clone_update_and_failure_transport_cleanup(
    reused, disposition, tmp_path, monkeypatch, capsys
):
    token = "ansible-service-harmless-token-canary"  # noqa: S105 - harmless canary
    old_token = "ansible-service-released-token-canary"  # noqa: S105 - harmless canary
    with GitHTTPFixture(tmp_path, token, github_proxy=True) as remote:
        deployment = tmp_path / "deployment"
        if reused:
            remote.run("clone", str(remote.remote), str(deployment))
            remote.run(
                "-C",
                str(deployment),
                "remote",
                "set-url",
                "origin",
                f"https://x-access-token:{old_token}@github.com/org/repo.git",
            )
            # Give the update real remote work to fetch.
            seed = tmp_path / "seed"
            (seed / "update.txt").write_text("remote update")
            remote.run("-C", str(seed), "add", ".")
            remote.run("-C", str(seed), "commit", "-m", "remote update")
            remote.run("-C", str(seed), "push", str(remote.remote), "main")
        source = Path(__file__).parents[2] / "ansible"
        # Execute the shipped Git task alone: no SSH, firewall, sudo or Docker
        # dependency is part of this credential transport boundary.
        tasks = yaml.safe_load((source / "playbooks" / "deploy_project.yml").read_text())[0][
            "tasks"
        ]
        git_task = copy.deepcopy(
            next(task for task in tasks if "block" in task and "git" in task["tags"])
        )
        if disposition == "git_failure":
            remote.token = "fixture-rejects-the-supplied-token"  # noqa: S105 - harmless canary
        elif disposition == "copy_failure":
            # Inject a native remote file-transfer failure after the unique
            # directory exists, rather than mocking Ansible's cleanup owner.
            transfer = next(task for task in git_task["block"] if "copy" in task)
            transfer["copy"]["dest"] = (
                "{{ git_credential_directory.path }}/missing-parent/credentials"
            )
        elif disposition == "tempfile_failure":
            git_task["block"][0]["tempfile"]["path"] = str(tmp_path / "missing-parent")
        playbooks = tmp_path / "ansible" / "playbooks"
        playbooks.mkdir(parents=True)
        shutil.copyfile(source / "ansible.cfg", playbooks.parent / "ansible.cfg")
        (playbooks / "probe.yml").write_text(
            yaml.safe_dump(
                [{"hosts": "target", "gather_facts": False, "become": False, "tasks": [git_task]}]
            )
        )
        monkeypatch.setattr(Paths, "ANSIBLE_PLAYBOOKS", str(playbooks))
        for key, value in remote.environment.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("PATH", f"{remote.environment['PATH']}:{Path(sys.executable).parent}")
        raw_output = []
        native_run = subprocess.run

        def capture_transport(cmd, **kwargs):
            if cmd[0] == "ansible-playbook":
                cmd = [*cmd, "-vvv"]
                result = native_run(cmd, **kwargs)
                raw_output.append(result.stdout + result.stderr)
                return result
            return native_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", capture_transport)
        success, output = AnsibleRunner().run_playbook(
            server_ip="localhost ansible_connection=local",
            server_handle="fixture",
            playbook_name="probe.yml",
            tags=["git"],
            timeout=30,
            extra_vars={
                "github_token": token,
                "repo_full_name": "org/repo",
                "project_name": "fixture",
                "service_port": "8123",
                "deploy_dir": str(deployment),
            },
        )
        assert success == (disposition == "success"), output
        config = ""
        if success:
            assert any(remote.headers)
            assert all(
                header == "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
                for header in remote.headers
                if header
            )
            config = (deployment / ".git" / "config").read_text()
            assert "https://github.com/org/repo.git" in config
            assert remote.run("-C", str(deployment), "rev-parse", "HEAD") == remote.run(
                "-C", str(remote.remote), "rev-parse", "main"
            )
        captured = capsys.readouterr()
        assert raw_output and "EXEC /bin/sh" in raw_output[0]
        argv = remote.argv() if remote.argv_path.exists() else []
        observed = config + str(argv) + output + captured.out + captured.err + str(raw_output)
        for secret in (
            token,
            old_token,
            base64.b64encode(f"x-access-token:{token}".encode()).decode(),
        ):
            assert secret not in observed
        assert "extraheader" not in config.lower()
        _assert_remote_file_cleanup(disposition, remote, raw_output[0], deployment)
