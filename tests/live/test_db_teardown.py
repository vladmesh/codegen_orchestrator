"""The database half of the residue proof, offline.

Run 35441716423 ended `cleanup failed` and left project
fa8ad4c3-4d41-4eb9-9c4d-11f62282bd07 on the stand: the level-1 grant deploy had
written a `users_grant_intents` row pointing at its `deploy-grant-…` run, and
the hand-written delete list did not know that table existed. These tests hold
the two properties that make that failure impossible to repeat — the plan is
derived from the foreign keys rather than written down, and a row that survives
is named — without needing a stack.
"""

import json

import db_teardown
from db_teardown import SqlResult, TeardownError
from db_teardown_fake import FakeDatabase
from live_harness import run_user_sweep_predicate
import pipeline_helpers
import pytest

from shared import live_contour
from shared.tests.project_cleanup import metadata_catalog_payload

pytestmark = pytest.mark.needs_no_api_credential

PROJECT_ID = "fa8ad4c3-4d41-4eb9-9c4d-11f62282bd07"
GRANT_RUN_ID = "deploy-grant-2c632bdb07cd4948a6b9804a899f1e35"
GRANT_INTENT_ID = "grant-intent-2c632bdb"

# The teardown error of run 35441716423, as psql printed it.
REFUSAL_STDERR = (
    'ERROR:  update or delete on table "runs" violates foreign key constraint '
    '"users_grant_intents_execution_run_id_fkey" on table "users_grant_intents"\n'
    f"DETAIL:  Key (id)=({GRANT_RUN_ID}) is still referenced from table "
    '"users_grant_intents".\n'
)


RUN_TELEGRAM_ID = 970_000_777
RUN_USER_ROW = "4242"
LEDGER_ROW = "0a4a4d0e-0000-4000-8000-000000000001"


def _plan(payload: str | None = None, *, user_predicate: str | None = None):
    catalog = db_teardown.parse_catalog(payload or metadata_catalog_payload())
    roots = [db_teardown.Root(table="projects", predicate=f"id::text = '{PROJECT_ID}'")]
    if user_predicate is not None:
        roots.append(db_teardown.Root(table="users", predicate=user_predicate))
    plan = db_teardown.build_plan(catalog, roots=roots)
    return catalog, plan


def _grant_path_database(**kwargs) -> FakeDatabase:
    """A project whose runs a grant intent references, as the evidence run had."""
    owned = {
        "projects": [PROJECT_ID],
        "runs": [GRANT_RUN_ID],
        "users_grant_intents": [GRANT_INTENT_ID],
    }
    return FakeDatabase(owned=owned, **kwargs)


# ── the plan is derived, not written down ────────────────────────────────


def test_plan_covers_every_table_that_would_refuse_the_project_delete():
    _, plan = _plan()
    tables = [step.table for step in plan]

    # The table that stranded the evidence run, and its sibling on the same run.
    assert "users_grant_intents" in tables
    assert "temporary_access_grants" in tables
    # Rows the database unlinks by itself are neither deleted nor expected gone:
    # the append-only attempt ledger FKs runs with ON DELETE SET NULL.
    assert "engineering_attempt_ledger" not in tables
    # Only incoming references are followed, so the project's owner is untouched.
    assert "users" not in tables
    assert tables[-1] == "projects"


def test_plan_deletes_a_grant_intent_before_the_run_it_references():
    _, plan = _plan()
    tables = [step.table for step in plan]

    assert tables.index("users_grant_intents") < tables.index("runs")
    step = next(step for step in plan if step.table == "users_grant_intents")
    assert "users_grant_intents_execution_run_id_fkey" in step.belongs_because()


def test_a_new_table_referencing_runs_joins_the_plan_by_itself():
    """The property the hand-written list did not have.

    `users_grant_intents` was new once. The next such table is in the plan
    because its foreign key exists, not because somebody remembered it.
    """
    catalog_payload = json.loads(metadata_catalog_payload())
    catalog_payload["foreign_keys"].append(
        {
            "constraint_name": "later_table_run_id_fkey",
            "child_table": "later_table",
            "parent_table": "runs",
            "delete_rule": "a",
            "child_columns": ["run_id"],
            "parent_columns": ["id"],
        }
    )
    catalog_payload["primary_keys"].append({"table_name": "later_table", "columns": ["id"]})
    catalog_payload["columns"].extend(
        [
            {"table_name": "later_table", "column_name": "id"},
            {"table_name": "later_table", "column_name": "run_id"},
        ]
    )

    _, plan = _plan(json.dumps(catalog_payload))
    tables = [step.table for step in plan]

    assert tables.index("later_table") < tables.index("runs")
    assert "DELETE FROM later_table WHERE run_id IN" in db_teardown.delete_sql(plan)


