"""Prove, after a run has been cleaned up, that nothing of it is left anywhere.

Cleanup already verifies itself, kind by kind, as it goes: `run_cleanup._verify`
re-asks its label query, `cleanup_github_repo` reads the repository back as a
404, `db_teardown` reads every key it owned back out of the catalog. Those are
verified *removals*, and they are not the same claim as the one the Definition
of Done makes — "after cleanup there is nothing left" — for two reasons.

**A removal can only verify what it removed.** The kinds nothing removed are
invisible to it. `docker compose down -v` removes the services of a worker's
bounded compose plan and not the one-shot containers `docker compose run`
created beside them, and two such containers outlived a completed story on
production for 7+ hours and then outlived a whole project teardown
(`issue:868e40fc0377b0dabb77`). A proof that only re-asks the removals would
have reported that run clean. This module asks about the kinds instead of about
the removals, so a kind nobody removes is a red run rather than a blind spot.

**The control host is not the only host.** The run's containers are asked for by
label on the control host; its *deployment* lives on a target reached only over
SSH, and `docker ps -a` there is the only thing that can see a container of it
that exited.

**The three answers, and why the third one exists.** Every kind here is asked
through `run_proof.ask`, which keeps "asked and found nothing" apart from "could
not ask". An unreachable target, an unreadable registry or a Redis that refused
the query is a red run naming the kind it could not check — never a clean one.
That distinction is the one card 1318 had to add when an unreadable log was
rendering as an empty log, and the probes below are written to preserve it: each
raises on a non-answer — a non-zero exit, a missing marker, an unparseable
payload — rather than returning an empty finding list.

**A question no state could answer yes to is not a passing question either.**
Three outcomes are only worth having if a reader can trust them, and the way
this proof failed review the first time was not a probe that broke: it was a
kind asking `thread_id = <run id>` when nothing in the system ever writes a
checkpoint under a run id, and rendering that as `absent` on every run. Two
things answer that now. `po_checkpoints` asks about the thread the PO consumer
really writes — and about the rows *this run* added to it, since the thread is a
fixture every run shares — and refuses to answer at all without the snapshot
that makes the difference knowable. And `vacuity_notes` names, in a green
proof's own notes, every kind whose subject list was empty: a run that owns no
stack passes `target_containers` whatever the target does, and a reader is told
so rather than left to assume otherwise.

**The database half is read, not rewritten.** Cards 1311 and 1313 derived the
closure from `pg_constraint`, gave the run's teardown and the stand sweep the
same `teardown_selection`, and made a surviving row name its table, key and
constraint. `database_rows` here is that report, carried into this proof as the
check it already is — `database_check` — so the one place that knows how to ask
the database stays the one place that asks it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess

import po_checkpoints
from run_proof import (
    Proof,
    ProofCheck,
    ProofFailed,
    ProofOutcome,
    Question,
    marker_payload,
    prove,
    require_mapping,
)

from shared.contracts.queues.worker import WorkerLabel
from shared.live_harness_cleanup import (
    GITHUB_ORG,
    RESIDUE_ERROR_KEY,
    RESIDUE_FINDINGS_KEY,
    RUN_RESIDUE_MARKER,
)
from shared.live_harness_workspaces import WORKSPACE_RESIDUE_MARKER
from shared.queues import STORY_WORKERS_KEY
from shared.worker_compose import (
    COMPOSE_ONEOFF_NAME_INFIX,
    COMPOSE_PLAN_DIRECTORY,
    worker_compose_project,
)

#: Every kind the Definition of Done names. The proof must answer all of them:
#: a kind no check answered is reported as unasked and fails, so this tuple is
#: what stops the proof from shrinking when a probe is lost.
RESIDUE_KINDS = (
    "control_host_containers",
    "target_containers",
    "registry_repositories",
    "workspaces",
    "redis_keys",
    "github_repository",
    "po_checkpoint_thread",
    "database_rows",
)

#: The one Redis key a cleaned run is *expected* to still have. The removal
#: records are the evidence of how this run's workers ended; they outlive the
#: cleanup on purpose and expire on their own TTL
#: (`shared/contracts/worker_evidence.py`, and the rule `run_cleanup` states).
#: Excluded by name rather than by pattern, so the exception stays one key.
RETAINED_EVIDENCE_KEY = "worker:evidence:removed:{run_id}"

#: Said of a run that never fixed its place on the PO thread. Without that
#: snapshot there is no way to tell this run's checkpoint rows from the rows the
#: fixture chat already carried, so the kind cannot be asked at all — and saying
#: so is the whole difference between this version and the one that asked
#: `thread_id = <run id>` and matched nothing on every run.
NO_PO_SNAPSHOT = (
    "this run fixed no PO checkpoint snapshot before it started, so its own rows "
    "on the shared fixture thread cannot be told from the rows that were already there"
)

#: Said when nothing recorded *why* the snapshot is missing. A reason is what a
#: reader of a red run repairs the run with, so its absence is itself named
#: rather than left as a blank: run 35486586267 failed on this kind and the
#: artifact carried no sentence about why.
NO_PO_SNAPSHOT_REASON = "and nothing recorded why the snapshot is missing"


#: Said of a kind whose subject list is empty. The question was put and nothing
#: came back, which is a pass — but a pass that no state of the *world* could
#: have turned red, because the run named nothing of that kind to ask about.
#: A reader of a green proof is told which of its kinds were vacuous, so
#: "absent" never has to be trusted blind. This is the general form of the
#: defect that made the PO checkpoint kind a tautology.
NOTHING_TO_ASK_ABOUT = "{kind}: asked about nothing — {why}"


class RunResidueError(AssertionError):
    """A run left something behind, or could not prove that it did not."""


@dataclass(frozen=True)
class RunInventory:
    """Everything one run owns, in the terms each kind has to be asked in.

    Assembled from the ownership manifest and the run's context, never from a
    live listing: the listing is what this proof is about to take, and a proof
    that derives its own expectations from the thing it is checking proves
    nothing.
    """

    run_id: str
    project_id: str = ""
    repo_id: str = ""
    repo_name: str = ""
    story_ids: tuple[str, ...] = ()
    worker_ids: tuple[str, ...] = ()
    registry_repositories: tuple[str, ...] = ()
    stack_names: tuple[str, ...] = ()
    server_handle: str | None = None
    #: The PO conversation thread this run wrote to — `po-chat-<telegram id>`,
    #: the key the PO consumer actually checkpoints under — and the identity of
    #: every row that was already on it when the run started. The run owns the
    #: difference, not the thread: the Telegram id is a fixture every live run
    #: shares. See `po_checkpoints`.
    po_thread_id: str = ""
    po_checkpoint_snapshot: dict[str, list[str]] | None = None
    #: Why the snapshot above is `None`, when it is. Carried so the unaskable
    #: check names the read that failed and not only the consequence.
    po_checkpoint_snapshot_error: str | None = None

    def workspace_entries(self) -> list[str]:
        """The workspace root's children this run owns, in creation order of kind.

        The repository checkout is the one a developer worker's teardown
        deliberately preserves; the QA scratch directories and the per-worker
        compiled compose plans are the manager's own children of the same root.
        """
        entries = [self.repo_id] if self.repo_id else []
        entries += [f"qa-{worker_id}" for worker_id in self.worker_ids]
        entries += [f"{COMPOSE_PLAN_DIRECTORY}/{worker_id}" for worker_id in self.worker_ids]
        return entries

    def redis_patterns(self) -> list[str]:
        """Globs that select any key naming this run, its project or its stories.

        Deliberately wider than the keys cleanup knows about. A key the harness
        never heard of is precisely the kind the Definition of Done is about, so
        the question is "does any key in this Redis name this run", not "are the
        keys we remembered gone".

        The worker ids are in here as identities of their own, and not as a
        convenience: a worker key is named `worker:<something>:<worker id>` and
        carries its project only as a *field value*, so a scan for the project
        id alone would answer "nothing" for every key cleanup is supposed to
        have removed.
        """
        identities = [
            self.run_id,
            self.project_id,
            self.repo_id,
            *self.story_ids,
            *self.worker_ids,
        ]
        return [f"*{identity}*" for identity in dict.fromkeys(identities) if identity]


def retained_keys(run_id: str) -> frozenset[str]:
    """The keys a cleaned run is expected to keep, and may not be failed for."""
    return frozenset({RETAINED_EVIDENCE_KEY.format(run_id=run_id)})


def unexpected_keys(keys: Iterable[str], run_id: str) -> list[str]:
    """Every key a scan returned that this run was not supposed to keep."""
    expected = retained_keys(run_id)
    return sorted({key.strip() for key in keys if key.strip()} - expected)


@dataclass(frozen=True)
class ResidueOps:
    """Every read this proof makes, so the offline suite can drive all of them.

    Each one answers with what it found, and raises when it could not look.
    Returning an empty list for a failed read is the one thing none of them may
    do.
    """

    #: Containers carrying this run's ownership label, on the control host.
    run_labelled_containers: Callable[[str], list[str]]
    #: Containers of one worker's bounded compose plan, whatever created them.
    compose_project_containers: Callable[[str], list[str]]
    #: The three off-host kinds, answered together, each with its own outcome.
    off_host_residue: Callable[[RunInventory], dict]
    #: Workspace entries the workspace root still has.
    workspace_entries: Callable[[list[str]], list[str]]
    #: Keys any of these globs still selects.
    redis_keys: Callable[[list[str]], list[str]]
    #: The workers these stories are still bound to, if any.
    story_worker_bindings: Callable[[list[str]], list[str]]
    #: PO checkpoint rows that appeared on this run's thread while it ran, or
    #: None if there is no checkpointer in this database at all.
    po_checkpoint_rows: Callable[[RunInventory], list[str] | None]


def _off_host(ops: ResidueOps, inventory: RunInventory) -> Callable[[str], list[str]]:
    """Read one kind out of the single off-host answer, memoised across kinds.

    Three kinds, one SSH key fetch and one container exec: reading them
    separately would pay for the same round trip three times. They stay three
    answers — each carries its own error — so one unreadable source neither
    hides nor is hidden by the others.
    """
    answer: dict[str, dict] = {}
    failure: list[BaseException] = []

    def read(kind: str) -> list[str]:
        if not answer and not failure:
            try:
                answer.update(ops.off_host_residue(inventory))
            except BaseException as exc:  # noqa: BLE001 — re-raised for every kind below
                failure.append(exc)
        if failure:
            raise failure[0]
        result = require_mapping(answer.get(kind, {}), subject=f"the {kind} residue probe")
        if RESIDUE_ERROR_KEY in result:
            raise ProofFailed(str(result[RESIDUE_ERROR_KEY]))
        if RESIDUE_FINDINGS_KEY not in result:
            raise ProofFailed(f"the {kind} residue probe answered with neither findings nor error")
        return [str(finding) for finding in result[RESIDUE_FINDINGS_KEY]]

    return read


def _control_host_containers(ops: ResidueOps, inventory: RunInventory) -> list[str]:
    """Everything of this run still on the control host, by both of its names.

    Two questions, because one label cannot answer both. `com.codegen.run.id` is
    stamped on what the orchestrator creates and finds a worker, its QA-egress
    proxy and its network after they have died. It is stamped on nothing Compose
    creates inside the worker's bounded plan, and those containers are the ones
    that survived teardown on production, so they are asked for by the label
    Compose does stamp: the worker's own project name.

    A one-shot container is said to be one, because that shape —
    `*-integration-tests-run-*` — is the kind the Definition of Done calls out by
    name, and a reader of a red run needs to know at once whether they are
    looking at `issue:868e40fc0377b0dabb77` or at something new.
    """
    findings = [f"labelled {name}" for name in ops.run_labelled_containers(inventory.run_id)]
    for worker_id in inventory.worker_ids:
        project = worker_compose_project(worker_id)
        for name in ops.compose_project_containers(project):
            shape = "one-shot " if COMPOSE_ONEOFF_NAME_INFIX in name else ""
            findings.append(f"{shape}compose container {name} of project {project}")
    return findings


def _redis_keys(ops: ResidueOps, inventory: RunInventory) -> list[str]:
    patterns = inventory.redis_patterns()
    findings = unexpected_keys(ops.redis_keys(patterns) if patterns else [], inventory.run_id)
    findings += [
        f"{STORY_WORKERS_KEY} still binds {binding}"
        for binding in ops.story_worker_bindings(list(inventory.story_ids))
    ]
    return findings


def _no_snapshot_reason(inventory: RunInventory) -> str:
    """Why this run has no snapshot, in the words of whatever failed to take it."""
    if not inventory.po_thread_id:
        return "this run recorded no PO thread id"
    return inventory.po_checkpoint_snapshot_error or NO_PO_SNAPSHOT_REASON


def _po_checkpoint_thread(ops: ResidueOps, inventory: RunInventory, notes: list[str]) -> list[str]:
    """The PO checkpoint rows this run added to its thread, or why it cannot say.

    Three outcomes, and keeping them apart is the point. A run with no snapshot
    **raises**: it cannot tell its own rows from the ones the shared fixture
    chat already carried, so the kind could not be asked — and the version of
    this check that asked `thread_id = <run id>` was that case wearing an
    `absent` label. A database with no checkpoint table answers `None`, which is
    an absence with a reason said out loud in the notes. Rows that appeared
    during the run and survived cleanup are named.
    """
    if not inventory.po_thread_id or inventory.po_checkpoint_snapshot is None:
        raise RunResidueError(f"{NO_PO_SNAPSHOT} ({_no_snapshot_reason(inventory)})")
    rows = ops.po_checkpoint_rows(inventory)
    if rows is None:
        notes.append(po_checkpoints.NO_CHECKPOINTER)
        return []
    return list(rows)


#: Why each kind is vacuous when its subject list is empty. A kind not here has
#: a subject that always exists — the run id, the project, the PO thread — so it
#: is never asked about nothing.
VACUOUS_WHEN_EMPTY: dict[str, str] = {
    "control_host_containers": (
        "this run recorded no worker, so the compose-project half of the question "
        "named no project (the run-label half was still asked)"
    ),
    "target_containers": "this run recorded no deployed stack, so no target was scanned",
    "registry_repositories": "this run recorded no image repository",
    "workspaces": "this run recorded no workspace entry",
    "redis_keys": "this run named no identity to scan Redis for",
}


def vacuity_notes(inventory: RunInventory) -> list[str]:
    """Name every kind this run had nothing to ask about."""
    subjects = {
        "control_host_containers": inventory.worker_ids,
        "target_containers": inventory.stack_names,
        "registry_repositories": inventory.registry_repositories,
        "workspaces": inventory.workspace_entries(),
        "redis_keys": inventory.redis_patterns(),
    }
    return [
        NOTHING_TO_ASK_ABOUT.format(kind=kind, why=VACUOUS_WHEN_EMPTY[kind])
        for kind, named in subjects.items()
        if not named
    ]


def residue_questions(ops: ResidueOps, inventory: RunInventory, notes: list[str]) -> list[Question]:
    """The question this proof puts to each kind, in the source's own words."""
    off_host = _off_host(ops, inventory)
    return [
        Question(
            kind="control_host_containers",
            question=(
                f"docker ps -a --filter label={WorkerLabel.RUN.value}={inventory.run_id}, "
                "and the compose project of each of this run's workers"
            ),
            probe=lambda: _control_host_containers(ops, inventory),
        ),
        Question(
            kind="target_containers",
            question=(
                "docker ps -a and /opt/services on every managed target, "
                f"for stacks {list(inventory.stack_names)}"
            ),
            probe=lambda: off_host("target_containers"),
        ),
        Question(
            kind="registry_repositories",
            question=f"GET /v2/<repository>/tags/list for {list(inventory.registry_repositories)}",
            probe=lambda: off_host("registry_repositories"),
        ),
        Question(
            kind="github_repository",
            question=f"GET /repos/<org>/{inventory.repo_name}",
            probe=lambda: off_host("github_repository"),
        ),
        Question(
            kind="workspaces",
            question=f"the workspace root's {inventory.workspace_entries()}",
            probe=lambda: ops.workspace_entries(inventory.workspace_entries()),
        ),
        Question(
            kind="redis_keys",
            question=f"redis SCAN {inventory.redis_patterns()}, minus this run's retained evidence",
            probe=lambda: _redis_keys(ops, inventory),
            field_note=(
                f"{RETAINED_EVIDENCE_KEY.format(run_id=inventory.run_id)} is excluded: the "
                "removal records are this run's worker evidence, kept on purpose until their TTL"
            ),
        ),
        Question(
            kind="po_checkpoint_thread",
            question=(
                f"rows of thread {inventory.po_thread_id!r} in "
                f"{po_checkpoints.SCHEMA}.{{{','.join(po_checkpoints.ROW_IDENTITY)}}} that were "
                "not there when this run started"
            ),
            probe=lambda: _po_checkpoint_thread(ops, inventory, notes),
        ),
    ]


