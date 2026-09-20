"""Offline regressions for the run's residue proof.

Two properties, for every kind the Definition of Done names: a leftover of that
kind is reported **by name**, and a source that could not be read is reported as
unaskable rather than as clean. The second half is the one that needs a test:
a proof whose probe silently returns `[]` on failure passes every "nothing left"
assertion ever written, which is exactly the shape card 1318 had to remove from
the manager log.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pipeline_helpers
import po_checkpoints
import pytest
from run_proof import NEVER_ASKED, ProofFailed, ProofOutcome, prove as prove_questions
from run_residue import (
    NO_PO_SNAPSHOT,
    RESIDUE_KINDS,
    RETAINED_EVIDENCE_KEY,
    ResidueOps,
    RunInventory,
    database_check_from,
    prove_run_residue,
    residue_questions,
    unexpected_keys,
    vacuity_notes,
)

from shared.live_harness_cleanup import RESIDUE_ERROR_KEY, RESIDUE_FINDINGS_KEY
from shared.queues import STORY_WORKERS_KEY
from shared.worker_compose import COMPOSE_PLAN_DIRECTORY, worker_compose_project

pytestmark = pytest.mark.needs_no_api_credential

RUN = "run-1"
PROJECT = "11111111-1111-1111-1111-111111111111"
WORKER = "dev-p-abc-1234"
STORY = "story-1"
ONE_OFF = f"worker_{WORKER}-integration-tests-run-67ea0c169cf0"

INVENTORY = RunInventory(
    run_id=RUN,
    project_id=PROJECT,
    repo_id="repo-9",
    repo_name="live-test-9",
    story_ids=(STORY,),
    worker_ids=(WORKER,),
    registry_repositories=("live-test-9/backend",),
    stack_names=("live-test-9-abc",),
    server_handle="server-1",
    po_thread_id="po-chat-999000001",
    po_checkpoint_snapshot={"checkpoints": ["|1f0-aaa"]},
)


def _raise(message: str):
    def probe(*_args, **_kwargs):
        raise RuntimeError(message)

    return probe


def clean_ops(**overrides) -> ResidueOps:
    """Ops that answer every kind with "nothing", so a test changes one thing."""
    defaults = {
        "run_labelled_containers": lambda _run: [],
        "compose_project_containers": lambda _project: [],
        "off_host_residue": lambda _inventory: {
            kind: {RESIDUE_FINDINGS_KEY: []}
            for kind in ("github_repository", "registry_repositories", "target_containers")
        },
        "workspace_entries": lambda _entries: [],
        "redis_keys": lambda _patterns: [],
        "story_worker_bindings": lambda _stories: [],
        "po_checkpoint_rows": lambda _inventory: [],
    }
    return ResidueOps(**{**defaults, **overrides})


def prove(ops: ResidueOps, *, report=object()):
    return prove_run_residue(ops, INVENTORY, database_check=database_check_from(report))


def outcome(proof, kind: str) -> ProofOutcome:
    return next(check.outcome for check in proof.checks if check.kind == kind)


def check_for(proof, kind: str):
    return next(check for check in proof.checks if check.kind == kind)


class TestTheProofCoversTheDefinitionOfDone:
    def test_every_kind_the_definition_of_done_names_is_declared(self):
        assert set(RESIDUE_KINDS) == {
            "control_host_containers",
            "target_containers",
            "registry_repositories",
            "workspaces",
            "redis_keys",
            "github_repository",
            "po_checkpoint_thread",
            "database_rows",
        }

    def test_a_clean_run_proves_every_kind_absent(self):
        proof = prove(clean_ops())
        assert proof.failures == []
        assert {check.kind for check in proof.checks} == set(RESIDUE_KINDS)
        assert all(check.outcome is ProofOutcome.ABSENT for check in proof.checks)

    def test_a_kind_nobody_asked_fails_as_unasked(self):
        """Losing a probe must not read as a kind with nothing to report."""
        questions = [
            question
            for question in residue_questions(clean_ops(), INVENTORY, [])
            if question.kind != "workspaces"
        ]
        proof = prove_questions("run x", questions, required_kinds=RESIDUE_KINDS)
        assert outcome(proof, "workspaces") is ProofOutcome.UNASKED
        assert f"workspaces: {NEVER_ASKED}" in proof.failures


class TestALeftoverIsNamed:
    def test_a_labelled_container_on_the_control_host(self):
        proof = prove(clean_ops(run_labelled_containers=lambda _run: ["worker-dev-1"]))
        assert "control_host_containers: labelled worker-dev-1" in "; ".join(proof.failures)

    def test_an_exited_integration_tests_run_container_the_run_label_cannot_see(self):
        """The defect `issue:868e40fc0377b0dabb77` records, in its own shape.

        The one-shot container carries the worker's Compose project label and no
        `com.codegen.run.id`, so the run-label query answers "nothing" for it.
        The proof must still name it.
        """
        ops = clean_ops(
            run_labelled_containers=lambda _run: [],
            compose_project_containers=lambda project: (
                [ONE_OFF] if project == worker_compose_project(WORKER) else []
            ),
        )
        proof = prove(ops)
        failure = check_for(proof, "control_host_containers")
        assert failure.outcome is ProofOutcome.LEFTOVER
        assert failure.findings == (
            f"one-shot compose container {ONE_OFF} of project {worker_compose_project(WORKER)}",
        )

    def test_a_container_on_the_deployment_target(self):
        ops = clean_ops(
            off_host_residue=lambda _inventory: {
                "target_containers": {
                    RESIDUE_FINDINGS_KEY: ["server-1: container live-test-9-abc-backend-1"]
                },
                "github_repository": {RESIDUE_FINDINGS_KEY: []},
                "registry_repositories": {RESIDUE_FINDINGS_KEY: []},
            }
        )
        assert check_for(prove(ops), "target_containers").findings == (
            "server-1: container live-test-9-abc-backend-1",
        )

    def test_a_registry_repository_and_a_github_repository(self):
        ops = clean_ops(
            off_host_residue=lambda _inventory: {
                "target_containers": {RESIDUE_FINDINGS_KEY: []},
                "github_repository": {RESIDUE_FINDINGS_KEY: ["repository org/live-test-9"]},
                "registry_repositories": {RESIDUE_FINDINGS_KEY: ["live-test-9/backend:abc"]},
            }
        )
        proof = prove(ops)
        assert check_for(proof, "github_repository").findings == ("repository org/live-test-9",)
        assert check_for(proof, "registry_repositories").findings == ("live-test-9/backend:abc",)

    def test_a_workspace(self):
        ops = clean_ops(workspace_entries=lambda entries: [f"/data/workspaces/{entries[0]}"])
        assert check_for(prove(ops), "workspaces").findings == ("/data/workspaces/repo-9",)

    def test_a_redis_key_and_a_story_binding(self):
        ops = clean_ops(
            redis_keys=lambda _patterns: [f"worker:meta:{WORKER}"],
            story_worker_bindings=lambda _stories: [f"{STORY} -> {WORKER}"],
        )
        assert check_for(prove(ops), "redis_keys").findings == (
            f"worker:meta:{WORKER}",
            f"{STORY_WORKERS_KEY} still binds {STORY} -> {WORKER}",
        )

    def test_a_po_checkpoint_row_this_run_added_to_the_shared_thread(self):
        """The rows the run added, not the thread: the thread is a fixture."""
        ops = clean_ops(
            po_checkpoint_rows=lambda inventory: [
                f"langgraph.checkpoints row |1f0-bbb of thread {inventory.po_thread_id}"
            ]
        )
        assert check_for(prove(ops), "po_checkpoint_thread").findings == (
            "langgraph.checkpoints row |1f0-bbb of thread po-chat-999000001",
        )

    def test_a_database_row_keeps_the_teardowns_own_words(self):
        """The database half is read, not re-asked: no report is not an absence."""
        proof = prove_run_residue(clean_ops(), INVENTORY, database_check=database_check_from(None))
        check = check_for(proof, "database_rows")
        assert check.outcome is ProofOutcome.UNASKABLE
        assert "asked the database" in (check.unaskable_reason or "")


class TestASourceThatCouldNotAnswerIsNotACleanSource:
    @pytest.mark.parametrize(
        ("kind", "override"),
        [
            ("control_host_containers", {"run_labelled_containers": _raise("docker ps failed")}),
            ("workspaces", {"workspace_entries": _raise("worker-manager is not running")}),
            ("redis_keys", {"redis_keys": _raise("redis refused the scan")}),
            ("po_checkpoint_thread", {"po_checkpoint_rows": _raise("psql exited 2")}),
        ],
    )
    def test_a_probe_that_raises_is_unaskable_and_fails_the_run(self, kind, override):
        proof = prove(clean_ops(**override))
        check = check_for(proof, kind)
        assert check.outcome is ProofOutcome.UNASKABLE
        assert check.unaskable_reason.startswith("RuntimeError: ")
        assert any(
            failure.startswith(f"{kind}: could not be checked") for failure in proof.failures
        )

    def test_one_unreadable_off_host_kind_does_not_make_the_others_unaskable(self):
        """Three kinds share one probe, and they stay three answers."""
        ops = clean_ops(
            off_host_residue=lambda _inventory: {
                "target_containers": {RESIDUE_ERROR_KEY: "server-1: ssh failed"},
                "github_repository": {RESIDUE_FINDINGS_KEY: []},
                "registry_repositories": {RESIDUE_FINDINGS_KEY: []},
            }
        )
        proof = prove(ops)
        assert outcome(proof, "target_containers") is ProofOutcome.UNASKABLE
        assert "server-1: ssh failed" in check_for(proof, "target_containers").unaskable_reason
        assert outcome(proof, "github_repository") is ProofOutcome.ABSENT
        assert outcome(proof, "registry_repositories") is ProofOutcome.ABSENT

    def test_an_off_host_probe_that_could_not_run_makes_all_three_unaskable(self):
        ops = clean_ops(off_host_residue=_raise("langgraph exec exited 1"))
        proof = prove(ops)
        for kind in ("target_containers", "github_repository", "registry_repositories"):
            assert outcome(proof, kind) is ProofOutcome.UNASKABLE, kind

    def test_an_answer_with_neither_findings_nor_error_is_unaskable(self):
        ops = clean_ops(
            off_host_residue=lambda _inventory: {
                "target_containers": {},
                "github_repository": {},
                "registry_repositories": {},
            }
        )
        assert outcome(prove(ops), "target_containers") is ProofOutcome.UNASKABLE

    def test_the_proof_raises_naming_every_kind_it_could_not_prove(self):
        ops = clean_ops(
            redis_keys=_raise("redis refused the scan"),
            workspace_entries=lambda _entries: ["/data/workspaces/repo-9"],
        )
        proof = prove(ops)
        with pytest.raises(ProofFailed) as failure:
            proof.raise_if_unproven("run left resources behind")
        message = str(failure.value)
        assert "redis_keys: could not be checked" in message
        assert "workspaces: /data/workspaces/repo-9" in message


class TestWhatTheProofAsksAbout:
    def test_the_workspace_entries_are_the_checkout_the_scratch_and_the_plans(self):
        assert INVENTORY.workspace_entries() == [
            "repo-9",
            f"qa-{WORKER}",
            f"{COMPOSE_PLAN_DIRECTORY}/{WORKER}",
        ]

    def test_the_redis_patterns_name_the_run_project_repository_stories_and_workers(self):
        """The worker id is what a worker key is named by; the project is not."""
        assert INVENTORY.redis_patterns() == [
            f"*{RUN}*",
            f"*{PROJECT}*",
            "*repo-9*",
            f"*{STORY}*",
            f"*{WORKER}*",
        ]

    def test_the_retained_removal_evidence_is_the_one_key_a_clean_run_keeps(self):
        retained = RETAINED_EVIDENCE_KEY.format(run_id=RUN)
        assert unexpected_keys([retained, f"worker:meta:{WORKER}"], RUN) == [
            f"worker:meta:{WORKER}"
        ]
        assert unexpected_keys([retained], RUN) == []

    def test_a_database_with_no_checkpointer_is_an_absence_with_a_reason(self):
        proof = prove(clean_ops(po_checkpoint_rows=lambda _inventory: None))
        assert outcome(proof, "po_checkpoint_thread") is ProofOutcome.ABSENT
        assert po_checkpoints.NO_CHECKPOINTER in proof.notes

    def test_a_run_with_no_checkpoint_snapshot_could_not_ask_at_all(self):
        """The defect this kind was rebuilt for: it used to report `absent`.

        Without a snapshot of the shared fixture thread there is no way to tell
        this run's conversation rows from the ones that were already there, so
        the kind has not been asked — and saying `absent` for it is exactly the
        tautological pass criterion 3 forbids.
        """
        blind = replace(INVENTORY, po_checkpoint_snapshot=None)
        proof = prove_run_residue(clean_ops(), blind, database_check=database_check_from(object()))
        check = check_for(proof, "po_checkpoint_thread")
        assert check.outcome is ProofOutcome.UNASKABLE
        assert NO_PO_SNAPSHOT in check.unaskable_reason

    def test_the_po_question_names_the_thread_the_consumer_actually_writes(self):
        """`po-chat-<telegram id>`, never a run id: nothing checkpoints under one."""
        question = check_for(prove(clean_ops()), "po_checkpoint_thread").question
        assert "po-chat-999000001" in question
        assert RUN not in question


class TestAGreenProofSaysWhichKindsAskedAboutNothing:
    """A kind with an empty subject passes whatever the installation does.

    That is the general shape of the defect the PO checkpoint kind had: not a
    probe that failed, but a question no reachable state could answer yes to.
    The proof cannot refuse to run for it — a scaffold-only run genuinely owns
    no stack — so it says so in its notes instead, and a reader of a green run
    can tell a proven absence from a vacuous one.
    """

    def test_a_run_that_owns_nothing_names_every_vacuous_kind(self):
        empty = RunInventory(
            run_id=RUN,
            po_thread_id="po-chat-1",
            po_checkpoint_snapshot={},
        )
        proof = prove_run_residue(clean_ops(), empty, database_check=database_check_from(object()))

        assert proof.failures == []
        vacuous = {note.split(":", 1)[0] for note in proof.notes if "asked about nothing" in note}
        assert vacuous == {
            "control_host_containers",
            "target_containers",
            "registry_repositories",
            "workspaces",
        }

    def test_a_run_that_owns_its_kinds_says_nothing_of_the_sort(self):
        proof = prove(clean_ops())
        assert [note for note in proof.notes if "asked about nothing" in note] == []

    def test_vacuity_is_reported_per_kind_not_for_the_whole_proof(self):
        """One empty kind must not make the others look unasked."""
        without_registry = replace(INVENTORY, registry_repositories=())
        notes = vacuity_notes(without_registry)
        assert len(notes) == 1
        assert notes[0].startswith("registry_repositories:")


class TestTheDatabaseKindIsTheTeardownsOwnVerdict:
    def test_a_report_carries_what_it_actually_proved(self):
        report = SimpleNamespace(
            tables=["projects", "stories"], owned_keys={"stories": ["s1", "s2"]}
        )
        check = database_check_from(report)
        assert check.outcome is ProofOutcome.ABSENT
        assert "2 table(s), 2 owned key(s)" in check.question

    def test_no_report_is_a_kind_that_was_never_asked_of_the_database(self):
        check = database_check_from(None)
        assert check.outcome is ProofOutcome.UNASKABLE
        assert "asked the database" in check.unaskable_reason


class FakePsql:
    """psql as `pipeline_helpers._psql` invokes it: SQL on stdin, tuples out.

    It answers `po_checkpoints`' own statements, so the capture below is driven
    through the real presence question, the real snapshot query and the real
    parser rather than through a restatement of them.

    The presence answer is produced the way the database produces it: a table
    this fake does not have contributes **no row**, because its `WHERE` selects
    none. That is the whole repair. The previous fake answered `t` for a present
    table and `f` for an absent one — the renderings the *code* expected — while
    PostgreSQL renders the cast in the old query `true`. So the fake agreed with
    the code, the code disagreed with the database, and the PO checkpoint kind
    was `unaskable` on every run until stand run 35486586267 said so out loud.
    """

    def __init__(self, *, tables: tuple[str, ...] = tuple(po_checkpoints.ROW_IDENTITY), rows=()):
        self.tables = tables
        self.rows = list(rows)
        self.statements: list[str] = []

    def subprocess_run(self, argv, **kwargs):
        self.statements.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=self._answer(kwargs["input"]), stderr="")

    def _answer(self, sql: str) -> str:
        if "to_regclass" in sql:
            return "".join(
                f"{table}\t{po_checkpoints.PRESENT}\n"
                for table in po_checkpoints.ROW_IDENTITY
                if table in self.tables and f"to_regclass('{po_checkpoints.SCHEMA}.{table}')" in sql
            )
        return "".join(f"{table}\t{identity}\n" for table, identity in self.rows)


class RefusingPsql:
    """A psql that could not run at all: the one case that is unaskable."""

    REASON = "could not connect to server: Connection refused"

    def subprocess_run(self, argv, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr=self.REASON)


def _capture(monkeypatch, psql) -> dict:
    """Drive the run's real PO capture against `psql`, and record it on a context."""
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", psql.subprocess_run)
    monkeypatch.setattr(pipeline_helpers, "po_input_cursor", lambda: "1700000000000-0")
    ctx: dict = {}
    pipeline_helpers.record_run_po_position(
        ctx, pipeline_helpers.capture_run_po_position(pipeline_helpers.TEST_TELEGRAM_ID)
    )
    return ctx


