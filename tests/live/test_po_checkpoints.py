"""Offline regressions for the run-scoped PO checkpoint removal and proof.

The property: a run takes back the conversation rows it added to the shared
fixture thread, and takes back nothing else. The failure this replaces is the
opposite of a leftover — a predicate that matched nothing on every possible run
and reported `absent` for it.
"""

from __future__ import annotations

import po_checkpoints
from po_checkpoints import (
    ROW_IDENTITY,
    SCHEMA,
    PoCheckpointError,
    SqlResult,
    checkpointer_present,
    delete_sql,
    group_rows,
    parse_rows,
    presence_sql,
    remove_run_rows,
    residue,
    residue_sql,
    snapshot,
    snapshot_sql,
)
import pytest

pytestmark = pytest.mark.needs_no_api_credential

THREAD = "po-chat-999000001"
BEFORE = {"checkpoints": ["|1f0-aaa"], "checkpoint_blobs": ["|messages|00000000001"]}


def rows(text: str) -> SqlResult:
    return SqlResult(returncode=0, stdout=text, stderr="")


def failing(message: str = "could not connect to server") -> SqlResult:
    return SqlResult(returncode=2, stdout="", stderr=message)


#: What psql prints for `presence_sql`. A present table contributes its row; an
#: absent one contributes nothing at all, because its `WHERE` selects no row —
#: which is why this fake can be trusted where the previous one could not. The
#: old presence question ended in a rendered boolean, and the old fake answered
#: `t` because the code compared to `t`; the database answers `true`, so the
#: check was false everywhere and the fake agreed with the code instead of with
#: psql. Nothing here depends on a rendering any more.
ALL_PRESENT = rows("".join(f"{table}\t{po_checkpoints.PRESENT}\n" for table in ROW_IDENTITY))
NONE_PRESENT = rows("")

#: The rendering the real database produced for the question that was asked
#: before this card: `(… IS NOT NULL)::text`. It must never again be read as an
#: answer — least of all as "no table".
BOOLEAN_RENDERINGS = ("t", "f", "true", "false")


class FakeDb:
    """A psql runner that answers each statement kind from a script."""

    def __init__(self, *, presence=ALL_PRESENT, snapshot=rows(""), residue=rows("")):
        self.presence, self.snapshot, self.residue = presence, snapshot, residue
        self.statements: list[str] = []

    def __call__(self, sql: str) -> SqlResult:
        self.statements.append(sql)
        if "to_regclass" in sql:
            return self.presence
        if sql.startswith("DELETE"):
            return rows("")
        return self.snapshot if "<> ALL" not in sql else self.residue


class TestTheQuestionIsAboutTheThreadThatExists:
    def test_the_identity_of_every_table_matches_its_primary_key(self):
        """A blob has no checkpoint id, so it cannot be scoped by one."""
        assert set(ROW_IDENTITY) == {"checkpoints", "checkpoint_writes", "checkpoint_blobs"}
        assert "checkpoint_id" not in ROW_IDENTITY["checkpoint_blobs"]
        assert "channel" in ROW_IDENTITY["checkpoint_blobs"]
        assert "task_id" in ROW_IDENTITY["checkpoint_writes"]

    def test_the_snapshot_asks_for_the_thread_and_nothing_else(self):
        sql = snapshot_sql(THREAD)
        assert sql.count(f"thread_id = '{THREAD}'") == len(ROW_IDENTITY)
        for table in ROW_IDENTITY:
            assert f"{SCHEMA}.{table}" in sql

    def test_a_thread_id_is_required(self):
        with pytest.raises(PoCheckpointError, match="scoped to a thread"):
            snapshot("", FakeDb())

    def test_a_quote_in_a_thread_id_cannot_leave_its_literal(self):
        assert po_checkpoints.sql_literal("po-chat-o'brien") == "'po-chat-o''brien'"


class TestOnlyTheRunsOwnRowsAreTaken:
    def test_the_delete_spares_every_row_the_snapshot_named(self):
        sql = delete_sql(THREAD, BEFORE)
        assert "'|1f0-aaa'" in sql
        assert "'|messages|00000000001'" in sql
        assert sql.count("DELETE FROM") == len(ROW_IDENTITY)
        # A table the snapshot said nothing about still gets the empty array,
        # which selects every row of the thread on that table as new.
        assert "ARRAY[]::text[]" in sql

    def test_writes_and_blobs_go_before_checkpoints(self):
        """A half-done delete must not strand a checkpoint on missing versions."""
        sql = delete_sql(THREAD, BEFORE)
        assert sql.index("checkpoint_writes") < sql.index(f"{SCHEMA}.checkpoints WHERE")
        assert sql.index("checkpoint_blobs") < sql.index(f"{SCHEMA}.checkpoints WHERE")

    def test_an_empty_snapshot_makes_every_row_of_the_thread_the_runs(self):
        sql = delete_sql(THREAD, {})
        assert sql.count("ARRAY[]::text[]") == len(ROW_IDENTITY)

    def test_the_proof_asks_the_deletes_own_predicate_again(self):
        for table in ROW_IDENTITY:
            assert f"{SCHEMA}.{table}" in residue_sql(THREAD, BEFORE)
        assert residue_sql(THREAD, BEFORE).count("<> ALL") == len(ROW_IDENTITY)


