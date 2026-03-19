"""GitHub App client for scaffolder — uses shared GitHubAppClient."""

from __future__ import annotations

from shared.clients.github import GitHubAppClient

_client: GitHubAppClient | None = None


def get_github_client() -> GitHubAppClient:
    """Get or create GitHubAppClient singleton.

    Reads GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH from env.
    """
    global _client
    if _client is None:
        _client = GitHubAppClient()
    return _client
