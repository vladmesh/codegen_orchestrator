"""Unit tests for deploy queue contracts."""

import pytest

from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger


class TestDeployAction:
    def test_is_str_enum(self):
        assert isinstance(DeployAction.CREATE, str)
        assert DeployAction.CREATE.value == "create"

    def test_all_values(self):
        assert DeployAction.CREATE == "create"
        assert DeployAction.FEATURE == "feature"
        assert DeployAction.FIX == "fix"
        assert DeployAction.STOP == "stop"
        assert DeployAction.UNDEPLOY == "undeploy"

    def test_roundtrip_via_string(self):
        for action in DeployAction:
            assert DeployAction(action.value) == action


class TestDeployMessageAction:
    def test_action_field_default(self):
        """DeployMessage defaults action to 'create' for backward compatibility."""
        msg = DeployMessage(
            task_id="deploy-123",
            project_id="proj-abc",
        )
        assert msg.action == DeployAction.CREATE

    def test_action_field_explicit(self):
        """DeployMessage accepts explicit action values."""
        for action in DeployAction:
            msg = DeployMessage(
                task_id="deploy-123",
                project_id="proj-abc",
                action=action,
            )
            assert msg.action == action

    def test_action_accepts_string_values(self):
        """DeployMessage coerces raw strings into DeployAction enum."""
        for action_str in ("create", "feature", "fix", "stop", "undeploy"):
            msg = DeployMessage(
                task_id="deploy-123",
                project_id="proj-abc",
                action=action_str,
            )
            assert isinstance(msg.action, DeployAction)
            assert msg.action.value == action_str

    def test_action_rejects_invalid_value(self):
        with pytest.raises(ValueError, match="Input should be"):
            DeployMessage(
                task_id="deploy-123",
                project_id="proj-abc",
                action="invalid",
            )

    def test_action_survives_json_roundtrip(self):
        """action field survives JSON serialization/deserialization."""
        msg = DeployMessage(
            task_id="deploy-123",
            project_id="proj-abc",
            action=DeployAction.FEATURE,
            triggered_by=DeployTrigger.WEBHOOK,
        )
        json_str = msg.model_dump_json()
        restored = DeployMessage.model_validate_json(json_str)
        assert restored.action == DeployAction.FEATURE
        assert restored.triggered_by == DeployTrigger.WEBHOOK

    def test_stop_undeploy_roundtrip(self):
        """stop/undeploy actions survive serialization roundtrip."""
        for action in (DeployAction.STOP, DeployAction.UNDEPLOY):
            msg = DeployMessage(
                task_id="deploy-123",
                project_id="proj-abc",
                action=action,
            )
            data = msg.model_dump()
            restored = DeployMessage.model_validate(data)
            assert restored.action == action

    def test_backward_compat_no_action_in_dict(self):
        """DeployMessage works when action is missing from input dict (old messages)."""
        data = {
            "task_id": "deploy-old",
            "project_id": "proj-abc",
        }
        msg = DeployMessage.model_validate(data)
        assert msg.action == DeployAction.CREATE

    def test_backward_compat_string_action_in_dict(self):
        """DeployMessage works when action is a raw string in input dict (old messages)."""
        data = {
            "task_id": "deploy-old",
            "project_id": "proj-abc",
            "action": "feature",
        }
        msg = DeployMessage.model_validate(data)
        assert msg.action == DeployAction.FEATURE
