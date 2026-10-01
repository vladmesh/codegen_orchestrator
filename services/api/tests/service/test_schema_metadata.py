"""The schema created by the migration chain must agree with the ORM."""

from itertools import count
from pprint import pformat
import uuid

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shared.contracts.dto.incident import IncidentStatus, IncidentType
from shared.contracts.dto.run import RunStatus, RunType
from shared.models import Base, Incident

_INSERT_RUN = text(
    "INSERT INTO runs (id, type, status, metadata) VALUES (:id, :type, :status, '{}')"
)


@pytest.mark.asyncio
async def test_migrated_schema_matches_model_metadata(db_session) -> None:
    # The service harness starts the API with `alembic upgrade head` against a
    # fresh PostgreSQL database. Comparing that schema catches changes omitted
    # from either the migration chain or the model, without create_all masking
    # the disagreement.
    def compare(session):
        context = MigrationContext.configure(session.connection(), opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    differences = await db_session.run_sync(compare)
    assert not differences, f"Migration/model schema drift:\n{pformat(differences)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("run_type", list(RunType))
@pytest.mark.parametrize("status", list(RunStatus))
async def test_migrated_run_checks_accept_every_declared_value(db_session, run_type, status):
    # Use the current enums, so extending them without a migration fails at the
    # actual persistence boundary even though CHECK comparison is not autogen's job.
    run_id = f"schema-run-{uuid.uuid4().hex}"
    await db_session.execute(
        _INSERT_RUN, {"id": run_id, "type": run_type.value, "status": status.value}
    )
    result = await db_session.execute(
        text("SELECT type, status FROM runs WHERE id = :id"), {"id": run_id}
    )
    assert result.one() == (run_type.value, status.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column,invalid,constraint",
    [("type", "unknown", "ck_runs_type_valid"), ("status", "done", "ck_runs_status_valid")],
)
async def test_migrated_run_checks_refuse_invalid_inserts_and_updates(
    db_session, column, invalid, constraint
):
    def exercise(session):
        connection = session.connection()
        values = {"id": f"schema-run-{uuid.uuid4().hex}", "type": "engineering", "status": "queued"}
        connection.execute(_INSERT_RUN, values)
        invalid_values = {**values, "id": f"invalid-{uuid.uuid4().hex}", column: invalid}
        with pytest.raises(IntegrityError, match=constraint), connection.begin_nested():
            connection.execute(_INSERT_RUN, invalid_values)
        with pytest.raises(IntegrityError, match=constraint), connection.begin_nested():
            connection.execute(
                text(f"UPDATE runs SET {column} = :value WHERE id = :id"),
                {"value": invalid, "id": values["id"]},
            )
        assert connection.execute(
            text("SELECT type, status FROM runs WHERE id = :id"), {"id": values["id"]}
        ).one() == ("engineering", "queued")

    await db_session.run_sync(exercise)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incident_type", [IncidentType.PROVISIONING_FAILED, IncidentType.TARGET_NOT_READY]
)
@pytest.mark.parametrize("schema_source", ["model", "migrations"])
async def test_incident_schemas_keep_one_active_episode_per_server_and_type(
    db_session, incident_type, schema_source
):
    def exercise(session):
        connection = session.connection()
        transaction = connection.begin_nested()
        try:
            schema = f"incident_metadata_{uuid.uuid4().hex}"
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            connection.execute(text("CREATE TABLE servers (handle varchar(255) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO servers VALUES ('first'), ('second')"))
            if schema_source == "model":
                Incident.__table__.create(connection, checkfirst=False)
            else:
                # Copy the actual migrated indexes, including their predicates;
                # autogenerate does not compare partial-index WHERE clauses.
                connection.execute(
                    text("CREATE TABLE incidents (LIKE public.incidents INCLUDING ALL)")
                )
            # A cloned serial default points at the public sequence. Explicit
            # ids keep all effects inside this test schema and savepoint.
            row_ids = count(1)

            def insert(server, kind, status):
                return connection.execute(
                    Incident.__table__.insert()
                    .values(
                        id=next(row_ids),
                        server_handle=server,
                        incident_type=kind.value,
                        status=status.value,
                    )
                    .returning(Incident.id)
                ).scalar_one()

            first = insert("first", incident_type, IncidentStatus.DETECTED)
            with pytest.raises(IntegrityError), connection.begin_nested():
                insert("first", incident_type, IncidentStatus.RECOVERING)
            # Neither another server nor the other failure family collides.
            other_type = (
                IncidentType.TARGET_NOT_READY
                if incident_type == IncidentType.PROVISIONING_FAILED
                else IncidentType.PROVISIONING_FAILED
            )
            insert("second", incident_type, IncidentStatus.DETECTED)
            insert("first", other_type, IncidentStatus.DETECTED)
            insert("first", incident_type, IncidentStatus.RESOLVED)
            # The uniqueness policy applies only to these two failure families.
            insert("first", IncidentType.SERVICE_DOWN, IncidentStatus.DETECTED)
            insert("first", IncidentType.SERVICE_DOWN, IncidentStatus.DETECTED)
            connection.execute(
                Incident.__table__.update()
                .where(Incident.id == first)
                .values(status=IncidentStatus.RESOLVED.value)
            )
            insert("first", incident_type, IncidentStatus.RECOVERING)
        finally:
            transaction.rollback()

    await db_session.run_sync(exercise)
