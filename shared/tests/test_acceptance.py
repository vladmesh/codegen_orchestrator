"""Tests for acceptance criteria parsing — which criteria QA can decide over HTTP."""

from __future__ import annotations

import pytest

from shared.contracts.acceptance import (
    BASELINE_ACCEPTANCE_CRITERIA,
    parse_health_only_criteria,
)


class TestParseHealthOnlyCriteria:
    def test_baseline_criteria_are_health_only(self):
        """The criteria every repository is seeded with must not need an LLM."""
        checks = parse_health_only_criteria(BASELINE_ACCEPTANCE_CRITERIA)

        assert checks is not None
        assert len(checks) == 1
        assert checks[0].path == "/health"
        assert checks[0].expected_status == 200

    def test_parses_several_get_checks(self):
        checks = parse_health_only_criteria(
            "- GET /health returns 200\n- GET /api/cities returns 404\n"
        )

        assert [(c.path, c.expected_status) for c in checks] == [
            ("/health", 200),
            ("/api/cities", 404),
        ]

    def test_blank_lines_are_ignored(self):
        checks = parse_health_only_criteria("\n- GET /health returns 200\n\n")

        assert len(checks) == 1

    @pytest.mark.parametrize(
        "criteria",
        [
            # One prose line makes the whole block undecidable over HTTP.
            "- GET /health returns 200\n- Telegram: /start responds with welcome message",
            "- POST /api/cities with {'name': 'Moscow'} returns 201",
            "- GET /api/weather returns forecast",
            "- The service starts without errors",
        ],
    )
    def test_criteria_needing_an_agent_return_none(self, criteria):
        assert parse_health_only_criteria(criteria) is None

    def test_empty_criteria_return_none(self):
        """No criteria is not 'zero checks that trivially pass'."""
        assert parse_health_only_criteria("") is None
        assert parse_health_only_criteria("   \n\n") is None
