"""Deterministic extraction and validation of environment contract usage."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from pydantic import ValidationError
import yaml

from shared.contracts.env_contract import (
    CanonicalEnvContract,
    EnvContractFragment,
    merge_env_contract_fragments,
    validate_env_contract_fragment,
)
from shared.diagnostics import redact_diagnostic, safe_validation_errors

_ENV_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_SHELL_REFERENCE = re.compile(r"\$(?:\{(" + _ENV_NAME + r")(?:[}:?+-])|(" + _ENV_NAME + r"))")
_SHELL_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:export|local|readonly|declare)\s+)?(" + _ENV_NAME + r")="
)
_SHELL_READ = re.compile(r"\bread(?:\s+-[A-Za-z]+)*\s+(" + _ENV_NAME + r")\b")
_SHELL_BUILTINS = {
    "BASH_SOURCE",
    "HOME",
    "LOGNAME",
    "OLDPWD",
    "PATH",
    "PWD",
    "PYTHONPATH",
    "RANDOM",
    "SHELL",
    "USER",
}
_GITHUB_BUILTIN_SECRETS = {"GITHUB_TOKEN"}


@dataclass(frozen=True, order=True)
class EnvReference:
    """A statically observable read or forwarding of an environment key."""

    key: str
    path: str
    line: int
    source: str

    @property
    def location(self) -> str:
        """Return a stable human-readable source location."""
        return f"{self.path}:{self.line}"


@dataclass(frozen=True)
class EnvUsageCheck:
    """The gate output, separated into failing errors and MVP warnings."""

    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    contract: CanonicalEnvContract
    references: tuple[EnvReference, ...]


class EnvContractUsageError(ValueError):
    """Raised when static environment usage is absent from the contract."""


class EnvUsageParseError(ValueError):
    """Raised when a supported source file cannot be parsed safely."""


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _literal_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _attribute_name(node: ast.expr) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _field_alias(node: ast.expr | None) -> str | None:
    if not isinstance(node, ast.Call) or _attribute_name(node.func) not in {
        "Field",
        "pydantic.Field",
    }:
        return None
    for keyword in node.keywords:
        if keyword.arg in {"alias", "validation_alias"}:
            return _literal_string(keyword.value)
    return None


def _is_settings_class(node: ast.ClassDef) -> bool:
    return any(
        (name := _attribute_name(base)) is not None
        and name.split(".")[-1] in {"BaseSettings", "PydanticBaseSettings"}
        for base in node.bases
    )


def _settings_env_prefix(node: ast.ClassDef) -> str:
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        is_model_config = any(
            isinstance(target, ast.Name) and target.id == "model_config" for target in targets
        )
        if not is_model_config:
            continue
        if not isinstance(value, ast.Call) or _attribute_name(value.func) != "SettingsConfigDict":
            continue
        for keyword in value.keywords:
            if keyword.arg == "env_prefix":
                return _literal_string(keyword.value) or ""
    return ""


def _settings_references(node: ast.ClassDef, relative_path: str) -> list[EnvReference]:
    references: list[EnvReference] = []
    env_prefix = _settings_env_prefix(node)
    for statement in node.body:
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        if statement.target.id.startswith("_") or statement.target.id == "model_config":
            continue
        key = _field_alias(statement.value) or f"{env_prefix}{statement.target.id.upper()}"
        references.append(EnvReference(key, relative_path, statement.lineno, "python-settings"))
    return references


def _python_references(root: Path, path: Path) -> list[EnvReference]:
    relative_path = _relative_path(root, path)
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (OSError, UnicodeDecodeError) as error:
        raise EnvUsageParseError(f"could not read Python file {relative_path}") from error
    except SyntaxError as error:
        location = f":{error.lineno}" if error.lineno else ""
        raise EnvUsageParseError(
            f"could not parse Python file {relative_path}{location}"
        ) from error

    references: list[EnvReference] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            name = _attribute_name(node.func)
            key = _literal_string(node.args[0])
            if key and name in {"os.getenv", "os.environ.get", "os.environ.setdefault"}:
                references.append(EnvReference(key, relative_path, node.lineno, "python"))
        elif isinstance(node, ast.Subscript) and _attribute_name(node.value) == "os.environ":
            key = _literal_string(node.slice)
            if key:
                references.append(EnvReference(key, relative_path, node.lineno, "python"))
        elif isinstance(node, ast.ClassDef) and _is_settings_class(node):
            references.extend(_settings_references(node, relative_path))

    return references


def _interpolation_references(value: str) -> list[str]:
    """Return Compose interpolation names, honoring Compose's $$ literal escape."""
    references: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "$":
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == "$":
            index += 2
            continue
        start = index + 2 if index + 1 < len(value) and value[index + 1] == "{" else index + 1
        end = start
        if start >= len(value) or not (value[start].isalpha() or value[start] == "_"):
            index += 1
            continue
        while end < len(value) and (value[end].isalnum() or value[end] == "_"):
            end += 1
        if (
            index + 1 < len(value)
            and value[index + 1] == "{"
            and (end >= len(value) or value[end] not in "}:?+-")
        ):
            index += 1
            continue
        references.append(value[start:end])
        index = end
    return references


