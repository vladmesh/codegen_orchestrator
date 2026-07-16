"""Resource Allocator - functional node for port/server allocation."""

import structlog

from ..allocations import AllocationError, ensure_project_allocations
from ..clients.api import api_client
from .base import FunctionalNode

logger = structlog.get_logger()


class ResourceAllocatorNode(FunctionalNode):
    """Allocate server resources for a project before deployment.

    This is a deterministic functional node.
    Uses shared allocation logic from tools/allocator.py.
    """

    def __init__(self):
        super().__init__(node_id="resource_allocator")

    async def run(self, state: dict) -> dict:
        """Allocate ports for each module in project."""
        project_id = state.get("project_id")
        project_spec = state.get("project_spec") or {}

        if not project_id:
            return {
                "errors": state.get("errors", []) + ["No project_id provided"],
            }

        # Check if already allocated in state
        existing = state.get("allocated_resources", {})
        if existing:
            logger.info(
                "resources_already_allocated",
                project_id=project_id,
                count=len(existing),
            )
            return {}  # Already done

        # Get config from project spec
        config = project_spec.get("config", {})
        modules = config.get("modules", ["backend"])
        min_ram_mb = config.get("estimated_ram_mb", 512)
        service_name = project_spec.get("name", "project").replace(" ", "_").lower()

        # Get repo_id from primary repository
        repo = await api_client.get_primary_repository(project_id)
        if not repo:
            return {
                "errors": state.get("errors", []) + ["No repository found for project"],
            }
        repo_id = repo.id

        logger.info(
            "resource_allocation_start",
            project_id=project_id,
            repo_id=repo_id,
            modules=modules,
            min_ram_mb=min_ram_mb,
        )

        try:
            allocated = await ensure_project_allocations(
                project_id=project_id,
                repo_id=repo_id,
                service_name=service_name,
                modules=modules,
                min_ram_mb=min_ram_mb,
            )
            return {"allocated_resources": allocated}

        except AllocationError as e:
            logger.error(
                "resource_allocation_failed",
                project_id=project_id,
                error=str(e),
            )
            return {
                "errors": state.get("errors", []) + [str(e)],
            }

        except Exception as e:
            logger.error(
                "resource_allocation_failed",
                project_id=project_id,
                error=str(e),
                exc_info=True,
            )
            return {
                "errors": state.get("errors", []) + [f"Resource allocation failed: {e}"],
            }


resource_allocator_node = ResourceAllocatorNode()
