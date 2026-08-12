"""Command entry point for giving an existing server the QA run identity.

    docker compose exec infra-service python -m src.provisioner.qa_identity_retrofit vps-267179

Run it once per host that was provisioned before the identity existed. It is
idempotent: a second run creates nothing, removes nothing and still exits 0, so
"run it on every host" is a safe instruction. A failure raises with the playbook
output rather than exiting quietly, because a host that was not repaired must not
read as one that was.
"""

import asyncio
import sys

from .ansible_runner import AnsibleRunner
from .operations import retrofit_qa_identity

EXPECTED_ARG_COUNT = 2


async def main(server_handle: str) -> int:
    """Provision the QA identity on one server and return a process exit status."""
    success, message = await retrofit_qa_identity(server_handle, AnsibleRunner())
    if success:
        return 0
    raise RuntimeError(message)


if __name__ == "__main__":
    if len(sys.argv) != EXPECTED_ARG_COUNT:
        raise SystemExit("Usage: python -m src.provisioner.qa_identity_retrofit SERVER_HANDLE")
    raise SystemExit(asyncio.run(main(sys.argv[1])))
