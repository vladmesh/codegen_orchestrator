"""Remove everything one run created, from the ownership labels alone.

Cleanup used to be a property of the harness that made the resources: the run
held a context, the context named a container and a network, and teardown
removed what the context could still remember. `scripts/clean_live_tests.py`
shows what that costs after a crash — it rebuilds a `ctx` dict by hand out of a
manifest so that `pipeline_helpers.cleanup_all` can read the decisions back out
of it (`issue:6b4cae67568ff1d8bf82`). A context that has to be reconstructed is
a context that can be reconstructed wrongly, and the run that hung the host
(`issue:0efd1dce9f456903fab4`) left six worker containers and QA-egress
resources behind precisely because nothing was left alive to remember them.

Ownership labels replace all of that. Every dynamic worker container, every
QA-egress proxy and — since this module — every `dev_proj_<worker_id>` network
is stamped at creation with `com.codegen.run.id`, so the question this module
asks is the only question it needs:

    docker ps -a  --filter label=com.codegen.run.id=<run id>
    docker network ls --filter label=com.codegen.run.id=<run id>

That answers after the container has died, after `worker:meta:<id>` has been
deleted, and after the harness that created it is gone. Recovery is therefore
"find this run's resources and remove them", never "reconstruct the context the
harness had".

**It removes what the label selects and nothing else.** The label is both the
finder and the fence: a listed resource whose `com.codegen.run.id` is not this
run is refused rather than removed, so a neighbouring run's worker and the
long-lived service containers — which carry no run label at all and are
therefore never listed — survive a cleanup scoped to somebody else.

**It is idempotent.** Every removal treats "already absent" as success, and the
second pass over a cleaned run lists nothing, removes nothing and is not an
error.

**It verifies, and fails loudly.** After removing, it asks the same two label
queries again; anything still selected is raised as `RunCleanupError`.

**Evidence comes first — including the residue evidence deliberately leaves.**
`delete_worker` keeps `worker:meta:<id>` when a worker's removal record could
not be stored, because that key is then the last thing that can name the worker
to its run (`shared/contracts/worker_evidence.py`). Such a key is *expected*
residue, not an anomaly, and this module removes it only for a worker this run's
evidence already accounts for — `accounted_workers`, the set of worker ids the
run's evidence collector holds a record for. A worker with no record keeps its
name, and the report says so. Nothing here touches
`worker:evidence:removed:<run id>`: the removal records are the evidence, they
outlive the cleanup on purpose, and they expire on their own TTL.

**Accounting fences removal; a capture attempt does not.** Two rules make that
true rather than merely intended.

*One run has one artifact, and it only ever gains.* `retain_evidence` merges into
`.live-manifests/evidence/<run id>.json` instead of replacing it, keeping the
record that knows more whenever two passes describe the same worker. Recovery
runs more than one pass over a run — the label sweep and then the manifest
round-trip — and the later pass, taken after the container and the metadata are
already gone, knows strictly less than the one that authorised their removal. A
sequence of passes may fill a record in; it may never lose one.

*Nothing labelled is removed before its worker is in that artifact.* `clean_run`
compares every listed container and network against `accounted_workers` and
leaves in place — loudly, as a `RunCleanupError` — anything whose worker has no
record. A capture that failed is not a licence to remove: `account_listed_workers`
is how a caller turns such a failure into a stated missed capture, which names
the worker and is therefore an acceptable ending. A silent disappearance is not.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import time

from shared.contracts.queues.worker import WorkerLabel

# How long a removal is given to become an absence before it is called a
# failure, and how often that is re-checked. `docker rm -f` returns before the
# daemon has finished, and "removal already in progress" is a success in
# progress — only a container that is still there after the wait is a fault.
REMOVAL_TIMEOUT = 15.0
REMOVAL_POLL_INTERVAL = 0.25

# The per-worker Redis keys a run owns outright. `worker:meta:<id>` is not here:
# it is the worker's last durable name and is governed by the evidence rule
# above, so it is deleted separately and only when accounted for.
WORKER_KEY_PATTERNS = (
    "worker:status:{worker_id}",
    "worker:error:{worker_id}",
    "worker:broker:{worker_id}",
    "worker:last_activity:{worker_id}",
    "worker:{worker_id}:input",
    "worker:{worker_id}:output",
)
WORKER_META_KEY = "worker:meta:{worker_id}"

RETAINED_FOR_EVIDENCE = (
    "kept: this run's evidence has no record for this worker yet, and "
    "`worker:meta` is the last thing that can still name it to its run"
)

# Said of a labelled resource whose worker the run's evidence does not name. The
# resource is the last thing that still attributes that worker to this run, so it
# stays, and the cleanup that wanted it gone is red.
UNACCOUNTED_FENCE = (
    "{name} is left in place: this run's evidence holds no record for worker "
    "{worker_id!r}, so removing it would leave nothing naming that worker"
)

# Said of a worker a listing named and a capture could not read. It is a record,
# not an error: the worker is in the artifact with the reason its ending is
# unknown, which is what makes removing its container an accounted removal.
UNREADABLE_ENDING = (
    "this run's evidence pass listed this worker and could not read its ending "
    "before cleanup removed it: {detail}"
)
NO_STATED_CAPTURE_FAILURE = "the capture pass recorded no failure naming this worker"

# `run_evidence.CaptureStatus.CAPTURED`, as it appears in a retained artifact.
# Spelled out because merging reads the artifact's JSON rather than a collector's
# objects, and this module imports `run_evidence` only when it needs the harness
# around it. `test_run_cleanup.py` holds the two spellings together.
CAPTURED = "captured"


class RunCleanupError(AssertionError):
    """A run's resources could not be removed, or could not be proven absent."""


