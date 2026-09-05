"""Typed, fail-closed service manifests for core product settings and jobs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, Field, field_validator

META_SCHEMA_URL = "https://json-schema.org/draft/2020-12/schema"

#: Core contracts an optional core-module may declare that it provides. The set is
#: the framework's own list of core capabilities, not a catalogue of modules: a name
#: outside it is refused so a manifest cannot claim an undefined capability.
CORE_CAPABILITIES = frozenset({"jobs.fire"})


def empty_declaration_schema() -> dict[str, Any]:
    """Return the declaration schema of a service that declares nothing."""

    return {
        "$schema": META_SCHEMA_URL,
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


class ServiceManifest(BaseModel):
    """A service-owned declaration of what a product exposes to its operators.

    ``settings_schema`` declares settings users may control. ``jobs_schema``
    declares, in the same fail-closed form, the named behaviours that may be
    fired through the core jobs contract. Both are independent named entries, so
    the supported JSON Schema subset is an object with named properties and no
    cross-property ``required`` constraint. References are deliberately excluded:
    a generated product must not resolve schemas from the network or another
    undeclared source at runtime.

    ``jobs_schema`` and ``provides`` are additive: a ``version: 1`` manifest that
    declares neither stays valid, and ``extra="forbid"`` still refuses anything
    that is not one of these fields.
    """

    version: Literal[1]
    settings_schema: dict[str, Any] = Field(alias="settings_schema")
    jobs_schema: dict[str, Any] = Field(
        alias="jobs_schema", default_factory=empty_declaration_schema
    )
    provides: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid", "populate_by_name": True}

    @field_validator("settings_schema")
    @classmethod
    def validate_settings_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        return _validate_declaration_schema(schema, "settings_schema")

    @field_validator("jobs_schema")
    @classmethod
    def validate_jobs_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        _validate_declaration_schema(schema, "jobs_schema")
        for name, arguments in schema["properties"].items():
            if arguments.get("type") != "object":
                msg = f"jobs_schema.properties.{name}.type must be 'object'"
                raise ValueError(msg)
            if arguments.get("additionalProperties") is not False:
                msg = f"jobs_schema.properties.{name}.additionalProperties must be false"
                raise ValueError(msg)
        return schema

    @field_validator("provides")
    @classmethod
    def validate_provides(cls, provided: list[str]) -> list[str]:
        if len(set(provided)) != len(provided):
            msg = "provides must not repeat a capability"
            raise ValueError(msg)
        unknown = sorted(name for name in provided if name not in CORE_CAPABILITIES)
        if unknown:
            known = ", ".join(sorted(CORE_CAPABILITIES))
            msg = (
                f"provides declares unknown capability {unknown[0]!r}; "
                f"known capabilities: {known}"
            )
            raise ValueError(msg)
        return provided


def _validate_declaration_schema(schema: dict[str, Any], field: str) -> dict[str, Any]:
    """Validate one fail-closed, reference-free declaration schema."""

    if schema.get("$schema") != META_SCHEMA_URL:
        msg = f"{field}.$schema must be the Draft 2020-12 meta-schema URL"
        raise ValueError(msg)
    if schema.get("type") != "object":
        msg = f"{field}.type must be 'object'"
        raise ValueError(msg)
    if schema.get("additionalProperties") is not False:
        msg = f"{field}.additionalProperties must be false"
        raise ValueError(msg)
    if "required" in schema:
        msg = f"{field}.required is unsupported; entries are declared independently"
        raise ValueError(msg)

    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        msg = f"{field}.properties must be an object"
        raise ValueError(msg)
    if any(not isinstance(key, str) or not key for key in properties):
        msg = f"{field}.properties keys must be non-empty strings"
        raise ValueError(msg)
    if any(not isinstance(value, Mapping) for value in properties.values()):
        msg = f"{field}.properties values must be object schemas"
        raise ValueError(msg)
    if _contains_ref(schema):
        msg = f"{field} does not support $ref"
        raise ValueError(msg)

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValueError(f"invalid {field}: {error.message}") from error
    return schema


def _contains_ref(value: object) -> bool:
    if isinstance(value, Mapping):
        return "$ref" in value or any(_contains_ref(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_ref(item) for item in value)
    return False


def parse_service_manifest(data: dict[str, Any]) -> ServiceManifest:
    """Validate raw YAML data as a versioned service manifest."""

    return ServiceManifest.model_validate(data)
