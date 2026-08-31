"""Type system for spec validation.

Defines the formal grammar of types supported in models.yaml:
- Primitives: int, string, bool, float, datetime, uuid
- Complex: list, dict, optional, enum
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

# Primitive types supported by the generator
PRIMITIVE_TYPES = Literal["int", "string", "bool", "float", "datetime", "uuid"]


class PrimitiveType(BaseModel):
    """A primitive type like int, string, bool, etc."""

    type: PRIMITIVE_TYPES


class ListType(BaseModel):
    """A list type with inner element type."""

    type: Literal["list"]
    of: "TypeSpec"  # noqa: UP037 - Forward reference needed for recursion


class DictType(BaseModel):
    """A dictionary type with key and value types."""

    type: Literal["dict"]
    key: "TypeSpec"  # noqa: UP037 - Forward reference needed for recursion
    value: "TypeSpec"  # noqa: UP037 - Forward reference needed for recursion

    @model_validator(mode="after")
    def validate_key_type(self) -> DictType:
        """Ensure dict key is a primitive (typically string)."""
        if not isinstance(self.key, PrimitiveType):
            msg = "Dict key must be a primitive type (typically 'string')"
            raise ValueError(msg)
        return self


class OptionalType(BaseModel):
    """An optional type wrapping another type."""

    type: Literal["optional"]
    of: "TypeSpec"  # noqa: UP037 - Forward reference needed for recursion


class EnumType(BaseModel):
    """An enumeration type with a fixed set of string values."""

    type: Literal["enum"]
    values: list[str]
    default: str | None = None

    @field_validator("values")
    @classmethod
    def validate_values(cls, v: list[str]) -> list[str]:
        """Ensure enum has at least 2 unique values."""
        if len(v) < 2:  # noqa: PLR2004 - Enum must have 2+ values by definition
            msg = "Enum must have at least 2 values"
            raise ValueError(msg)
        if len(v) != len(set(v)):
            msg = "Enum values must be unique"
            raise ValueError(msg)
        # Ensure all values are valid Python identifiers (for Enum class generation)
        for val in v:
            if not val.isidentifier() or val.startswith("_"):
                msg = (
                    f"Enum value '{val}' must be a valid identifier "
                    "(no spaces, no leading underscore)"
                )
                raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def validate_default_in_values(self) -> EnumType:
        """Ensure default is one of the enum values."""
        if self.default is not None and self.default not in self.values:
            msg = f"Default '{self.default}' is not in enum values: {self.values}"
            raise ValueError(msg)
        return self


# TypeSpec is a discriminated union of all type variants
TypeSpec = Annotated[
    PrimitiveType | ListType | DictType | OptionalType | EnumType,
    Field(discriminator="type"),
]

# Rebuild models to resolve forward references
ListType.model_rebuild()
DictType.model_rebuild()
OptionalType.model_rebuild()

# TypeAdapter for parsing the discriminated union
_type_spec_adapter = TypeAdapter(TypeSpec)


T = TypeVar("T")


class TypeRenderer(Protocol[T]):
    """Leaf hooks for rendering a TypeSpec into a value of type T.

    fold_type_spec owns the structural recursion; a renderer only says how to
    render the five variant shapes. Children passed to the composite hooks
    (list/dict/optional) are already folded.
    """

    def primitive(self, name: str) -> T: ...
    def list_of(self, item: T) -> T: ...
    def dict_of(self, key: T, value: T) -> T: ...
    def optional_of(self, inner: T) -> T: ...
    def enum_of(self, values: list[str], default: str | None) -> T: ...


def fold_type_spec(spec: TypeSpec, renderer: TypeRenderer[T]) -> T:
    """Fold a TypeSpec into a value of type T using renderer's leaf hooks.

    This is the single structural traversal of the TypeSpec union. Adding a new
    variant means extending this fold and the TypeRenderer protocol once, instead
    of patching every converter.
    """
    if isinstance(spec, PrimitiveType):
        return renderer.primitive(spec.type)
    if isinstance(spec, ListType):
        return renderer.list_of(fold_type_spec(spec.of, renderer))
    if isinstance(spec, DictType):
        return renderer.dict_of(
            fold_type_spec(spec.key, renderer),
            fold_type_spec(spec.value, renderer),
        )
    if isinstance(spec, OptionalType):
        return renderer.optional_of(fold_type_spec(spec.of, renderer))
    if isinstance(spec, EnumType):
        return renderer.enum_of(spec.values, spec.default)

    msg = f"Unknown type spec: {spec}"
    raise ValueError(msg)


class _PythonRenderer:
    """Render a TypeSpec as a Python type annotation string."""

    _PRIMITIVES = {
        "int": "int",
        "string": "str",
        "bool": "bool",
        "float": "float",
        "datetime": "AwareDatetime",
        "uuid": "UUID",
    }

    def primitive(self, name: str) -> str:
        return self._PRIMITIVES[name]

    def list_of(self, item: str) -> str:
        return f"list[{item}]"

    def dict_of(self, key: str, value: str) -> str:
        return f"dict[{key}, {value}]"

    def optional_of(self, inner: str) -> str:
        return f"{inner} | None"

    def enum_of(self, values: list[str], default: str | None) -> str:
        # Enum types are generated as separate classes, referenced by name.
        # The actual class name is added by the generator; "str" is a placeholder.
        return "str"


class _JsonSchemaRenderer:
    """Render a TypeSpec as a JSON Schema fragment."""

    _PRIMITIVES: dict[str, dict[str, Any]] = {
        "int": {"type": "integer"},
        "string": {"type": "string"},
        "bool": {"type": "boolean"},
        "float": {"type": "number"},
        "datetime": {"type": "string", "format": "date-time"},
        "uuid": {"type": "string", "format": "uuid"},
    }

    def primitive(self, name: str) -> dict[str, Any]:
        # Copy so callers can mutate the result without touching the shared table.
        return dict(self._PRIMITIVES[name])

    def list_of(self, item: dict[str, Any]) -> dict[str, Any]:
        return {"type": "array", "items": item}

    def dict_of(self, key: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": value}

    def optional_of(self, inner: dict[str, Any]) -> dict[str, Any]:
        # JSON Schema 2020-12 / OpenAPI 3.1 nullable form (the 3.0 `nullable`
        # keyword was removed). Matches what Pydantic v2 / FastAPI emit for X | None.
        return {"anyOf": [inner, {"type": "null"}]}

    def enum_of(self, values: list[str], default: str | None) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string", "enum": values}
        if default is not None:
            schema["default"] = default
        return schema


class _TypeScriptRenderer:
    """Render a TypeSpec as a TypeScript type string."""

    _PRIMITIVES = {
        "int": "number",
        "float": "number",
        "string": "string",
        "bool": "boolean",
        "datetime": "string",  # ISO datetime string
        "uuid": "string",
    }

    def primitive(self, name: str) -> str:
        return self._PRIMITIVES[name]

    def list_of(self, item: str) -> str:
        return f"{item}[]"

    def dict_of(self, key: str, value: str) -> str:
        return f"Record<{key}, {value}>"

    def optional_of(self, inner: str) -> str:
        return f"{inner} | null"

    def enum_of(self, values: list[str], default: str | None) -> str:
        # Inline union of string literals
        return " | ".join(f'"{v}"' for v in values)


_PYTHON_RENDERER = _PythonRenderer()
_JSON_SCHEMA_RENDERER = _JsonSchemaRenderer()
_TYPESCRIPT_RENDERER = _TypeScriptRenderer()


def type_spec_to_python(spec: TypeSpec) -> str:
    """Convert TypeSpec to a Python type annotation string."""
    return fold_type_spec(spec, _PYTHON_RENDERER)


def type_spec_to_json_schema(spec: TypeSpec) -> dict[str, Any]:
    """Convert TypeSpec to a JSON Schema fragment."""
    return fold_type_spec(spec, _JSON_SCHEMA_RENDERER)


def type_spec_to_typescript(spec: TypeSpec) -> str:
    """Convert TypeSpec to a TypeScript type string."""
    return fold_type_spec(spec, _TYPESCRIPT_RENDERER)


def parse_type_spec(data: dict[str, Any] | str) -> TypeSpec:
    """Parse raw YAML data into a TypeSpec.

    Handles both dict format and shorthand string format.
    """
    # Shorthand: just a string like "int" or "string"
    if isinstance(data, str):
        if data in ("int", "string", "bool", "float", "datetime", "uuid"):
            return PrimitiveType(type=data)  # type: ignore[arg-type]

        # Parse list shorthand.
        if data.startswith("list[") and data.endswith("]"):
            inner = data[5:-1]
            return ListType(type="list", of=parse_type_spec(inner))

        # Support dict[key,value] shorthand
        if data.startswith("dict[") and data.endswith("]"):
            inner = data[5:-1]
            parts = inner.split(",", 1)
            if len(parts) == 2:  # noqa: PLR2004
                key_type, value_type = parts[0].strip(), parts[1].strip()
                return DictType(
                    type="dict",
                    key=parse_type_spec(key_type),
                    value=parse_type_spec(value_type),
                )

        msg = f"Unknown type shorthand: '{data}'"
        raise ValueError(msg)

    # Full dict format
    type_name = data.get("type")
    if type_name is None:
        msg = "Field spec must have a 'type' key"
        raise ValueError(msg)

    # Use Pydantic's discriminated union to parse
    return _type_spec_adapter.validate_python(data)
