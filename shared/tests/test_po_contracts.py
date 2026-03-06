"""Unit tests for shared.contracts.queues.po — PO stream contracts."""

from pydantic import TypeAdapter, ValidationError
import pytest

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
        msg = POUserMessage(text="hi", user_id="42", request_id="abc")
        assert msg.type == "user_message"
        assert msg.text == "hi"
        assert msg.timestamp  # auto-filled

    def test_user_name_default_empty(self):
        msg = POUserMessage(text="hi", user_id="42", request_id="abc")
        assert msg.user_name == ""

    def test_user_name_set(self):
        msg = POUserMessage(text="hi", user_id="42", request_id="abc", user_name="Vlad")
        assert msg.user_name == "Vlad"

    def test_user_name_in_flat_fields(self):
        msg = POUserMessage(text="hi", user_id="42", request_id="abc", user_name="Vlad")
        fields = to_flat_fields(msg)
        assert fields["user_name"] == "Vlad"

    def test_user_name_empty_omitted_from_flat_fields(self):
        msg = POUserMessage(text="hi", user_id="42", request_id="abc")
        fields = to_flat_fields(msg)
        assert "user_name" not in fields

    def test_round_trip(self):
        msg = POUserMessage(
            text="hello", user_id="1", request_id="r1", timestamp="2025-01-01T00:00:00"
        )
        fields = to_flat_fields(msg)
        restored = from_flat_fields(fields, POUserMessage)
        assert restored.text == msg.text
        assert restored.user_id == msg.user_id
        assert restored.request_id == msg.request_id

    def test_round_trip_with_user_name(self):
        msg = POUserMessage(
            text="hello",
            user_id="1",
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
            user_id="42",
            project_id="p1",
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
        assert "user_id" not in fields
        assert "project_id" not in fields


class TestPOReminderMessage:
    def test_defaults(self):
        msg = POReminderMessage(text="check status", user_id="42")
        assert msg.type == "reminder"

    def test_round_trip(self):
        msg = POReminderMessage(text="follow up", user_id="99", timestamp="2025-06-01T12:00:00")
        fields = to_flat_fields(msg)
        restored = from_flat_fields(fields, POReminderMessage)
        assert restored.text == msg.text
        assert restored.user_id == msg.user_id


class TestPOInputDiscriminator:
    adapter = TypeAdapter(POInputMessage)

    def test_user_message_dispatch(self):
        result = self.adapter.validate_python(
            {"type": "user_message", "text": "hi", "user_id": "1", "request_id": "r1"}
        )
        assert isinstance(result, POUserMessage)

    def test_system_event_dispatch(self):
        result = self.adapter.validate_python(
            {"type": "system_event", "event": "completed", "text": "done"}
        )
        assert isinstance(result, POSystemEvent)

    def test_reminder_dispatch(self):
        result = self.adapter.validate_python(
            {"type": "reminder", "text": "check", "user_id": "42"}
        )
        assert isinstance(result, POReminderMessage)

    def test_unknown_type_raises(self):
        with pytest.raises(ValidationError):
            self.adapter.validate_python({"type": "unknown", "text": "hi"})


class TestPOResponse:
    def test_basic(self):
        resp = POResponse(text="answer", user_id="42")
        assert resp.error is None

    def test_with_error(self):
        resp = POResponse(text="oops", user_id="42", error="true")
        assert resp.error == "true"

    def test_round_trip(self):
        resp = POResponse(text="answer", user_id="42")
        fields = to_flat_fields(resp)
        restored = from_flat_fields(fields, POResponse)
        assert restored.text == resp.text


class TestPOProactiveMessage:
    def test_basic(self):
        msg = POProactiveMessage(text="notification", user_id="42")
        assert msg.text == "notification"

    def test_round_trip(self):
        msg = POProactiveMessage(text="update", user_id="99")
        fields = to_flat_fields(msg)
        restored = from_flat_fields(fields, POProactiveMessage)
        assert restored.text == msg.text
        assert restored.user_id == msg.user_id


class TestFlatFieldHelpers:
    def test_to_flat_fields_all_strings(self):
        msg = POUserMessage(text="hi", user_id="42", request_id="r1", timestamp="ts")
        fields = to_flat_fields(msg)
        for v in fields.values():
            assert isinstance(v, str)

    def test_from_flat_fields_validation_error(self):
        with pytest.raises(ValidationError):
            from_flat_fields({}, POUserMessage)  # missing required fields
