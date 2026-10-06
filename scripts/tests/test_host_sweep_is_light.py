"""The host profile `python -m shared` runs on the control host stays light.

Every file the `--host` profile collects is scanned (scripts/host_sweep.py) for a
test that starts ansible-playbook, PlaybookCLI, sudo, useradd, runuser, systemctl,
docker, xenon, deptry or copier without a ci_only-family marker. Such a test runs in
CI only: `make test-unit` without `--host`, or a CI target of its own.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from scripts import host_sweep

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "test-unit-local.sh"


@pytest.mark.slow(reason="the CI Contract job runs the same scan (scripts/check-ci-gate.py)")
def test_no_heavy_test_reaches_the_host_profile():
    violations = host_sweep.host_violations()
    assert not violations, "\n".join(str(violation) for violation in violations)


def test_the_host_profile_leaves_live_offline_to_ci():
    labels = {label for label, _ in host_sweep.host_suites()}
    assert "live-offline" not in labels
    assert {"api", "langgraph", "shared", "scripts", "repo"} <= labels
    assert not [path for path in host_sweep.host_test_files() if path.startswith("tests/live/")]


def test_the_privileged_ansible_apply_is_out_of_the_host_profile():
    files = host_sweep.host_test_files()
    assert not [path for path in files if path.endswith("test_ansible_deploy_target_role.py")]
    assert (ROOT / "tests/integration/infra/test_ansible_deploy_target_role.py").is_file()


def test_the_runner_caps_jobs_and_deselects_ci_only_on_the_host():
    script = RUNNER.read_text()
    assert 'UNIT_JOBS="${UNIT_JOBS:-2}"' in script
    assert 'MARKER_ARGS+=(-m "not ci_only")' in script
    assert "wait -n" in script
    assert 'echo "Wall time: ${SECONDS}s"' in script
    # The docker-guard passthrough is gone: docker tests are not in the host profile.
    assert "UMMANU_DOCKER" not in script


def _scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, name: str = "test_sample.py"
) -> list[str]:
    monkeypatch.setattr(host_sweep, "ROOT", tmp_path)
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return [violation.owner for violation in host_sweep.violations_in(path)]


def test_an_unmarked_docker_run_is_a_violation(tmp_path, monkeypatch):
    found = _scan(
        tmp_path,
        monkeypatch,
        """
        import subprocess

        def test_render():
            command = ["docker", "compose", "config"]
            subprocess.run([*command, "--quiet"], check=True)
        """,
    )
    assert found == ["test_render"]


@pytest.mark.parametrize(
    ("imports", "call"),
    [
        ("from subprocess import run", 'run(["docker", "compose", "config"])'),
        ("from subprocess import run as go", 'go(["docker", "info"])'),
        ("import subprocess as sp", 'sp.check_output(["docker", "info"])'),
        ("import subprocess", 'subprocess.run(args=["docker", "compose", "config"])'),
        ("import subprocess", 'subprocess.Popen(args="docker info", shell=True)'),
        ("from os import system", 'system("sudo -n true")'),
    ],
    ids=["from-import", "from-import-alias", "module-alias", "args-keyword", "shell-keyword", "os"],
)
def test_every_spelling_of_a_process_start_is_seen(tmp_path, monkeypatch, imports, call):
    source = f"{imports}\n\ndef test_render():\n    {call}\n"
    assert _scan(tmp_path, monkeypatch, source) == ["test_render"]


def test_a_wrapper_receiving_the_argv_by_keyword_is_seen(tmp_path, monkeypatch):
    found = _scan(
        tmp_path,
        monkeypatch,
        """
        import subprocess

        def _run(command):
            return subprocess.run(command, check=False)

        def test_render():
            _run(command=["docker", "compose", "config"])
        """,
    )
    assert found == ["test_render"]


def test_a_heavy_conftest_fixture_is_a_violation_whatever_it_is_marked(tmp_path, monkeypatch):
    found = _scan(
        tmp_path,
        monkeypatch,
        """
        import subprocess
        import pytest

        pytestmark = pytest.mark.docker

        @pytest.fixture
        def stack():
            return subprocess.run(["docker", "compose", "config"], check=True)

        @pytest.fixture
        def light():
            return subprocess.run(["git", "status"], check=False)
        """,
        name="conftest.py",
    )
    assert found == ["stack"]


def test_the_host_scan_reads_every_conftest_of_a_host_suite():
    files = host_sweep.host_test_files()
    assert "shared/tests/conftest.py" in files
    assert (
        "services/api/tests/unit/conftest.py" in files
        or not (ROOT / "services/api/tests/unit/conftest.py").exists()
    )


@pytest.mark.parametrize(
    "conftest",
    [
        "services/langgraph/tests/conftest.py",
        "services/infra-service/tests/conftest.py",
        "services/scheduler/tests/conftest.py",
        "packages/worker-wrapper/tests/conftest.py",
    ],
)
def test_the_host_scan_reads_the_parent_conftests_pytest_imports(conftest):
    assert (ROOT / conftest).is_file()
    assert conftest in host_sweep.host_test_files()


def test_a_heavy_parent_conftest_of_a_host_suite_is_a_violation(tmp_path, monkeypatch):
    runner = tmp_path / "scripts" / "test-unit-local.sh"
    runner.parent.mkdir()
    runner.write_text(
        "ALL_SUITES=(\n"
        '    "svc|services/svc/tests/unit|"\n'
        '    "live-offline|tests/live||"\n'
        ")\n"
        "HOST_EXCLUDED_SUITES=(live-offline)\n"
    )
    (tmp_path / "services/svc/tests/unit").mkdir(parents=True)
    (tmp_path / "tests/live").mkdir(parents=True)
    (tmp_path / "services/svc/tests/unit/test_light.py").write_text("def test_x():\n    pass\n")
    (tmp_path / "services/svc/tests/conftest.py").write_text(
        "import subprocess\n\nsubprocess.run(['docker', 'compose', 'config'], check=True)\n"
    )
    monkeypatch.setattr(host_sweep, "ROOT", tmp_path)
    monkeypatch.setattr(host_sweep, "TEST_UNIT_LOCAL", runner)

    assert "services/svc/tests/conftest.py" in host_sweep.host_test_files()
    assert [(v.path, v.owner) for v in host_sweep.host_violations()] == [
        ("services/svc/tests/conftest.py", "import time")
    ]


def test_a_sub_marker_or_module_mark_covers_the_test(tmp_path, monkeypatch):
    marked = """
        import subprocess
        import pytest

        @pytest.mark.docker
        def test_render():
            subprocess.run(["docker", "compose", "config"], check=True)
        """
    assert _scan(tmp_path, monkeypatch, marked) == []
    module = """
        import subprocess
        import pytest

        pytestmark = pytest.mark.kit_gate

        def test_gate():
            subprocess.run("xenon --max-absolute B .", shell=True, check=True)
        """
    assert _scan(tmp_path, monkeypatch, module) == []


def test_a_heavy_helper_charges_every_test_that_uses_it(tmp_path, monkeypatch):
    found = _scan(
        tmp_path,
        monkeypatch,
        """
        import shutil
        import subprocess
        import pytest

        def _run(tool, *args):
            return subprocess.run([shutil.which(tool), *args], check=False)

        def _privileged(command):
            return _run("sudo", "-n", *command)

        @pytest.fixture
        def user():
            _privileged(["useradd", "probe"])

        @pytest.mark.privileged
        def test_marked(user):
            pass

        def test_unmarked(user):
            pass

        def test_light():
            subprocess.run(["git", "status"], check=False)
        """,
    )
    assert found == ["test_unmarked"]


def test_playbook_cli_counts_even_inside_a_written_shim(tmp_path, monkeypatch):
    # Spelled in two halves, or this very file would carry the name it scans for.
    source = """
        def test_shim(tmp_path):
            (tmp_path / "ansible-playbook").write_text(
                "from ansible.cli.playbook import CLI_CLASS"
            )
        """.replace("CLI_CLASS", "Playbook" + "CLI")
    found = _scan(tmp_path, monkeypatch, source)
    assert found == ["test_shim"]


def test_fakes_and_expected_argv_are_not_heavy(tmp_path, monkeypatch):
    found = _scan(
        tmp_path,
        monkeypatch,
        """
        import subprocess

        def test_fake_on_path(tmp_path):
            for name in ("docker", "sudo", "systemctl"):
                (tmp_path / name).write_text("#!/bin/sh\\n")
            subprocess.run(["bash", "-c", "true"], check=True)

        def test_expected_command(monkeypatch):
            calls = []
            assert calls != [["docker", "ps", "-a"]]
        """,
    )
    assert found == []


def test_a_spawn_at_import_time_fails_even_when_marked(tmp_path, monkeypatch):
    found = _scan(
        tmp_path,
        monkeypatch,
        """
        import subprocess
        import pytest

        @pytest.mark.docker
        @pytest.mark.skipif(
            subprocess.run(["docker", "version"]).returncode != 0, reason="no docker"
        )
        def test_render():
            pass
        """,
    )
    assert found == ["import time"]


@pytest.mark.subprocess
def test_a_sub_marker_implies_ci_only_for_the_host_expression(tmp_path):
    (tmp_path / "test_family.py").write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.docker
            def test_docker():
                pass

            @pytest.mark.ci_only
            def test_ci_only():
                pass

            def test_light():
                pass
            """
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.ci_only_markers",
            "-m",
            "not ci_only",
            "--strict-markers",
            "-o",
            "addopts=",
            "--rootdir",
            str(tmp_path),
            str(tmp_path / "test_family.py"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 2 deselected" in result.stdout