def prove_run_residue(
    ops: ResidueOps,
    inventory: RunInventory,
    *,
    database_check: ProofCheck,
    notes: Sequence[str] = (),
):
    """Ask every kind, and answer the database kind with the proof it already has."""
    observed: list[str] = list(vacuity_notes(inventory))
    proof = prove(
        f"run {inventory.run_id}",
        residue_questions(ops, inventory, observed),
        required_kinds=RESIDUE_KINDS,
        notes=notes,
    )
    checks = [check for check in proof.checks if check.kind != "database_rows"]
    return Proof(
        subject=proof.subject,
        checks=tuple(sorted([*checks, database_check], key=lambda check: check.kind)),
        notes=(*proof.notes, *observed),
    )


def database_check_from(report) -> ProofCheck:
    """Carry the database teardown's own verdict into this proof unchanged.

    `db_teardown` derives the closure from `pg_constraint`, records every key
    the run owns before the deletes and asks for those exact keys again after
    them, raising by table, key and constraint. That is already the proof for
    this kind, so it is neither re-asked nor re-implemented here.

    **The rows the plan declares it cannot delete are named, not hidden.** The
    append-only attempt ledger and the `users` row it points at are retained by
    a rule of the plan, and the teardown already failed if the retained set was
    anything other than exactly this run's own. They are not residue, so the
    kind stays `absent`; they are also not nothing, so the question carries them
    by table, key and count and a reader sees what stayed and why.

    **Where this kind can go red, and where it cannot.** It goes red in
    `cleanup_all`, loudly and before this proof is reached: a surviving row
    fails the teardown, so by the time the residue proof runs the answer is
    settled. Inside this proof the kind can therefore only be `absent` or —
    for a run that reached no teardown report at all — `unaskable`. That is the
    card's instruction ("the database half is already proven — read it, do not
    rewrite it") rather than an accident, and the check carries what was
    actually proven so a reader is not asked to take the label on trust.
    """
    question = "db_teardown.proof_sql over the closure derived from pg_constraint"
    if report is None:
        return ProofCheck(
            kind="database_rows",
            question=question,
            outcome=ProofOutcome.UNASKABLE,
            unaskable_reason=(
                "this run reached no database teardown report, so nothing here "
                "has asked the database"
            ),
        )
    tables = getattr(report, "tables", [])
    owned = getattr(report, "owned_keys", {})
    asked = (
        f"{question}: {len(tables)} table(s), "
        f"{sum(len(keys) for keys in owned.values())} owned key(s) read back"
    )
    retained = getattr(report, "retention_report", "")
    if retained:
        asked = f"{asked}; retained by the plan's declared rule: {retained}"
    return ProofCheck(
        kind="database_rows",
        question=asked,
        outcome=ProofOutcome.ABSENT,
    )


