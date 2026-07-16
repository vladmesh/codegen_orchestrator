"""Unit tests for deterministic environment usage extraction and gates."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

from shared.contracts.env_usage import (
    build_env_contract_artifact,
    check_env_contract_usage,
    extract_env_references,
    main,
)


def write_fragment(root: Path, entries: dict[str, dict]) -> None:
    fragment = root / "infra" / "env.contract.yaml"
    fragment.parent.mkdir(parents=True, exist_ok=True)
    lines = ["version: '1'", "owner: infra", "entries:" if entries else "entries: {}"]
    for key, entry in entries.items():
        lines.extend([f"  {key}:", *[f"    {name}: {value}" for name, value in entry.items()]])
    fragment.write_text("\n".join(lines) + "\n")


def literal_entry() -> dict[str, str]:
    return {
        "source": "literal",
        "environments": "[local]",
        "required": "true",
        "value": "example",
    }


def test_python_references_include_static_accesses_and_settings_fields(tmp_path: Path):
    source = tmp_path / "src" / "settings.py"
    source.parent.mkdir()
    source.write_text(
        """import os
from pydantic_settings import BaseSettings

first = os.getenv("FIRST")
second = os.environ["SECOND"]
third = os.environ.get("THIRD")
dynamic = os.getenv(name)

class Settings(BaseSettings):
    api_key: str
    endpoint: str = Field(validation_alias="API_ENDPOINT")
"""
    )

    references = extract_env_references(tmp_path)

    assert {reference.key for reference in references} == {
        "FIRST",
        "SECOND",
        "THIRD",
        "API_KEY",
        "API_ENDPOINT",
    }
    assert all(reference.path == "src/settings.py" for reference in references)


def test_settings_fields_apply_env_prefix_and_skip_model_config(tmp_path: Path):
    source = tmp_path / "src" / "settings.py"
    source.parent.mkdir()
    source.write_text(
        """from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    '''Settings docstring from the service-template baseline.'''
    model_config: SettingsConfigDict = SettingsConfigDict(env_prefix="APP_")
    api_key: str
    debug: bool = False
"""
    )

    references = extract_env_references(tmp_path)

    assert {reference.key for reference in references} == {"APP_API_KEY", "APP_DEBUG"}


def test_compose_references_include_interpolation_not_literals(tmp_path: Path):
    (tmp_path / "compose.yaml").write_text(
        "services:\n  app:\n    image: ${IMAGE_TAG:-latest}\n    command: fixed\n"
    )

    references = extract_env_references(tmp_path)

    assert [(reference.key, reference.source) for reference in references] == [
        ("IMAGE_TAG", "compose")
    ]


def test_compose_project_files_include_template_compose_variants(tmp_path: Path):
    compose = tmp_path / "infra" / "compose.prod.yml"
    compose.parent.mkdir()
    compose.write_text("services:\n  app:\n    image: ${BACKEND_IMAGE:?required}\n")

    references = extract_env_references(tmp_path)

    assert [(reference.key, reference.source) for reference in references] == [
        ("BACKEND_IMAGE", "compose")
    ]


def test_workflow_references_include_env_and_secrets_forwarding(tmp_path: Path):
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """jobs:
  verify:
    container:
      image: python:3.12
      env:
        APP_TOKEN: ${{ secrets.APP_TOKEN }}
        BUILD_MODE: test
    steps:
      - run: echo ok
"""
    )

    references = extract_env_references(tmp_path)

    assert {(reference.key, reference.source) for reference in references} == {
        ("APP_TOKEN", "workflow"),
    }


def test_workflow_ignores_builtin_and_non_env_secret_references(tmp_path: Path):
    workflow = tmp_path / ".github" / "workflows" / "deploy.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """jobs:
  deploy:
    env:
      DEPLOY_TOKEN: ${{ secrets.DEPLOY_TOKEN }}
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    steps:
      - uses: docker/login-action@v3
        with:
          password: ${{ secrets.REGISTRY_PASSWORD }}