def test_a_cycle_is_named_rather_than_guessed_at():
    payload = {
        "foreign_keys": [
            {
                "constraint_name": "left_right_fkey",
                "child_table": "left_table",
                "parent_table": "projects",
                "delete_rule": "a",
                "child_columns": ["project_id"],
                "parent_columns": ["id"],
            },
            {
                "constraint_name": "right_left_fkey",
                "child_table": "right_table",
                "parent_table": "left_table",
                "delete_rule": "a",
                "child_columns": ["left_id"],
                "parent_columns": ["id"],
            },
            {
                "constraint_name": "left_right_back_fkey",
                "child_table": "left_table",
                "parent_table": "right_table",
                "delete_rule": "a",
                "child_columns": ["right_id"],
                "parent_columns": ["id"],
            },
        ],
        "primary_keys": [
            {"table_name": table, "columns": ["id"]}
            for table in ("projects", "left_table", "right_table")
        ],
        "columns": [
            {"table_name": "projects", "column_name": "id"},
            {"table_name": "left_table", "column_name": "id"},
            {"table_name": "left_table", "column_name": "project_id"},
            {"table_name": "left_table", "column_name": "right_id"},
            {"table_name": "right_table", "column_name": "id"},
            {"table_name": "right_table", "column_name": "left_id"},
        ],
    }
    with pytest.raises(TeardownError) as excinfo:
        _plan(json.dumps(payload))

    assert "cycle" in str(excinfo.value)
    # Every table the cycle stranded, the root included: an operator reading
    # this needs the names, not a traceback out of psql.
    assert "left_table" in str(excinfo.value)
    assert "right_table" in str(excinfo.value)


# ── the columns that are not foreign keys ────────────────────────────────


def test_a_denormalized_project_column_is_covered_without_being_listed():
    """`service_deployments.project_id` carries no foreign key, on purpose.

    `shared/models/deployment.py` denormalizes it from the application and
    leaves `application_id` nullable, so a row can name this run's project and
    be reachable through no key at all. A walk of foreign keys alone cannot see
    it: it is not deleted, it refuses nothing, and the residue proof would call
    the teardown clean.
    """
    _, plan = _plan()
    step = next(step for step in plan if step.table == "service_deployments")

    assert "project_id IN (SELECT id FROM projects WHERE" in step.predicate
    assert "no foreign key" in step.belongs_because()
    # Same derivation, second table, nothing listed anywhere.
    assert "api_keys" in [step.table for step in plan]


def test_a_row_the_foreign_keys_cannot_see_is_deleted_and_named(monkeypatch):
    deployment_row = "4711"
    database = FakeDatabase(
        owned={"projects": [PROJECT_ID], "service_deployments": [deployment_row]},
        residue=[("service_deployments", deployment_row)],
    )
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    with pytest.raises(TeardownError) as excinfo:
        pipeline_helpers._cleanup_db(PROJECT_ID)

    message = str(excinfo.value)
    assert f"service_deployments: id={deployment_row}" in message
    assert "service_deployments.project_id → projects.id (no foreign key" in message


def test_the_schemas_own_unlinking_key_outranks_a_column_name():
    """`engineering_budget_reservations` is retained on purpose.

    Its `project_id` is `ON DELETE SET NULL` — the schema's own instruction for
    teardown — while its `story_id` happens to carry no key. The explicit key
    wins, so the table is neither deleted nor expected gone.
    """
    _, plan = _plan()

    assert "engineering_budget_reservations" not in [step.table for step in plan]


def test_an_ambiguous_denormalized_column_is_raised():
    payload = {
        "foreign_keys": [
            {
                "constraint_name": f"{child}_thing_id_fkey",
                "child_table": child,
                "parent_table": parent,
                "delete_rule": "a",
                "child_columns": ["thing_id"],
                "parent_columns": ["id"],
            }
            for child, parent in (("left_things", "projects"), ("right_things", "left_things"))
        ],
        "primary_keys": [
            {"table_name": table, "columns": ["id"]}
            for table in ("projects", "left_things", "right_things", "stowaway")
        ],
        "columns": [
            {"table_name": "projects", "column_name": "id"},
            {"table_name": "left_things", "column_name": "thing_id"},
            {"table_name": "right_things", "column_name": "thing_id"},
            {"table_name": "stowaway", "column_name": "id"},
            {"table_name": "stowaway", "column_name": "thing_id"},
        ],
    }
    with pytest.raises(TeardownError) as excinfo:
        _plan(json.dumps(payload))

    assert "stowaway.thing_id" in str(excinfo.value)
    assert "more than one parent table" in str(excinfo.value)


