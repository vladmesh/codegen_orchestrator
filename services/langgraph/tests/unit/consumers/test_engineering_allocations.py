"""Regression tests for allocation failure result handling."""

from unittest.mock import AsyncMock, patch

import pytest

from tests.unit.factories import make_project


@pytest.mark.asyncio
async def test_non_allocation_error_keeps_its_original_run_message():
    """Validation metadata is mandatory only for typed admission failures."""
    from src.consumers.engineering import _resolve_allocations

    with (
        patch(
            "src.consumers.engineering.resource_allocator_node.run",
            new=AsyncMock(return_value={"errors": ["No repository found for project"]}),
        ),
        patch("src.consumers.engineering.api_client.patch", new=AsyncMock()) as patch_run,
    ):
        result = await _resolve_allocations("task-1", "project-1", make_project())

    assert result is None
    assert patch_run.await_args.kwargs["json"]["error_message"] == "No repository found for project"
