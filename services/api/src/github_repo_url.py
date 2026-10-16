"""Owner and name of a GitHub repository from the ``git_url`` a ``Repository`` row stores."""

from urllib.parse import urlparse

_GITHUB_REPO_PATH_PARTS = 2


def parse_github_repo_url(git_url: str) -> tuple[str, str]:
    """``(owner, repo)`` of an https or ssh GitHub URL; ``ValueError`` for any other URL."""
    if git_url.startswith("git@github.com:"):
        path = git_url.removeprefix("git@github.com:")
    else:
        parsed = urlparse(git_url)
        if parsed.netloc != "github.com":
            raise ValueError(f"Repository URL is not a GitHub URL: {git_url}")
        path = parsed.path.lstrip("/")

    parts = path.rstrip("/").removesuffix(".git").split("/")
    if len(parts) != _GITHUB_REPO_PATH_PARTS or not all(parts):
        raise ValueError(f"Repository URL does not identify owner/repo: {git_url}")
    return parts[0], parts[1]
