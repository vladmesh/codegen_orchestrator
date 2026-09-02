"""Operation context builder for code generation.

Provides a unified way to build template context for operations,
eliminating duplication between protocols and controllers generators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from framework.spec.types import parse_type_spec, type_spec_to_python

if TYPE_CHECKING:
    from pathlib import Path

    from framework.spec.operations import DomainSpec, OperationSpec


def controller_path(repo_root: Path, domain: DomainSpec) -> Path:
    """Path to a domain's controller implementation file.

    Single owner of the controllers/<module>.py convention, shared by the
    controllers generator (writes the file) and the sync linter (reads it).
    """
    return (
        repo_root / "services" / domain.service_name / "src" / "controllers" / f"{domain.name}.py"
    )

# Param types that need explicit imports in generated code
_PARAM_TYPE_IMPORTS: dict[str, str] = {
    "uuid": "from uuid import UUID",
    "datetime": "from pydantic import AwareDatetime",
}

# Map Python type names back to spec primitives (ParamSpec uses Python names as defaults)
_PYTHON_TO_SPEC: dict[str, str] = {
    "str": "string",
    "int": "int",
    "bool": "bool",
    "float": "float",
}


@dataclass
class ParamContext:
    """Context for a single parameter in generated code.

    Attributes:
        name: Parameter name
        type: Python type annotation
        required: Whether param is required
        param_source: Original source type ("path" or "query")
        default: Default value for optional params
        fastapi_source: FastAPI dependency string (e.g., "Path(...)" or "Query(default=10)")
    """

    name: str
    type: str
    required: bool = True
    param_source: str = "path"  # "path" or "query"
    default: str | int | float | bool | None = None
    fastapi_source: str | None = None  # e.g., "Path(...)" or "Query(default=10)"


@dataclass
class OperationContext:
    """Complete context for generating code for an operation.

    This is the unified structure used by all generators (protocols,
    controllers, event handlers).
    """

    name: str
    params: list[ParamContext] = field(default_factory=list)
    input_model: str | None = None
    output_model: str | None = None
    imports: set[str] = field(default_factory=set)
    param_type_imports: set[str] = field(default_factory=set)

    # REST-specific (populated only for REST operations)
    http_method: str | None = None
    path: str | None = None
    status_code: int | None = None
    response_many: bool = False  # True if response is list[Model]

    # Events-specific (populated only for Events operations)
    subscribe_channel: str | None = None
    publish_channel: str | None = None
    publish_on_error_channel: str | None = None

    # Transport type flags
    has_rest: bool = False
    has_events: bool = False

    @property
    def is_rest_only(self) -> bool:
        """True if operation only has REST transport."""
        return self.has_rest and not self.has_events

    @property
    def is_events_only(self) -> bool:
        """True if operation only has Events transport."""
        return self.has_events and not self.has_rest

    @property
    def is_dual_transport(self) -> bool:
        """True if operation has both REST and Events transports."""
        return self.has_rest and self.has_events

    @property
    def computed_return_type(self) -> str:
        """Get return type, handling response_many."""
        if not self.output_model:
            return "None"
        if self.response_many:
            return f"list[{self.output_model}]"
        return self.output_model


class OperationContextBuilder:
    """Builds OperationContext from OperationSpec.

    Eliminates code duplication by centralizing all the logic for:
    - Extracting path parameters
    - Building import lists
    - Determining return types
    - Adding transport-specific data
    """

    def build(
        self,
        operation: OperationSpec,
        *,
        include_rest: bool = True,
        include_events: bool = True,
    ) -> OperationContext:
        """Build a complete OperationContext from an OperationSpec.

        Args:
            operation: The operation specification
            include_rest: Whether to include REST-specific context
            include_events: Whether to include Events-specific context

        Returns:
            OperationContext ready for template rendering
        """
        imports: set[str] = set()

        # Collect models for imports (use base models, not wrapped types)
        if operation.input_model:
            imports.add(operation.input_model)
        if operation.base_output_model:
            imports.add(operation.base_output_model)

        params, param_type_imports = self._build_params(operation)

        # Build base context with transport type flags
        ctx = OperationContext(
            name=operation.name,
            params=params,
            input_model=operation.input_model,
            output_model=operation.base_output_model,  # Use unwrapped model
            imports=imports,
            param_type_imports=param_type_imports,
            response_many=operation.response_many,  # Pass the list flag
            has_rest=operation.rest is not None,
            has_events=operation.events is not None,
        )

        # Add REST-specific context
        if include_rest and operation.rest:
            ctx.http_method = operation.rest.method
            ctx.path = operation.rest.path
            ctx.status_code = operation.rest.effective_status

        # Add Events-specific context
        if include_events and operation.events:
            ctx.subscribe_channel = operation.events.subscribe
            ctx.publish_channel = operation.events.publish_on_success
            ctx.publish_on_error_channel = operation.events.publish_on_error

        return ctx

    @staticmethod
    def _build_params(
        operation: OperationSpec,
    ) -> tuple[list[ParamContext], set[str]]:
        """Build param contexts and collect type imports from operation params."""
        params: list[ParamContext] = []
        param_type_imports: set[str] = set()

        for param in operation.params:
            # Convert param type through the type system
            # ParamSpec.type may use Python names (str, int) or spec names (string, uuid)
            spec_type = _PYTHON_TO_SPEC.get(param.type, param.type)
            try:
                type_spec = parse_type_spec(spec_type)
                python_type = type_spec_to_python(type_spec)
            except ValueError:
                # Not a spec primitive — pass through as-is (e.g. custom model name)
                python_type = param.type

            # Track stdlib/pydantic imports needed for this param type
            if spec_type in _PARAM_TYPE_IMPORTS:
                param_type_imports.add(_PARAM_TYPE_IMPORTS[spec_type])

            # Determine FastAPI source based on param source type
            if param.source == "query":
                if param.default is not None:
                    fastapi_source = f"Query(default={param.default!r})"
                elif not param.required:
                    fastapi_source = "Query(default=None)"
                else:
                    fastapi_source = "Query(...)"
            else:
                fastapi_source = "Path(...)"

            params.append(
                ParamContext(
                    name=param.name,
                    type=python_type,
                    required=param.required,
                    param_source=param.source,
                    default=param.default,
                    fastapi_source=fastapi_source,
                )
            )

        return params, param_type_imports

    def build_for_protocol(self, operation: OperationSpec) -> OperationContext:
        """Build context specifically for Protocol generation.

        Protocols are transport-agnostic, so we exclude transport-specific data.
        """
        return self.build(operation, include_rest=False, include_events=False)

    def build_for_rest(self, operation: OperationSpec) -> OperationContext:
        """Build context specifically for REST router generation."""
        if not operation.rest:
            msg = f"Operation '{operation.name}' has no REST transport configured"
            raise ValueError(msg)
        return self.build(operation, include_rest=True, include_events=True)

    def build_for_events(self, operation: OperationSpec) -> OperationContext:
        """Build context specifically for Events handler generation."""
        if not operation.events:
            msg = f"Operation '{operation.name}' has no Events transport configured"
            raise ValueError(msg)
        return self.build(operation, include_rest=False, include_events=True)
