"""API endpoints for OpenRouter model management."""

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
import httpx
import structlog

logger = structlog.get_logger()

router = APIRouter()

# In-memory cache
_models_cache = None
_cache_timestamp = None
CACHE_TTL = timedelta(hours=1)


@router.get("/available-models")
async def list_available_models(
    provider: str | None = Query(
        None, description="Filter by provider (e.g., openai, anthropic, google)"
    ),
    sort_by: str = Query("name", description="Sort by: name, price, or context"),
):
    """List available models from OpenRouter.

    Args:
        provider: Optional provider filter (openai, anthropic, google, etc.)
        sort_by: Sort criterion (name, price, or context)

    Returns:
        Dict with models list and count

    Example:
        GET /api/available-models?provider=anthropic&sort_by=price
    """
    try:
        models = await _fetch_openrouter_models()

        # Filter by provider if specified
        if provider:
            models = [m for m in models if provider.lower() in m["id"].lower()]

        # Sort models
        if sort_by == "price":
            # Sort by prompt price (cheapest first)
            models.sort(key=lambda m: m.get("pricing", {}).get("prompt", 0))
        elif sort_by == "context":
            # Sort by context length (largest first)
            models.sort(key=lambda m: m.get("context_length", 0), reverse=True)
        else:  # name (default)
            models.sort(key=lambda m: m.get("name", "").lower())

        return {
            "models": models,
            "count": len(models),
            "cached": _is_cache_valid(),
        }

    except httpx.HTTPError as e:
        logger.error("openrouter_fetch_failed", error=str(e), error_type=type(e).__name__)
        raise HTTPException(
            status_code=503,
            detail="Failed to fetch models from OpenRouter API",
        ) from e


@router.get("/available-models/{model_id:path}")
async def get_model_details(model_id: str):
    """Get details for a specific model.

    Args:
        model_id: Full model identifier (e.g., openai/gpt-4o, anthropic/claude-3.5-sonnet)

    Returns:
        Model details dict

    Raises:
        HTTPException: If model not found
    """
    try:
        models = await _fetch_openrouter_models()

        # Find model by ID
        model = next((m for m in models if m["id"] == model_id), None)

        if not model:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model_id}' not found in OpenRouter",
            )

        return model

    except HTTPException:
        raise
    except httpx.HTTPError as e:
        logger.error("openrouter_fetch_failed", error=str(e), error_type=type(e).__name__)
        raise HTTPException(
            status_code=503,
            detail="Failed to fetch models from OpenRouter API",
        ) from e


async def _fetch_openrouter_models() -> list[dict]:
    """Fetch models from OpenRouter API with caching.

    Returns:
        List of model dictionaries

    Raises:
        httpx.HTTPError: If API request fails
    """
    global _models_cache, _cache_timestamp

    # Check cache validity
    if _is_cache_valid():
        logger.debug("openrouter_cache_hit")
        return _models_cache

    # Fetch fresh data from OpenRouter
    logger.info("openrouter_fetching_models")

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get("https://openrouter.ai/api/v1/models")
        response.raise_for_status()
        data = response.json()

    _models_cache = data.get("data", [])
    _cache_timestamp = datetime.now()

    logger.info("openrouter_models_cached", model_count=len(_models_cache))
    return _models_cache


def _is_cache_valid() -> bool:
    """Check if cache is still valid.

    Returns:
        True if cache exists and is not expired
    """
    if _models_cache is None or _cache_timestamp is None:
        return False

    age = datetime.now() - _cache_timestamp
    return age < CACHE_TTL


async def validate_model_identifier(model_id: str, provider: str) -> bool:
    """Validate that a model identifier exists for the given provider.

    Args:
        model_id: Model identifier to validate
        provider: Provider type (openrouter, openai, etc.)

    Returns:
        True if valid

    Raises:
        HTTPException: If model is not valid
    """
    if provider == "openrouter":
        models = await _fetch_openrouter_models()
        model_ids = [m["id"] for m in models]

        if model_id not in model_ids:
            # Try to find similar models for better error message
            similar = [m for m in model_ids if model_id.split("/")[0] in m][:3]
            detail = f"Model '{model_id}' not found in OpenRouter."
            if similar:
                detail += f" Did you mean: {', '.join(similar)}?"

            raise HTTPException(status_code=400, detail=detail)

    # For direct OpenAI, we could validate against known models
    # But for now, accept any identifier
    return True
