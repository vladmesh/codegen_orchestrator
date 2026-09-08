"""What the package variant requires of the deployed product, judged offline.

The paid run's own judgement, exercised here against fixture artifacts in the
three shapes that decide it: a product that carries the kit package and
attributes the behaviour to it, a product that carries no package at all, and a
product whose same-named behaviour is its own. The last two are the products a
permitted architect choice produces, and neither may be green.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from package_route import (
    NOT_THE_PACKAGE_ROUTE,
    PACKAGE_ROUTE_ARTIFACTS,
    behaviour_not_attributed,
    package_not_active,
    package_route_facts,
    parse_package_probe,
    unreadable_package_route,
)
import pipeline_helpers
from pipeline_helpers import BRIEF_PACKAGE_JOB_NAME, BRIEF_PACKAGE_NAME
import pytest

from services.langgraph.src.agents.qa.packages import (
    ACTIVE_PACKAGE_CONTRACT,
    GENERATED_JOB_REGISTRY,
)
from shared.live_harness_cleanup import (
    PACKAGE_CONTRACT_ABSENT_MARKER,
    PACKAGE_CONTRACT_FILE_MARKER,
    build_remote_package_contract_command,
)

pytestmark = pytest.mark.needs_no_api_credential

_REMINDERS_CONTRACT = """ACTIVE_PACKAGES = [
    {
        "name": "reminders",
        "version": "0.1.0",
        "manifest_sha256": "6f1c0f2a1b3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddee",
    },
]
"""
_PACKAGE_REGISTRY = (
    'JOB_SCHEMA_SOURCES = {"reminders.tick": "package:reminders", "digest": "backend"}\n'
)
_PRODUCT_REGISTRY = 'JOB_SCHEMA_SOURCES = {"reminders.tick": "backend"}\n'
_EMPTY_CONTRACT = "ACTIVE_PACKAGES = []\n"


def _probe(contract: str | None, registry: str | None) -> str:
    """The probe output a deployment carrying these artifacts produces."""
    lines = []
    for path, body in ((ACTIVE_PACKAGE_CONTRACT, contract), (GENERATED_JOB_REGISTRY, registry)):
        if body is None:
            lines.append(f"{PACKAGE_CONTRACT_ABSENT_MARKER} {path}")
            continue
        lines.append(f"{PACKAGE_CONTRACT_FILE_MARKER} {path}")
        lines.append(body.rstrip("\n"))
    return "\n".join(lines) + "\n"


def _facts(contract: str | None, registry: str | None):
    return package_route_facts(
        _probe(contract, registry),
        package=BRIEF_PACKAGE_NAME,
        behaviour=BRIEF_PACKAGE_JOB_NAME,
    )


def test_the_kit_package_product_passes_and_says_what_it_rests_on():
    facts, error = _facts(_REMINDERS_CONTRACT, _PACKAGE_REGISTRY)

    assert error is None
    assert facts["package"] == "reminders"
    assert facts["version"] == "0.1.0"
    assert facts["behaviour"] == BRIEF_PACKAGE_JOB_NAME
    assert facts["declared_by"] == "package:reminders"
    assert facts["read_from"] == list(PACKAGE_ROUTE_ARTIFACTS)


def test_a_product_with_no_package_is_red_with_the_reason():
    """The shared-service product a permitted architect choice produces."""
    facts, error = _facts(_EMPTY_CONTRACT, _PRODUCT_REGISTRY)

    assert facts is None
    assert error == package_not_active("reminders", "no active kit package")
    assert error.endswith(NOT_THE_PACKAGE_ROUTE)


def test_a_hand_written_behaviour_of_the_same_name_is_red_with_the_reason():
    """The package is installed and the fired job is still the product's own."""
    facts, error = _facts(_REMINDERS_CONTRACT, _PRODUCT_REGISTRY)

    assert facts is None
    assert error == behaviour_not_attributed("reminders", BRIEF_PACKAGE_JOB_NAME, "'backend'")
    assert error.endswith(NOT_THE_PACKAGE_ROUTE)


def test_a_behaviour_the_deployment_declares_nowhere_is_red_with_the_reason():
    facts, error = _facts(_REMINDERS_CONTRACT, 'JOB_SCHEMA_SOURCES = {"digest": "backend"}\n')

    assert facts is None
    assert error == behaviour_not_attributed(
        "reminders", BRIEF_PACKAGE_JOB_NAME, "no declarer at all"
    )


