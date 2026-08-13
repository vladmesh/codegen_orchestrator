"""What the grant contract refuses to let a caller say.

The revocation guarantee rests on one asymmetry: the system can ask for a value
to be cleared, but only the server can say it is gone. These are the places where
that asymmetry is written into the types rather than left to a caller's manners.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shared.contracts.dto.temporary_access import (
    REVOKE_CONFIRMATION_READINGS,
    REVOKE_CONFIRMATION_WINDOW,
    TemporaryAccessGrantDTO,
    TemporaryAccessGrantUpdate,
    TemporaryAccessObservation,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.qa import QAMessage

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def _grant(**overrides) -> dict:
    fields = {
        "id": "tempaccess-1",
        "project_id": PROJECT_ID,
        "env_key": "TG_BOT_TEST_TELEGRAM_ID",
        "subject": "424242",
        "head_sha": "a" * 40,
        "qa_run_id": "qa-1",
        "grant_run_id": "deploy-grant-1",
        "qa_message": QAMessage(
            project_id=PROJECT_ID,
            initiating_run_id="live-run-1",
            telegram_chat_id="",
            deployed_url="https://example.com",
            application_id=42,
            acceptance_criteria="the bot answers /start",
            run_id="qa-1",
        ),
        "status": TemporaryAccessStatus.REVOKED,
        "granted_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
        "revoked_at": datetime.now(UTC),
        "revoke_reason": TemporaryAccessRevokeReason.RUN_TERMINAL,
        "observation_id": "envobs-deploy-revoke-1-1",
    }
    fields.update(overrides)
    return fields


def test_an_update_may_not_ask_for_revoked():
    """A caller asserting the access is gone is asserting what it cannot see."""
    with pytest.raises(ValueError, match="observation of the running service"):
        TemporaryAccessGrantUpdate(status=TemporaryAccessStatus.REVOKED)


def test_every_other_status_is_still_an_update():
    """The refusal is about one status, not about the record being frozen."""
    for status in TemporaryAccessStatus:
        if status is TemporaryAccessStatus.REVOKED:
            continue
        assert TemporaryAccessGrantUpdate(status=status).status is status


def test_a_revoked_grant_names_the_reading_that_closed_it():
    """Without it the record cannot say why it believes the access is gone."""
    with pytest.raises(ValueError, match="name the reading"):
        TemporaryAccessGrantDTO(**_grant(observation_id=None))


def test_a_revoked_grant_still_needs_its_moment_and_its_reason():
    with pytest.raises(ValueError, match="revoked_at and revoke_reason"):
        TemporaryAccessGrantDTO(**_grant(revoked_at=None))


def test_a_reading_of_nothing_running_is_not_a_reading():
    """No containers is an unreachable service, and that never reaches the record."""
    with pytest.raises(ValueError):
        TemporaryAccessObservation(
            observation_id="envobs-deploy-revoke-1-0",
            application_id=42,
            server_handle="vps-1",
            service_slug="palindrome-bot",
            env_key="TG_BOT_TEST_TELEGRAM_ID",
            present=False,
            containers=0,
        )


def test_confirmation_takes_more_than_one_reading_and_more_than_an_instant():
    """The two bounds the guarantee's interval is made of."""
    assert REVOKE_CONFIRMATION_READINGS >= 2
    assert REVOKE_CONFIRMATION_WINDOW.total_seconds() > 0