# ── teardown of the run the evidence describes ───────────────────────────


def test_grant_path_project_tears_down_completely(monkeypatch):
    database = _grant_path_database()
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    pipeline_helpers._cleanup_db(PROJECT_ID)

    deleted = database.deleted_tables
    assert deleted.index("users_grant_intents") < deleted.index("runs")
    assert deleted[-1] == "projects"
    # A refused statement ends the batch instead of running the rest of the
    # transaction against a failure.
    assert "ON_ERROR_STOP=1" in database.argv[0]
    # The batch goes in on stdin: the residue pass names every key the run
    # owned, and one argv element is capped at 128 KiB.
    assert database.argv[0][-2:] == ["-f", "-"]
    assert not any("DELETE FROM" in argument for argument in database.argv[0])


def test_a_deliberately_left_row_is_reported_by_name(monkeypatch):
    """The teardown that leaves a row fails, and says which row.

    Before this, the database half of the residue proof was the delete list
    itself: a table it did not mention was residue nobody counted.
    """
    database = _grant_path_database(residue=[("users_grant_intents", GRANT_INTENT_ID)])
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    with pytest.raises(TeardownError) as excinfo:
        pipeline_helpers._cleanup_db(PROJECT_ID)

    message = str(excinfo.value)
    assert "users_grant_intents" in message
    assert GRANT_INTENT_ID in message
    assert "users_grant_intents_execution_run_id_fkey" in message


def test_a_surviving_row_of_a_composite_keyed_table_is_named_by_both_columns(monkeypatch):
    database = FakeDatabase(
        owned={"projects": [PROJECT_ID], "analytics_known_users": [f"{PROJECT_ID}|abc123"]},
        residue=[("analytics_known_users", f"{PROJECT_ID}|abc123")],
    )
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    with pytest.raises(TeardownError) as excinfo:
        pipeline_helpers._cleanup_db(PROJECT_ID)

    assert "analytics_known_users: project_id|user_id_hash=" in str(excinfo.value)


def test_a_refused_delete_names_the_table_and_the_constraint(monkeypatch):
    database = _grant_path_database(
        delete_result=SqlResult(returncode=3, stdout="", stderr=REFUSAL_STDERR)
    )
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    with pytest.raises(TeardownError) as excinfo:
        pipeline_helpers._cleanup_db(PROJECT_ID)

    message = str(excinfo.value)
    assert "users_grant_intents_execution_run_id_fkey" in message
    assert "users_grant_intents.execution_run_id → runs.id" in message
    assert GRANT_RUN_ID in message


def test_a_refused_delete_from_outside_the_plan_says_the_catalog_is_stale():
    catalog, plan = _plan()
    stderr = (
        'ERROR:  update or delete on table "runs" violates foreign key constraint '
        '"stowaway_run_id_fkey" on table "stowaway"\n'
    )

    described = db_teardown.describe_failure(stderr, plan, catalog)

    assert "stowaway is not in the teardown plan" in described
    assert "the catalog it was built from is stale" in described


def test_a_teardown_that_cannot_read_the_catalog_says_so(monkeypatch):
    database = FakeDatabase(catalog_payload="")
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    with pytest.raises(TeardownError, match="foreign-key catalog"):
        pipeline_helpers._cleanup_db(PROJECT_ID)


def test_residue_is_asked_for_by_the_keys_the_run_owned(monkeypatch):
    """The proof survives the parent row being gone.

    Asking "does anything still reference this project" through the project row
    answers nothing once that row is deleted, so the keys are captured before
    the deletes and asked for again afterwards.
    """
    database = _grant_path_database()
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    pipeline_helpers._cleanup_db(PROJECT_ID)

    residue_query = database.queries[-1]
    assert f"'{GRANT_INTENT_ID}'" in residue_query
    assert f"'{GRANT_RUN_ID}'" in residue_query
    assert database.queries.index(residue_query) > database.queries.index(database.delete_sql)


# ── the user a run registers for itself ──────────────────────────────────


