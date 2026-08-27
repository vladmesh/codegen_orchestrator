"""Compare the admin's TypeScript declarations to authoritative Pydantic schemas."""

from collections.abc import Callable
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel
import pytest

from shared.contracts.dto.admin_overview import AdminOverviewResponse
from shared.contracts.dto.executor_diagnostics import (
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.dto.work_admission import (
    ExecutorDiagnosticConfirmationCommand,
    PaidWorkControlsCommand,
)
from src.schemas.agent_config import AgentConfigRead, AgentConfigUpdate
from src.schemas.project import ProjectRead
from src.schemas.system_config import SystemConfigRead, SystemConfigUpdate
from src.schemas.task import TaskRead
from src.schemas.user import UserRead

FRONTEND_TYPES = Path(__file__).resolve().parents[4] / "services/admin-frontend/src/types/api.ts"


def _matching_brace(source: str, opening: int) -> int:
    depth = 0
    for position in range(opening, len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return position
    raise AssertionError("unclosed TypeScript declaration")


def _top_level_parts(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    for position, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "<{(":
            depth += 1
        elif char in ">})":
            depth -= 1
        elif depth == 0 and value.startswith(separator, position):
            parts.append(value[start:position].strip())
            start = position + len(separator)
    parts.append(value[start:].strip())
    return parts


def _typescript_declarations(
    source: str,
) -> tuple[dict[str, dict[str, tuple[bool, str]]], dict[str, str]]:
    interfaces: dict[str, dict[str, tuple[bool, str]]] = {}
    for match in re.finditer(r"export interface (\w+)\s*\{", source):
        name = match.group(1)
        opening = source.index("{", match.start())
        body = source[opening + 1 : _matching_brace(source, opening)]
        fields: dict[str, tuple[bool, str]] = {}
        starts = list(re.finditer(r"(?m)^  (\w+)(\?)?:", body))
        for index, field_match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
            fields[field_match.group(1)] = (
                field_match.group(2) is None,
                body[field_match.end() : end].strip().rstrip(";"),
            )
        interfaces[name] = fields

    aliases: dict[str, str] = {}
    for match in re.finditer(r"export type (\w+)\s*=", source):
        endings = [
            position
            for position in (
                source.find("\n\n", match.end()),
                source.find("\nexport ", match.end()),
            )
            if position != -1
        ]
        end = min(endings, default=len(source))
        aliases[match.group(1)] = source[match.end() : end].strip().lstrip("|").strip()
    return interfaces, aliases


def _with_nullable(shape: dict[str, Any]) -> dict[str, Any]:
    return {**shape, "nullable": True}


def _pydantic_shape(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        return _pydantic_shape(root["$defs"][schema["$ref"].rsplit("/", 1)[-1]], root)
    if "anyOf" in schema:
        variants = schema["anyOf"]
        non_null = [variant for variant in variants if variant.get("type") != "null"]
        if len(non_null) == 1 and len(non_null) != len(variants):
            return _with_nullable(_pydantic_shape(non_null[0], root))
        return {
            "kind": "union",
            "variants": tuple(_pydantic_shape(item, root) for item in variants),
        }
    if "enum" in schema:
        return {"kind": "enum", "values": tuple(sorted(schema["enum"])), "nullable": False}
    if "const" in schema:
        return {"kind": "enum", "values": (schema["const"],), "nullable": False}
    if "properties" in schema or (
        schema.get("type") == "object" and "additionalProperties" not in schema
    ):
        required = set(schema.get("required", []))
        return {
            "kind": "object",
            "fields": {
                name: {"required": name in required, "shape": _pydantic_shape(value, root)}
                for name, value in schema.get("properties", {}).items()
            },
            "nullable": False,
        }
    if schema.get("type") == "array":
        return {"kind": "array", "items": _pydantic_shape(schema["items"], root), "nullable": False}
    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        return {
            "kind": "map",
            "values": _pydantic_shape(additional, root)
            if isinstance(additional, dict)
            else {"kind": "unknown", "nullable": False},
            "nullable": False,
        }
    scalar = schema.get("type")
    if scalar in {"integer", "number"}:
        scalar = "number"
    return {"kind": scalar or "unknown", "nullable": False}


def _typescript_shape_factory(source: str) -> Callable[[str], dict[str, Any]]:
    interfaces, aliases = _typescript_declarations(source)

    def shape(value: str, resolving: set[str] | None = None) -> dict[str, Any]:
        value = value.strip().rstrip(";")
        resolving = resolving or set()
        members = [member for member in _top_level_parts(value, "|") if member]
        if len(members) > 1:
            non_null = [member for member in members if member != "null"]
            if len(non_null) == 1 and len(non_null) != len(members):
                return _with_nullable(shape(non_null[0], resolving))
            literals = [member[1:-1] for member in members if re.fullmatch(r"'[^']*'", member)]
            if len(literals) == len(members):
                return {"kind": "enum", "values": tuple(sorted(literals)), "nullable": False}
            return {
                "kind": "union",
                "variants": tuple(shape(member, resolving) for member in members),
            }
        if value.endswith("[]"):
            return {"kind": "array", "items": shape(value[:-2], resolving), "nullable": False}
        if value.startswith("Partial<Record<") and value.endswith(">"):
            value = value[len("Partial<") : -1]
        if value.startswith("Record<") and value.endswith(">"):
            key_value = _top_level_parts(value[len("Record<") : -1], ",")
            assert len(key_value) == 2, f"cannot parse map {value}"
            return {"kind": "map", "values": shape(key_value[1], resolving), "nullable": False}
        if value in {"string", "number", "boolean", "unknown"}:
            return {"kind": value, "nullable": False}
        if re.fullmatch(r"'[^']*'", value):
            return {"kind": "enum", "values": (value[1:-1],), "nullable": False}
        indexed = re.fullmatch(r"(\w+)\['(\w+)'\]", value)
        if indexed:
            return shape(interfaces[indexed.group(1)][indexed.group(2)][1], resolving)
        if value in resolving:
            raise AssertionError(f"recursive TypeScript alias {value} is not supported")
        if value in interfaces:
            return {
                "kind": "object",
                "fields": {
                    field: {"required": required, "shape": shape(annotation, resolving | {value})}
                    for field, (required, annotation) in interfaces[value].items()
                },
                "nullable": False,
            }
        if value in aliases:
            return shape(aliases[value], resolving | {value})
        raise AssertionError(f"unknown TypeScript declaration {value}")

    return shape


def _assert_contract(source: str, frontend_name: str, model: type[BaseModel]) -> None:
    server_schema = model.model_json_schema()
    expected = _pydantic_shape(server_schema, server_schema)
    actual = _typescript_shape_factory(source)(frontend_name)
    assert actual == expected, (
        f"{frontend_name} drifted from {model.__name__}:\nexpected={expected}\nactual={actual}"
    )


def test_admin_frontend_contract_gate_matches_recursive_response_and_request_shapes():
    source = FRONTEND_TYPES.read_text()
    for frontend_name, model in (
        ("AdminOverview", AdminOverviewResponse),
        ("User", UserRead),
        ("Project", ProjectRead),
        ("Task", TaskRead),
        ("SystemConfig", SystemConfigRead),
        ("SystemConfigUpdate", SystemConfigUpdate),
        ("AgentConfig", AgentConfigRead),
        ("AgentConfigUpdate", AgentConfigUpdate),
        ("PaidWorkControls", PaidWorkControlsCommand),
        ("ExecutorDiagnostic", ExecutorDiagnostic),
        ("ExecutorDiagnosticSnapshot", ExecutorDiagnosticSnapshot),
        ("ExecutorDiagnosticConfirmationCommand", ExecutorDiagnosticConfirmationCommand),
    ):
        _assert_contract(source, frontend_name, model)


def test_contract_gate_demonstrably_rejects_obsolete_or_nested_drift():
    source = FRONTEND_TYPES.read_text()
    cases = (
        ("Project", ProjectRead, source.replace("title: string", "name: string", 1)),
        (
            "AdminOverview",
            AdminOverviewResponse,
            source.replace("DebugQueuesResponse", "QueueHealth", 1),
        ),
        (
            "Project",
            ProjectRead,
            re.sub(
                r"(export interface Project \{.*?updated_at)\?: string \| null",
                r"\1?: string",
                source,
                count=1,
                flags=re.DOTALL,
            ),
        ),
        ("ExecutorDiagnostic", ExecutorDiagnostic, source.replace("'unknown'", "'offline'", 1)),
        (
            "PaidWorkControls",
            PaidWorkControlsCommand,
            source.replace(
                "max_concurrent_paid_runs: number", "max_concurrent_paid_runs: string", 1
            ),
        ),
    )
    for frontend_name, model, changed in cases:
        with pytest.raises(AssertionError):
            _assert_contract(changed, frontend_name, model)
