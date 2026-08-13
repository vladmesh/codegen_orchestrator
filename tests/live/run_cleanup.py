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
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "removed_containers": sorted(self.removed_containers),
            "removed_networks": sorted(self.removed_networks),
            "deleted_meta": sorted(self.deleted_meta),
            "retained_meta": dict(sorted(self.retained_meta.items())),
            "refused": sorted(self.refused),
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

    Raises `RunCleanupError` if anything could not be removed, if a listing
    returned a resource belonging to another run, or if the verification pass
    still selects something for this run.
    """
    require_run_id(run_id)
    report = RunCleanupReport(run_id=run_id)
    workers: set[str] = set()

    for container in _owned(ops.list_containers(run_id), run_id, report):
        if container.worker_id:
            workers.add(container.worker_id)
        reason = ops.remove_container(container.name)
        if reason:
            report.errors.append(f"container {container.name}: {reason}")
        else:
            report.removed_containers.append(container.name)

    for network in _owned(ops.list_networks(run_id), run_id, report):
        reason = ops.remove_network(network.name)
        if reason:
            report.errors.append(f"network {network.name}: {reason}")
        else:
            report.removed_networks.append(network.name)

    _clean_worker_keys(ops, run_id, report, workers, accounted_workers)
    _verify(ops, run_id, report)

    if report.errors or report.refused:
        raise RunCleanupError(
            f"run {run_id} cleanup failed: " + "; ".join([*report.refused, *report.errors])
        )
    return report


def _clean_worker_keys(
    ops: CleanupOps,
    run_id: str,
    report: RunCleanupReport,
    listed_workers: set[str],
    accounted_workers: set[str] | frozenset[str],
) -> None:
    """Delete this run's per-worker keys, and its `worker:meta` only when accounted for.

    Two sources of worker ids, because either one alone leaves keys behind: the
    containers the run label just selected — a worker whose metadata is already
    deleted may still have stray keys — and the `worker:meta` records that name
    this run, which is the only source for a worker whose container is gone.
    """
    try:
        meta_workers = sorted(set(ops.meta_workers(run_id)))
    except Exception as exc:  # noqa: BLE001 — a failed read must not stop the teardown
        report.errors.append(f"worker metadata discovery: {exc}")
        return

    keys: list[str] = []
    for worker_id in sorted(listed_workers - set(meta_workers)):
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
    def _gone(result: subprocess.CompletedProcess) -> bool:
        message = f"{result.stderr}\n{result.stdout}".lower()
        return any(marker in message for marker in ("no such container", "no such object"))

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

    def _absent(self, inspect: list[str]) -> str | None:
        """Poll one `docker inspect` until it says the object is gone."""
        deadline = time.monotonic() + self.timeout
        while True:
            verify = self._docker(inspect, read_timeout=10)
            if verify.returncode != 0:
                # The daemon's own words are not reported: a failed inspect can
                # quote an environment that carries secrets.
                return None if self._gone(verify) else "docker inspect failed"
            if time.monotonic() >= deadline:
                return "still exists after removal wait"
            time.sleep(self.poll_interval)

    def remove_container(self, name: str) -> str | None:
        removed = self._docker(["rm", "-f", name])
        if removed.returncode != 0 and not (
            self._gone(removed) or "already in progress" in removed.stderr
        ):
            return "docker removal failed"
        return self._absent(["inspect", name])

    def remove_network(self, name: str) -> str | None:
        removed = self._docker(["network", "rm", name])
        if removed.returncode != 0 and not self._gone(removed):
            return "docker network removal failed"
        return self._absent(["network", "inspect", name])

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


def retain_evidence(collector, path: Path) -> Path:
    """Write this run's worker records down before cleanup removes their sources.

    Used by recovery, which has no artifact of its own: `clean_live_tests.py`
    finds a manifest for a run nobody is watching any more, takes one capture
    pass over what is left of it, and retains that here. Only then may cleanup
    delete a `worker:meta` key the run's evidence now accounts for.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": collector.run_id,
                "retained_at": datetime.now(tz=UTC).isoformat(),
                "reason": "capture before cleanup, for a run recovered without its harness",
                "workers": collector.records(),
                "capture_errors": collector.errors,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