def _yaml_root(root: Path, path: Path) -> yaml.Node:
    relative_path = _relative_path(root, path)
    try:
        return yaml.compose(path.read_text())
    except (OSError, UnicodeDecodeError) as error:
        raise EnvUsageParseError(f"could not read YAML file {relative_path}") from error
    except yaml.YAMLError as error:
        raise EnvUsageParseError(f"could not parse YAML file {relative_path}") from error


def _scalar_nodes(node: yaml.Node | None) -> Iterable[yaml.ScalarNode]:
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            yield from _scalar_nodes(child)
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _scalar_nodes(key)
            yield from _scalar_nodes(value)


def _compose_references(root: Path, path: Path) -> list[EnvReference]:
    node = _yaml_root(root, path)
    relative_path = _relative_path(root, path)
    return [
        EnvReference(key, relative_path, scalar.start_mark.line + 1, "compose")
        for scalar in _scalar_nodes(node)
        for key in _interpolation_references(scalar.value)
    ]


def _workflow_references(root: Path, path: Path) -> list[EnvReference]:
    node = _yaml_root(root, path)
    relative_path = _relative_path(root, path)
    references: list[EnvReference] = []
    for scalar in _scalar_nodes(node):
        for match in re.finditer(r"\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)", scalar.value):
            key = match.group(1)
            if key not in _GITHUB_BUILTIN_SECRETS:
                references.append(
                    EnvReference(key, relative_path, scalar.start_mark.line + 1, "workflow")
                )
    return references


def _shell_references(root: Path, path: Path) -> list[EnvReference]:
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    local_names: set[str] = set()
    for line in lines:
        assignment = _SHELL_ASSIGNMENT.match(line)
        if assignment:
            assigned_name = assignment.group(1)
            value_references = {
                next((value for value in match.groups() if value is not None), None)
                for match in _SHELL_REFERENCE.finditer(
                    _shell_expandable_text(line[assignment.end() :])
                )
            }
            if assigned_name not in value_references:
                local_names.add(assigned_name)
        read = _SHELL_READ.search(line)
        if read:
            local_names.add(read.group(1))
    relative_path = _relative_path(root, path)
    references: list[EnvReference] = []
    for line_number, line in enumerate(lines, start=1):
        for match in _SHELL_REFERENCE.finditer(_shell_expandable_text(line)):
            key = next((value for value in match.groups() if value is not None), None)
            if key:
                references.append(EnvReference(key, relative_path, line_number, "shell"))
    return [
        reference
        for reference in references
        if reference.key not in local_names and reference.key not in _SHELL_BUILTINS
    ]


def _shell_expandable_text(line: str) -> str:
    """Keep shell text where parameter expansion is valid, excluding comments."""
    characters: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(line):
        character = line[index]
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if quote == '"':
            if character == '"':
                quote = None
            else:
                characters.append(character)
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "#" and (index == 0 or line[index - 1].isspace()):
            break
        else:
            characters.append(character)
        index += 1
    return "".join(characters)


def _is_compose_file(path: Path) -> bool:
    return path.suffix in {".yaml", ".yml"} and (
        path.name.startswith("compose") or path.name.startswith("docker-compose")
    )


def _is_workflow(path: Path) -> bool:
    return ".github/workflows" in path.as_posix() and path.suffix in {".yaml", ".yml"}


