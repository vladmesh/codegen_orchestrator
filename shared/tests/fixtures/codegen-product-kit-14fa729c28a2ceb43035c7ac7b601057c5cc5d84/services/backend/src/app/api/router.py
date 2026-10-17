"""Top-level API router composition."""

from fastapi import APIRouter

from services.backend.src.generated.registry import create_api_router

from .v1.health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(create_api_router())

__all__ = ["api_router"]
