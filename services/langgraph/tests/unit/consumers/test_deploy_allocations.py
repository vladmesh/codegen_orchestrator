"""Unit tests for deploy resource allocation."""

from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.factories import make_project, make_repository


@pytest.mark.asyncio
async def test_deploy_requests_template_infrastructure_port_allocations():
    """The deploy handoff requests host ports used by template 0.3.1."""
    from src.consumers.deploy import _allocate_resources

    project = make_project(
        title="Fancy_Project With Spaces",
        slug="fancy-project-with-spaces-0000",
        config={"modules": ["backend"]},
    )
    repository = make_repository()
    with (
        patch(
            "src.consumers.deploy.api_client.get_primary_repository",
            AsyncMock(return_value=repository),
        ),
        patch("src.allocations.ensure_project_allocations", AsyncMock(return_value={})) as allocate,
    ):
        await _allocate_resources(str(project.id), project)

    assert allocate.await_args.kwargs["modules"] == ["backend", "postgres", "redis"]
    assert allocate.await_args.kwargs["service_name"] == "fancy-project-with-spaces-0000"
