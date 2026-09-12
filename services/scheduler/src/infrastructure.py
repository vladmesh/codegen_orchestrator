"""Infrastructure scheduler service entry point."""

import asyncio

import structlog

from shared.log_config import setup_logging
from shared.provisioning_policy import (
    TIME4VPS_PROVIDER,
    managed_provider_ids,
    validate_provider_policies,
)

from . import runtime
from .startup import INFRASTRUCTURE_REQUIRED_KEYS
from .tasks.health_checker import health_check_worker
from .tasks.provisioner_result_listener import provisioner_results_worker
from .tasks.provisioner_trigger import retry_pending_servers
from .tasks.server_sync import sync_servers_worker

logger = structlog.get_logger()


async def main() -> None:
    setup_logging(service_name="scheduler-infrastructure")
    validate_provider_policies()
    managed_ids = managed_provider_ids(TIME4VPS_PROVIDER)
    logger.info("scheduler_infrastructure_started")
    logger.info(
        "provider_policy_validated",
        provider=TIME4VPS_PROVIDER,
        managed_server_count=len(managed_ids),
    )
    await runtime.initialize_configs(
        INFRASTRUCTURE_REQUIRED_KEYS, service_name="scheduler-infrastructure"
    )
    # Give infra-service time to subscribe before replaying pending provisioning.
    await asyncio.sleep(5)
    await retry_pending_servers()
    await runtime.run_workers(
        [
            ("server_sync", sync_servers_worker),
            ("health_checker", health_check_worker),
            ("provisioner_results", provisioner_results_worker),
        ],
        service_name="scheduler-infrastructure",
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("scheduler_infrastructure_stopped_by_user")