"""
    )

    references = extract_env_references(tmp_path)

    assert references == ()


def test_shell_self_default_assignment_remains_an_environment_read(tmp_path: Path):
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text('export APP_ENV="${APP_ENV:-production}"\n')

    references = extract_env_references(tmp_path)

    assert {(reference.key, reference.source) for reference in references} == {
        ("APP_ENV", "shell"),
    }


def test_shell_entrypoint_references_include_expansion_not_positional_args(tmp_path: Path):
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text("#!/bin/sh\necho ${DATABASE_URL:-missing} $LOG_LEVEL $1\n")

    references = extract_env_references(tmp_path)

    assert {(reference.key, reference.source) for reference in references} == {
        ("DATABASE_URL", "shell"),
        ("LOG_LEVEL", "shell"),
    }


def test_shell_entrypoint_ignores_local_and_builtin_variables(tmp_path: Path):
    entrypoint = tmp_path / "services" / "backend" / "scripts" / "start.sh"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(
        """#!/usr/bin/env bash
SCRIPT_DIR="$(pwd)"
REPO_ROOT="${SCRIPT_DIR}/.."
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH-}"
exec uvicorn app:main --port "${PORT:-8000}"
"""
    )

    references = extract_env_references(tmp_path)

    assert {(reference.key, reference.source) for reference in references} == {
        ("PORT", "shell"),
    }


def test_service_template_0_3_3_baseline_patterns(tmp_path: Path):
    """Keep extraction aligned with the baseline template named by the contract MVP."""
    compose = tmp_path / "infra" / "compose.prod.yml"
    compose.parent.mkdir()
    compose.write_text(
        """services:
  backend:
    image: ${BACKEND_IMAGE:?Set BACKEND_IMAGE}
    ports:
      - "${BACKEND_PORT:?Set BACKEND_PORT}:8000"
    deploy:
      replicas: ${BACKEND_REPLICAS:-1}
"""
    )
    start = tmp_path / "services" / "backend" / "scripts" / "start.sh"
    start.parent.mkdir(parents=True)
    start.write_text(
        """#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH-}"
exec uvicorn services.backend.src.main:app --port "${PORT:-8000}"
"""
    )
    settings = tmp_path / "services" / "backend" / "src" / "core" / "settings.py"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        """from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    '''Base settings for the backend application.'''
    model_config = SettingsConfigDict(env_file=".env")
    app_name: str = Field(validation_alias="APP_NAME")
"""
    )
    workflow = tmp_path / ".github" / "workflows" / "deploy.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """jobs:
  deploy:
    env:
      SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
    steps:
      - uses: docker/login-action@v3
        with:
          password: ${{ secrets.REGISTRY_PASSWORD }}
"""
    )

    references = extract_env_references(tmp_path)

    assert {(reference.key, reference.source) for reference in references} == {
        ("APP_NAME", "python-settings"),
        ("BACKEND_IMAGE", "compose"),
        ("BACKEND_PORT", "compose"),
        ("BACKEND_REPLICAS", "compose"),
        ("PORT", "shell"),
    }


def test_service_template_0_3_3_fixture_extracts_without_crashing(tmp_path: Path):
    fixture = Path(__file__).parents[1] / "fixtures" / "service-template-0.3.3"
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)

    references = extract_env_references(tmp_path)

    assert {(reference.key, reference.source) for reference in references} == {
        ("APP_ENV", "python-settings"),
        ("APP_NAME", "python-settings"),
        ("APP_SECRET_KEY", "python-settings"),
        ("BACKEND_IMAGE", "compose"),
        ("BACKEND_PORT", "compose"),
        ("BACKEND_REPLICAS", "compose"),
        ("DEBUG", "python-settings"),
        ("PORT", "shell"),
    }


def test_undeclared_usage_is_an_error_with_location(tmp_path: Path):
    (tmp_path / "app.py").write_text('import os\nos.getenv("MISSING_KEY")\n')
    write_fragment(tmp_path, {})

    result = check_env_contract_usage(tmp_path)

    assert result.errors == ("undeclared environment key MISSING_KEY used at app.py:2 (python)",)


def test_cli_does_not_echo_invalid_fragment_values(tmp_path: Path, capsys):
    secret = "_".join(("ghp", "SUPERSECRET", "TOKEN", "VALUE"))
    fragment = tmp_path / "infra" / "env.contract.yaml"
    fragment.parent.mkdir()
    fragment.write_text(
        f"""version: "1"
