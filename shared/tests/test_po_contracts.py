"""Unit tests for shared.contracts.queues.po — PO stream contracts."""

from pydantic import TypeAdapter, ValidationError
import pytest

from shared.contracts.dto.owner_notification import OwnerNotification
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.po import (
    POInputMessage,
    POProactiveMessage,
    POReminderMessage,
    POResponse,
    POSystemEvent,
    POUserMessage,
    from_flat_fields,
    to_flat_fields,
)


class TestPOUserMessage:
    def test_defaults(self):
        msg = POUserMessage(text="hi", telegram_chat_id="42", request_id="abc")
        assert msg.type == "user_message"
        assert msg.text == "hi"
        assert msg.timestamp  # auto-filled

    def test_user_name_default_empty(self):
        msg = POUserMessage(text="hi", telegram_chat_id="42", request_id="abc")
        assert msg.user_name == ""

    def test_user_name_set(self):
        msg = POUserMessage(text="hi", telegram_chat_id="42", request_id="abc", user_name="Vlad")
        assert msg.user_name == "Vlad"

    def test_user_name_in_flat_fields(self):
        msg = POUserMessage(text="hi", telegram_chat_id="42", request_id="abc", user_name="Vlad")
        fields = to_flat_fields(msg)
        assert fields["user_name"] == "Vlad"

    def test_user_name_empty_omitted_from_flat_fields(self):
        msg = POUserMessage(text="hi", telegram_chat_id="42", request_id="abc")
        fields = to_flat_fields(msg)
        assert "user_name" not in fields

    def test_round_trip(self):
        msg = POUserMessage(
            text="hello", telegram_chat_id="1", request_id="r1", timestamp="2025-01-01T00:00:00"
        )
        fields = to_flat_fields(msg)
        restored = from_flat_fields(fields, POUserMessage)
        assert restored.text == msg.text
        assert restored.telegram_chat_id == msg.telegram_chat_id
        assert restored.request_id == msg.request_id

    def test_round_trip_with_user_name(self):
        msg = POUserMessage(
            text="hello",
            telegram_chat_id="1",
            request_id="r1",
            timestamp="2025-01-01T00:00:00",
            user_name="Vlad",
        )
        fields = to_flat_fields(msg)
        restored = from_flat_fields(fields, POUserMessage)
        assert restored.user_name == "Vlad"


class TestPOSystemEvent:
    def test_defaults(self):
        msg = POSystemEvent(event="completed", text="done")
        assert msg.type == "system_event"
        assert msg.timestamp  # auto-filled

    def test_round_trip(self):
        msg = POSystemEvent(
            event="completed",
            text="Task finished",
            task_id="t1",
            telegram_chat_id="42",
            project_id="00000000-0000-0000-0000-000000000001",
            timestamp="2025-01-01T00:00:00+00:00",
        )
        fields = to_flat_fields(msg)
        restored = from_flat_fields(fields, POSystemEvent)
        assert restored.event == msg.event
        assert restored.task_id == msg.task_id

    def test_empty_optional_fields_omitted(self):
        msg = POSystemEvent(event="progress", text="building")
        fields = to_flat_fields(msg)
        assert "task_id" not in fields
        assert "telegram_chat_id" not in fields
        assert "project_id" not in fields

    def test_rejects_unknown_owner_notification_event(self):
        """An owner notification cannot be accepted then dropped by PO."""
        with pytest.raises(ValidationError):
            POSystemEvent(event="story_engineering_budget_denied", text="budget exhausted")

        with pytest.raises(ValidationError):
            OwnerNotification.model_validate(
                {
                    "event": "story_engineering_budget_denied",
                    "text": "budget exhausted",
                    "story_id": "story-1",
                    "project_id": "project-1",
                    "terminal_status": StoryStatus.WAITING_HUMAN_REVIEW,
                    "state": "owed",
                    "owed_at": "2026-08-24T00:00:00+00:00",
                }
            )


class TestPOReminderMessage:
    def test_defaults(self):
        msg = POReminderMessage(text="check status", telegram_chat_id="42")
        assert msg.type == "reminder"

    def test_round_trip(self):
        msg = POReminderMessage(
            text="follow up", telegram_chat_id="99", timestamp="2025-06-01T12:00:00"
        )
        fields = to_flat_fields(msg)
        restored = from_flat_fields(fields, POReminderMessage)
        assert restored.text == msg.text
        assert restored.telegram_chat_id == msg.telegram_chat_id


