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
import pipeline_helpers
import pytest

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


def _plan(payload: str | None = None):
    catalog = db_teardown.parse_catalog(payload or metadata_catalog_payload())
    plan = db_teardown.build_plan(
        catalog,
        root_table="projects",
        root_predicate=f"id::text = '{PROJECT_ID}'",
    )
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