class TestTheThreeAnswers:
    def test_a_clean_removal_leaves_nothing_and_says_so(self):
        db = FakeDb()
        assert remove_run_rows(THREAD, BEFORE, db) == []

    def test_a_row_that_survives_is_named_by_its_table_and_key(self):
        db = FakeDb(residue=rows("checkpoints\t|1f0-bbb\n"))
        assert remove_run_rows(THREAD, BEFORE, db) == [
            f"{SCHEMA}.checkpoints row |1f0-bbb of thread {THREAD}"
        ]

    def test_no_checkpoint_table_is_an_absence_not_an_error(self):
        db = FakeDb(presence=NONE_PRESENT)
        assert snapshot(THREAD, db) is None
        assert residue(THREAD, {}, db) is None
        assert remove_run_rows(THREAD, {}, db) == []
        assert not any(statement.startswith("DELETE") for statement in db.statements)

    def test_a_psql_that_failed_raises_rather_than_answering_nothing(self):
        """The whole point: an unreadable source is never an empty source."""
        with pytest.raises(PoCheckpointError, match="could not connect"):
            snapshot(THREAD, FakeDb(presence=failing()))
        with pytest.raises(PoCheckpointError, match="could not connect"):
            residue(THREAD, {}, FakeDb(presence=failing()))
        with pytest.raises(PoCheckpointError, match="could not connect"):
            remove_run_rows(THREAD, {}, FakeDb(snapshot=failing(), residue=failing()))

    def test_the_residue_read_is_independent_of_the_removals_own_read(self):
        """A removal can only verify what it removed; the proof asks again."""
        db = FakeDb()
        residue(THREAD, BEFORE, db)
        assert not any(statement.startswith("DELETE") for statement in db.statements)


class TestParsing:
    def test_psql_tuples_become_table_and_identity_pairs(self):
        parsed = parse_rows("checkpoints\t|1f0-aaa\n\ncheckpoint_blobs\t|messages|1\n")
        assert parsed == [("checkpoints", "|1f0-aaa"), ("checkpoint_blobs", "|messages|1")]
        assert group_rows(parsed) == {
            "checkpoints": ["|1f0-aaa"],
            "checkpoint_blobs": ["|messages|1"],
        }

    def test_presence_is_read_through_to_regclass_for_every_table(self):
        sql = presence_sql()
        for table in ROW_IDENTITY:
            assert f"to_regclass('{SCHEMA}.{table}')" in sql
        assert checkpointer_present(FakeDb()) is True
        assert checkpointer_present(FakeDb(presence=NONE_PRESENT)) is False

    def test_a_table_that_is_not_there_answers_with_no_row_rather_than_a_value(self):
        """Existence is carried by the row, so no rendering can be misread.

        The defect this replaces: the question ended in `(… IS NOT NULL)::text`
        and the answer was compared to `'t'`. PostgreSQL renders that cast
        `true`, so the comparison was false for every table of every database —
        `snapshot` answered `None` on every run and the residue proof reported
        the PO kind as one it could not ask, on stand run 35486586267 and on
        every run before it.
        """
        sql = presence_sql()
        assert "::text" not in sql
        for table in ROW_IDENTITY:
            assert (
                f"SELECT '{table}', '{po_checkpoints.PRESENT}' "
                f"WHERE to_regclass('{SCHEMA}.{table}') IS NOT NULL"
            ) in sql

    @pytest.mark.parametrize("rendering", BOOLEAN_RENDERINGS)
    def test_a_rendered_boolean_is_unreadable_and_never_an_absence(self, rendering):
        """An answer this module does not recognise raises; it is not "no table"."""
        answered = rows("".join(f"{table}\t{rendering}\n" for table in ROW_IDENTITY))
        with pytest.raises(PoCheckpointError, match="only 'present' is an answer"):
            checkpointer_present(FakeDb(presence=answered))
        with pytest.raises(PoCheckpointError, match="only 'present' is an answer"):
            snapshot(THREAD, FakeDb(presence=answered))
