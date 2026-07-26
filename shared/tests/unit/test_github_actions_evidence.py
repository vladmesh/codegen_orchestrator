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
            {
                "name": "unit",
                "conclusion": "failure",
                "steps": [
                    {"name": "Checkout", "conclusion": "success"},
                    {"name": "Run pytest", "conclusion": "failure"},
                ],
            },
            {"name": "lint", "conclusion": "success", "steps": []},
        ]
    }
    client._make_request = AsyncMock(return_value=response)

    assert await client.get_workflow_failure_details("org", "repo", 42) == {
        "failed_jobs": [{"name": "unit", "failed_steps": ["Run pytest"]}],
        "unavailable_reason": None,
    }


@pytest.mark.asyncio
async def test_workflow_failure_details_center_excerpt_on_error_not_log_end():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="secret")
    jobs_response = MagicMock()
    jobs_response.json.return_value = {
        "jobs": [
            {
                "id": 98,
                "name": "unit",
                "conclusion": "failure",
                "steps": [{"name": "Run pytest", "conclusion": "failure"}],
            }
        ]
    }
    log_response = MagicMock()
    log_response.text = "\n".join(
        [
            "2026-07-26T10:00:00Z preparing tests",
            "2026-07-26T10:00:01Z FileNotFoundError: settings.yaml",
            "2026-07-26T10:00:02Z cleanup 1",
            "2026-07-26T10:00:03Z cleanup 2",
            "2026-07-26T10:00:04Z cleanup 3",
            "2026-07-26T10:00:05Z cleanup 4",
        ]
    )
    client._make_request = AsyncMock(side_effect=[jobs_response, log_response])

    details = await client.get_workflow_failure_details("org", "repo", 42, log_excerpt_lines=3)

    failed_job = details["failed_jobs"][0]
    assert "FileNotFoundError: settings.yaml" in failed_job["log_excerpt"]
    assert "cleanup 4" not in failed_job["log_excerpt"]
    assert failed_job["log_unavailable_reason"] is None
    assert client._make_request.await_args_list[1].args[1].endswith("/actions/jobs/98/logs")
    assert client._make_request.await_args_list[1].kwargs["follow_redirects"] is True


@pytest.mark.asyncio
async def test_workflow_failure_details_keeps_traceback_cause_before_pytest_summary():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="secret")
    jobs_response = MagicMock()
    jobs_response.json.return_value = {
        "jobs": [
            {
                "id": 98,
                "name": "unit",
                "conclusion": "failure",
                "steps": [{"name": "Run pytest", "conclusion": "failure"}],
            }
        ]
    }
    log_response = MagicMock()
    log_response.text = "\n".join(
        [
            "tests/test_config.py:17: in test_load_config",
            "    assert load_config() == expected",
            "E   AssertionError: expected default configuration",
            *[f"verbose test output {index}" for index in range(20)],
            "FAILED tests/test_config.py::test_load_config - AssertionError",
            "============================== 1 failed in 0.12s ==============================",
            "Post job cleanup.",
        ]
    )
    client._make_request = AsyncMock(side_effect=[jobs_response, log_response])

    details = await client.get_workflow_failure_details("org", "repo", 42, log_excerpt_lines=5)

    excerpt = details["failed_jobs"][0]["log_excerpt"]
    assert "AssertionError: expected default configuration" in excerpt
    assert "1 failed in 0.12s" not in excerpt


@pytest.mark.asyncio
async def test_workflow_failure_details_marks_unavailable_job_logs():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="secret")
    jobs_response = MagicMock()
    jobs_response.json.return_value = {
        "jobs": [
            {
                "id": 99,
                "name": "build",
                "conclusion": "failure",
                "steps": [{"name": "Set up Docker Buildx", "conclusion": "failure"}],
            }
        ]
    }
    client._make_request = AsyncMock(side_effect=[jobs_response, RuntimeError("unavailable")])

    details = await client.get_workflow_failure_details("org", "repo", 42, log_excerpt_lines=20)

    failed_job = details["failed_jobs"][0]
    assert failed_job["log_excerpt"] is None
    assert failed_job["log_unavailable_reason"] == "RuntimeError"
