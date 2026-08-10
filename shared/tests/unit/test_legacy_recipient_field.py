"""A message addressed by the removed ``user_id`` field is refused, not emptied.

``user_id`` meant a Telegram chat id from one producer and an internal
``User.id`` from another. Pydantic ignores unknown fields by default, so without
this rejection a payload from before the rename would validate with its
recipient silently dropped: work nobody hears the result of, and no trace that a
recipient was supplied at all. Every addressable contract therefore fails
validation on it, and the consumers that see the failure raise an admin alert.
"""

from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
import pytest

from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.deploy import DeployMessage
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.po import (
    POProactiveMessage,
    POSystemEvent,
    POUserMessage,
)
from shared.contracts.queues.qa import QAMessage
from shared.contracts.queues.scaffold import ScaffoldMessage
from shared.contracts.recipient import (
    LEGACY_RECIPIENT_FIELD,
    LegacyRecipientFieldError,
    alert_legacy_recipient_field,
    has_legacy_recipient_field,
    legacy_recipient_identifiers,
)


class TestQueueContractsRejectIt:
    def test_a_legacy_deploy_message_is_rejected_instead_of_losing_its_recipient(self):
        payload = {"task_id": "run-1", "project_id": "project-1", "user_id": "1"}

        with pytest.raises(ValidationError, match=LEGACY_RECIPIENT_FIELD):
            DeployMessage.model_validate(payload)

    @pytest.mark.parametrize(
        "message_type",
        [ArchitectMessage, EngineeringMessage, QAMessage, ScaffoldMessage],
    )
    def test_every_addressable_queue_contract_rejects_it(self, message_type):
        payload = {
            "task_id": "run-1",
            "project_id": "project-1",
            "story_id": "story-1",
            "description": "do the thing",
            "run_id": "run-1",
            "deployed_url": "https://example.test",
            "user_id": "1",
        }

        with pytest.raises(ValidationError, match=LEGACY_RECIPIENT_FIELD):
            message_type.model_validate(payload)

    @pytest.mark.parametrize(
        "message_type",
        [POUserMessage, POSystemEvent, POProactiveMessage],
    )
    def test_po_contracts_reject_it(self, message_type):
        payload = {
            "text": "your project is deployed",
            "event": "story_completed",
            "request_id": "req-1",
            "user_id": "987654321",
        }

        with pytest.raises(ValidationError, match=LEGACY_RECIPIENT_FIELD):
            message_type.model_validate(payload)

    def test_a_message_naming_the_new_fields_validates(self):
        msg = DeployMessage.model_validate(
            {
                "task_id": "run-1",
                "project_id": "project-1",
                "telegram_chat_id": "987654321",
            }
        )

        assert msg.telegram_chat_id == "987654321"
        assert LEGACY_RECIPIENT_FIELD not in msg.model_dump()

    def test_the_error_is_a_legacy_recipient_field_error(self):
        with pytest.raises(ValidationError) as caught:
            POSystemEvent.model_validate({"event": "story_completed", "text": "x", "user_id": "1"})

        assert isinstance(caught.value.errors()[0]["ctx"]["error"], LegacyRecipientFieldError)


class TestRecipientHelpers:
    def test_identifiers_are_collected_for_the_alert(self):
        payload = {
            "user_id": "1",
            "event": "story_completed",
            "story_id": "story-7",
            "project_id": "proj-3",
            "task_id": "eng-9",
            "text": "ignored",
        }

        assert has_legacy_recipient_field(payload) is True
        assert legacy_recipient_identifiers(payload) == {
            "po_event": "story_completed",
            "story_id": "story-7",
            "project_id": "proj-3",
            "task_id": "eng-9",
        }

    def test_a_payload_without_the_field_is_not_flagged(self):
        assert has_legacy_recipient_field({"telegram_chat_id": "987654321"}) is False

    @pytest.mark.asyncio
    async def test_the_alert_names_story_project_and_event(self):
        payload = {
            "user_id": "1",
            "event": "story_completed",
            "story_id": "story-7",
            "project_id": "proj-3",
        }

        with patch("shared.notifications.notify_admins_best_effort", new=AsyncMock()) as alert:
            await alert_legacy_recipient_field(source="po:proactive", entry_id="1-0", data=payload)

        alert.assert_awaited_once()
        text = alert.await_args.args[0]
        assert LEGACY_RECIPIENT_FIELD in text
        assert "story-7" in text
        assert "proj-3" in text
        assert "story_completed" in text
        assert alert.await_args.kwargs["level"] == "error"