@dataclass(frozen=True)
class LabelledResource:
    """One Docker object a run-scoped label query selected.

    `run_id` is read back off the object's own labels rather than assumed from
    the filter, because it is what the removal is authorised against.
    """

    name: str
    kind: str
    worker_id: str
    run_id: str


@dataclass(frozen=True)
class CleanupOps:
    """Every write and read this module makes outside its own process.

    `remove_container` and `remove_network` answer with `None` for a removal
    that ended in a verified absence, and with a short reason otherwise — the
    reason is reported, never raised, so one stuck resource cannot stop the rest
    of a run's teardown.
    """

    list_containers: Callable[[str], list[LabelledResource]]
    remove_container: Callable[[str], str | None]
    list_networks: Callable[[str], list[LabelledResource]]
    remove_network: Callable[[str], str | None]
    # Worker ids whose `worker:meta:<id>` names this run.
    meta_workers: Callable[[str], list[str]]
    delete_keys: Callable[[list[str]], None]
    existing_keys: Callable[[list[str]], list[str]]


@dataclass
class RunCleanupReport:
    """What one cleanup pass did, in the words its caller has to report in."""

    run_id: str
    removed_containers: list[str] = field(default_factory=list)
    removed_networks: list[str] = field(default_factory=list)
    deleted_meta: list[str] = field(default_factory=list)
    retained_meta: dict[str, str] = field(default_factory=dict)
    # Anything a listing returned that does not carry this run's label. Refused,
    # not removed, and loud: a query that selects a neighbour is a defect.
    refused: list[str] = field(default_factory=list)
    # Anything this run does own and whose worker its evidence cannot name.
    # Kept, and loud: it is that worker's last attribution to this run.
    fenced: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "removed_containers": sorted(self.removed_containers),
            "removed_networks": sorted(self.removed_networks),
            "deleted_meta": sorted(self.deleted_meta),
            "retained_meta": dict(sorted(self.retained_meta.items())),
            "refused": sorted(self.refused),
            "fenced": sorted(self.fenced),
            "errors": list(self.errors),
        }


def require_run_id(run_id: str) -> str:
    """Refuse an unscoped cleanup. There is no such thing as everybody's run."""
    if not run_id:
        raise RunCleanupError("cleanup is scoped to a run id; a cleanup of no run removes nothing")
    return run_id


