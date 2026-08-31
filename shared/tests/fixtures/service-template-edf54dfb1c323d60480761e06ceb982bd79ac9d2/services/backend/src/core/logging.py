"""Logging configuration for the backend application."""

from __future__ import annotations

from shared.logging import configure_logging as _configure

from .settings import get_settings


def configure_logging() -> None:
    """Configure structured logging for the backend service."""

    settings = get_settings()
    _configure(service_name=settings.app_name, log_level="DEBUG" if settings.debug else "INFO")
