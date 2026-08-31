"""Event publisher specifications for async messaging validation.

Defines the structure of events.yaml:
- EventSpec: A single event definition
- EventsSpec: The root container for all events
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class EventSpec(BaseModel):
    """Specification for a single event publisher."""

    name: str = ""  # Set by parent
    message: str  # Model name for the event payload
    publish: bool = False

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_publish_enabled(self) -> EventSpec:
        """Ensure every global event declaration generates a publisher."""
        if not self.publish:
            msg = f"Event '{self.name}' must have publish=true"
            raise ValueError(msg)
        return self


class EventsSpec(BaseModel):
    """Root specification containing all events."""

    events: list[EventSpec] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @classmethod
    def from_yaml(cls, data: dict[str, Any] | None) -> EventsSpec:
        """Create EventsSpec from raw YAML dict."""
        if data is None:
            return cls(events=[])

        events_data = data.get("events", {})
        events = []

        for name, event_data in events_data.items():
            if not isinstance(event_data, dict):
                msg = f"Event '{name}' must be a dict"
                raise ValueError(msg)

            events.append(
                EventSpec.model_validate({"name": name, **event_data})
            )

        return cls(events=events)

    def get_referenced_models(self) -> set[str]:
        """Get all model names referenced by events."""
        return {event.message for event in self.events if event.message}

    def get_publishers(self) -> list[EventSpec]:
        """Get events that can be published."""
        return [e for e in self.events if e.publish]