def run_label_filter(run_id: str) -> str:
    """The one label a run's resources are found and fenced by."""
    return f"{WorkerLabel.RUN.value}={require_run_id(run_id)}"


def worker_keys(worker_id: str) -> list[str]:
    """The per-worker Redis keys cleanup owns, `worker:meta` excluded."""
    return [pattern.format(worker_id=worker_id) for pattern in WORKER_KEY_PATTERNS]


def _owned(resources: Iterable[LabelledResource], run_id: str, report: RunCleanupReport):
    """Yield only what carries this run's label, refusing everything else."""
    for resource in resources:
        if resource.run_id == run_id:
            yield resource
            continue
        report.refused.append(
            f"{resource.name} is labelled run {resource.run_id!r}, not {run_id!r}"
        )


def clean_run(
    ops: CleanupOps,
    run_id: str,
    *,
    accounted_workers: set[str] | frozenset[str] = frozenset(),
) -> RunCleanupReport:
    """Remove this run's containers, sidecars and networks, then prove they are gone.

    Order is containers before networks, because a network with a container
    still attached cannot be removed. Redis comes last: a key is only a name,
    and a name is worth keeping until the thing it named is actually gone.

    **Accounting fences the removal.** A labelled resource is removed only once
    `accounted_workers` names its worker — the set of worker ids this run's
    evidence holds a record for, a real ending or a stated missed capture alike.
    A resource whose worker has no record is the last thing attributing that
    worker to this run, so it is kept and the cleanup fails. Callers that would
    rather remove it must first make the miss a record (`account_listed_workers`);
    a capture that merely failed is not a licence to remove.

    Raises `RunCleanupError` if anything could not be removed, if a listing
    returned a resource belonging to another run, if a resource's worker is not
    accounted for, or if the verification pass still selects something for this
    run.
    """
    require_run_id(run_id)
    report = RunCleanupReport(run_id=run_id)
    containers = list(_owned(ops.list_containers(run_id), run_id, report))
    networks = list(_owned(ops.list_networks(run_id), run_id, report))
    unaccounted = _unaccounted(containers + networks, accounted_workers)
    workers: set[str] = set()

    for container in containers:
        if _fence(container, unaccounted, report):
            continue
        if container.worker_id:
            workers.add(container.worker_id)
        reason = ops.remove_container(container.name)
        if reason:
            report.errors.append(f"container {container.name}: {reason}")
        else:
            report.removed_containers.append(container.name)

    for network in networks:
        if _fence(network, unaccounted, report):
            continue
        reason = ops.remove_network(network.name)
        if reason:
            report.errors.append(f"network {network.name}: {reason}")
        else:
            report.removed_networks.append(network.name)

    _clean_worker_keys(ops, run_id, report, workers, accounted_workers, unaccounted)
    _verify(ops, run_id, report)

    if report.errors or report.refused or report.fenced:
        raise RunCleanupError(
            f"run {run_id} cleanup failed: "
            + "; ".join([*report.refused, *report.fenced, *report.errors])
        )
    return report


def _unaccounted(
    resources: Iterable[LabelledResource], accounted_workers: set[str] | frozenset[str]
) -> set[str]:
    """The worker ids this run owns resources for and has no evidence record of."""
    return {
        resource.worker_id
        for resource in resources
        if resource.worker_id and resource.worker_id not in accounted_workers
    }


def _fence(resource: LabelledResource, unaccounted: set[str], report: RunCleanupReport) -> bool:
    """Keep one resource whose worker nothing names, and say so. True if kept."""
    if resource.worker_id not in unaccounted:
        return False
    report.fenced.append(UNACCOUNTED_FENCE.format(name=resource.name, worker_id=resource.worker_id))
    return True