def _run_user_database(**kwargs) -> FakeDatabase:
    """A run that walked the registration door and then did two noop attempts."""
    owned = {
        "projects": [PROJECT_ID],
        "users": [RUN_USER_ROW],
        "engineering_attempt_ledger": [LEDGER_ROW],
        "engineering_budget_policies": [RUN_USER_ROW],
        "promo_codes": ["71"],
        "work_admission_audits": ["audit-1"],
    }
    kwargs.setdefault(
        "residue", [("users", RUN_USER_ROW), ("engineering_attempt_ledger", LEDGER_ROW)]
    )
    return FakeDatabase(owned=owned, **kwargs)


def test_a_run_owned_user_brings_the_rows_that_hang_off_it_into_the_closure():
    """The tables card 1311's reviewer named, in the plan because the root is.

    None of them hangs off the project, so a project-rooted plan cannot see
    them: the budget policy, the reservations, the code the registration
    redeemed and the admission audits belong to the *user*. Extending the roots
    is what reaches them; nothing about the derivation changed.
    """
    _, plan = _plan(user_predicate=f"telegram_id = {RUN_TELEGRAM_ID}")
    tables = [step.table for step in plan]

    assert "engineering_budget_policies" in tables
    assert "engineering_budget_reservations" in tables
    assert "promo_codes" in tables
    assert "work_admission_audits" in tables
    # `work_admission_audits.user_id` carries no foreign key at all, so it is in
    # the plan by the same derivation that covers `service_deployments`.
    audits = next(step for step in plan if step.table == "work_admission_audits")
    assert "no foreign key" in audits.belongs_because()
    assert tables[-1] == "users"


def test_the_user_root_does_not_widen_the_project_it_tears_down():
    """`projects.owner_id` reaches the project root from the user one.

    That edge orders the two roots — a project goes before its owner — and it
    must not select anything: a run deletes the project it named, never every
    project its owner happens to have.
    """
    _, plan = _plan(user_predicate=f"telegram_id = {RUN_TELEGRAM_ID}")
    projects = next(step for step in plan if step.table == "projects")

    assert projects.predicate == f"id::text = '{PROJECT_ID}'"
    assert projects.is_root


def test_the_ledger_and_the_user_are_declared_retained_not_deleted():
    """Neither delete is issued, because the plan already knows it is refused.

    The trigger stays and the foreign key stays: the plan states the rule
    instead of writing a statement it expects to fail and ignoring the error.
    """
    _, plan = _plan(user_predicate=f"telegram_id = {RUN_TELEGRAM_ID}")
    retained = {step.table for step in plan if step.retained}
    sql = db_teardown.delete_sql(plan)

    assert retained == {"users", "engineering_attempt_ledger"}
    assert "DELETE FROM users " not in sql
    assert "DELETE FROM engineering_attempt_ledger " not in sql
    assert "DELETE FROM engineering_budget_policies WHERE" in sql
    assert "DELETE FROM promo_codes WHERE" in sql


def test_the_retained_rows_are_reported_by_table_key_and_count(monkeypatch):
    database = _run_user_database()
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    retention = pipeline_helpers._cleanup_db(PROJECT_ID, RUN_TELEGRAM_ID).retention_report

    assert f"users: 1 row(s), id={RUN_USER_ROW}" in retention
    assert f"engineering_attempt_ledger: 1 row(s), id={LEDGER_ROW}" in retention
    assert "append-only" in retention
    # The retained tables are asked for by predicate, not by key: a row that
    # appeared since the inventory has to be catchable.
    proof_query = database.queries[-1]
    assert f"FROM users WHERE telegram_id = {RUN_TELEGRAM_ID}" in proof_query


def test_a_retained_set_that_is_not_the_declared_one_fails_the_teardown(monkeypatch):
    """A second user row under this run's predicate is not something to report.

    The run registered one user. Two answering means teardown addressed rows it
    did not create, and a teardown that cannot say which rows are its own has
    not proven anything about residue.
    """
    database = _run_user_database(
        residue=[
            ("users", RUN_USER_ROW),
            ("users", "9999"),
            ("engineering_attempt_ledger", LEDGER_ROW),
        ]
    )
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    with pytest.raises(TeardownError) as excinfo:
        pipeline_helpers._cleanup_db(PROJECT_ID, RUN_TELEGRAM_ID)

    message = str(excinfo.value)
    assert "not the rows its plan declared" in message
    assert "users: 2 row(s)" in message


