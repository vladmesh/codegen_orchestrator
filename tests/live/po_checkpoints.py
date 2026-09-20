"""Remove the PO conversation state one run caused, and prove it is gone.

The Definition of Done names "no PO checkpoint thread" among the things a run
must leave nothing of. Getting that question right took two tries, and the wrong
one is worth writing down because it is the failure mode this whole proof exists
to prevent.

**The wrong question.** The first version asked
`SELECT count(*) … WHERE thread_id = '<run id>'`. Nothing in this repository ever
writes a checkpoint under a run id: the PO consumer is the only graph with a
Postgres checkpointer, and the thread it passes is
`po_thread_id(telegram_chat_id)` → `po-chat-<chat id>`
(`services/langgraph/src/consumers/po.py:372`, `shared/contracts/queues/po.py`).
The provisioner uses `provisioner-<handle>` and the architect a fresh `uuid4()`,
and both compile with `MemorySaver`. So the predicate matched nothing on every
run, for every possible state of the system — a kind that always reported
`absent` while asking a question the system cannot answer yes to. A tautological
pass is an unasked check wearing a passed label.

**The right question, and why it is not "delete the thread".** The thread a live
run causes is `po-chat-<the harness's Telegram id>`, and that Telegram id is a
*fixture*: every live run on the contour uses the same one, so the thread is not
the run's to delete — dropping it would take away conversation state that belongs
to whatever ran before, and on a contour with two concurrent runs, to a run that
is still using it. What *is* the run's is the rows that appeared on that thread
while it ran.

So this module works by difference. Before the run can publish anything, it
snapshots the identity of every row already on the thread; after cleanup it
deletes the rows on that thread that are *not* in the snapshot, and reads the
same predicate back. The thread returns to exactly the state it had before the
run, which keeps the surviving head checkpoint referencing only channel versions
that also survive.

**The bound this leaves, stated rather than hidden.** "Appeared during this run"
is the run's rows only while no other run is writing to the same thread at the
same time. The stand runs its suites one at a time, so that holds there; on a
contour where two runs shared the fixture chat concurrently this would remove the
other run's rows too. The fix for that is not here: it is the same fixture-identity
question parked with the owner on card 1314, which makes the Telegram id run-owned
and therefore the thread run-owned with it.

**Absence is read, never inferred.** The delete is followed by the snapshot
predicate asked again, and a row that survives is named by its table and its key.
A database where the PO checkpointer has never created a table answers "no such
table", which is an absence with a stated reason and not an unreadable source; a
psql that failed is neither, and fails the proof as a kind that could not be
checked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

#: The schema LangGraph's Postgres checkpointer writes into, as
#: `CHECKPOINT_DATABASE_URL` points it (`docker-compose.yml`:
#: `…?options=-c search_path=langgraph`).
SCHEMA = "langgraph"

#: Each checkpoint table and the expression that identifies one of its rows
#: within a thread, from `langgraph/checkpoint/postgres/base.py`'s MIGRATIONS:
#:
#: * `checkpoints` is keyed by `(thread_id, checkpoint_ns, checkpoint_id)`.
#: * `checkpoint_writes` adds `task_id` and `idx` to that key.
#: * `checkpoint_blobs` carries **no** `checkpoint_id` at all — it is keyed by
#:   `(thread_id, checkpoint_ns, channel, version)`. That is why the snapshot
#:   records a per-table identity expression instead of a list of checkpoint
#:   ids: a blob cannot be attributed to a checkpoint, only to a channel
#:   version, and deleting the wrong ones would strand the surviving head
#:   checkpoint on channel versions that no longer exist.
ROW_IDENTITY: dict[str, str] = {
    "checkpoints": "checkpoint_ns || '|' || checkpoint_id",
    "checkpoint_writes": "checkpoint_ns || '|' || checkpoint_id || '|' || task_id || '|' || idx",
    "checkpoint_blobs": "checkpoint_ns || '|' || channel || '|' || version",
}

#: Said when the checkpointer has never created a table in this database. An
#: absence with a reason: with no table there is no thread, and a reader is told
#: which of the two it is looking at.
NO_CHECKPOINTER = (
    "the PO checkpointer has created no table in this database, "
    "so no checkpoint row of this run can exist"
)

#: Said of a *run start* with no checkpoint table. It is not the same statement
#: as `NO_CHECKPOINTER` above, which is about the moment the residue is asked:
#: with no table at the start, nothing pre-existed on the thread, so the empty
#: snapshot is the true one and every row found later is this run's. Recording
#: that is what keeps the kind askable instead of turning a perfectly good
#: answer into "this run fixed no snapshot".
NO_CHECKPOINTER_AT_START = (
    "the PO checkpointer had created no table when this run started, so nothing "
    "pre-existed on the thread and every row on it now appeared during this run"
)

#: The one word a present table answers with. The presence question is asked so
#: that an absent table returns **no row at all**, and a present one returns
#: this token — nothing about the answer depends on how a value is rendered.
#:
#: That is not a style choice, it is the repair of the defect stand run
#: 35486586267 hit. The question used to be
#: `SELECT '<table>', (to_regclass(...) IS NOT NULL)::text`, and it was read by
#: comparing the second field to `'t'`. psql prints a *boolean column* as `t`,
#: but this one is cast to `text` first, and PostgreSQL's boolean-to-text cast
#: renders `true`. So the comparison was false for every table of every
#: database that has ever existed: `checkpointer_present` returned False always,
#: `snapshot` answered `None` always, and the residue kind was `unaskable` on
#: every run — the same tautology as the `thread_id = <run id>` predicate this
#: module was written to replace, wearing the other label. The offline fake
#: answered `t` because the code said `t`, so nothing caught it.
PRESENT = "present"


class PoCheckpointError(AssertionError):
    """The PO checkpoint rows could not be read, removed or proven gone."""


@dataclass(frozen=True)
class SqlResult:
    """One psql invocation's outcome, as the caller's runner reports it."""

    returncode: int
    stdout: str
    stderr: str


RunSql = Callable[[str], SqlResult]


def sql_literal(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _text_array(values: Iterable[str]) -> str:
    """A `text[]` literal, or the empty one — which selects everything as new."""
    items = ", ".join(sql_literal(value) for value in values)
    return f"ARRAY[{items}]::text[]"


def presence_sql() -> str:
    """Ask which of the checkpointer's tables this database has.

    `to_regclass` rather than a `SELECT` against the table, because a missing
    table makes a query fail, and a failed query is how a kind that could not be
    checked is told apart from one that found nothing.

    The existence is carried by **whether a row comes back**, and the one field
    that does come back is a fixed word this module wrote itself. A question
    whose answer is a rendered value is a question whose answer a driver, a
    server version or a psql flag can change under it, and that is exactly how
    this check came to be false on every database (see `PRESENT`).
    """
    parts = [
        f"SELECT {sql_literal(table)}, {sql_literal(PRESENT)} "
        f"WHERE to_regclass({sql_literal(f'{SCHEMA}.{table}')}) IS NOT NULL"
        for table in ROW_IDENTITY
    ]
    return "\nUNION ALL\n".join(parts) + ";"


def snapshot_sql(thread_id: str) -> str:
    """Identify every row already on this thread, table by table."""
    predicate = f"thread_id = {sql_literal(thread_id)}"
    parts = [
        f"SELECT {sql_literal(table)}, {identity} FROM {SCHEMA}.{table} WHERE {predicate}"
        for table, identity in ROW_IDENTITY.items()
    ]
    return "\nUNION ALL\n".join(parts) + ";"


def _new_row_predicate(thread_id: str, table: str, snapshot: dict[str, list[str]]) -> str:
    """Rows of this thread that were not there when the run started.

    An empty snapshot makes this every row of the thread, which is the right
    answer: nothing pre-existed, so everything on it appeared during the run.
    """
    known = _text_array(snapshot.get(table, []))
    return f"thread_id = {sql_literal(thread_id)} AND ({ROW_IDENTITY[table]}) <> ALL ({known})"


def delete_sql(thread_id: str, snapshot: dict[str, list[str]]) -> str:
    """Remove this run's rows, leaving the thread as the run found it.

    Writes and blobs before checkpoints, so that a delete interrupted half way
    cannot leave a checkpoint whose channel versions are gone — the state the
    saver would actually fail to read.
    """
    order = ["checkpoint_writes", "checkpoint_blobs", "checkpoints"]
    return "\n".join(
        f"DELETE FROM {SCHEMA}.{table} WHERE {_new_row_predicate(thread_id, table, snapshot)};"
        for table in order
    )


def residue_sql(thread_id: str, snapshot: dict[str, list[str]]) -> str:
    """Ask the delete's own predicate again; anything it answers is a leftover."""
    parts = [
        f"SELECT {sql_literal(table)}, {identity} FROM {SCHEMA}.{table} "
        f"WHERE {_new_row_predicate(thread_id, table, snapshot)}"
        for table, identity in ROW_IDENTITY.items()
    ]
    return "\nUNION ALL\n".join(parts) + ";"


