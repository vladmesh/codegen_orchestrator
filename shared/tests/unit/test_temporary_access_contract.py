"""The capability-backed temporary QA grant keeps only an immutable target."""

from datetime import UTC, datetime

from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessStatus,
)
from shared.contracts.queues.qa import QAMessage


def test_durable_grant_has_identity_and_target_but_no_capability_or_environment_slot() -> None:
    grant = TemporaryAccessGrantDTO(
        id="tempaccess-qa-1",
        project_id="00000000-0000-0000-0000-000000000001",
        channel="telegram",
        external_id="8202532144",
        target_application_id=42,
        target_base_url="https://example.com",
        head_sha="a" * 40,
        qa_run_id="qa-1",
        grant_run_id="temporary-access-grant-1",
        grant_attempts=1,
        qa_message=QAMessage(
            project_id="00000000-0000-0000-0000-000000000001",
            initiating_run_id="live-1",
            telegram_chat_id="",
            deployed_url="https://example.com",
            application_id=42,
            acceptance_criteria="the bot answers /start",
            run_id="qa-1",
        ),
        status=TemporaryAccessStatus.GRANTING,
        granted_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )

    stored = grant.model_dump()
    assert stored["external_id"] == "8202532144"
    assert stored["grant_attempts"] == 1
    assert {"env_key", "subject", "capability", "bot_token"}.isdisjoint(stored)
