"""Disposable real API with deterministic read-only GitHub observations."""

from types import SimpleNamespace

import uvicorn

from src.main import app
from src.routers import _story_actions


def dirty_pr(owner: str, repo: str, number: int) -> dict:
    return {
        "number": number,
        "state": "open",
        "merged_at": None,
        "mergeable_state": "dirty",
        "head": {"sha": "a" * 40, "ref": f"story/{repo}", "repo": {"full_name": f"{owner}/{repo}"}},
        "base": {"sha": "b" * 40, "ref": "trunk", "repo": {"full_name": f"{owner}/{repo}"}},
    }


class SyntheticGitHub:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, owner, repo, number):
        return dirty_pr(owner, repo, number)

    async def get_repo(self, owner, repo):
        return SimpleNamespace(default_branch="trunk")

    async def get_ref_sha(self, owner, repo, ref):
        return "b" * 40 if ref == "heads/trunk" else "a" * 40


_story_actions.PR_CONFLICT_GITHUB = SyntheticGitHub
uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104 - isolated Compose network, no host port