class TestPOInputDiscriminator:
    adapter = TypeAdapter(POInputMessage)

    def test_user_message_dispatch(self):
        result = self.adapter.validate_python(
            {"type": "user_message", "text": "hi", "telegram_chat_id": "1", "request_id": "r1"}
        )
        assert isinstance(result, POUserMessage)

    def test_system_event_dispatch(self):
        result = self.adapter.validate_python(
            {"type": "system_event", "event": "completed", "text": "done"}
        )
        assert isinstance(result, POSystemEvent)

    def test_reminder_dispatch(self):
        result = self.adapter.validate_python(
            {"type": "reminder", "text": "check", "telegram_chat_id": "42"}
        )
        assert isinstance(result, POReminderMessage)

    def test_unknown_type_raises(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python({"type": "unknown", "text": "hi"})


class TestPOResponse:
    def test_basic(self):
        resp = POResponse(text="answer", telegram_chat_id="42")
        assert resp.error is None

    def test_with_error(self):
        resp = POResponse(text="oops", telegram_chat_id="42", error="true")
        assert resp.error == "true"

    def test_round_trip(self):
        resp = POResponse(text="answer", telegram_chat_id="42")
        fields = to_flat_fields(resp)
        restored = from_flat_fields(fields, POResponse)
        assert restored.text == resp.text


class TestPOProactiveMessage:
    def test_basic(self):
        msg = POProactiveMessage(text="notification", telegram_chat_id="42")
        assert msg.text == "notification"

    def test_round_trip(self):
        msg = POProactiveMessage(text="update", telegram_chat_id="99")
        fields = to_flat_fields(msg)
        restored = from_flat_fields(fields, POProactiveMessage)
        assert restored.text == msg.text
        assert restored.telegram_chat_id == msg.telegram_chat_id


class TestFlatFieldHelpers:
    def test_to_flat_fields_all_strings(self):
        msg = POUserMessage(text="hi", telegram_chat_id="42", request_id="r1", timestamp="ts")
        fields = to_flat_fields(msg)
        for v in fields.values():
            assert isinstance(v, str)

    def test_from_flat_fields_validation_error(self):
        with pytest.raises(ValidationError):
            from_flat_fields({}, POUserMessage)  # missing required fields


class TestQAVerificationFacts:
    """The settling event's QA facts travel as one JSON-encoded flat field."""

    FACTS = {
        "qa_run_id": "qa-1",
        "passed_checks": ["GET /health returns 200"],
        "unverified_checks": [
            {"name": "POST /api/transactions", "reason": "needs a write", "origin": "withheld"}
        ],
    }

    def test_the_flat_field_is_json_and_decodes_back(self):
        import json

        event = POSystemEvent(
            event="story_completed", text="done", telegram_chat_id="1", qa_verification=self.FACTS
        )

        fields = to_flat_fields(event)

        assert json.loads(fields["qa_verification"]) == self.FACTS
        assert from_flat_fields(fields, POSystemEvent) == event

    def test_an_event_without_facts_writes_no_field(self):
        event = POSystemEvent(event="story_quarantined", text="stopped", telegram_chat_id="1")

        assert "qa_verification" not in to_flat_fields(event)

    def test_an_unknown_origin_is_refused(self):
        facts = {**self.FACTS, "unverified_checks": [{"name": "x", "reason": "y", "origin": "?"}]}

        with pytest.raises(ValidationError):
            POSystemEvent(
                event="story_completed", text="done", telegram_chat_id="1", qa_verification=facts
            )

    def test_the_owner_record_carries_the_same_facts(self):
        from datetime import UTC, datetime

        record = OwnerNotification(
            event="story_completed",
            text="done",
            story_id="story-1",
            project_id="p",
            terminal_status=StoryStatus.COMPLETED,
            state="owed",
            owed_at=datetime.now(UTC),
            qa_verification=self.FACTS,
        )

        assert record.model_dump(mode="json")["qa_verification"] == self.FACTS
