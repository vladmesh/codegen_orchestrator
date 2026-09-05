"""FastAPI application factory."""

from fastapi import FastAPI

from services.backend.src.core.settings import get_settings

from .api.router import api_router
from .grant_capability import GrantCapabilityMiddleware
from .jobs_capability import JobsCapabilityMiddleware
from .lifespan import lifespan
from .middleware import RequestLoggingMiddleware
from .settings_capability import SettingsCapabilityMiddleware


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    settings = get_settings()
    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    application.add_middleware(
        GrantCapabilityMiddleware,
        capability=settings.users_grant_capability,
    )
    application.add_middleware(
        JobsCapabilityMiddleware,
        capability=settings.jobs_fire_capability,
    )
    application.add_middleware(
        SettingsCapabilityMiddleware,
        capability=settings.settings_write_capability,
    )
    application.add_middleware(RequestLoggingMiddleware)
    application.include_router(api_router)
    return application
