"""OpenRouter model lookup used by the agent-config router to validate identifiers."""

from datetime import datetime, timedelta

from fastapi import HTTPException
import httpx
import structlog

logger = structlog.get_logger()

# In-memory cache
_models_cache = None
_cache_timestamp = None
CACHE_TTL = timedelta(hours=1)


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
