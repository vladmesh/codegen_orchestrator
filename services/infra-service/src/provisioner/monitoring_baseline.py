"""Command entry point for applying monitoring to an adopted server."""

import asyncio
import os
import sys

from .ansible_runner import AnsibleRunner
from .operations import provision_monitoring_baseline

EXPECTED_ARG_COUNT = 2


async def main(server_handle: str) -> int:
    """Apply the monitoring baseline and return a process exit status."""
    success, message = await provision_monitoring_baseline(
        server_handle,
        AnsibleRunner(),
        orchestrator_ip=os.getenv("ORCHESTRATOR_PUBLIC_IP"),
        orchestrator_hostname=os.getenv("ORCHESTRATOR_HOSTNAME"),
    )
    if success:
        return 0
    raise RuntimeError(message)


if __name__ == "__main__":
    if len(sys.argv) != EXPECTED_ARG_COUNT:
        raise SystemExit("Usage: python -m src.provisioner.monitoring_baseline SERVER_HANDLE")
    raise SystemExit(asyncio.run(main(sys.argv[1])))
