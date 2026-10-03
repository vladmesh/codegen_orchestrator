"""The project deploy lock as a fence that every deploy write checks.

One deploy of a project holds `deploy:<project_id>:lock` at a time. The value is
a token unique to the holder (`<task_id>:<uuid>`), set with `SET NX EX` when the
deploy is claimed. The lock expires (`deploy.deploy_lock_ttl`), so holding it at
claim time says nothing about holding it ten minutes later: a deploy that outlives
the TTL, or whose key was replaced, would otherwise keep writing while a second
deploy of the same project holds the lock.

So the claim produces a `DeployFence`, and the fence is threaded explicitly to
every place a deploy changes external or durable state for the project. Each of
those writes calls `ensure_held` immediately before it is performed; it is the
only check. A deploy whose fence is lost raises `DeployFenceLost`, performs no
further write, and is recorded once as `DeployOutcome.DEPLOY_LOCK_LOST` on its
own Run. Release is a compare-and-delete, so a deploy never deletes a lock that
another holder now owns.

The check narrows the window to one Redis round trip; it cannot stop a write that
is already in flight when the key expires, because GitHub, the API and the target
server do not check the token themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)

_HOLDS_DEPLOY_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return 1
end
return 0
"""

_COMPARE_AND_DELETE_DEPLOY_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class DeployWrite(StrEnum):
    """Every kind of write a deploy performs, each fenced at its gateway call.

    The four write classes: (a) secret persistence and GitHub secret writes,
    (b) workflow dispatch, (c) deployer and remote execution, and (d) API run,
    application and story state transitions by the deploy consumer and its
    result handlers.
    """

    # (a) secret persistence and GitHub secret writes
    SECRET_PERSISTENCE = "secret_persistence"  # noqa: S105
    GITHUB_SECRETS = "github_secrets"
    # (b) workflow dispatch: the pin tag, stopping older runs, dispatch and rerun
    WORKFLOW_PIN_TAG = "workflow_pin_tag"
    WORKFLOW_FENCE = "workflow_fence"
    WORKFLOW_DISPATCH = "workflow_dispatch"
    # (c) deployer and remote execution
    DEPLOYMENT_RECORD = "deployment_record"
    REMOTE_EXECUTION = "remote_execution"
    PRODUCT_ACCESS = "product_access"
    PRODUCT_SETTINGS = "product_settings"
    # (d) API state transitions by the deploy consumer and its result handlers
    RUN_STATE = "run_state"
    APPLICATION_STATE = "application_state"


class DeployFenceLost(Exception):
    """This deploy no longer holds its project's deploy lock, so it may not write.

    Deliberately not a `RuntimeError`: the deploy path turns those into ordinary
    failed or cancelled deploys, and a lost fence must reach the consumer intact.
    """

    def __init__(self, project_id: str, write: DeployWrite) -> None:
        super().__init__(
            f"deploy lock for project {project_id} is no longer held by this deploy; "
            f"refused {write.value}"
        )
        self.project_id = project_id
        self.write = write


@dataclass(frozen=True)
class DeployFence:
    """One deploy's claim on its project's deploy lock."""

    redis: Any
    project_id: str
    token: str

    @classmethod
    def for_job(cls, redis: Any, project_id: str, task_id: str) -> DeployFence:
        """A fence with a token no other deploy, or retry of this one, can share."""
        return cls(redis=redis, project_id=project_id, token=f"{task_id}:{uuid4().hex}")

    @property
    def lock_key(self) -> str:
        return f"deploy:{self.project_id}:lock"

    async def acquire(self, ttl_seconds: int) -> bool:
        """Take the lock if nobody holds it."""
        return bool(await self.redis.set(self.lock_key, self.token, nx=True, ex=ttl_seconds))

    async def ensure_held(self, write: DeployWrite) -> None:
        """Refuse `write` unless the lock still stores this deploy's token."""
        held = await self.redis.eval(_HOLDS_DEPLOY_LOCK, 1, self.lock_key, self.token)
        if held != 1:
            logger.warning(
                "deploy_fence_lost",
                project_id=self.project_id,
                lock_key=self.lock_key,
                refused_write=write.value,
            )
            raise DeployFenceLost(self.project_id, write)

    async def release(self) -> None:
        """Delete the lock only while it is still this deploy's."""
        await self.redis.eval(_COMPARE_AND_DELETE_DEPLOY_LOCK, 1, self.lock_key, self.token)
