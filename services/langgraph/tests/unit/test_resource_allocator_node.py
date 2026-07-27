"""Regression tests for engineering resource allocation outcomes."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.application import DEFAULT_APPLICATION_RESERVED_RAM_MB
from tests.unit.factories import make_repository


@pytest.mark.asyncio
async def test_allocator_uses_default_ram_when_project_config_omits_estimate():
    """Ordinary projects do not persist ProjectSpec's optional RAM estimate."""
    from src.nodes.resource_allocator import ResourceAllocatorNode

    allocated = {"srv-1:8000": {"port": 8000}}
    with (
        patch(
            "src.nodes.resource_allocator.api_client.get_primary_repository",
            new=AsyncMock(return_value=make_repository()),
        ),
        patch(
            "src.nodes.resource_allocator.ensure_project_allocations",
            new=AsyncMock(return_value=allocated),
        ) as ensure_allocations,
    ):
        result = await ResourceAllocatorNode().run(
            {
                "project_id": "project-1",
                "project_spec": {"slug": "project-1", "config": {"modules": ["backend"]}},
            }
        )

    assert result["allocated_resources"] == allocated
    assert ensure_allocations.await_args.kwargs["min_ram_mb"] == DEFAULT_APPLICATION_RESERVED_RAM_MB
