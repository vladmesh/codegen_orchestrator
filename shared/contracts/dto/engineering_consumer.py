"""Typed operator state for the engineering consumer drain."""

from datetime import datetime

from pydantic import BaseModel, model_validator


class EngineeringConsumerDrain(BaseModel):
    """The durable stop-claiming decision for engineering consumers."""

    draining: bool
    requested_at: datetime | None = None
    actor: str | None = None

    @model_validator(mode="after")
    def _require_audit_identity_while_draining(self) -> "EngineeringConsumerDrain":
        if self.draining and (self.requested_at is None or not self.actor):
            raise ValueError("a draining engineering consumer requires requested_at and actor")
        if not self.draining and (self.requested_at is not None or self.actor is not None):
            raise ValueError("a cleared engineering consumer drain retains no active actor or time")
        return self
