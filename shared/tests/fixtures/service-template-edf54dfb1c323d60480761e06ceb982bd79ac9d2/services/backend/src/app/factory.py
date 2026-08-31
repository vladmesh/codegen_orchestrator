"""FastAPI application factory."""

from fastapi import FastAPI

from services.backend.src.core.settings import get_settings

from .api.router import api_router
from .grant_capability import GrantCapabilityMiddleware
from .lifespan import lifespan
from .middleware import RequestLoggingMiddleware


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    settings = get_settings()
    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    application.add_middleware(
        GrantCapabilityMiddleware,
        capability=settings.users_grant_capability,
    )
    application.add_middleware(RequestLoggingMiddleware)
    application.include_router(api_router)
    return application
