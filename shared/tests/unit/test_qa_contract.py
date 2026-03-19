"""Unit tests for QA queue message contract and queue topology."""

from shared.contracts.queues.qa import QAMessage
from shared.queues import QA_GROUP, QA_QUEUE, QUEUE_TOPOLOGY


class TestQAMessage:
    def test_minimal_construction(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=17,
        )
        assert msg.story_id == "story-abc"
        assert msg.project_id == "proj-123"
        assert msg.user_id == "user-1"
        assert msg.deployed_url == "https://example.com"
        assert msg.application_id == 17

    def test_defaults(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=1,
        )
        assert msg.qa_attempt == 0
        assert msg.bot_username is None
        assert msg.callback_stream is None
        assert msg.version == "1"
        assert msg.request_id  # auto-generated UUID
        assert msg.correlation_id  # auto-generated UUID

    def test_with_bot_username(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=1,
            bot_username="my_test_bot",
        )
        assert msg.bot_username == "my_test_bot"

    def test_qa_attempt_increment(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=1,
            qa_attempt=2,
        )
        assert msg.qa_attempt == 2

    def test_serialization_roundtrip(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=17,
            bot_username="bot",
            qa_attempt=1,
        )
        data = msg.model_dump()
        restored = QAMessage.model_validate(data)
        assert restored.story_id == msg.story_id
        assert restored.deployed_url == msg.deployed_url
        assert restored.qa_attempt == msg.qa_attempt
        assert restored.bot_username == msg.bot_username
        assert restored.application_id == 17


class TestQAQueueTopology:
    def test_qa_queue_constant(self):
        assert QA_QUEUE == "qa:queue"

    def test_qa_group_constant(self):
        assert QA_GROUP == "qa-consumers"

    def test_qa_queue_in_topology(self):
        streams = [b.stream for b in QUEUE_TOPOLOGY]
        assert QA_QUEUE in streams

    def test_qa_queue_binding_group(self):
        binding = next(b for b in QUEUE_TOPOLOGY if b.stream == QA_QUEUE)
        assert binding.group == QA_GROUP