@pytest.mark.parametrize(
    ("contract", "registry"),
    [(None, _PACKAGE_REGISTRY), (_REMINDERS_CONTRACT, None), (None, None)],
)
def test_an_artifact_that_is_not_there_is_a_failure_not_a_skip(contract, registry):
    facts, error = _facts(contract, registry)

    assert facts is None
    assert error.startswith("the deployed product's kit package contract could not be read")
    assert error.endswith(NOT_THE_PACKAGE_ROUTE)


def test_an_unparseable_contract_is_a_failure_not_an_empty_package_set():
    facts, error = _facts("ACTIVE_PACKAGES = (lambda: 1)\n", _PACKAGE_REGISTRY)

    assert facts is None
    assert error.startswith("the deployed product's kit package contract could not be read")


def test_a_probe_that_answered_nothing_at_all_is_a_failure():
    """An empty answer is not a product without packages."""
    facts, error = package_route_facts(
        "", package=BRIEF_PACKAGE_NAME, behaviour=BRIEF_PACKAGE_JOB_NAME
    )

    assert facts is None
    assert error == unreadable_package_route(
        f"the probe of the deployment answered nothing about {', '.join(PACKAGE_ROUTE_ARTIFACTS)}"
    )


def _image_only_target(tmp_path: Path, *, containers: str = "proj-1-backend-1\n"):
    """A host checkout with only compose files and contracts inside its image."""
    host = tmp_path / "services" / "proj-1" / "infra"
    host.mkdir(parents=True)
    (host / "compose.base.yml").write_text("services: {}\n")
    (host / "compose.prod.yml").write_text("services: {}\n")
    container = tmp_path / "container" / "app"
    (container / "codegen_kit").mkdir(parents=True)
    (container / "services/backend/src/generated").mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s' \"$FAKE_BACKEND_CONTAINERS\"\n")
    docker.chmod(0o755)
    wrapper = bin_dir / "qa-docker"
    wrapper.write_text(
        "#!/bin/sh\n"
        "path=$3\n"
        '[ "$path" != "$FAKE_UNREADABLE" ] || exit 7\n'
        "file=$FAKE_CONTAINER_ROOT/app/$path\n"
        '[ -f "$file" ] || exit 5\n'
        'size=$(wc -c < "$file")\n'
        '[ "$size" -le "$4" ] || exit 6\n'
        'cat "$file"\n'
    )
    wrapper.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_BACKEND_CONTAINERS": containers,
        "FAKE_CONTAINER_ROOT": str(tmp_path / "container"),
        "FAKE_UNREADABLE": "",
    }
    return container, wrapper, env


def test_the_probe_reads_the_named_artifacts_from_the_running_backend_container(tmp_path):
    container, wrapper, env = _image_only_target(tmp_path)
    (container / ACTIVE_PACKAGE_CONTRACT).write_text(_REMINDERS_CONTRACT)
    command = build_remote_package_contract_command(
        "proj-1", list(PACKAGE_ROUTE_ARTIFACTS), docker_wrapper=str(wrapper)
    )

    result = subprocess.run(  # noqa: S602
        command, shell=True, capture_output=True, text=True, check=False, env=env
    )

    assert result.returncode == 0
    artifacts = parse_package_probe(result.stdout)
    assert artifacts[ACTIVE_PACKAGE_CONTRACT].strip() == _REMINDERS_CONTRACT.strip()
    assert artifacts[GENERATED_JOB_REGISTRY] is None


def test_a_file_that_cannot_be_read_fails_the_probe_rather_than_reading_absent(tmp_path):
    container, wrapper, env = _image_only_target(tmp_path)
    (container / ACTIVE_PACKAGE_CONTRACT).write_text(_REMINDERS_CONTRACT)
    env["FAKE_UNREADABLE"] = ACTIVE_PACKAGE_CONTRACT
    command = build_remote_package_contract_command(
        "proj-1", [ACTIVE_PACKAGE_CONTRACT], docker_wrapper=str(wrapper)
    )

    result = subprocess.run(  # noqa: S602
        command, shell=True, capture_output=True, text=True, check=False, env=env
    )

    assert result.returncode != 0
    assert PACKAGE_CONTRACT_ABSENT_MARKER not in result.stdout


