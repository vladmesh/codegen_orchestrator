"""Unit tests for deterministic environment usage extraction and gates."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from shared.contracts.env_usage import (
    build_env_contract_artifact,
    check_env_contract_usage,
    extract_env_references,
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


def test_compose_references_include_interpolation_not_literals(tmp_path: Path):
    (tmp_path / "compose.yaml").write_text(
        "services:\n  app:\n    image: ${IMAGE_TAG:-latest}\n    command: fixed\n"
    )

    references = extract_env_references(tmp_path)

    assert [(reference.key, reference.source) for reference in references] == [
        ("IMAGE_TAG", "compose")
    ]


def test_workflow_references_include_env_and_secrets_forwarding(tmp_path: Path):
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """jobs:
  verify:
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
        ("BUILD_MODE", "workflow"),
    }


def test_shell_entrypoint_references_include_expansion_not_positional_args(tmp_path: Path):
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text("#!/bin/sh\necho ${DATABASE_URL:-missing} $LOG_LEVEL $1\n")

    references = extract_env_references(tmp_path)

    assert {(reference.key, reference.source) for reference in references} == {
        ("DATABASE_URL", "shell"),
        ("LOG_LEVEL", "shell"),
    }


def test_undeclared_usage_is_an_error_with_location(tmp_path: Path):
    (tmp_path / "app.py").write_text('import os\nos.getenv("MISSING_KEY")\n')
    write_fragment(tmp_path, {})

    result = check_env_contract_usage(tmp_path)

    assert result.errors == ("undeclared environment key MISSING_KEY used at app.py:2 (python)",)


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


def test_contract_package_exposes_a_vendorable_cli_entrypoint():
    package_config = Path(__file__).parents[2] / "pyproject.toml"

    assert 'env-contract-check = "shared.contracts.env_usage:main"' in package_config.read_text()
