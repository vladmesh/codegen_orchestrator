"""Healthcheck endpoint."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", summary="Application health check")
async def healthcheck() -> dict[str, str]:
    """Simple endpoint to verify the application is running."""

    return {"status": "ok"}


__all__ = ["router"]
