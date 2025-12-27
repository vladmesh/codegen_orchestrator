"""Shared clients for external services."""

from .embedding import EmbeddingClient, EmbeddingResult, generate_embeddings
from .github import GitHubAppClient
from .time4vps import Time4VPSClient

__all__ = [
    "EmbeddingClient",
    "EmbeddingResult",
    "GitHubAppClient",
    "Time4VPSClient",
    "generate_embeddings",
]
