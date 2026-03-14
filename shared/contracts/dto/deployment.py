"""Deployment DTO — result status of a deploy attempt."""

from enum import StrEnum


class DeploymentResult(StrEnum):
    """Outcome of a deployment attempt. Immutable after completion."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
