"""Unit tests for shared.contracts.queues.engineering."""

from shared.contracts.queues.engineering import EngineeringMessage, EngineeringResult


class TestEngineeringMessage:
    def test_branch_field_default_none(self):
        msg = EngineeringMessage(
            task_id="eng-123",
            project_id="proj-456",
            user_id="user-1",
        )
        assert msg.branch is None

    def test_branch_field_set(self):
        msg = EngineeringMessage(
            task_id="eng-123",
            project_id="proj-456",
            user_id="user-1",
            branch="story/story-abc",
        )
        assert msg.branch == "story/story-abc"

    def test_branch_serialization_roundtrip(self):
        msg = EngineeringMessage(
            task_id="eng-123",
            project_id="proj-456",
            user_id="user-1",
            branch="story/story-abc",
        )
        data = msg.model_dump()
        restored = EngineeringMessage(**data)
        assert restored.branch == "story/story-abc"

    def test_branch_absent_in_json_uses_default(self):
        """Backward compat: old messages without branch field still parse."""
        data = {
            "task_id": "eng-123",
            "project_id": "proj-456",
            "user_id": "user-1",
        }
        msg = EngineeringMessage(**data)
        assert msg.branch is None


class TestEngineeringResult:
    def test_branch_field(self):
        result = EngineeringResult(
            request_id="req-1",
            status="success",
            files_changed=["foo.py"],
            commit_sha="abc123",
            branch="story/story-abc",
        )
        assert result.branch == "story/story-abc"