def test_a_row_hanging_off_the_run_user_is_named_when_it_survives(monkeypatch):
    """The residue proof covers the user's rows exactly as it covers the project's."""
    database = _run_user_database(
        residue=[
            ("users", RUN_USER_ROW),
            ("engineering_attempt_ledger", LEDGER_ROW),
            ("promo_codes", "71"),
        ]
    )
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    with pytest.raises(TeardownError) as excinfo:
        pipeline_helpers._cleanup_db(PROJECT_ID, RUN_TELEGRAM_ID)

    message = str(excinfo.value)
    assert "promo_codes: id=71" in message
    assert "promo_codes.redeemed_by_user_id → users.id" in message


def test_a_run_without_its_own_user_tears_down_exactly_as_before(monkeypatch):
    """The suites that share the fixture user are untouched by all of this.

    No user root means no user closure: the fixture user, the rows that hang off
    it and the append-only ledger are all outside the plan, which is the regime
    those runs are still in.
    """
    database = _grant_path_database()
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", database.subprocess_run)

    assert pipeline_helpers._cleanup_db(PROJECT_ID).retention_report == ""
    assert "users" not in database.deleted_tables
    assert "FROM users WHERE" not in database.queries[-1]


# ── the stand sweep deletes through the same derivation ──────────────────

SWEEP_RUN_ID = "deploy-grant-71672573103249b09aaf4cb3dbdf2a54"


