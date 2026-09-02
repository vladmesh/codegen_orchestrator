"""DevOps subgraph nodes.

Holds the readiness check and the node instances the DevOps graph wires up;
the secret resolver and the deployer live in their own modules.
"""

from langchain_core.messages import AIMessage
import structlog

from ...nodes.base import FunctionalNode
from .deployer import DeployerNode
from .secret_resolver import SecretResolverNode
from .state import DevOpsState

logger = structlog.get_logger()


class ReadinessCheckNode(FunctionalNode):
    """Check if all user secrets are provided."""

    def __init__(self):
        super().__init__(node_id="readiness_check")

    async def run(self, state: DevOpsState) -> dict:
        """Check deployment readiness."""
        missing = state.get("missing_user_secrets", [])

        if missing:
            missing_keys = [entry["key"] for entry in missing]
            logger.info(
                "readiness_check_missing_secrets",
                missing=missing_keys,
            )
            return {
                "messages": [
                    AIMessage(
                        content=f"Missing user secrets: {', '.join(missing_keys)}. "
                        "Please provide these secrets to continue deployment."
                    )
                ],
            }

        logger.info("readiness_check_ready")
        return {}


# Node instances
secret_resolver_node = SecretResolverNode()
readiness_check_node = ReadinessCheckNode()
deployer_node = DeployerNode()
