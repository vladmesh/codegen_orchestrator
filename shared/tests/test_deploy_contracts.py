"""Unit tests for deploy queue contracts."""

from shared.contracts.queues.deploy import DeployMessage, DeployTrigger


class TestDeployMessageAction:
    def test_action_field_default(self):
        """DeployMessage defaults action to 'create' for backward compatibility."""
        msg = DeployMessage(
            task_id="deploy-123",
            project_id="proj-abc",
        )
        assert msg.action == "create"

    def test_action_field_explicit(self):
        """DeployMessage accepts explicit action values."""
        for action in ("create", "feature", "fix"):
            msg = DeployMessage(
                task_id="deploy-123",
                project_id="proj-abc",
                action=action,
            )
            assert msg.action == action

    def test_action_survives_json_roundtrip(self):
        """action field survives JSON serialization/deserialization."""
        msg = DeployMessage(
            task_id="deploy-123",
            project_id="proj-abc",
            action="feature",
            triggered_by=DeployTrigger.WEBHOOK,
        )
        json_str = msg.model_dump_json()
        restored = DeployMessage.model_validate_json(json_str)
        assert restored.action == "feature"
        assert restored.triggered_by == DeployTrigger.WEBHOOK

    def test_backward_compat_no_action_in_dict(self):
        """DeployMessage works when action is missing from input dict (old messages)."""
        data = {
            "task_id": "deploy-old",
            "project_id": "proj-abc",
        }
        msg = DeployMessage.model_validate(data)
        assert msg.action == "create"
