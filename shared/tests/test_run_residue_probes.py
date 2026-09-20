"""The pieces of the run residue proof that live outside `tests/live`.

Three of them, and each is here because getting it wrong would make the proof
lie rather than fail: the Compose project name the worker sweep and the residue
question both derive from, the remote scan that must fail rather than report an
empty host, and the workspace remover that has to refuse a path outside its root
and read absence back rather than assume it.
"""

from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from shared.live_harness_cleanup import build_remote_run_residue_command
from shared.live_harness_workspaces import (
    PLAN_DIRECTORY,
    WORKSPACE_RESIDUE_MARKER,
    main,
    present,
    remove,
    resolve_entry,
    workspace_root,
)
from shared.worker_compose import (
    COMPOSE_ONEOFF_NAME_INFIX,
    COMPOSE_PROJECT_LABEL,
    worker_compose_project,
    worker_compose_project_filter,
    worker_id_of_compose_project,
)

WORKER = "dev-p-163f3678dc694b9ea2-f0523fb9"


class TestTheComposeProjectAWorkerOwns:
    def test_the_name_is_what_the_manager_runs_compose_under(self):
        assert worker_compose_project(WORKER) == f"worker_{WORKER}"
        assert worker_compose_project_filter(WORKER) == {
            "label": f"{COMPOSE_PROJECT_LABEL}=worker_{WORKER}"
        }

    def test_the_name_round_trips_back_to_its_worker(self):
        assert worker_id_of_compose_project(worker_compose_project(WORKER)) == WORKER

    @pytest.mark.parametrize("project", ["codegen", "live-test-9-abc", "worker_", ""])
    def test_a_project_that_is_no_workers_resolves_to_nobody(self, project):
        """Reading a non-worker project as a worker id would invent an orphan."""
        assert worker_id_of_compose_project(project) is None

    def test_the_one_shot_name_compose_gives_a_run_container_is_recognised(self):
        name = f"{worker_compose_project(WORKER)}-integration-tests-run-67ea0c169cf0"
        assert COMPOSE_ONEOFF_NAME_INFIX in name


class TestTheRemoteRunResidueScan:
    def test_it_reports_a_container_and_a_directory_of_the_run(self, tmp_path):
        base = tmp_path / "services"
        (base / "live-test-9-abc").mkdir(parents=True)
        fake_docker = tmp_path / "bin"
        fake_docker.mkdir()
        (fake_docker / "docker").write_text(
            "#!/bin/sh\nprintf 'live_test-9-abc-backend-1\\nunrelated-1\\n"
            "live-test-9-abcdef-backend-1\\n'\n"
        )
        (fake_docker / "docker").chmod(0o755)

        command = build_remote_run_residue_command(["live-test-9-abc"], service_base=str(base))
        result = subprocess.run(  # noqa: S602 — the command is the artifact under test
            f"PATH={shlex.quote(str(fake_docker))}:$PATH {command}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            # The dash Docker replaced with an underscore is still this run's;
            # a neighbouring stack whose name merely starts the same is not,
            # because the match is anchored on the separator Compose adds.
            "container live_test-9-abc-backend-1",
            f"directory {base}/live-test-9-abc",
        ]

    def test_an_unreachable_daemon_fails_rather_than_reporting_a_clean_host(self):
        """The one thing a residue scan may never do is answer emptily on failure."""
        command = build_remote_run_residue_command(["live-test-9-abc"], service_base="/nowhere")
        result = subprocess.run(  # noqa: S602 — see above
            f"PATH=/nonexistent {command}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert result.stdout.strip() == ""

    def test_a_host_with_nothing_of_this_run_answers_nothing_and_succeeds(self, tmp_path):
        fake_docker = tmp_path / "bin"
        fake_docker.mkdir()
        (fake_docker / "docker").write_text("#!/bin/sh\nprintf 'unrelated-1\\n'\n")
        (fake_docker / "docker").chmod(0o755)

        command = build_remote_run_residue_command(
            ["live-test-9-abc"], service_base=str(tmp_path / "services")
        )
        result = subprocess.run(  # noqa: S602 — see above
            f"PATH={shlex.quote(str(fake_docker))}:$PATH {command}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestTheWorkspaceRemover:
    def test_it_removes_a_checkout_a_scratch_and_a_plan_and_reads_absence_back(
        self, tmp_path, monkeypatch
    ):
        for entry in ("repo-9", f"qa-{WORKER}", f"{PLAN_DIRECTORY}/{WORKER}"):
            (tmp_path / entry).mkdir(parents=True)
        (tmp_path / "repo-9" / "file.txt").write_text("x")
        (tmp_path / "repo-other").mkdir()
        monkeypatch.setenv("SCAFFOLDED_WORKSPACE_PATH", str(tmp_path))

        entries = ["repo-9", f"qa-{WORKER}", f"{PLAN_DIRECTORY}/{WORKER}"]
        assert present(workspace_root(), entries) == entries
        assert remove(workspace_root(), entries) == []
        assert (tmp_path / "repo-other").exists()

    @pytest.mark.parametrize(
        "entry", ["..", "../elsewhere", "/etc", "repo-9/../..", f"deep/{PLAN_DIRECTORY}/x", ""]
    )
    def test_it_refuses_anything_that_is_not_inside_its_root(self, entry, tmp_path):
        with pytest.raises(ValueError, match="workspace entry"):
            resolve_entry(tmp_path, entry)

    def test_an_unconfigured_root_is_an_error_rather_than_a_default(self, monkeypatch):
        monkeypatch.delenv("SCAFFOLDED_WORKSPACE_PATH", raising=False)
        with pytest.raises(RuntimeError, match="SCAFFOLDED_WORKSPACE_PATH"):
            workspace_root()

    def test_the_cli_prints_one_marked_payload_the_proof_can_parse(
        self, tmp_path, monkeypatch, capsys
    ):
        (tmp_path / "repo-9").mkdir()
        monkeypatch.setenv("SCAFFOLDED_WORKSPACE_PATH", str(tmp_path))

        main(["residue", "--entry", "repo-9", "--entry", "repo-absent"])

        line = capsys.readouterr().out.strip()
        assert line.startswith(WORKSPACE_RESIDUE_MARKER)
        payload = json.loads(line[len(WORKSPACE_RESIDUE_MARKER) :])
        assert payload["findings"] == ["repo-9"]
        assert payload["asked"] == ["repo-9", "repo-absent"]
        assert payload["root"] == str(tmp_path)
