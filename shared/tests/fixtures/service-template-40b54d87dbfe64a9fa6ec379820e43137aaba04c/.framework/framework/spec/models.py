"""Model specifications for models.yaml validation.

Defines the structure of models.yaml:
- ModelSpec: A single model with fields and variants
- FieldSpec: A single field with type and constraints
- VariantSpec: A variant that modifies the base model
- ModelsSpec: The root container for all models
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from framework.spec.types import (
    EnumType,
    TypeSpec,
    parse_type_spec,
    type_spec_to_json_schema,
)


class FieldSpec(BaseModel):
    """Specification for a single field in a model."""

    type_spec: TypeSpec = Field(..., alias="type_data")

    # Constraints (numeric)
    ge: int | float | None = None
    gt: int | float | None = None
    le: int | float | None = None
    lt: int | float | None = None

    # Constraints (string)
    min_length: int | None = None
    max_length: int | None = None

    # Modifiers
    readonly: bool = False
    optional: bool = False  # Makes field nullable (T | None) in all variants
    default: Any = Field(default=None)

    model_config = {"extra": "forbid"}

    @classmethod
    def from_yaml(cls, data: dict[str, Any] | str) -> FieldSpec:
        """Create FieldSpec from raw YAML dict."""
        # Handle shorthand (just "int")
        if isinstance(data, str):
            return cls(type_data=parse_type_spec(data))

        # Extract type and parse it
        type_data = data.get("type")
        if type_data is None:
            msg = "Field must have a 'type' key"
            raise ValueError(msg)

        type_spec = parse_type_spec(type_data)

        # Build remaining fields
        return cls(
            type_data=type_spec,
            ge=data.get("ge"),
            gt=data.get("gt"),
            le=data.get("le"),
            lt=data.get("lt"),
            min_length=data.get("min_length"),
            max_length=data.get("max_length"),
            readonly=data.get("readonly", False),
            optional=data.get("optional", False),
            default=data.get("default"),
        )

    def to_json_schema(self) -> dict[str, Any]:
        """Convert field spec to JSON Schema."""
        schema = type_spec_to_json_schema(self.type_spec)

        # Numeric/string constraints describe the value type. If the type itself
        # is already nullable (anyOf with a null branch), attach them to the typed
        # branch rather than alongside anyOf.
        typed = schema["anyOf"][0] if "anyOf" in schema else schema
        if self.ge is not None:
            typed["minimum"] = self.ge
        if self.gt is not None:
            typed["exclusiveMinimum"] = self.gt
        if self.le is not None:
            typed["maximum"] = self.le
        if self.lt is not None:
            typed["exclusiveMaximum"] = self.lt
        if self.min_length is not None:
            typed["minLength"] = self.min_length
        if self.max_length is not None:
            typed["maxLength"] = self.max_length

        # Field-level optional (no default) adds a null branch unless the type
        # already carries one. Same anyOf form as the canonical type converter.
        if self.optional and self.default is None and "anyOf" not in schema:
            schema = {"anyOf": [schema, {"type": "null"}]}

        # Annotations live at field (top) level, beside anyOf when present.
        if self.default is not None:
            schema["default"] = self.default
        if self.readonly:
            schema["readOnly"] = True

        return schema

    @property
    def is_required(self) -> bool:
        """Whether the field must be provided: no default and not field-level optional.

        Variant-level optional is layered on top by the owning model, not here.
        This is the single source of truth for field requiredness across generators.
        """
        return not self.optional and self.default is None

    @property
    def is_enum(self) -> bool:
        """Check if this field is an enum type."""
        return isinstance(self.type_spec, EnumType)

    @property
    def enum_values(self) -> list[str] | None:
        """Get enum values if this is an enum field."""
        if isinstance(self.type_spec, EnumType):
            return self.type_spec.values
        return None


class VariantSpec(BaseModel):
    """Specification for a model variant."""

    exclude: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class ModelSpec(BaseModel):
    """Specification for a single model."""

    fields: dict[str, FieldSpec]
    variants: dict[str, VariantSpec] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @classmethod
    def from_yaml(cls, data: dict[str, Any]) -> ModelSpec:
        """Create ModelSpec from raw YAML dict."""
        fields_data = data.get("fields", {})
        if not fields_data:
            msg = "Model must have at least one field"
            raise ValueError(msg)

        fields = {name: FieldSpec.from_yaml(field_data) for name, field_data in fields_data.items()}

        variants_data = data.get("variants", {})
        variants = {}
        for variant_name, variant_data in variants_data.items():
            if variant_data is None:
                variant_data = {}
            variants[variant_name] = VariantSpec.model_validate(variant_data)

        return cls(fields=fields, variants=variants)

    @model_validator(mode="after")
    def validate_variant_references(self) -> ModelSpec:
        """Ensure variant exclude/optional reference existing fields."""
        field_names = set(self.fields.keys())

        for variant_name, variant in self.variants.items():
            for excluded in variant.exclude:
                if excluded not in field_names:
                    msg = f"Variant '{variant_name}' excludes unknown field '{excluded}'"
                    raise ValueError(msg)
            for optional in variant.optional:
                if optional not in field_names:
                    msg = f"Variant '{variant_name}' marks unknown field '{optional}' as optional"
                    raise ValueError(msg)

        return self

    def get_readonly_fields(self) -> set[str]:
        """Get names of all readonly fields."""
        return {name for name, field in self.fields.items() if field.readonly}

    def get_variant_fields(self, variant_name: str) -> dict[str, FieldSpec]:
        """Get fields for a specific variant."""
        if variant_name not in self.variants:
            # Return all fields for base model
            return dict(self.fields)

        variant = self.variants[variant_name]

        result = {}
        for name, field in self.fields.items():
            # Skip excluded fields
            if name in variant.exclude:
                continue

            # Auto-exclude readonly from Create/Update
            if field.readonly and variant_name in ("Create", "Update"):
                continue

            result[name] = field

        return result


class ModelsSpec(BaseModel):
    """Root specification containing all models."""

    models: dict[str, ModelSpec]

    model_config = {"extra": "forbid"}

    @classmethod
    def from_yaml(cls, data: dict[str, Any]) -> ModelsSpec:
        """Create ModelsSpec from raw YAML dict."""
        models_data = data.get("models", {})
        if not models_data:
            msg = "Spec must define at least one model"
            raise ValueError(msg)

        models = {name: ModelSpec.from_yaml(model_data) for name, model_data in models_data.items()}

        return cls(models=models)

    def get_model_names(self) -> set[str]:
        """Get all model names including variants."""
        names = set()
        for model_name, model in self.models.items():
            names.add(model_name)
            for variant_name in model.variants:
                names.add(f"{model_name}{variant_name}")
        return names

    def to_json_schema(self) -> dict[str, Any]:
        """Convert all models to JSON Schema definitions."""
        definitions: dict[str, Any] = {}

        for model_name, model in self.models.items():
            # Base model
            definitions[model_name] = self._model_to_schema(model_name, model, None)

            # Variants
            for variant_name in model.variants:
                full_name = f"{model_name}{variant_name}"
                definitions[full_name] = self._model_to_schema(model_name, model, variant_name)

        return {"definitions": definitions}

    def _model_to_schema(
        self, model_name: str, model: ModelSpec, variant_name: str | None
    ) -> dict[str, Any]:
        """Convert a model (or variant) to JSON Schema."""
        if variant_name:
            fields = model.get_variant_fields(variant_name)
            title = f"{model_name}{variant_name}"
            variant = model.variants[variant_name]
            optional_fields = set(variant.optional)
        else:
            fields = dict(model.fields)
            title = model_name
            optional_fields = set()

        properties = {}
        required = []

        for field_name, field in fields.items():
            prop = field.to_json_schema()

            # Variant-level optional fields must default to None, not the field's original default
            if field_name in optional_fields:
                prop.pop("default", None)

            properties[field_name] = prop

            # Required unless the variant marks it optional or the field itself is not required
            if field_name not in optional_fields and field.is_required:
                required.append(field_name)

        return {
            "type": "object",
            "title": title,
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