def parse_rows(stdout: str) -> list[tuple[str, str]]:
    """psql's unaligned tab-separated tuples, as `(table, row identity)`."""
    rows: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        table, _, identity = line.partition("\t")
        rows.append((table.strip(), identity.strip()))
    return rows


def group_rows(rows: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for table, identity in rows:
        grouped.setdefault(table, []).append(identity)
    return grouped


def _require(result: SqlResult, doing: str) -> str:
    if result.returncode != 0:
        raise PoCheckpointError(f"{doing} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def checkpointer_present(run_sql: RunSql) -> bool:
    """Whether any checkpoint table exists in this database at all.

    A row means the table is there. An answer this module does not recognise is
    **raised**, never read as an absence: "the database said something else" and
    "the checkpointer has no tables" are different facts, and the run that
    conflated them reported a proof it had never taken.
    """
    rows = parse_rows(_require(run_sql(presence_sql()), "reading the PO checkpoint tables"))
    unreadable = sorted({answer for _table, answer in rows if answer != PRESENT})
    if unreadable:
        raise PoCheckpointError(
            "the PO checkpoint presence query answered "
            f"{unreadable} where only {PRESENT!r} is an answer this can read"
        )
    return bool(rows)


def snapshot(thread_id: str, run_sql: RunSql) -> dict[str, list[str]] | None:
    """What is already on this thread, or None when there is no checkpointer."""
    if not thread_id:
        raise PoCheckpointError("a PO checkpoint snapshot is scoped to a thread; none was named")
    if not checkpointer_present(run_sql):
        return None
    return group_rows(
        parse_rows(_require(run_sql(snapshot_sql(thread_id)), "reading this thread's rows"))
    )


def remove_run_rows(
    thread_id: str, snapshot_rows: dict[str, list[str]], run_sql: RunSql
) -> list[str]:
    """Delete the rows that appeared on this thread during the run.

    The snapshot is required, and that is a safety property rather than a type
    preference: with no snapshot every row on the thread looks like the run's,
    and a run whose snapshot read failed would wipe a shared fixture
    conversation instead of reporting a kind it could not ask. A caller with no
    snapshot removes nothing.

    Returns what the delete's own read-back still sees — reported by the caller,
    and asked again independently by the residue proof.
    """
    if not checkpointer_present(run_sql):
        return []
    _require(
        run_sql(delete_sql(thread_id, snapshot_rows)), "removing this run's PO checkpoint rows"
    )
    return _left_on_thread(thread_id, snapshot_rows, run_sql)


def residue(
    thread_id: str, snapshot_rows: dict[str, list[str]], run_sql: RunSql
) -> list[str] | None:
    """The rows of this run still on the thread, or None when there is no table.

    Asked after cleanup and independently of the removal above, because a
    removal can only verify what it removed. `None` is an absence with a stated
    reason (`NO_CHECKPOINTER`), never an unreadable source: a psql that failed
    raises instead.
    """
    if not checkpointer_present(run_sql):
        return None
    return _left_on_thread(thread_id, snapshot_rows, run_sql)


def _left_on_thread(thread_id: str, known: dict[str, list[str]], run_sql: RunSql) -> list[str]:
    left = parse_rows(
        _require(run_sql(residue_sql(thread_id, known)), "proving this run's PO rows are gone")
    )
    return [f"{SCHEMA}.{table} row {identity} of thread {thread_id}" for table, identity in left]