# --- The real reads, over the CLIs the live harness has ----------------------


@dataclass(frozen=True)
class _HostCli:
    """Docker, Redis and psql as the live harness reaches them: subprocesses.

    The harness drives the stack from the control host and has neither a Docker
    SDK nor a Redis client, exactly as `run_cleanup._DockerCli` describes. The
    one exception is SQL: the caller already owns a psql runner that feeds
    statements on stdin, and this module borrows it rather than growing a second
    one with a different ceiling on statement size.
    """

    root: Path
    api_url: str
    run_sql: po_checkpoints.RunSql
    timeout: int = 60

    def _run(self, args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
            cwd=self.root,
        )

    def _compose(self, service: str, args: list[str], *, timeout: int | None = None) -> str:
        result = self._run(["docker", "compose", "exec", "-T", service, *args], timeout=timeout)
        if result.returncode != 0:
            raise RunResidueError(
                f"{service} {args[0]} exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )
        return result.stdout

    def _redis(self, args: list[str]) -> str:
        return self._compose("redis", ["redis-cli", *args], timeout=30)

    def run_labelled_containers(self, run_id: str) -> list[str]:
        result = self._run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={WorkerLabel.RUN.value}={run_id}",
                "--format",
                "{{.Names}}",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise RunResidueError(f"docker ps failed: {result.stderr.strip()[:300]}")
        return [name for name in result.stdout.splitlines() if name.strip()]

    def compose_project_containers(self, project: str) -> list[str]:
        result = self._run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.Names}}",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise RunResidueError(
                f"docker ps for compose project {project} failed: {result.stderr.strip()[:300]}"
            )
        return [name for name in result.stdout.splitlines() if name.strip()]

    def off_host_residue(self, inventory: RunInventory) -> dict:
        args = [
            "run-residue",
            "--owner",
            GITHUB_ORG,
            "--repo",
            inventory.repo_name,
            "--api-url",
            self.api_url,
        ]
        for repository in inventory.registry_repositories:
            args += ["--repository", repository]
        for stack in inventory.stack_names:
            args += ["--stack-name", stack]
        if inventory.server_handle is not None:
            args += ["--server-handle", inventory.server_handle]
        stdout = self._compose(
            "langgraph", ["python", "-m", "shared.live_harness_cleanup", *args], timeout=180
        )
        return json.loads(marker_payload(stdout, RUN_RESIDUE_MARKER, subject="the residue probe"))

    def workspace_entries(self, entries: list[str]) -> list[str]:
        return self._workspaces("residue", entries)

    def remove_workspaces(self, entries: list[str]) -> list[str]:
        return self._workspaces("cleanup", entries)

    def _workspaces(self, command: str, entries: list[str]) -> list[str]:
        if not entries:
            return []
        args: list[str] = [command]
        for entry in entries:
            args += ["--entry", entry]
        stdout = self._compose(
            "worker-manager",
            ["python", "-m", "shared.live_harness_workspaces", *args],
            timeout=120,
        )
        payload = json.loads(
            marker_payload(stdout, WORKSPACE_RESIDUE_MARKER, subject="the workspace probe")
        )
        return [f"{payload['root']}/{entry}" for entry in payload["findings"]]

    def redis_keys(self, patterns: list[str]) -> list[str]:
        found: set[str] = set()
        for pattern in patterns:
            found.update(
                line.strip()
                for line in self._redis(["--scan", "--pattern", pattern]).splitlines()
                if line.strip()
            )
        return sorted(found)

    def story_worker_bindings(self, story_ids: list[str]) -> list[str]:
        bindings = []
        for story_id in story_ids:
            worker = self._redis(["HGET", STORY_WORKERS_KEY, story_id]).strip()
            if worker:
                bindings.append(f"{story_id} -> {worker}")
        return bindings

    def po_checkpoint_rows(self, inventory: RunInventory) -> list[str] | None:
        """The PO checkpoint rows this run added to its thread, or None for no table.

        Delegated to `po_checkpoints`, which owns the predicate, and run through
        the caller's own psql runner — the one that feeds SQL on stdin. The
        snapshot of a long-lived fixture thread is far past the 128 KiB Linux
        puts on one argv element, and a `-c` form would have failed the proof
        with a bare `OSError` exactly on the busy contours it matters most on.
        """
        return po_checkpoints.residue(
            inventory.po_thread_id, inventory.po_checkpoint_snapshot, self.run_sql
        )


def host_residue_ops(
    root: Path, api_url: str, run_sql: po_checkpoints.RunSql
) -> tuple[ResidueOps, Callable[[list[str]], list[str]]]:
    """The proof's reads, and the one write cleanup needs, over the real stack.

    The workspace remover is handed back alongside the read-only ops rather than
    hidden inside them: this module proves, and the one thing it must also do —
    take the workspaces away, which nothing else does — belongs to the caller's
    cleanup step, before the proof asks whether they are gone.
    """
    cli = _HostCli(root=root, api_url=api_url, run_sql=run_sql)
    ops = ResidueOps(
        run_labelled_containers=cli.run_labelled_containers,
        compose_project_containers=cli.compose_project_containers,
        off_host_residue=cli.off_host_residue,
        workspace_entries=cli.workspace_entries,
        redis_keys=cli.redis_keys,
        story_worker_bindings=cli.story_worker_bindings,
        po_checkpoint_rows=cli.po_checkpoint_rows,
    )
    return ops, cli.remove_workspaces