def _clean_worker_keys(
    ops: CleanupOps,
    run_id: str,
    report: RunCleanupReport,
    listed_workers: set[str],
    accounted_workers: set[str] | frozenset[str],
    fenced_workers: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Delete this run's per-worker keys, and its `worker:meta` only when accounted for.

    Two sources of worker ids, because either one alone leaves keys behind: the
    containers the run label just selected — a worker whose metadata is already
    deleted may still have stray keys — and the `worker:meta` records that name
    this run, which is the only source for a worker whose container is gone.

    A worker whose resources the accounting fence kept is skipped in both: its
    container is still there and unaccounted for, so nothing that names it may be
    taken away either.
    """
    try:
        meta_workers = sorted(set(ops.meta_workers(run_id)) - set(fenced_workers))
    except Exception as exc:  # noqa: BLE001 — a failed read must not stop the teardown
        report.errors.append(f"worker metadata discovery: {exc}")
        return

    keys: list[str] = []
    for worker_id in sorted(listed_workers - set(meta_workers) - set(fenced_workers)):
        keys += worker_keys(worker_id)
    for worker_id in meta_workers:
        keys += worker_keys(worker_id)
        if worker_id in accounted_workers:
            keys.append(WORKER_META_KEY.format(worker_id=worker_id))
            report.deleted_meta.append(worker_id)
        else:
            report.retained_meta[worker_id] = RETAINED_FOR_EVIDENCE
    if not keys:
        return
    try:
        ops.delete_keys(keys)
        remaining = ops.existing_keys(keys)
    except Exception as exc:  # noqa: BLE001 — see above
        report.errors.append(f"worker key removal: {exc}")
        return
    if remaining:
        report.errors.append(f"worker keys remain: {', '.join(sorted(remaining))}")


def _verify(ops: CleanupOps, run_id: str, report: RunCleanupReport) -> None:
    """Ask the same run-scoped questions again; anything still selected is a failure."""
    try:
        containers = [resource.name for resource in ops.list_containers(run_id)]
        networks = [resource.name for resource in ops.list_networks(run_id)]
    except Exception as exc:  # noqa: BLE001 — an unverifiable cleanup is a failed one
        report.errors.append(f"cleanup verification failed: {exc}")
        return
    if containers:
        report.errors.append(f"containers remain for run {run_id}: {', '.join(sorted(containers))}")
    if networks:
        report.errors.append(f"networks remain for run {run_id}: {', '.join(sorted(networks))}")


# --- The real operations, over the docker CLI the live harness has ------------


def _labels_from_pairs(raw: str) -> dict[str, str]:
    """Parse docker's `k=v,k=v` label rendering. Ids carry no commas."""
    labels: dict[str, str] = {}
    for entry in raw.split(","):
        name, _, value = entry.partition("=")
        if name.strip():
            labels[name.strip()] = value
    return labels


def _resource(labels: dict[str, str], name: str) -> LabelledResource:
    return LabelledResource(
        name=name,
        kind=labels.get(WorkerLabel.TYPE.value, ""),
        worker_id=labels.get(WorkerLabel.ID.value, ""),
        run_id=labels.get(WorkerLabel.RUN.value, ""),
    )


_CONTAINER_FORMAT = "\t".join(
    [
        "{{.Names}}",
        f'{{{{.Label "{WorkerLabel.ID.value}"}}}}',
        f'{{{{.Label "{WorkerLabel.TYPE.value}"}}}}',
        f'{{{{.Label "{WorkerLabel.RUN.value}"}}}}',
    ]
)


@dataclass(frozen=True)
class _DockerCli:
    """The docker CLI and the stack's Redis, as the live harness has them.

    The harness drives the stack from the host and has neither a docker SDK nor
    a Redis client, so every operation here is a subprocess — the same way the
    rest of `tests/live` reaches the stack.
    """

    root: Path
    timeout: float
    poll_interval: float

    def _docker(self, args: list[str], read_timeout: int = 15) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=read_timeout, cwd=self.root
        )

    def _redis(self, args: list[str]) -> str:
        result = self._docker(["compose", "exec", "-T", "redis", "redis-cli", *args])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout

    @staticmethod
    def _gone(result: subprocess.CompletedProcess, name: str) -> bool:
        """Whether a refusal says the object is not there, in each kind's wording.

        A missing container is `no such object` to `docker inspect`; a missing
        network is `network <name> not found` to `docker network inspect` and to
        `docker network rm` alike. A bare `not found` would read some unrelated
        failure as an absence, so the network's form is anchored on the name the
        command was asked about.
        """
        message = f"{result.stderr}\n{result.stdout}".lower()
        return any(
            marker in message
            for marker in (
                "no such container",
                "no such object",
                "no such network",
                f"network {name.lower()} not found",
            )
        )

    def list_containers(self, run_id: str) -> list[LabelledResource]:
        result = self._docker(
            [
                "ps",
                "-a",
                "--filter",
                f"label={run_label_filter(run_id)}",
                "--format",
                _CONTAINER_FORMAT,
            ]
        )
        if result.returncode != 0:
            raise RunCleanupError(f"docker ps failed: {result.stderr.strip()}")
        return [_container_from_line(line) for line in result.stdout.splitlines() if line.strip()]

    def list_networks(self, run_id: str) -> list[LabelledResource]:
        result = self._docker(
            [
                "network",
                "ls",
                "--filter",
                f"label={run_label_filter(run_id)}",
                "--format",
                "{{.Name}}\t{{.Labels}}",
            ]
        )
        if result.returncode != 0:
            raise RunCleanupError(f"docker network ls failed: {result.stderr.strip()}")
        return [_network_from_line(line) for line in result.stdout.splitlines() if line.strip()]

    def _absent(self, inspect: list[str], name: str) -> str | None:
        """Poll one `docker inspect` until it says the object is gone."""
        deadline = time.monotonic() + self.timeout
        while True:
            verify = self._docker(inspect, read_timeout=10)
            if verify.returncode != 0:
                # The daemon's own words are not reported: a failed inspect can
                # quote an environment that carries secrets.
                return None if self._gone(verify, name) else "docker inspect failed"
            if time.monotonic() >= deadline:
                return "still exists after removal wait"
            time.sleep(self.poll_interval)

    def remove_container(self, name: str) -> str | None:
        removed = self._docker(["rm", "-f", name])
        if removed.returncode != 0 and not (
            self._gone(removed, name) or "already in progress" in removed.stderr
        ):
            return "docker removal failed"
        return self._absent(["inspect", name], name)

    def remove_network(self, name: str) -> str | None:
        removed = self._docker(["network", "rm", name])
        if removed.returncode != 0 and not self._gone(removed, name):
            return "docker network removal failed"
        return self._absent(["network", "inspect", name], name)

    def meta_workers(self, run_id: str) -> list[str]:
        require_run_id(run_id)
        owned = []
        for line in self._redis(["--scan", "--pattern", "worker:meta:*"]).splitlines():
            key = line.strip()
            if key and self._redis(["HGET", key, "run_id"]).strip() == run_id:
                owned.append(key.removeprefix("worker:meta:"))
        return owned

    def delete_keys(self, keys: list[str]) -> None:
        self._redis(["DEL", *keys])

    def existing_keys(self, keys: list[str]) -> list[str]:
        return [key for key in keys if self._redis(["EXISTS", key]).strip() == "1"]


