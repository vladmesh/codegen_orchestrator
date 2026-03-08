"""Tests for ArchitectMessage contract and queue constants."""

from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import (
    ARCHITECT_GROUP,
    ARCHITECT_QUEUE,
    QUEUE_TOPOLOGY,
)


class TestArchitectMessage:
    def test_serialization_roundtrip(self):
        msg = ArchitectMessage(
            story_id="story-abc123",
            project_id="proj-456",
            user_id="user-789",
        )
        data = msg.model_dump()
        restored = ArchitectMessage.model_validate(data)

        assert restored.story_id == "story-abc123"
        assert restored.project_id == "proj-456"
        assert restored.user_id == "user-789"

    def test_has_base_message_fields(self):
        msg = ArchitectMessage(
            story_id="story-abc123",
            project_id="proj-456",
            user_id="user-789",
        )
        assert msg.version == "1"
        assert msg.correlation_id
        assert msg.timestamp
        assert msg.request_id

    def test_json_roundtrip(self):
        msg = ArchitectMessage(
            story_id="story-abc123",
            project_id="proj-456",
            user_id="user-789",
        )
        json_str = msg.model_dump_json()
        restored = ArchitectMessage.model_validate_json(json_str)
        assert restored.story_id == msg.story_id


class TestArchitectQueueConstants:
    def test_queue_name(self):
        assert ARCHITECT_QUEUE == "architect:queue"

    def test_group_name(self):
        assert ARCHITECT_GROUP == "architect-consumers"

    def test_topology_includes_architect(self):
        architect_bindings = [b for b in QUEUE_TOPOLOGY if b.stream == ARCHITECT_QUEUE]
        assert len(architect_bindings) == 1
        binding = architect_bindings[0]
        assert binding.group == ARCHITECT_GROUP
        assert binding.description
