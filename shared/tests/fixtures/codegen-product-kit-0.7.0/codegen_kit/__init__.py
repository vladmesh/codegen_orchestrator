"""Stable public API for packages installed into a generated product."""

from datetime import datetime
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .packages import (
    CORE_VERSION,
    PACKAGE_PROTOCOL_VERSION,
    Package,
    SettingSeedPackage,
)

if TYPE_CHECKING:
    from services.backend.src.app.caller_identity import caller_identity


def __getattr__(name: str) -> Any:
    """Resolve the core caller-identity dependency only when a package asks for it.

    ``caller_identity`` is a FastAPI dependency answering the verified caller's canonical
    ``user_ref``; importing it loads the backend's settings and database, which a plain
    ``import codegen_kit`` must not do.
    """

    if name == "caller_identity":
        from services.backend.src.app.caller_identity import caller_identity

        return caller_identity
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def package_database() -> Any:
    """Return the database capability owned by the calling installed package."""

    from .database import owned_package_database

    frame = inspect.currentframe()
    if frame is None or frame.f_back is None:
        raise RuntimeError("cannot identify the package database caller")
    return owned_package_database(Path(frame.f_back.f_code.co_filename))


async def publish_event(
    stream: str,
    payload: Any,
    *,
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
    schema_version: int = 1,
) -> Any:
    """Publish through the generated product transport without exposing its module."""

    from shared.generated.events import publish_event as generated_publish_event

    return await generated_publish_event(
        stream,
        payload,
        event_id=event_id,
        occurred_at=occurred_at,
        schema_version=schema_version,
    )


__all__ = [
    "CORE_VERSION",
    "PACKAGE_PROTOCOL_VERSION",
    "Package",
    "SettingSeedPackage",
    "caller_identity",
    "package_database",
    "publish_event",
]