def _container_from_line(line: str) -> LabelledResource:
    name, worker_id, kind, run_id = (line.split("\t") + [""] * 4)[:4]
    return LabelledResource(name=name, kind=kind, worker_id=worker_id, run_id=run_id)


def _network_from_line(line: str) -> LabelledResource:
    name, _, raw_labels = line.partition("\t")
    return _resource(_labels_from_pairs(raw_labels), name)


def docker_cli_ops(
    root: Path,
    *,
    timeout: float = REMOVAL_TIMEOUT,
    poll_interval: float = REMOVAL_POLL_INTERVAL,
) -> CleanupOps:
    """Cleanup over the docker CLI and the stack's Redis, as the live harness has them."""
    cli = _DockerCli(root=root, timeout=timeout, poll_interval=poll_interval)
    return CleanupOps(
        list_containers=cli.list_containers,
        remove_container=cli.remove_container,
        list_networks=cli.list_networks,
        remove_network=cli.remove_network,
        meta_workers=cli.meta_workers,
        delete_keys=cli.delete_keys,
        existing_keys=cli.existing_keys,
    )


def docker_sdk_ops(client, redis_url: str) -> CleanupOps:
    """The same cleanup over a docker SDK client, for a daemon reached by socket.

    Same label query and same rules, so a test that owns a daemon exercises this
    module's real removal rather than a look-alike of it.
    """
    from docker.errors import NotFound  # imported here: the live harness has no docker SDK
    import redis as redis_sdk  # imported here: the live harness has no redis client

    redis_client = redis_sdk.Redis.from_url(redis_url, decode_responses=True)

    def list_containers(run_id: str) -> list[LabelledResource]:
        containers = client.containers.list(all=True, filters={"label": run_label_filter(run_id)})
        return [_resource(container.labels, container.name) for container in containers]

    def list_networks(run_id: str) -> list[LabelledResource]:
        networks = client.networks.list(filters={"label": run_label_filter(run_id)})
        return [_resource(network.attrs.get("Labels") or {}, network.name) for network in networks]

    def remove_container(name: str) -> str | None:
        try:
            client.containers.get(name).remove(force=True)
        except NotFound:
            return None
        except Exception as exc:  # noqa: BLE001 — reported, never raised
            return f"docker removal failed: {type(exc).__name__}"
        try:
            client.containers.get(name)
        except NotFound:
            return None
        return "still exists after removal"

    def remove_network(name: str) -> str | None:
        try:
            client.networks.get(name).remove()
        except NotFound:
            return None
        except Exception as exc:  # noqa: BLE001 — see above
            return f"docker network removal failed: {type(exc).__name__}"
        try:
            client.networks.get(name)
        except NotFound:
            return None
        return "still exists after removal"

    def meta_workers(run_id: str) -> list[str]:
        require_run_id(run_id)
        return [
            key.removeprefix("worker:meta:")
            for key in redis_client.scan_iter(match="worker:meta:*")
            if redis_client.hget(key, "run_id") == run_id
        ]

    def delete_keys(keys: list[str]) -> None:
        redis_client.delete(*keys)

    def existing_keys(keys: list[str]) -> list[str]:
        return [key for key in keys if redis_client.exists(key)]

    return CleanupOps(
        list_containers=list_containers,
        remove_container=remove_container,
        list_networks=list_networks,
        remove_network=remove_network,
        meta_workers=meta_workers,
        delete_keys=delete_keys,
        existing_keys=existing_keys,
    )


