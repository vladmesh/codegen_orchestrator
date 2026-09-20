"""How this service reads GitHub's own identifiers.

Two readings every GitHub-facing task needs and none of them owns: the
owner/repo of a repository's git URL, and one of GitHub's `...Z` timestamps.
They live here rather than in the first task that happened to need them, so a
later reader can reuse them without importing that task — which is how the
poller and the supervisor package ended up in an import cycle.
"""

from __future__ import annotations

from datetime import datetime


def _parse_owner_repo(git_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub git_url.

    Handles both HTTPS and token-based URLs:
    - https://github.com/org/repo
    - https://x-access-token:TOKEN@github.com/org/repo.git
    """
    # Strip .git suffix and trailing slashes
    url = git_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # Take last two path segments
    parts = url.split("/")
    return parts[-2], parts[-1]


def _parse_github_timestamp(value: object) -> datetime | None:
    """A GitHub `...Z` timestamp as an aware datetime, or None when unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
