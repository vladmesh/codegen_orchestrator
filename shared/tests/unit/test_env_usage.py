"""Unit tests for deterministic environment usage extraction and gates."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from scripts.template_pin import TEMPLATE_PIN
from shared.contracts.env_usage import (
    EnvUsageParseError,
    build_env_contract_artifact,
    check_env_contract_usage,
    extract_env_references,
    main,
)

REPO_ROOT = Path(__file__).parents[3]
FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
GENERATED_FIXTURE_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})


def pinned_template_ref() -> str:
    """Return the template ref the orchestrator actually deploys with."""
    return TEMPLATE_PIN.ref


def template_fixture() -> Path:
    """Return the rendered fixture for the pinned template ref."""
    return TEMPLATE_PIN.fixture_path(REPO_ROOT)


def fixture_tree_digest(root: Path) -> str:
    """Return a stable digest of every generated fixture path and its content."""
    digest = sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or GENERATED_FIXTURE_CACHE_DIRS.intersection(path.parts):
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


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


def test_compose_ignores_comments_and_dollar_escapes(tmp_path: Path):
    (tmp_path / "compose.yaml").write_text(
        "# stale: ${COMMENT_ONLY}\n"
        "services:\n"
        "  app:\n"
        '    command: sh -c "echo $${LITERAL} ${REAL_VALUE:-ok}"\n'
    )

    references = extract_env_references(tmp_path)

    assert [(reference.key, reference.source) for reference in references] == [
        ("REAL_VALUE", "compose")
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


def test_workflow_references_include_env_and_secret_forwarding(tmp_path: Path):
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

    assert {(reference.key, reference.source) for reference in references} == {
        ("DEPLOY_TOKEN", "workflow"),
        ("REGISTRY_PASSWORD", "workflow"),
    }


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


def test_shell_ignores_quotes_comments_and_non_shell_shebang(tmp_path: Path):
    shell = tmp_path / "scripts" / "backup.sh"
    shell.parent.mkdir()
    shell.write_text(
        "awk '{print $NF}' input\n"
        "echo 'literal $NOT_AN_ENV_VAR'\n"
        'echo "don\'t drop $REAL"\n'
        "# docs mention $COMMENTED_ONLY\n"
        "n=$(($RANDOM % 5))\n"
    )
    javascript = tmp_path / "tools" / "gen.js"
    javascript.parent.mkdir()
    javascript.write_text("#!/usr/bin/env node\nconst value = `${userName}`\n")

    references = extract_env_references(tmp_path)

    assert {(reference.key, reference.source) for reference in references} == {
        ("REAL", "shell"),
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
        ("DEPLOY_SSH_KEY", "workflow"),
        ("PORT", "shell"),
        ("REGISTRY_PASSWORD", "workflow"),
    }


def test_template_fixture_tracks_the_pinned_template_ref():
    """The fixture's Copier record must identify the production template revision."""
    fixture = template_fixture()

    # Every directory, not only the ones named after the pinned template: a move to
    # another template renames the fixture, and the render it replaces has to go.
    stale = sorted(
        path.name for path in FIXTURES_DIR.iterdir() if path.is_dir() and path != fixture
    )
    assert fixture.is_dir(), (
        f"no fixture for pinned template ref {pinned_template_ref()}; found {stale}"
    )
    assert not stale, f"fixtures left behind for unpinned template revisions: {stale}"
    answers = yaml.safe_load((fixture / ".copier-answers.yml").read_text())
    assert answers["_src_path"] == TEMPLATE_PIN.source
    # The pin is the kit's release tag, and the tag is reachable in Copier's clone, so
    # what Copier records is the pinned ref itself.
    assert answers["_commit"] == pinned_template_ref()


