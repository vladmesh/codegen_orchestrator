"""What the production reset is allowed to destroy.

The script itself is plumbing over ssh, but the decisions below answer «may this
be deleted». A wrong answer here deletes somebody else's server, repository or
project, so they are kept free of side effects and tested on their own.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from danger_prod_reset import (  # noqa: E402
    CONFIRMATION_PHRASE,
    DEFAULT_KEEP_REPOS,
    confirmation_matches,
    managed_handles,
    parse_server_ids,
    repos_to_delete,
    unexpected_owners,
    wipeable_servers,
)


class TestServerAllowlist:
    def test_ids_are_read_from_the_env_value(self):
        assert parse_server_ids("275301, 275198") == {275301, 275198}

    def test_an_empty_setting_allows_nothing(self):
        assert parse_server_ids("") == set()
        assert parse_server_ids(None) == set()

    def test_handles_follow_the_provider_id(self):
        assert managed_handles({275301}) == {"vps-275301"}

    def test_a_managed_server_outside_the_allowlist_is_not_wiped(self):
        """The database calling a server managed is not permission to wipe it.

        The secretary's own machine (273036) is managed by the same account and
        must never be reachable this way.
        """
        servers = [
            {"handle": "vps-275301", "is_managed": True},
            {"handle": "vps-273036", "is_managed": True},
        ]
        assert wipeable_servers(servers, {"vps-275301"}) == [servers[0]]

    def test_an_unmanaged_server_in_the_allowlist_is_still_skipped(self):
        servers = [{"handle": "vps-275301", "is_managed": False}]
        assert wipeable_servers(servers, {"vps-275301"}) == []


class TestRepositories:
    def test_the_keep_list_survives(self):
        repos = ["p-1", "fortune-teller-bot", "p-2"]
        assert repos_to_delete(repos, DEFAULT_KEEP_REPOS) == ["p-1", "p-2"]

    def test_nothing_is_deleted_when_everything_is_kept(self):
        assert repos_to_delete(["a", "b"], ("a", "b")) == []


class TestOwners:
    def test_a_declared_account_raises_no_objection(self):
        users = [{"telegram_id": 625038902}]
        assert unexpected_owners(users, {625038902}) == []

    def test_an_undeclared_account_is_reported(self):
        """Once real users exist, the reset must not proceed silently."""
        users = [{"telegram_id": 625038902}, {"telegram_id": 111}]
        assert unexpected_owners(users, {625038902}) == [{"telegram_id": 111}]

    def test_no_declared_account_means_every_user_is_unexpected(self):
        users = [{"telegram_id": 625038902}]
        assert unexpected_owners(users, set()) == users


class TestConfirmation:
    def test_the_phrase_must_be_exact(self):
        assert confirmation_matches(CONFIRMATION_PHRASE)

    @pytest.mark.parametrize("typed", [None, "", "yes", CONFIRMATION_PHRASE.lower()])
    def test_anything_else_refuses(self, typed):
        assert not confirmation_matches(typed)
