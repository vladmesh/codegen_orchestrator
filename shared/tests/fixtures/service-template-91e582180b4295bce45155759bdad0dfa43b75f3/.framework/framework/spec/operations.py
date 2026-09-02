"""Typed operation specifications for REST and event transports."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def unwrap_list(ref: str) -> tuple[bool, str]:
    """Split a model reference into (is_list, base_model).

    Single owner of the "list of models" convention:
        "User"       -> (False, "User")
        "list[User]" -> (True, "User")
        "List[User]" -> (True, "User")
    """
    for prefix in ("list[", "List["):
        if ref.startswith(prefix) and ref.endswith("]"):
            return True, ref[len(prefix) : -1]
    return False, ref


class ParamSpec(BaseModel):
    """Specification for an operation parameter (path param, query param, etc).

    Attributes:
        name: Parameter name (used in path templates and function signatures)
        type: Python type annotation (int, str, bool, float, etc.)
        required: Whether the parameter is required (for query params)
        source: Where the parameter comes from - "path" (URL path) or "query" (query string)
        default: Default value for optional query parameters
    """

    name: str
    type: str = "str"  # Python type annotation
    required: bool = True
    source: Literal["path", "query"] = "path"
    default: str | int | float | bool | None = None

    model_config = {"extra": "forbid"}


class RestConfig(BaseModel):
    """REST-specific configuration for an operation."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = ""
    status: int | None = None  # Default based on method if not specified

    model_config = {"extra": "forbid"}

    @property
    def effective_status(self) -> int:
        """Get the effective status code, using defaults if not specified."""
        if self.status is not None:
            return self.status
        if self.method == "POST":
            return 201
        if self.method == "DELETE":
            return 204
        return 200


class EventsConfig(BaseModel):
    """Events-specific configuration for an operation.

    Attributes:
        subscribe: Channel to subscribe to for incoming events.
        publish_on_success: Channel to publish to after successful execution.
        publish_on_error: Channel to publish to if execution fails (optional).
    """

    subscribe: str | None = None
    publish_on_success: str | None = None
    publish_on_error: str | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_at_least_one(self) -> EventsConfig:
        """Ensure at least one direction is specified."""
        if not self.subscribe and not self.publish_on_success:
            msg = "Events config must have 'subscribe' or 'publish_on_success' (or both)"
            raise ValueError(msg)
        return self


class OperationSpec(BaseModel):
    """Unified specification for a single operation.

    An operation is transport-agnostic and can be exposed via REST, Events, or both.
    """

    name: str = ""  # Set by parent
    input_model: str | None = None
    output_model: str | None = None
    params: list[ParamSpec] = Field(default_factory=list)

    # Transport configurations (at least one required)
    rest: RestConfig | None = None
    events: EventsConfig | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_has_transport(self) -> OperationSpec:
        """Ensure at least one transport is configured."""
        if not self.rest and not self.events:
            msg = f"Operation '{self.name}' must have at least one transport (rest or events)"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_events_models(self) -> OperationSpec:
        """Validate events configuration has required models."""
        if self.events and self.events.subscribe and not self.input_model:
            msg = f"Operation '{self.name}' with events.subscribe must have 'input' model"
            raise ValueError(msg)
        if self.events and self.events.publish_on_success and not self.output_model:
            msg = f"Operation '{self.name}' with events.publish_on_success must have 'output' model"
            raise ValueError(msg)
        return self

    @property
    def response_many(self) -> bool:
        """Check if output is a list type."""
        if not self.output_model:
            return False
        return unwrap_list(self.output_model)[0]

    @property
    def base_output_model(self) -> str | None:
        """Get the base output model (unwrapping list[] if present)."""
        if not self.output_model:
            return None
        return unwrap_list(self.output_model)[1]

    @classmethod
    def from_yaml(cls, name: str, data: dict[str, Any]) -> OperationSpec:
        """Create OperationSpec from raw YAML dict."""
        params = []
        for param_data in data.get("params", []):
            if isinstance(param_data, str):
                params.append(ParamSpec(name=param_data))
            else:
                params.append(ParamSpec.model_validate(param_data))

        rest_data = data.get("rest")
        rest = RestConfig.model_validate(rest_data) if rest_data else None

        events_data = data.get("events")
        events = EventsConfig.model_validate(events_data) if events_data else None

        return cls(
            name=name,
            input_model=data.get("input"),
            output_model=data.get("output"),
            params=params,
            rest=rest,
            events=events,
        )


class DomainConfig(BaseModel):
    """Domain-level configuration for REST and Events."""

    rest: RestDomainConfig | None = None

    model_config = {"extra": "forbid"}


class RestDomainConfig(BaseModel):
    """REST-specific domain configuration."""

    prefix: str = ""
    tags: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class DomainSpec(BaseModel):
    """Specification for a domain (group of related operations).

    A domain corresponds to a spec file like users.yaml.
    """

    name: str  # e.g., "users" (also the module name)
    service_name: str = ""  # Owning service; set by loader
    config: DomainConfig = Field(default_factory=DomainConfig)
    operations: list[OperationSpec] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @property
    def protocol_name(self) -> str:
        """Controller protocol class name, e.g. 'UsersControllerProtocol'."""
        return f"{self.name.capitalize()}ControllerProtocol"

    @property
    def controller_class_name(self) -> str:
        """Controller implementation class name, e.g. 'UsersController'."""
        return f"{self.name.capitalize()}Controller"

    @classmethod
    def from_yaml(cls, name: str, data: dict[str, Any]) -> DomainSpec:
        """Create DomainSpec from raw YAML dict."""
        # Parse config
        config_data = data.get("config", {})
        rest_config = config_data.get("rest")
        config = DomainConfig(
            rest=RestDomainConfig.model_validate(rest_config) if rest_config else None,
        )

        # Parse operations
        operations = []
        for op_name, op_data in data.get("operations", {}).items():
            operations.append(OperationSpec.from_yaml(op_name, op_data))

        return cls(name=name, config=config, operations=operations)

    def get_rest_operations(self) -> list[OperationSpec]:
        """Get operations that have REST transport configured."""
        return [op for op in self.operations if op.rest is not None]

    def get_events_operations(self) -> list[OperationSpec]:
        """Get operations that have Events transport configured."""
        return [op for op in self.operations if op.events is not None]
