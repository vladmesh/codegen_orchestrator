"""`commit_adds_changes`: whether a reported commit is work over a base commit."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.clients.github import GitHubAppClient


def _client(comparison: dict) -> GitHubAppClient:
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="secret")
    response = MagicMock()
    response.json.return_value = comparison
    client._make_request = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("comparison", "adds"),
    [
        ({"status": "ahead", "ahead_by": 1, "files": [{"filename": "bot.py"}]}, True),
        ({"status": "identical", "ahead_by": 0, "files": []}, False),
        ({"status": "behind", "ahead_by": 0, "files": []}, False),
        ({"status": "ahead", "ahead_by": 1, "files": []}, False),
    ],
    ids=["new-commit", "same-head", "behind-the-head", "empty-commit"],
)
async def test_only_a_commit_ahead_with_a_file_change_adds_changes(comparison, adds):
    client = _client(comparison)

    assert await client.commit_adds_changes("org", "repo", "base-sha", "head-sha") is adds
    url = client._make_request.await_args.args[1]
    assert url == "https://api.github.com/repos/org/repo/compare/base-sha...head-sha"
