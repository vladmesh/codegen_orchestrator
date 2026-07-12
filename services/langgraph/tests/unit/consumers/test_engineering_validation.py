"""Engineering consumer validates its input via EngineeringMessage before business logic."""

from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
import pytest

from shared.contracts.queues.engineering import EngineeringMessage
from src.consumers.engineering import process_engineering_job


@pytest.mark.asyncio
async def test_missing_required_field_raises_before_business_logic():
    """A job missing a required EngineeringMessage field fails validation and never
    reaches the API/business layer (no half-applied state)."""
    api = AsyncMock()
    redis = AsyncMock()
    # project_id is required by EngineeringMessage — omit it.
    bad_job = {"task_id": "eng-1", "user_id": "123", "action": "feature"}

    with patch("src.consumers.engineering.api_client", api):
        with pytest.raises(ValidationError):
            await process_engineering_job(bad_job, redis)

    api.patch.assert_not_called()
    api.get_project.assert_not_called()


def test_unknown_extra_fields_are_ignored_and_defaults_apply():
    """Wire additions do not crash the boundary; omitted optional fields take defaults."""
    msg = EngineeringMessage.model_validate(
        {"task_id": "t", "project_id": "p", "user_id": "u", "surprise": "x"}
    )
    assert msg.task_id == "t"
    assert msg.action.value == "create"
    assert msg.deploy_fix_attempt == 0
    assert msg.skip_deploy is False
