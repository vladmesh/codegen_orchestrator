from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.clients.github import GitHubAppClient
from shared.clients.github._actions import _failure_log_excerpt


@pytest.mark.parametrize(
    "name, retained",
    [
        ("stand", ("Error: SomeError: detail", "make: *** [Makefile:152")),
        ("pytest", ("____ test_load_config ____", "E   AssertionError:", "FAILED tests/")),
        ("traceback", ("ValueError: invalid configuration", "make: *** [Makefile:152")),
        ("traceback-custom", ("RuntimeFailure: invalid configuration", "make: *** [Makefile:152")),
        ("lint", ("src/main.py:12:5: F821", "make: *** [Makefile:44")),
        ("tail", ("progress 69",)),
    ],
)
def test_failure_excerpt_retains_root_and_final_diagnostics(name, retained):
    log = (Path(__file__).parent / "fixtures/github-actions" / f"{name}.log").read_text()

    excerpt = _failure_log_excerpt(log, 40)

    for diagnostic in retained:
        assert diagnostic in excerpt
    assert len(excerpt.splitlines()) <= 41
    if name == "tail":
        assert excerpt == "\n".join(log.splitlines()[-40:])


@pytest.mark.parametrize(
    "name, retained",
    [
        ("stand-gap-26", ("Error: SomeError: detail", "make: *** [Makefile:152")),
        ("stand-gap-30", ("Error: SomeError: detail", "make: *** [Makefile:152")),
        ("stand-gap-38", ("Error: SomeError: detail", "make: *** [Makefile:152")),
        ("pytest-long", ("____ test_load_config ____", "E       AssertionError:", "FAILED tests/")),
        (
            "pytest-oversized",
            ("____ test_load_config ____", "E       AssertionError:", "FAILED tests/"),
        ),
        ("steps", ("Error: SomeError: actual failing step", "make: *** [Makefile:152")),
    ],
)
def test_failure_excerpt_preserves_context_and_final_error(name, retained):
    log = (Path(__file__).parent / "fixtures/github-actions" / f"{name}.log").read_text()

    excerpt = _failure_log_excerpt(log, 40)

    for diagnostic in retained:
        assert diagnostic in excerpt
    assert len(excerpt.splitlines()) <= 42
    assert len(excerpt) <= 131_072
    if name == "steps":
        assert "handled failure in successful setup" not in excerpt
        assert "handled cleanup retry" not in excerpt


@pytest.mark.parametrize("line_limit", [3, 4, 40, 1000])
def test_failure_excerpt_bounds_separated_pytest_header_cause_and_final_error(line_limit):
    log = (Path(__file__).parent / "fixtures/github-actions/pytest-oversized.log").read_text()

    excerpt = _failure_log_excerpt(log, line_limit)

    assert "____ test_load_config ____" in excerpt
    assert "E       AssertionError:" in excerpt
    assert "FAILED tests/" in excerpt
    assert len(excerpt.splitlines()) <= line_limit + 2
    assert len(excerpt) <= 131_072


def test_failure_excerpt_character_bound_with_three_diagnostic_windows():
    log = (Path(__file__).parent / "fixtures/github-actions/pytest-oversized.log").read_text()
    log = "\n".join(line + " " * 20_000 for line in log.splitlines())

    excerpt = _failure_log_excerpt(log, 1000)

    assert "____ test_load_config ____" in excerpt
    assert "E       AssertionError:" in excerpt
    assert "FAILED tests/" in excerpt
    assert len(excerpt) <= 131_072
    assert all(len(line) <= 2048 for line in excerpt.splitlines())


def test_failure_excerpt_without_diagnostics_keeps_job_tail_across_step_boundaries():
    log = "\n".join(
        [
            "##[group]Run prepare tests",
            *[f"progress {index}" for index in range(40)],
            "##[group]Run tests",
            "tests started",
            "##[error]Process completed with exit code 1.",
        ]
    )

    assert _failure_log_excerpt(log, 40) == "\n".join(log.splitlines()[-40:])


@pytest.mark.parametrize(
    "log",
    ["", "one line", "one\r\ntwo\r\n", "progress\n" * 600_000, "x" * 20_000],
    ids=["empty", "no-newline", "crlf", "five-megabytes", "long-line"],
)
@pytest.mark.parametrize("line_limit", [1, 2, 40, 1000])
def test_failure_excerpt_bounds_hostile_logs(log, line_limit):
    excerpt = _failure_log_excerpt(log, line_limit)

    assert len(excerpt.splitlines()) <= line_limit + 1
    assert len(excerpt) <= 131_072
    assert all(len(line) <= 2048 for line in excerpt.splitlines())


@pytest.mark.parametrize("line_limit", [1, 2, 3, 40, 1000])
def test_failure_excerpt_keeps_earliest_cause_with_tiny_or_large_budget(line_limit):
    log = "\n".join(
        [
            "Error: first cause",
            *["Error: secondary failure" for _ in range(100)],
            "make: *** [Makefile:152: test-integration] Error 1",
        ]
    )

    excerpt = _failure_log_excerpt(log, line_limit)

    assert "Error: first cause" in excerpt
    if line_limit > 1:
        assert "make: *** [Makefile:152: test-integration] Error 1" in excerpt
    assert len(excerpt.splitlines()) <= line_limit + 1


def test_failure_excerpt_character_bound_includes_separator_and_newlines():
    log = "\n".join(
        ["Error: first cause " + "x" * 20_000]
        + ["x" * 20_000 for _ in range(100)]
        + ["make: *** [Makefile:152: test-integration] Error 1 " + "x" * 20_000]
    )

    excerpt = _failure_log_excerpt(log, 1000)

    assert "Error: first cause" in excerpt
    assert "make: *** [Makefile:152: test-integration] Error 1" in excerpt
    assert len(excerpt) <= 131_072


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
async def test_workflow_failure_details_keeps_test_diagnostic_before_runner_wrapper():
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
            "E   AssertionError: expected default configuration",
            *[f"verbose test output {index}" for index in range(50)],
            "FAILED tests/test_config.py::test_load_config - AssertionError",
            "Error: Process completed with exit code 1.",
        ]
    )
    client._make_request = AsyncMock(side_effect=[jobs_response, log_response])

    details = await client.get_workflow_failure_details("org", "repo", 42, log_excerpt_lines=40)

    excerpt = details["failed_jobs"][0]["log_excerpt"]
    assert "AssertionError: expected default configuration" in excerpt
    assert "Process completed with exit code 1" not in excerpt


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


@pytest.mark.asyncio
async def test_workflow_failure_details_marks_empty_job_log_unavailable():
    client = object.__new__(GitHubAppClient)
    client.get_token = AsyncMock(return_value="secret")
    jobs_response = MagicMock()
    jobs_response.json.return_value = {
        "jobs": [
            {
                "id": 99,
                "name": "build",
                "conclusion": "failure",
                "steps": [{"name": "Build", "conclusion": "failure"}],
            }
        ]
    }
    log_response = MagicMock()
    log_response.text = " \n\t"
    client._make_request = AsyncMock(side_effect=[jobs_response, log_response])

    details = await client.get_workflow_failure_details("org", "repo", 42, log_excerpt_lines=20)

    failed_job = details["failed_jobs"][0]
    assert failed_job["log_excerpt"] is None
    assert failed_job["log_unavailable_reason"] == "GitHub job log was empty"
