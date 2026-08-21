"""Audience canonicalization: the one place a comma-separated list is touched."""

from shared.contracts.bot_access import (
    add_to_audience,
    canonical_audience,
    remove_from_audience,
)


class TestCanonicalAudience:
    def test_sorts_deduplicates_and_drops_garbage(self):
        assert canonical_audience("84, 42,84, x") == "42,84"

    def test_empty_stays_empty(self):
        assert canonical_audience("") == ""
        assert canonical_audience("  ") == ""

    def test_round_trips_through_the_template_parser(self):
        from shared.contracts.bot_access import parse_allowed_telegram_ids

        for value in ("", "42", "42,84", "7,8,9"):
            assert parse_allowed_telegram_ids(canonical_audience(value)) == (
                {int(v) for v in value.split(",") if v.strip()}
            )


class TestAddToAudience:
    def test_adds_sorted(self):
        assert add_to_audience("42", 7) == "7,42"

    def test_adding_an_existing_id_is_the_same_audience(self):
        assert add_to_audience("42,84", 84) == "42,84"


class TestRemoveFromAudience:
    def test_removes_only_the_named_id(self):
        assert remove_from_audience("42,84,99", 84) == "42,99"

    def test_removing_an_absent_id_changes_nothing(self):
        assert remove_from_audience("42,84", 999) == "42,84"
