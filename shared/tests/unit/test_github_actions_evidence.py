from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.clients.github import GitHubAppClient


@pytest.mark.asyncio
async def test_workflow_failure_details_are_structured():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="secret")
    response = MagicMock()
    response.json.return_value = {
        "jobs": [
            {"name": "unit", "conclusion": "failure", "steps": [
                {"name": "Checkout", "conclusion": "success"},
                {"name": "Run pytest", "conclusion": "failure"},
            ]},
            {"name": "lint", "conclusion": "success", "steps": []},
        ]
    }
    client._make_request = AsyncMock(return_value=response)

    assert await client.get_workflow_failure_details("org", "repo", 42) == {
        "failed_jobs": [{"name": "unit", "failed_steps": ["Run pytest"]}],
        "unavailable_reason": None,
    }
