"""Provisioner proxy node - delegates to infrastructure-worker.

Lightweight node that maintains the provisioner interface in the graph
while delegating actual provisioning work to the infrastructure-worker service.
"""

from langchain_core.messages import AIMessage
import structlog

from ..clients import provisioner_client
from ..nodes.base import FunctionalNode, log_node_execution

logger = structlog.get_logger(__name__)


class ProvisionerProxyNode(FunctionalNode):
    """Proxy node that delegates provisioning to infrastructure-worker."""

    def __init__(self):
        super().__init__(node_id="provisioner_proxy")

    @log_node_execution("provisioner")
    async def run(self, state: dict) -> dict:
        """Delegate provisioning to infrastructure-worker via Redis.

        Args:
            state: Graph state containing:
                - server_to_provision: Server handle to provision
                - is_incident_recovery: Whether this is incident recovery
                - force_reinstall: Force OS reinstall flag

        Returns:
            Updated state with provisioning result
        """
        server_handle = state.get("server_to_provision")
        is_recovery = state.get("is_incident_recovery", False)
        force_reinstall = state.get("force_reinstall", False)
        correlation_id = state.get("correlation_id")

        if not server_handle:
            return {
                "messages": [AIMessage(content="⚠️ No server specified for provisioning")],
                "errors": state.get("errors", []) + ["No server_to_provision in state"],
            }

        logger.info(
            "provisioner_proxy_queueing",
            server_handle=server_handle,
            is_recovery=is_recovery,
            force_reinstall=force_reinstall,
        )

        try:
            # Queue provisioning job to infrastructure-worker
            request_id = await provisioner_client.trigger_provisioning(
                server_handle=server_handle,
                force_reinstall=force_reinstall,
                is_recovery=is_recovery,
                correlation_id=correlation_id,
            )

            logger.info(
                "provisioner_proxy_waiting",
                request_id=request_id,
                server_handle=server_handle,
            )

            # Wait for result (timeout: 20 minutes)
            result = await provisioner_client.wait_for_result(request_id, timeout=1200)

            if not result:
                logger.error(
                    "provisioner_proxy_timeout",
                    request_id=request_id,
                    server_handle=server_handle,
                )
                return {
                    "messages": [
                        AIMessage(
                            content=f"❌ Provisioning timeout for {server_handle} after 20 minutes"
                        )
                    ],
                    "errors": state.get("errors", []) + ["Provisioning timeout"],
                    "provisioning_result": {"status": "timeout"},
                    "current_agent": "provisioner",
                }

            logger.info(
                "provisioner_proxy_complete",
                request_id=request_id,
                server_handle=server_handle,
                status=result.get("status"),
            )

            # Return result from infrastructure-worker
            # The worker returns the same state format as the old ProvisionerNode
            return {
                "messages": result.get("messages", []),
                "provisioning_result": result.get("provisioning_result"),
                "errors": state.get("errors", []) + result.get("errors", []),
                "current_agent": "provisioner",
            }

        except Exception as e:
            logger.error(
                "provisioner_proxy_error",
                server_handle=server_handle,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return {
                "messages": [AIMessage(content=f"❌ Provisioning error: {e!s}")],
                "errors": state.get("errors", []) + [f"Provisioner proxy error: {e!s}"],
                "provisioning_result": {"status": "error"},
                "current_agent": "provisioner",
            }


# Export same interface as old provisioner module
provisioner_proxy_node = ProvisionerProxyNode()
run = provisioner_proxy_node.run