def _po_check(ctx: dict, *, rows: list[str] | None = None):
    """The residue proof's PO kind, for the position this context recorded."""
    inventory = replace(
        INVENTORY,
        po_thread_id=ctx["po_thread_id"],
        po_checkpoint_snapshot=ctx["po_checkpoint_snapshot"],
        po_checkpoint_snapshot_error=ctx["po_checkpoint_snapshot_error"],
    )
    ops = clean_ops(po_checkpoint_rows=lambda _inventory: rows if rows is not None else [])
    proof = prove_run_residue(
        ops,
        inventory,
        database_check=database_check_from(object()),
        notes=[
            note
            for note in (
                ctx["po_checkpoint_snapshot_error"],
                ctx["po_checkpoint_snapshot_note"],
            )
            if note
        ],
    )
    return proof, check_for(proof, "po_checkpoint_thread")


class TestTheRunTakesThePoSnapshotItsProofNeeds:
    """Criterion 1: the kind has to be askable on an ordinary run.

    A kind that is `unaskable` on every run is the same defect as one that is
    `absent` on every run — it reports on nothing. Stand run 35486586267 was the
    first to say so, and the reason was a presence query whose answer no
    database could give.
    """

    def test_an_ordinary_run_fixes_a_snapshot_and_the_kind_is_asked(self, monkeypatch):
        psql = FakePsql(rows=[("checkpoints", "|1f0-aaa")])
        ctx = _capture(monkeypatch, psql)

        assert ctx["po_checkpoint_snapshot"] == {"checkpoints": ["|1f0-aaa"]}
        assert ctx["po_checkpoint_snapshot_error"] is None
        assert ctx["po_checkpoint_snapshot_note"] is None

        _proof, check = _po_check(ctx)
        assert check.outcome is ProofOutcome.ABSENT

    def test_a_row_this_run_added_is_still_named_a_leftover(self, monkeypatch):
        """Askable is not lenient: the snapshot is what makes a leftover visible."""
        ctx = _capture(monkeypatch, FakePsql(rows=[("checkpoints", "|1f0-aaa")]))
        _proof, check = _po_check(ctx, rows=["langgraph.checkpoints row |1f0-bbb"])
        assert check.outcome is ProofOutcome.LEFTOVER
        assert check.findings == ("langgraph.checkpoints row |1f0-bbb",)

    def test_no_checkpoint_table_at_the_start_is_an_answer_not_a_missing_snapshot(
        self, monkeypatch
    ):
        """Nothing pre-existed, so the empty snapshot is the true one.

        This was read as "this run fixed no snapshot" and made the kind
        unaskable, which is the opposite of what it means: with no table there
        is nothing to tell this run's rows apart from, because there are no
        other rows.
        """
        ctx = _capture(monkeypatch, FakePsql(tables=()))

        assert ctx["po_checkpoint_snapshot"] == {}
        assert ctx["po_checkpoint_snapshot_error"] is None
        assert ctx["po_checkpoint_snapshot_note"] == po_checkpoints.NO_CHECKPOINTER_AT_START

        proof, check = _po_check(ctx)
        assert check.outcome is ProofOutcome.ABSENT
        assert po_checkpoints.NO_CHECKPOINTER_AT_START in proof.notes

    def test_a_read_that_failed_is_unaskable_and_names_what_failed(self, monkeypatch):
        """The one case that must stay red — and it now says why, in the check."""
        ctx = _capture(monkeypatch, RefusingPsql())

        assert ctx["po_checkpoint_snapshot"] is None
        assert RefusingPsql.REASON in ctx["po_checkpoint_snapshot_error"]

        proof, check = _po_check(ctx)
        assert check.outcome is ProofOutcome.UNASKABLE
        assert NO_PO_SNAPSHOT in check.unaskable_reason
        assert RefusingPsql.REASON in check.unaskable_reason
        assert proof.failures, "an unaskable kind still fails the run"