def _sweep_module():
    """The sweep, imported the way the stand runs it (`python -m`)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "clean_live_tests.py"
    spec = importlib.util.spec_from_file_location("clean_live_tests_for_teardown", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sweep_plan(module):
    """The plan the sweep's own roots derive, read back through `db_teardown`.

    The predicates come from the sweep, not from this test: the point is the
    order the derivation puts its two roots in, not a second copy of them.
    """
    catalog = db_teardown.parse_catalog(metadata_catalog_payload())
    roots = [db_teardown.Root(table="projects", predicate=module._build_conditions())]
    roots.append(db_teardown.Root(table="users", predicate=run_user_sweep_predicate()))
    return catalog, db_teardown.build_plan(catalog, roots=roots)


def _sweep_against(database: FakeDatabase, monkeypatch, *, contour: str = "stand") -> object:
    """The sweep, running in a contour. The stand unless a test says otherwise.

    The contour is not decoration here: it decides both which titles the sweep
    names and whether it takes a `users` root at all, and the sweep reads it
    once at import — so it is selected the way the shell selects it, before the
    module is loaded.
    """
    monkeypatch.setenv(live_contour.CONTOUR_ENV, contour)
    module = _sweep_module()
    monkeypatch.setattr(module, "run_cmd", database.subprocess_run)
    return module


def test_the_sweep_deletes_through_the_derived_plan(monkeypatch):
    """Not a second enumeration: the same closure, from its own selection.

    The sweep selects a set of projects by title prefix rather than one project
    id, and that predicate is all it brings. The tables, their order and the
    keys read back afterwards come from the catalog, exactly as they do for one
    run's teardown.
    """
    database = FakeDatabase(owned={"projects": ["project-1"]})
    module = _sweep_against(database, monkeypatch)

    module.clean_database()

    sql = database.delete_sql
    deleted = database.deleted_tables
    # Everything that hangs off a project goes before it. `projects` is no
    # longer the last statement of all, because the sweep has a second root: the
    # rows that hang off a run-owned user rather than off its project.
    assert "title LIKE" in sql
    # The catalog's order, not a person's: children before the parents they hang off.
    assert deleted.index("port_allocations") < deleted.index("applications")
    assert deleted.index("applications") < deleted.index("repositories")
    assert deleted.index("repositories") < deleted.index("projects")
    # Read on stdin, with a refused statement ending the batch.
    assert "ON_ERROR_STOP=1" in database.argv[0]
    assert database.argv[0][-2:] == ["-f", "-"]


def test_a_grant_intent_no_longer_refuses_the_sweep(monkeypatch):
    """The exact row that stranded run 35451082771.

    `users_grant_intents` references `runs.id` and was in no hand-written list,
    so the sweep's project delete was refused with
    `Key (id)=(deploy-grant-…) is still referenced`. The derived plan empties it
    before the runs it points at, and proves afterwards that it is gone.
    """
    database = FakeDatabase(
        owned={
            "projects": ["project-1"],
            "runs": [SWEEP_RUN_ID],
            "users_grant_intents": ["grant-intent-71672573"],
        }
    )
    module = _sweep_against(database, monkeypatch)

    module.clean_database()

    deleted = database.deleted_tables
    assert deleted.index("users_grant_intents") < deleted.index("runs")
    residue_query = database.queries[-2]
    assert SWEEP_RUN_ID in residue_query


def test_the_sweep_owns_run_registered_users_through_the_same_plan(monkeypatch):
    """The sweep's second root: the users this harness registered itself.

    A run registers before it creates a project, so a run that died in between
    owns a user, a code and a policy and no project at all — nothing the title
    prefixes can find. What makes that root addressable is the username the
    harness writes, the way a title prefix is written by the harness; the id
    band narrows it but grants no ownership on its own.
    """
    database = FakeDatabase(
        owned={"projects": ["project-1"], "users": [RUN_USER_ROW], "promo_codes": ["71"]},
        residue=[("users", RUN_USER_ROW)],
    )
    module = _sweep_against(database, monkeypatch)

    module.clean_database()

    sql = database.delete_sql
    assert "DELETE FROM promo_codes WHERE redeemed_by_user_id IN (SELECT id FROM users" in sql
    assert "telegram_id BETWEEN 970000000 AND 970999999" in sql
    assert "username LIKE 'live_run_%'" in sql
    assert "DELETE FROM users " not in sql
    # The claim the project-only sweep used to make as `deleted[-1] == "projects"`,
    # in the form the second root leaves true: the plan still ends at a root, and
    # the root it ends at is the retained one, after everything that hangs off it.
    _, sweep_plan = _sweep_plan(module)
    assert [step.table for step in sweep_plan][-1] == "users"
    assert "users" not in database.deleted_tables


def test_a_users_row_the_harness_did_not_name_is_not_swept(monkeypatch):
    """A real account inside the id band is not this harness's residue.

    Telegram issues account ids; the harness only picks a band to register in,
    so the band alone selects strangers too. The username it writes is the
    predicate that is genuinely its own, and both halves are required — a row
    that matches only the band is left where it is.
    """
    database = FakeDatabase(owned={"projects": ["project-1"]})
    module = _sweep_against(database, monkeypatch)

    module.clean_database()

    for statement in database.delete_sql.split("\n"):
        if "FROM users" in statement:
            assert "username LIKE 'live_run_%'" in statement


def test_the_production_sweep_takes_no_user_root_at_all(monkeypatch):
    """Production is swept exactly as it was before the registration door.

    `make test-live-clean` runs this sweep with `LIVE_CONTOUR` unset, which is
    the prod contour — the one that "holds real users' data" and creates no
    live runs. There is no run-owned user there to collect, so the sweep names
    `users` only in the one statement it always did: the fixture id.
    """
    database = FakeDatabase(owned={"projects": ["project-1"]})
    module = _sweep_against(database, monkeypatch, contour="prod")

    module.clean_database()

    assert not module.CONTOUR.allows_live_runs
    assert "SELECT id FROM users WHERE" not in database.delete_sql
    assert "DELETE FROM users WHERE" not in database.delete_sql
    assert "telegram_id BETWEEN" not in database.delete_sql
    assert "DELETE FROM users WHERE telegram_id = 999000001" in database.queries[-1]


def test_the_sweep_still_removes_the_reusable_fixture_user_after_the_projects(monkeypatch):
    """The shared fixture user is not run-owned, so it is not a root.

    It is reused by every run of the other suites rather than being one run's
    residue, so it stays its own statement, issued after the derived plan and
    only while the append-only attempt ledger points at nothing.
    """
    database = FakeDatabase(owned={"projects": ["project-1"]})
    module = _sweep_against(database, monkeypatch)

    module.clean_database()

    user_delete = database.queries[-1]
    assert "DELETE FROM users WHERE telegram_id = 999000001" in user_delete
    assert "NOT EXISTS (SELECT 1 FROM engineering_attempt_ledger" in user_delete
    assert database.queries.index(database.delete_sql) < database.queries.index(user_delete)


def test_a_sweep_that_is_refused_says_which_constraint_refused_it(monkeypatch):
    database = FakeDatabase(
        owned={"projects": ["project-1"], "runs": [SWEEP_RUN_ID]},
        delete_result=SqlResult(returncode=3, stdout="", stderr=REFUSAL_STDERR),
    )
    module = _sweep_against(database, monkeypatch)

    with pytest.raises(module.CleanupFailure) as excinfo:
        module.clean_database()

    assert "users_grant_intents_execution_run_id_fkey" in str(excinfo.value)
