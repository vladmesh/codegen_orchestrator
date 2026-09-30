"""Bounded retry policy for GitHub git transport's transient repository 404.

Both the initial scaffold fetch and a worker-manager story checkout can get this
answer for a repository that already exists over REST. Five waits total 30 s;
authentication, permission and unrelated transport errors do not use this policy.
"""

GIT_NOT_FOUND_RETRY_DELAYS_SECONDS: tuple[int, ...] = (1, 2, 4, 8, 15)

_NOT_FOUND_MARKERS = (
    "repository not found",
    "the requested url returned error: 404",
)


def git_repository_not_found(stderr: str | bytes, stdout: str | bytes) -> bool:
    """Recognize only GitHub's git-transport repository-not-found answers."""
    streams = (stderr, stdout)
    text = "\n".join(
        stream.decode(errors="replace") if isinstance(stream, bytes) else stream
        for stream in streams
    ).lower()
    return any(marker in text for marker in _NOT_FOUND_MARKERS)


def retry_delay_after(attempt: int) -> int | None:
    """Delay after a failed attempt, or None when the shared budget is spent."""
    if attempt < 1 or attempt > len(GIT_NOT_FOUND_RETRY_DELAYS_SECONDS):
        return None
    return GIT_NOT_FOUND_RETRY_DELAYS_SECONDS[attempt - 1]
