"""`compare_commits`: the files a three-dot comparison of two commits lists."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.clients.github import GitHubAppClient


@pytest.mark.asyncio
async def test_compare_commits_returns_the_comparison_files_of_base_three_dot_head():
    files = [{"filename": "bot.py", "status": "modified", "additions": 2, "deletions": 1}]
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="secret")
    response = MagicMock()
    response.json.return_value = {"status": "ahead", "ahead_by": 1, "files": files}
    client._make_request = AsyncMock(return_value=response)

    assert await client.compare_commits("org", "repo", "main", "story/st-1") == files
    url = client._make_request.await_args.args[1]
    assert url == "https://api.github.com/repos/org/repo/compare/main...story%2Fst-1"
