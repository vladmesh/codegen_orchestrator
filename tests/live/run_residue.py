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
from shared.live_harness_workspaces import PLAN_DIRECTORY, WORKSPACE_RESIDUE_MARKER
from shared.queues import STORY_WORKERS_KEY
from shared.worker_compose import (
    COMPOSE_ONEOFF_NAME_INFIX,
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

#: What the PO conversation state lives in. LangGraph's Postgres checkpointer
#: writes these three tables in its own schema, outside the `public` schema the
#: database closure is derived from — which is exactly why the Definition of
#: Done names the PO checkpoint thread separately from the database rows.
PO_CHECKPOINT_SCHEMA = "langgraph"
PO_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")

#: Said when the checkpointer has never been set up in this database. It is an
#: absence, not an unreadable source: with no table there is no thread, and the
#: note says so rather than letting a reader wonder which of the two it was.
NO_CHECKPOINTER = (
    "the PO checkpointer has created no table in this database, "
    "so no checkpoint thread of this run can exist"
)


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

    def workspace_entries(self) -> list[str]:
        """The workspace root's children this run owns, in creation order of kind.

        The repository checkout is the one a developer worker's teardown
        deliberately preserves; the QA scratch directories and the per-worker
        compiled compose plans are the manager's own children of the same root.
        """
        entries = [self.repo_id] if self.repo_id else []
        entries += [f"qa-{worker_id}" for worker_id in self.worker_ids]
        entries += [f"{PLAN_DIRECTORY}/{worker_id}" for worker_id in self.worker_ids]
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


def one_off_containers(names: Iterable[str]) -> list[str]:
    """The one-shot compose containers among these names, said in their own terms.

    Named apart from the rest because they are the kind the Definition of Done
    calls out by shape — `*-integration-tests-run-*` — and because a reader of a
    red run needs to know at once whether they are looking at the known defect
    or at something new.
    """
    return [name for name in names if COMPOSE_ONEOFF_NAME_INFIX in name]


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
    #: PO checkpoint rows for this run's thread, or None if there is no
    #: checkpointer in this database at all.
    po_checkpoint_rows: Callable[[str], list[str] | None]


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


def _po_checkpoint_thread(ops: ResidueOps, inventory: RunInventory, notes: list[str]) -> list[str]:
    """This run's PO thread, or an absence the note explains.

    `None` is the answer for a database whose PO checkpointer has never created
    a table. That is genuinely an absence — with no table there is no thread —
    but it is a different absence from an empty table, so it is said out loud in
    the proof's notes rather than silently collapsed into the other one.
    """
    rows = ops.po_checkpoint_rows(inventory.run_id)
    if rows is None:
        notes.append(NO_CHECKPOINTER)
        return []
    return list(rows)


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
                f"SELECT thread_id FROM {PO_CHECKPOINT_SCHEMA}.{{{','.join(PO_CHECKPOINT_TABLES)}}}"
                f" WHERE thread_id = {inventory.run_id!r}"
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
    observed: list[str] = []
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

    `db_teardown` reads every key it owned back out of the catalog and raises
    naming the table, the key and the constraint. That is already the proof for
    this kind, so it is neither re-asked nor re-implemented here: a report is an
    absence, and no report is a kind that could not be checked — either the run
    owned no project, or the teardown raised, and a teardown that raised has
    already failed the run in its own words before this proof is reached.
    """
    question = "db_teardown.residue_sql over the closure derived from pg_constraint"
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
    return ProofCheck(kind="database_rows", question=question, outcome=ProofOutcome.ABSENT)


# --- The real reads, over the CLIs the live harness has ----------------------


@dataclass(frozen=True)
class _HostCli:
    """Docker, Redis and psql as the live harness reaches them: subprocesses.

    The harness drives the stack from the control host and has neither a Docker
    SDK nor a Redis client, exactly as `run_cleanup._DockerCli` describes.
    """

    root: Path
    api_url: str
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

    def po_checkpoint_rows(self, thread_id: str) -> list[str] | None:
        """Rows of this run's PO thread, or None when there is no checkpointer.

        Asked table by table through `to_regclass`, so a database where the PO
        consumer has never run answers "there is no such table" — an absence
        with a reason — instead of failing the query and rendering as a kind
        that could not be checked.
        """
        rows: list[str] = []
        present = False
        for table in PO_CHECKPOINT_TABLES:
            qualified = f"{PO_CHECKPOINT_SCHEMA}.{table}"
            exists = self._psql(f"SELECT to_regclass('{qualified}') IS NOT NULL;").strip()
            if exists != "t":
                continue
            present = True
            count = self._psql(
                f"SELECT count(*) FROM {qualified} WHERE thread_id = '{thread_id}';"
            ).strip()
            if count and count != "0":
                rows.append(f"{count} row(s) in {qualified} for thread {thread_id}")
        return rows if present else None

    def _psql(self, sql: str) -> str:
        return self._compose(
            "db",
            [
                "psql",
                "-U",
                "postgres",
                "-d",
                "orchestrator",
                "-v",
                "ON_ERROR_STOP=1",
                "-t",
                "-A",
                "-c",
                sql,
            ],
            timeout=30,
        )


def host_residue_ops(
    root: Path, api_url: str
) -> tuple[ResidueOps, Callable[[list[str]], list[str]]]:
    """The proof's reads, and the one write cleanup needs, over the real stack.

    The workspace remover is handed back alongside the read-only ops rather than
    hidden inside them: this module proves, and the one thing it must also do —
    take the workspaces away, which nothing else does — belongs to the caller's
    cleanup step, before the proof asks whether they are gone.
    """
    cli = _HostCli(root=root, api_url=api_url)
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