owner: infra
entries:
  API_TOKEN:
    source: user_secret
    environments: [production]
    consumers: [backend]
    required: true
    description: token
    value: {secret}
"""
    )

    exit_code = main(["--root", str(tmp_path), "--artifact", str(tmp_path / "artifact.json")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.err
    assert "API_TOKEN" in captured.err
    assert "value" in captured.err


def test_cli_does_not_echo_malformed_yaml_values(tmp_path: Path, capsys):
    secret = "_".join(("ghp", "SUPERSECRET", "TOKEN", "VALUE"))
    fragment = tmp_path / "infra" / "env.contract.yaml"
    fragment.parent.mkdir()
    fragment.write_text(f"entries: [{secret}\n")

    exit_code = main(["--root", str(tmp_path), "--artifact", str(tmp_path / "artifact.json")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.err
    assert "malformed YAML at line 2, column 1" in captured.err


def test_required_declared_but_unobserved_key_is_a_warning(tmp_path: Path):
    write_fragment(tmp_path, {"DYNAMIC_KEY": literal_entry()})

    result = check_env_contract_usage(tmp_path)

    assert result.errors == ()
    assert result.warnings == ("required environment contract key DYNAMIC_KEY was not observed",)


def test_artifact_is_deterministic_and_bound_to_commit(tmp_path: Path):
    (tmp_path / "app.py").write_text('import os\nos.getenv("DECLARED")\n')
    write_fragment(tmp_path, {"DECLARED": literal_entry()})

    first = build_env_contract_artifact(tmp_path, commit_sha="a" * 40)
    second = build_env_contract_artifact(tmp_path, commit_sha="a" * 40)

    assert first == second
    assert json.loads(first) == {
        "commit_sha": "a" * 40,
        "contract": {
            "entries": {
                "DECLARED": {
                    "consumers": [],
                    "description": None,
                    "environments": ["local"],
                    "required": True,
                    "sensitive": False,
                    "source": "literal",
                    "value": "example",
                }
            },
            "version": "1",
        },
    }


def test_cli_runs_against_generated_project_without_codegen_repository(tmp_path: Path):
    (tmp_path / "app.py").write_text('import os\nos.getenv("DECLARED")\n')
    write_fragment(tmp_path, {"DECLARED": literal_entry()})
    artifact = tmp_path / "build" / "env-contract.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shared.contracts.env_usage",
            "--root",
            str(tmp_path),
            "--artifact",
            str(artifact),
            "--commit-sha",
            "b" * 40,
        ],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(artifact.read_text())["commit_sha"] == "b" * 40


def test_vendor_copy_runs_in_isolated_process_without_repository(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text('import os\nos.getenv("DECLARED")\n')
    write_fragment(project, {"DECLARED": literal_entry()})
    vendor_root = tmp_path / "vendor"
    source_root = Path(__file__).parents[2]
    shutil.copytree(source_root, vendor_root / "shared")
    artifact = project / "build" / "env-contract.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(vendor_root)!r}); "
                "import shared; "
                f"assert shared.__file__.startswith({str(vendor_root)!r}); "
                "from shared.contracts.env_usage import main; "
                "assert 'redis' not in sys.modules; "
                "raise SystemExit(main(['--root', "
                f"{str(project)!r}, '--artifact', {str(artifact)!r}, "
                f"'--commit-sha', {'c' * 40!r}]))"
            ),
        ],
        check=False,
        capture_output=True,
        cwd=project,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(artifact.read_text())["commit_sha"] == "c" * 40


def test_contract_package_exposes_a_vendorable_cli_entrypoint():
    package_config = Path(__file__).parents[2] / "pyproject.toml"

    assert 'env-contract-check = "shared.contracts.env_usage:main"' in package_config.read_text()
