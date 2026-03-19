"""Engineering DTOs and enums — single source of truth for engineering subgraph statuses."""

from enum import StrEnum


class EngineeringStatus(StrEnum):
    """Status of the engineering subgraph execution.

    Lifecycle:
        IDLE → (subgraph runs) → DONE | GAVE_UP | FAILED
        FAILED → (supervisor retries) → IDLE  OR  (retries exhausted) → GAVE_UP

    FAILED is transient — supervisor either retries or escalates to GAVE_UP.
    """

    IDLE = "idle"
    DONE = "done"
    GAVE_UP = "gave_up"
    FAILED = "failed"