# --- Evidence, retained before anything is removed ----------------------------


def accounted_workers(collector) -> set[str]:
    """The worker ids this run's evidence holds a record for.

    A record is the whole test, and it is a deliberately strict one: the
    collector writes a record for a worker it read from a container, for one the
    remover captured before removing, and for one only the ownership manifest
    could name — but in every case with the worker's ending or a stated reason
    the ending could not be read. So "accounted for" means the worker is in the
    artifact and cannot be lost by removing its Redis metadata now.
    """
    return set(collector.accounted_workers())


def account_listed_workers(collector, ops: CleanupOps, run_id: str) -> list[str]:
    """Give every labelled worker of this run a record, before anything is removed.

    The gap this closes: a listing succeeds, a worker's inspect or log read then
    fails with something other than "no such container", and the collector notes
    a capture *error* and moves on. An error is not a record — it names a
    container, not an ending, and it is not what `accounted_workers` reads — so
    the fence in `clean_run` would keep that worker's container forever while the
    artifact still said nothing about the worker.

    So the miss is written down as a miss: the worker enters the artifact with
    the stated reason its ending could not be read, which is an acceptable ending
    and makes removing its resources an accounted removal. Returns the worker ids
    that needed it, for the caller to report.
    """
    from run_evidence import role_from_worker_id  # tests/live module, imported on use

    listed = {
        resource.worker_id
        for resource in [*ops.list_containers(run_id), *ops.list_networks(run_id)]
        if resource.run_id == run_id and resource.worker_id
    }
    missed = sorted(listed - set(collector.accounted_workers()))
    for worker_id in missed:
        collector.observe_absent(
            worker_id,
            role_from_worker_id(worker_id),
            UNREADABLE_ENDING.format(detail=_capture_failure(collector, worker_id)),
        )
    return missed


