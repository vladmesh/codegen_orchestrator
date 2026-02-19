"""Provisioner proxy node - queues jobs to infrastructure-worker.

Fire-and-forget: queues a provisioning job to provisioner:queue and returns
immediately. Actual processing is handled by infra-service, results are
consumed by scheduler via provisioner:results stream.
"""

from langchain_core.messages import AIMessage
import structlog

from ..clients import provisioner_client
from ..nodes.base import FunctionalNode, log_node_execution

logger = structlog.get_logger(__name__)


class ProvisionerProxyNode(FunctionalNode):
    """Proxy node that queues provisioning to infrastructure-worker (fire-and-forget)."""

    def __init__(self):
        super().__init__(node_id="provisioner_proxy")

    @log_node_execution("provisioner")
    async def run(self, state: dict) -> dict:
        """Queue provisioning job to infrastructure-worker via Redis.

        Fire-and-forget: queues the job and returns immediately.
        Results are handled by infra-service → scheduler pipeline.

        Args:
            state: Graph state containing:
                - server_to_provision: Server handle to provision
                - is_incident_recovery: Whether this is incident recovery
                - force_reinstall: Force OS reinstall flag

        Returns:
            Updated state with queued status
        """
        server_handle = state.get("server_to_provision")
        is_recovery = state.get("is_incident_recovery", False)
        force_reinstall = state.get("force_reinstall", False)
        correlation_id = state.get("correlation_id")

        if not server_handle:
            return {
                "messages": [AIMessage(content="No server specified for provisioning")],
                "errors": state.get("errors", []) + ["No server_to_provision in state"],
            }

        try:
            request_id = await provisioner_client.trigger_provisioning(
                server_handle=server_handle,
                force_reinstall=force_reinstall,
                is_recovery=is_recovery,
                correlation_id=correlation_id,
            )

            logger.info(
                "provisioner_proxy_queued",
                request_id=request_id,
                server_handle=server_handle,
                is_recovery=is_recovery,
            )

            return {
                "messages": [
                    AIMessage(content=f"Provisioning queued for {server_handle} ({request_id})")
                ],
                "provisioning_result": {"status": "queued", "request_id": request_id},
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
                "messages": [AIMessage(content=f"Provisioning error: {e!s}")],
                "errors": state.get("errors", []) + [f"Provisioner proxy error: {e!s}"],
                "provisioning_result": {"status": "error"},
                "current_agent": "provisioner",
            }


# Export same interface as old provisioner module
provisioner_proxy_node = ProvisionerProxyNode()
run = provisioner_proxy_node.run
