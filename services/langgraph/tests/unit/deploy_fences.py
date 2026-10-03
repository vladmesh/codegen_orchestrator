"""Deploy-lock doubles for unit tests: a real fence on an in-memory Redis with Lua.

Kept out of `factories.py` on purpose. The service-test images import the DTO
factories but do not install fakeredis, which is a unit-test dependency.
"""

import fakeredis

from src.deploy_fence import DeployFence


def held_deploy_fence(project_id: str = "proj-1", task_id: str = "deploy-1") -> DeployFence:
    """A deploy's claim on its project lock, held, on an in-memory Redis with Lua.

    Each fence gets its own server, so tests never share a lock. A test that
    needs the lock to change hands writes the key through `fence.redis`.
    """
    server = fakeredis.FakeServer()
    fence = DeployFence.for_job(
        fakeredis.aioredis.FakeRedis(server=server, decode_responses=True), project_id, task_id
    )
    fakeredis.FakeRedis(server=server, decode_responses=True).set(fence.lock_key, fence.token)
    return fence
