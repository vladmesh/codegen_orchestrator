"""Resource Allocator - functional node for port/server allocation."""

import structlog

from ..tools.ports import allocate_port, get_next_available_port
from ..tools.servers import find_suitable_server
from .base import FunctionalNode

logger = structlog.get_logger()


class ResourceAllocatorNode(FunctionalNode):
    """Allocate server resources for a project before deployment.

    This is a FUNCTIONAL node (no LLM) - deterministic logic only.
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

        # Check if already allocated
        existing = state.get("allocated_resources", {})
        if existing:
            logger.info(
                "resources_already_allocated",
                project_id=project_id,
                count=len(existing),
            )
            return {}  # Already done

        # Determine modules from project config
        config = project_spec.get("config", {})
        modules = config.get("modules", ["backend"])

        # Estimate resources (simple heuristic)
        min_ram_mb = config.get("estimated_ram_mb", 512)

        logger.info(
            "resource_allocation_start",
            project_id=project_id,
            modules=modules,
            min_ram_mb=min_ram_mb,
        )

        try:
            # 1. Find suitable server
            server = await find_suitable_server.ainvoke(
                {
                    "min_ram_mb": min_ram_mb,
                    "min_disk_mb": 1024,
                }
            )

            if not server:
                return {
                    "errors": state.get("errors", [])
                    + ["No suitable server found with enough resources"],
                }

            server_handle = server.handle

            # 2. Allocate port for each module
            allocated = {}
            for module in modules:
                # Get next available port
                port = await get_next_available_port.ainvoke(
                    {
                        "server_handle": server_handle,
                        "start_port": 8000,
                    }
                )

                # Allocate it
                allocation = await allocate_port.ainvoke(
                    {
                        "server_handle": server_handle,
                        "port": port,
                        "service_name": module,
                        "project_id": project_id,
                    }
                )

                port_key = f"{server_handle}:{port}"
                allocated[port_key] = allocation.model_dump()

                logger.info(
                    "port_allocated",
                    project_id=project_id,
                    module=module,
                    server=server_handle,
                    port=port,
                )

            return {"allocated_resources": allocated}

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
