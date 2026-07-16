"""Unit tests for QA queue message contract and queue topology."""

from pydantic import ValidationError
import pytest

from shared.contracts.dto.run import RunType
from shared.contracts.queues.qa import QAMessage, QAOutcome
from shared.queues import QA_GROUP, QA_QUEUE, QUEUE_TOPOLOGY

CRITERIA = "- GET /health returns 200"


class TestQAMessage:
    def test_minimal_construction(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=17,
            acceptance_criteria=CRITERIA,
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
            acceptance_criteria=CRITERIA,
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
            acceptance_criteria=CRITERIA,
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
            acceptance_criteria=CRITERIA,
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
            acceptance_criteria=CRITERIA,
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
        assert restored.acceptance_criteria == CRITERIA

    def test_acceptance_criteria_is_required(self):
        """QA can't run without criteria, so a message may not omit them.

        The producer resolves them from the repository; a message that reaches the
        queue without them is a bug that must surface here, not in the consumer.
        """
        with pytest.raises(ValidationError):
            QAMessage(
                story_id="story-abc",
                project_id="proj-123",
                user_id="user-1",
                deployed_url="https://example.com",
                application_id=17,
            )

    def test_acceptance_criteria_may_not_be_blank(self):
        """Blank criteria parse to "no checks" — reject them at the contract."""
        with pytest.raises(ValidationError):
            QAMessage(
                story_id="story-abc",
                project_id="proj-123",
                user_id="user-1",
                deployed_url="https://example.com",
                application_id=17,
                acceptance_criteria="",
            )


class TestRunTypeQA:
    def test_qa_run_type_exists(self):
        assert RunType.QA == "qa"

    def test_qa_run_type_value(self):
        assert RunType.QA.value == "qa"


class TestQAOutcome:
    def test_values(self):
        assert QAOutcome.PASSED == "passed"
        assert QAOutcome.FAILED == "failed"
        assert QAOutcome.EXHAUSTED == "exhausted"
        assert QAOutcome.ERROR == "error"

    def test_is_str_enum(self):
        assert isinstance(QAOutcome.PASSED, str)
        assert QAOutcome.PASSED.value == "passed"

    def test_roundtrip_via_string(self):
        for outcome in QAOutcome:
            assert QAOutcome(outcome.value) == outcome


class TestQAMessageRunId:
    def test_run_id_field(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=17,
            acceptance_criteria=CRITERIA,
            run_id="qa-run-001",
        )
        assert msg.run_id == "qa-run-001"

    def test_run_id_roundtrip(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=17,
            acceptance_criteria=CRITERIA,
            run_id="qa-run-002",
        )
        data = msg.model_dump()
        restored = QAMessage.model_validate(data)
        assert restored.run_id == "qa-run-002"


class TestQAMessageOptionalStoryId:
    def test_story_id_defaults_to_empty(self):
        """QAMessage story_id defaults to empty string for standalone triggers."""
        msg = QAMessage(
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=17,
            acceptance_criteria=CRITERIA,
        )
        assert msg.story_id == ""

    def test_story_id_explicit(self):
        msg = QAMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=17,
            acceptance_criteria=CRITERIA,
        )
        assert msg.story_id == "story-abc"

    def test_standalone_roundtrip(self):
        """QAMessage without story_id survives serialization."""
        msg = QAMessage(
            project_id="proj-123",
            user_id="user-1",
            deployed_url="https://example.com",
            application_id=17,
            acceptance_criteria=CRITERIA,
        )
        data = msg.model_dump()
        restored = QAMessage.model_validate(data)
        assert restored.story_id == ""

    def test_backward_compat_no_story_id_in_dict(self):
        """QAMessage works when story_id is missing from input dict."""
        data = {
            "project_id": "proj-123",
            "user_id": "user-1",
            "deployed_url": "https://example.com",
            "application_id": 17,
            "acceptance_criteria": CRITERIA,
        }
        msg = QAMessage.model_validate(data)
        assert msg.story_id == ""


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