def test_template_fixture_pins_verified_uv_bootstrap():
    """The production render must not retain setup-uv's mutable manifest lookup.

    The kit ships no embedded framework mirror, so its CI workflow is the only place
    the bootstrap is pinned: every `setup-uv` step names an action commit, a uv version
    and the checksum that makes the download verified instead of looked up.
    """
    action = "astral-sh/setup-uv@6ee6290f1cbc4156c0bdd66691b2c144ef8df19a"
    version = "0.11.29"
    checksum = "04f8b82f5d47f0512dcd32c67a4a6f16a0ea27c81537c338fd0ad6b23cebe829"
    fixture = template_fixture()
    workflows = sorted((fixture / ".github" / "workflows").glob("*.yml"))
    steps = [
        step
        for workflow in workflows
        for job in yaml.safe_load(workflow.read_text())["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]

    assert not (fixture / ".framework").exists()
    assert steps, f"the render bootstraps uv nowhere in {[w.name for w in workflows]}"
    for step in steps:
        assert step["uses"] == action
        assert step["with"]["version"] == version
        assert step["with"]["checksum"] == checksum


def test_template_fixture_content_matches_its_pinned_render():
    """A renamed fixture must not be able to pass provenance with stale rendered files."""
    fixture = template_fixture()
    answers = yaml.safe_load((fixture / ".copier-answers.yml").read_text())

    assert answers == {
        "_commit": TEMPLATE_PIN.ref,
        "_src_path": TEMPLATE_PIN.source,
        "author_email": "dev@example.com",
        "author_name": "Developer",
        "modules": "backend,tg_bot",
        "project_description": "A product generated with codegen-product-kit",
        "project_name": "env_fixture",
        "python_version": "3.12",
        "task_description": "",
    }
    assert (
        fixture_tree_digest(fixture)
        == "2add3efc3be54afa3179810280a77fe9510178758532931d511e3f0ff1002b1d"
    )


def test_template_fixture_contains_the_0_6_runtime_boundaries():
    fixture = template_fixture()
    packages = (fixture / "codegen_kit/packages.py").read_text()
    database = (fixture / "codegen_kit/database.py").read_text()
    migrations = (fixture / "codegen_kit/migrations.py").read_text()
    settings = (fixture / "services/backend/src/controllers/settings.py").read_text()

    assert 'CORE_VERSION = "2.0.0"' in packages
    assert "await package.runtime.startup(application)" in packages
    assert "class SettingSeedPackage(Protocol):" in packages
    assert "def owned_package_database(caller_path: Path)" in database
    assert 'SET LOCAL search_path TO "{self._schema}", public' in database
    assert 'config.attributes["version_table_schema"] = schema' in migrations
    assert "await seed.seed_setting(session, payload.key, payload.value)" in settings


def test_template_fixture_extracts_without_crashing(tmp_path: Path):
    shutil.copytree(template_fixture(), tmp_path, dirs_exist_ok=True)

    references = extract_env_references(tmp_path)

    assert {
        ("DOTENV", "workflow"),
        ("REGISTRY_URL", "workflow"),
        ("REGISTRY_USER", "workflow"),
        ("REGISTRY_PASSWORD", "workflow"),
        ("DEPLOY_HOST", "workflow"),
        ("DEPLOY_PORT", "workflow"),
        ("DEPLOY_SSH_KEY", "workflow"),
        ("DEPLOY_USER", "workflow"),
        ("POSTGRES_USER", "compose"),
        ("DATABASE_URL", "shell"),
        ("SQLALCHEMY_SYNC_DRIVER", "python-settings"),
    }.issubset({(reference.key, reference.source) for reference in references})


def test_template_fixture_has_known_contract_gaps(tmp_path: Path):
    """What the pinned render is allowed to leave undeclared — with the kit, nothing."""
    shutil.copytree(template_fixture(), tmp_path, dirs_exist_ok=True)

    result = check_env_contract_usage(tmp_path)

    # The kit render carries no bundled framework tooling, so nothing in it reads a
    # key outside the generated deployment contract and there is no gap left to
    # accept. BACKEND_API_URL stays a warning: it is injected at deployment time.
    undeclared = {message.split()[3] for message in result.errors}
    assert undeclared == set()
    assert result.warnings == (
        "required environment contract key BACKEND_API_URL was not observed",
    )


def test_shell_undeclared_usage_is_a_warning(tmp_path: Path):
    (tmp_path / "entrypoint.sh").write_text("echo $MISSING_KEY\n")
    write_fragment(tmp_path, {})

    result = check_env_contract_usage(tmp_path)

    assert result.errors == ()
    assert result.warnings == (
        "undeclared environment key MISSING_KEY used at entrypoint.sh:1 (shell)",
    )


def test_cli_publishes_artifact_with_visible_shell_warning(tmp_path: Path, capsys):
    (tmp_path / "entrypoint.sh").write_text("echo $MISSING_KEY\n")
    write_fragment(tmp_path, {})
    artifact = tmp_path / "artifact.json"

    exit_code = main(
        ["--root", str(tmp_path), "--artifact", str(artifact), "--commit-sha", "a" * 40]
    )

    assert exit_code == 0
    assert artifact.exists()
    assert "warning summary: 1" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("path", "contents", "error"),
    [
        ("compose.yml", "services: [\n", "could not parse YAML file compose.yml"),
        ("app.py", "def broken(:\n", "could not parse Python file app.py"),
    ],
)
def test_invalid_source_syntax_fails_the_gate(tmp_path: Path, path: str, contents: str, error: str):
    (tmp_path / path).write_text(contents)
    write_fragment(tmp_path, {})

    with pytest.raises(EnvUsageParseError, match=error):
        check_env_contract_usage(tmp_path)


def test_workflow_reference_does_not_observe_runtime_contract_key(tmp_path: Path):
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs:\n  ci:\n    env:\n      APP_TOKEN: ${{ secrets.APP_TOKEN }}\n")
    write_fragment(
        tmp_path,
        {
            "APP_TOKEN": {
                "source": "user_secret",
                "environments": "[production]",
                "consumers": "[backend]",
                "description": "application token",
                "required": "true",
            }
        },
    )

    result = check_env_contract_usage(tmp_path)

    assert result.errors == ()
    assert result.warnings == ("required environment contract key APP_TOKEN was not observed",)


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
        # `shared` is never installed, so the child needs the repo tree on its path.
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
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
