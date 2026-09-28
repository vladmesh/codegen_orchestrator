"""Run the shipped recovery Git task against an authenticated offline remote."""

import base64
from pathlib import Path
import shutil
import sys

import pytest
import yaml

from shared.tests.git_http_fixture import GitHTTPFixture
from src.provisioner.ansible_runner import AnsibleRunner, Paths


@pytest.mark.parametrize("reused", [False, True])
def test_recovery_clone_and_update_leave_no_git_credentials(reused, tmp_path, monkeypatch, capsys):
    token = "ansible-service-harmless-token-canary"  # noqa: S105 - harmless canary
    old_token = "ansible-service-released-token-canary"  # noqa: S105 - harmless canary
    with GitHTTPFixture(tmp_path, token) as remote:
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
        git_task = next(task for task in tasks if "git" in task)
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
        assert success, output
        assert remote.headers
        assert all(
            header == "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
            for header in remote.headers
        )
        config = (deployment / ".git" / "config").read_text()
        assert "https://github.com/org/repo.git" in config
        assert remote.run("-C", str(deployment), "rev-parse", "HEAD") == remote.run(
            "-C", str(remote.remote), "rev-parse", "main"
        )
        captured = capsys.readouterr()
        observed = config + str(remote.argv()) + output + captured.out + captured.err
        for secret in (
            token,
            old_token,
            base64.b64encode(f"x-access-token:{token}".encode()).decode(),
        ):
            assert secret not in observed
        assert "extraheader" not in config.lower()
