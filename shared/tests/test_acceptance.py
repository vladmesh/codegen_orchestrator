"""Tests for acceptance criteria parsing — which criteria QA can decide over HTTP."""

from __future__ import annotations

import pytest

from shared.contracts.acceptance import (
    BASELINE_ACCEPTANCE_CRITERIA,
    parse_health_only_criteria,
    parse_scheduled_behaviours,
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


class TestParseScheduledBehaviours:
    """Where a scheduled behaviour's name comes from, and where it does not."""

    def test_a_named_behaviour_and_its_observable_are_read_off_the_line(self):
        behaviours = parse_scheduled_behaviours(
            "- GET /health returns 200\n"
            "- FIRE JOB daily_digest THEN the bot sends today's digest to the owner\n"
        )

        assert len(behaviours) == 1
        assert behaviours[0].name == "daily_digest"
        assert behaviours[0].arguments == {}
        assert behaviours[0].observable == "the bot sends today's digest to the owner"

    def test_declared_arguments_travel_with_the_name(self):
        behaviours = parse_scheduled_behaviours(
            '- FIRE JOB send_reminder WITH {"chat_id": 42} THEN the bot posts the reminder'
        )

        assert behaviours[0].arguments == {"chat_id": 42}
        assert behaviours[0].observable == "the bot posts the reminder"

    def test_prose_about_a_schedule_names_no_behaviour(self):
        """A name is read off a declaration or it is not read at all."""
        assert (
            parse_scheduled_behaviours(
                "- The bot sends a daily digest every morning at 09:00\n"
                "- Scheduled jobs run daily_digest once a day\n"
            )
            == []
        )

    def test_arguments_that_are_not_a_json_object_declare_nothing(self):
        """A fire the platform cannot spell exactly is a fire nobody may make."""
        assert (
            parse_scheduled_behaviours(
                "- FIRE JOB daily_digest WITH {not json} THEN the digest is sent"
            )
            == []
        )

    def test_a_line_with_no_observable_declares_nothing(self):
        assert parse_scheduled_behaviours("- FIRE JOB daily_digest") == []

    def test_the_same_name_twice_is_one_behaviour(self):
        """One name, one execution — the same rule the run's identity encodes."""
        behaviours = parse_scheduled_behaviours(
            "- FIRE JOB daily_digest THEN the owner receives the digest\n"
            "- FIRE JOB daily_digest THEN the digest is written to the log\n"
        )

        assert [one.observable for one in behaviours] == ["the owner receives the digest"]

    def test_a_behaviour_line_is_not_machine_checkable_over_http(self):
        """A checklist naming a behaviour needs an exploratory run, not GETs."""
        assert (
            parse_health_only_criteria(
                "- GET /health returns 200\n- FIRE JOB daily_digest THEN the digest is sent\n"
            )
            is None
        )

    def test_an_ordinary_checklist_declares_no_behaviour(self):
        """The forms the architect already authors stay prose to the fire path."""
        assert (
            parse_scheduled_behaviours(
                "- GET /health returns 200\n"
                '- POST /api/cities with {"name": "Moscow"} returns 201\n'
                "- Telegram: /start responds with welcome message\n"
            )
            == []
        )
