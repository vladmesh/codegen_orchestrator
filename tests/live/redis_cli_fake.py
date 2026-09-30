"""`redis-cli` and the residue proof's reads over one fake Redis, without a stack.

The run's Redis cleanup steps call `pipeline_helpers._redis_command`, which is
`redis-cli` inside the compose Redis; the residue proof scans the same Redis.
The offline suites that drive `cleanup_and_prove` put both on this one
`fakeredis` instance, so a key a removal missed is a key the proof names.
"""

from __future__ import annotations

import fakeredis
import run_residue

from shared.live_harness_cleanup import RESIDUE_FINDINGS_KEY
from shared.queues import STORY_WORKERS_KEY


class FakeRedisCli:
    """`redis-cli` as `_redis_command` calls it, answering with its stdout."""

    def __init__(self) -> None:
        self.redis = fakeredis.FakeRedis(decode_responses=True)

    def __call__(self, *args: str) -> str:
        if args[:2] == ("--scan", "--pattern") and len(args) == 3:
            return "\n".join(sorted(self.redis.scan_iter(match=args[2])))
        result = self.redis.execute_command(*args)
        if result is None:
            return ""
        if isinstance(result, list):
            return "\n".join(str(item) for item in result)
        return str(result)


def residue_ops(cli: FakeRedisCli) -> run_residue.ResidueOps:
    """The proof's reads: Redis from `cli`, every other kind answering nothing left."""
    return run_residue.ResidueOps(
        run_labelled_containers=lambda _run: [],
        compose_project_containers=lambda _project: [],
        off_host_residue=lambda _inventory: {
            kind: {RESIDUE_FINDINGS_KEY: []}
            for kind in ("github_repository", "registry_repositories", "target_containers")
        },
        workspace_entries=lambda _entries: [],
        redis_keys=lambda patterns: sorted(
            {key for pattern in patterns for key in cli.redis.scan_iter(match=pattern)}
        ),
        story_worker_bindings=lambda stories: [
            story for story in stories if cli.redis.hget(STORY_WORKERS_KEY, story)
        ],
        po_checkpoint_rows=lambda _inventory: [],
    )