def _is_shell_entrypoint(path: Path) -> bool:
    if path.suffix in {".sh", ".bash"}:
        return True
    try:
        with path.open("rb") as file:
            first_line = file.readline().decode(errors="ignore").lower()
            return first_line.startswith("#!") and any(
                shell in first_line for shell in ("/sh", "bash", "zsh", "dash", "ksh")
            )
    except OSError:
        return False


def _project_files(root: Path) -> Iterable[Path]:
    ignored_parts = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not ignored_parts.intersection(path.relative_to(root).parts):
            yield path


def extract_env_references(root: Path) -> tuple[EnvReference, ...]:
    """Extract static environment references from a project tree in stable order."""
    root = root.resolve()
    references: list[EnvReference] = []
    for path in _project_files(root):
        if path.suffix == ".py":
            references.extend(_python_references(root, path))
        elif _is_compose_file(path):
            references.extend(_compose_references(root, path))
        elif _is_workflow(path):
            references.extend(_workflow_references(root, path))
        elif _is_shell_entrypoint(path):
            references.extend(_shell_references(root, path))
    return tuple(sorted(set(references)))


def load_env_contract_fragments(root: Path) -> list[EnvContractFragment]:
    """Load all owner fragments from a generated project in path order."""
    fragments: list[EnvContractFragment] = []
    for path in _project_files(root):
        if path.name != "env.contract.yaml":
            continue
        loaded = yaml.safe_load(path.read_text())
        fragments.append(validate_env_contract_fragment(loaded))
    return fragments


def check_env_contract_usage(root: Path) -> EnvUsageCheck:
    """Compare static usage with fragments, retaining MVP dynamic-key warnings."""
    root = root.resolve()
    references = extract_env_references(root)
    contract = merge_env_contract_fragments(load_env_contract_fragments(root))
    declared = set(contract.entries)
    errors = tuple(
        "undeclared environment key "
        f"{reference.key} used at {reference.location} ({reference.source})"
        for reference in references
        if reference.key not in declared and reference.source != "workflow"
    )
    observed = {reference.key for reference in references if reference.source != "workflow"}
    required_warnings = tuple(
        f"required environment contract key {key} was not observed"
        for key, entry in sorted(contract.entries.items())
        if entry.required and key not in observed
    )
    warnings = required_warnings
    return EnvUsageCheck(errors=errors, warnings=warnings, contract=contract, references=references)


def build_env_contract_artifact(root: Path, commit_sha: str) -> bytes:
    """Build the reproducible commit-bound canonical contract artifact."""
    result = check_env_contract_usage(root)
    return build_env_contract_artifact_from_check(result, commit_sha)


def build_env_contract_artifact_from_check(result: EnvUsageCheck, commit_sha: str) -> bytes:
    """Serialize a previously checked contract without rereading the project tree."""
    if result.errors:
        raise EnvContractUsageError("\n".join(result.errors))
    return json.dumps(
        {"commit_sha": commit_sha, "contract": result.contract.model_dump(mode="json")},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _commit_sha(root: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise OSError("git executable is not available")
    completed = subprocess.run(
        [git, "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _yaml_error_location(error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    if mark is None:
        return ""
    return f" at line {mark.line + 1}, column {mark.column + 1}"


def main(argv: list[str] | None = None) -> int:
    """Run the vendorable CI entrypoint without network access."""
    parser = argparse.ArgumentParser(description="Check and build an environment contract artifact")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--commit-sha")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        result = check_env_contract_usage(root)
    except ValidationError as error:
        print(
            f"environment contract invalid: {safe_validation_errors(error)}",
            file=sys.stderr,
        )
        return 1
    except yaml.YAMLError as error:
        print(
            f"environment contract invalid: malformed YAML{_yaml_error_location(error)}",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as error:
        print(f"environment contract invalid: {redact_diagnostic(error)}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if result.errors:
        print("\n".join(result.errors), file=sys.stderr)
        return 1
    try:
        commit_sha = args.commit_sha or _commit_sha(root)
        artifact = build_env_contract_artifact_from_check(result, commit_sha)
        args.artifact.parent.mkdir(parents=True, exist_ok=True)
        args.artifact.write_bytes(artifact)
    except (OSError, subprocess.CalledProcessError, EnvContractUsageError) as error:
        print(f"environment contract artifact failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
