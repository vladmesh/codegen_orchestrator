"""Embedding client using OpenRouter API."""

from __future__ import annotations

from dataclasses import dataclass
import os

import httpx
import structlog

logger = structlog.get_logger()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/text-embedding-3-small"
DEFAULT_DIMENSIONS = 512
MAX_BATCH_SIZE = 100  # OpenRouter limit for embeddings per request


@dataclass(frozen=True)
class EmbeddingResult:
    """Result of embedding generation."""

    embeddings: list[list[float]]
    model: str
    total_tokens: int


class EmbeddingClient:
    """Client for generating embeddings via OpenRouter API.

    Designed for extensibility:
    - Supports batching for large documents
    - Returns metadata (model, tokens) for logging/debugging
    - Async-first for integration with FastAPI
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = OPENROUTER_BASE_URL,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.getenv("OPEN_ROUTER_KEY")
        if not self.api_key:
            raise ValueError("OPEN_ROUTER_KEY environment variable not set")

        self.base_url = base_url
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout

    async def generate(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> EmbeddingResult:
        """Generate embeddings for a list of texts.

        Args:
            texts: List of texts to embed.
            model: Override default model.
            dimensions: Override default dimensions.

        Returns:
            EmbeddingResult with embeddings, model name, and token count.

        Raises:
            httpx.HTTPError: On API errors.
            ValueError: On invalid response format.
        """
        if not texts:
            return EmbeddingResult(embeddings=[], model=self.model, total_tokens=0)

        model = model or self.model
        dimensions = dimensions or self.dimensions

        # Batch if needed
        all_embeddings: list[list[float]] = []
        total_tokens = 0

        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i : i + MAX_BATCH_SIZE]
            result = await self._generate_batch(batch, model, dimensions)
            all_embeddings.extend(result.embeddings)
            total_tokens += result.total_tokens

        return EmbeddingResult(
            embeddings=all_embeddings,
            model=model,
            total_tokens=total_tokens,
        )

    async def _generate_batch(
        self,
        texts: list[str],
        model: str,
        dimensions: int,
    ) -> EmbeddingResult:
        """Generate embeddings for a single batch."""
        payload = {
            "model": model,
            "input": texts,
            "dimensions": dimensions,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

        data = response.json()

        try:
            embeddings = [item["embedding"] for item in data["data"]]
            total_tokens = data.get("usage", {}).get("total_tokens", 0)
        except (KeyError, TypeError) as exc:
            logger.error(
                "embedding_response_parse_failed",
                response_keys=list(data.keys()) if isinstance(data, dict) else None,
            )
            raise ValueError("Invalid embedding response format") from exc

        return EmbeddingResult(
            embeddings=embeddings,
            model=model,
            total_tokens=total_tokens,
        )


# Singleton instance for convenience
_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    """Get or create singleton embedding client."""
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client


async def generate_embeddings(
    texts: list[str],
    *,
    model: str = DEFAULT_MODEL,
    dimensions: int = DEFAULT_DIMENSIONS,
) -> EmbeddingResult:
    """Convenience function to generate embeddings.

    Uses singleton client. For advanced use cases, instantiate EmbeddingClient directly.
    """
    client = get_embedding_client()
    return await client.generate(texts, model=model, dimensions=dimensions)