@pytest.mark.parametrize("containers", ["", "proj-1-backend-1\nproj-1-backend-2\n"])
def test_the_probe_refuses_a_missing_or_ambiguous_backend_container(tmp_path, containers):
    _, wrapper, env = _image_only_target(tmp_path, containers=containers)
    command = build_remote_package_contract_command(
        "proj-1", [ACTIVE_PACKAGE_CONTRACT], docker_wrapper=str(wrapper)
    )

    result = subprocess.run(  # noqa: S602
        command, shell=True, capture_output=True, text=True, check=False, env=env
    )

    assert result.returncode != 0
    assert "backend container" in result.stderr


def test_an_absent_container_file_is_reported_absent(tmp_path):
    _, wrapper, env = _image_only_target(tmp_path)
    command = build_remote_package_contract_command(
        "proj-1", [ACTIVE_PACKAGE_CONTRACT], docker_wrapper=str(wrapper)
    )

    result = subprocess.run(  # noqa: S602
        command, shell=True, capture_output=True, text=True, check=False, env=env
    )

    assert result.returncode == 0
    assert result.stdout == f"{PACKAGE_CONTRACT_ABSENT_MARKER} {ACTIVE_PACKAGE_CONTRACT}\n"


def test_an_oversized_container_file_fails_instead_of_returning_a_truncated_contract(tmp_path):
    container, wrapper, env = _image_only_target(tmp_path)
    (container / ACTIVE_PACKAGE_CONTRACT).write_text("x" * 262145)
    command = build_remote_package_contract_command(
        "proj-1", [ACTIVE_PACKAGE_CONTRACT], docker_wrapper=str(wrapper)
    )

    result = subprocess.run(  # noqa: S602
        command, shell=True, capture_output=True, text=True, check=False, env=env
    )

    assert result.returncode != 0
    assert PACKAGE_CONTRACT_FILE_MARKER not in result.stdout


def test_the_probe_refuses_every_path_outside_the_fixed_contract_set():
    with pytest.raises(ValueError, match="not a readable generated contract"):
        build_remote_package_contract_command("proj-1", ["../infra/.env"])


def test_backend_resolution_uses_exact_compose_project_and_service_labels():
    command = build_remote_package_contract_command("proj-1", [ACTIVE_PACKAGE_CONTRACT])

    assert "label=com.docker.compose.project=proj-1" in command
    assert "label=com.docker.compose.service=backend" in command


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["probe"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_the_live_check_keeps_the_facts_when_the_deployment_carries_the_package(monkeypatch):
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec_python_module",
        lambda *args, **kwargs: _completed(0, _probe(_REMINDERS_CONTRACT, _PACKAGE_REGISTRY)),
    )
    ctx = {"project_name": "proj-1", "server_handle": "srv-1"}

    assert pipeline_helpers.record_package_route(ctx) is None
    assert ctx["brief_package_route"]["package"] == "reminders"


def test_the_live_check_is_red_when_the_probe_could_not_run(monkeypatch):
    """The path the observer named: unread is a failure, never a skip."""

    def refuse(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=1)

    monkeypatch.setattr(pipeline_helpers, "docker_exec_python_module", refuse)
    ctx = {"project_name": "proj-1", "server_handle": "srv-1"}

    error = pipeline_helpers.record_package_route(ctx)

    assert error.startswith("the deployed product's kit package contract could not be read")
    assert error.endswith(NOT_THE_PACKAGE_ROUTE)
    assert "brief_package_route" not in ctx


def test_the_live_check_is_red_when_the_probe_exits_non_zero(monkeypatch):
    monkeypatch.setattr(
        pipeline_helpers,
        "docker_exec_python_module",
        lambda *args, **kwargs: _completed(2, stderr="ssh exited 255"),
    )
    ctx = {"project_name": "proj-1", "server_handle": None}

    error = pipeline_helpers.record_package_route(ctx)

    assert "exited 2" in error
    assert error.endswith(NOT_THE_PACKAGE_ROUTE)
    assert "brief_package_route" not in ctx


def test_the_probe_command_names_the_deployment_and_both_artifacts():
    args = pipeline_helpers._package_contract_args("proj-1", "srv-1")

    assert args[0] == "package-contract-probe"
    assert "--server-handle" in args and "srv-1" in args
    for path in PACKAGE_ROUTE_ARTIFACTS:
        assert path in args
    assert pipeline_helpers._package_contract_args("proj-1", None).count("--server-handle") == 0
