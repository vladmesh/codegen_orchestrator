"""Shared clients: the internal API transport and clients for external services."""

from .embedding import EmbeddingClient, EmbeddingResult, generate_embeddings
from .github import GitHubAppClient
from .infra_client import check_http_health
from .internal_api import InternalAPIClient
from .registry import DockerRegistryClient, sha_image_tag
from .time4vps import Time4VPSClient

__all__ = [
    "DockerRegistryClient",
    "EmbeddingClient",
    "EmbeddingResult",
    "GitHubAppClient",
    "InternalAPIClient",
    "Time4VPSClient",
    "check_http_health",
    "generate_embeddings",
    "sha_image_tag",
]