def _capture_failure(collector, worker_id: str) -> str:
    """What the capture pass said about this worker, in its own words."""
    stated = [error for error in collector.errors if worker_id in error]
    return "; ".join(stated) if stated else NO_STATED_CAPTURE_FAILURE


def _knowledge(record: dict) -> tuple[int, int]:
    """How much one worker record knows, for choosing between two of them.

    The exit code first, because it is the finding the artifact exists for, then
    everything else that was read rather than missed. It compares two
    descriptions of one worker, never two workers.
    """
    has_exit = 1 if record["exit_code"]["status"] == CAPTURED else 0
    return has_exit, sum(1 for capture in _captures(record) if capture["status"] == CAPTURED)


def _captures(record: dict):
    """Every capture in one worker record, including the nested transcript ones."""
    for value in record.values():
        if not isinstance(value, dict):
            continue
        if "status" in value:
            yield value
            continue
        for nested in value.values():
            if isinstance(nested, dict) and "status" in nested:
                yield nested


def merge_worker_records(kept: list[dict], incoming: list[dict]) -> list[dict]:
    """Fold a later pass into a retained artifact, losing nothing it already knew.

    A pass taken after cleanup removed a container knows less about it than the
    pass that authorised the removal — it cannot inspect what is gone. So a
    record is replaced only by one that knows more, and a worker only ever
    appears.
    """
    merged = {record["worker_id"]: record for record in kept}
    for record in incoming:
        known = merged.get(record["worker_id"])
        if known is None or _knowledge(record) > _knowledge(known):
            merged[record["worker_id"]] = record
    return [merged[worker_id] for worker_id in sorted(merged)]


def _retained_artifact(path: Path, run_id: str) -> dict:
    """What this run already retained, refusing to write over what cannot be read."""
    if not path.exists():
        return {}
    try:
        retained = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunCleanupError(
            f"{path} exists and cannot be read ({type(exc).__name__}); "
            "replacing it would destroy evidence this run already retained"
        ) from exc
    if retained.get("run_id") != run_id:
        raise RunCleanupError(
            f"{path} holds evidence for run {retained.get('run_id')!r}, not {run_id!r}"
        )
    return retained


def retain_evidence(collector, path: Path) -> Path:
    """Merge this run's worker records into its one artifact, before their sources go.

    Used by recovery, which has no artifact of its own: `clean_live_tests.py`
    finds a manifest for a run nobody is watching any more, takes one capture
    pass over what is left of it, and retains that here. Only then may cleanup
    delete a `worker:meta` key the run's evidence now accounts for.

    **The write is a merge, and that is the point.** Recovery makes more than one
    pass over the same run — the label sweep first, the manifest round-trip after
    it — and the later pass runs when the containers, the removal records and the
    metadata the first pass read are already gone. Overwriting would erase the
    very accounting that authorised their removal, leaving the worker named
    nowhere. So a record is only ever added or improved (`merge_worker_records`),
    and the capture errors of every pass are kept.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    retained = _retained_artifact(path, collector.run_id)
    now = datetime.now(tz=UTC).isoformat()
    path.write_text(
        json.dumps(
            {
                "run_id": collector.run_id,
                "first_retained_at": retained.get("first_retained_at") or now,
                "retained_at": now,
                "passes": int(retained.get("passes", 0)) + 1,
                "reason": "capture before cleanup, for a run recovered without its harness",
                "workers": merge_worker_records(retained.get("workers", []), collector.records()),
                "capture_errors": list(
                    dict.fromkeys([*retained.get("capture_errors", []), *collector.errors])
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
