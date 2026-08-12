"""The one-time migration that names the type of workers older than the field.

Two control-plane boundaries now decide every worker operation from a worker
type the server recorded: the broker's credential record (`worker:broker:<id>`)
and worker-manager's own record of the worker it created (`worker:meta:<id>`).
Both fail closed when the field is absent, and that is the right request-time
behaviour — a record whose type nobody wrote is not a worker this installation
created.

It is the wrong behaviour for records that predate the field. Worker containers
and their Redis state are not Compose services: a control-plane rollout replaces
the broker and worker-manager while developer workers keep running, and their
records — written by the previous version, which had no type to write — would
lose the lease, status, session, result and compose routes mid-turn.

So this is a migration, and it runs once at startup, before either service
serves anything. It is provable rather than lenient: until this cutover a worker
had exactly one kind, because the QA executor and the recorded type arrive in
the same change. A record without a type therefore cannot be a QA worker; it can
only be a developer worker created before the cutover.

Nothing written afterwards reaches it. Registration requires the type, so a
typeless record appearing later is refused everything, exactly as before — the
request-time decision keeps no fallback at all, and this migration is not one.

Death condition, so this does not become a second mode forever: these records
are per-worker and die with their worker. `delete_worker` drops both keys when a
worker finishes, times out or fails, and orphan GC drops the records of workers
whose containers are already gone. Once the last worker created before the
cutover has been deleted — a matter of hours, since a worker does not outlive
its task — this code can only ever find zero records, and it should be deleted
rather than kept as a permanent compatibility path.
"""

from __future__ import annotations

import structlog

from shared.contracts.vocab import WorkerType

logger = structlog.get_logger(__name__)

WORKER_TYPE_FIELD = "worker_type"

# The only type that existed before the field did.
PRE_CUTOVER_WORKER_TYPE = WorkerType.DEVELOPER


async def backfill_pre_cutover_worker_type(redis, *, key_pattern: str, boundary: str) -> int:
    """Record `developer` on every matching hash that has no worker type yet.

    Returns how many records were migrated. An existing value is never
    overwritten: a record that already names its type — including a QA
    executor's — is left exactly as the server wrote it.

    Args:
        key_pattern: the record namespace this boundary authorizes on, e.g.
            `worker:broker:*`.
        boundary: the service doing the migration, for the log line that is the
            operator's only notice that pre-cutover workers existed.
    """
    migrated: list[str] = []
    async for key in redis.scan_iter(match=key_pattern):
        name = key.decode() if isinstance(key, bytes) else key
        recorded = await redis.hget(name, WORKER_TYPE_FIELD)
        if recorded is not None:
            continue
        await redis.hset(name, WORKER_TYPE_FIELD, PRE_CUTOVER_WORKER_TYPE.value)
        migrated.append(name)

    if migrated:
        logger.warning(
            "pre_cutover_worker_type_backfilled",
            boundary=boundary,
            key_pattern=key_pattern,
            worker_type=PRE_CUTOVER_WORKER_TYPE.value,
            count=len(migrated),
            keys=sorted(migrated),
        )
    else:
        logger.info(
            "pre_cutover_worker_type_backfill_found_nothing",
            boundary=boundary,
            key_pattern=key_pattern,
        )
    return len(migrated)
