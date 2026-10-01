"""Retire unused access observations and enforce the persisted Run vocabulary.

Revision ID: b2d4f6a8c0e1
Revises: a4c6e8f0b2d5
Create Date: 2026-10-01 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b2d4f6a8c0e1"
down_revision: str | None = "a4c6e8f0b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OBSERVATION_COLUMNS = (
    "observed_at",
    "observation_id",
    "slot_clear_since",
    "slot_clear_readings",
    "reopened_at",
)
TIMESTAMP_TABLES = ("product_briefs", "requirement_coverages")

# Migration history must not change when a future application enum grows.
RUN_CHECKS = (
    ("ck_runs_type_valid", "type IN ('engineering', 'deploy', 'qa')"),
    (
        "ck_runs_status_valid",
        "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
    ),
)


def upgrade() -> None:
    connection = op.get_bind()
    # Take the locks needed by the DDL before reading: an older writer must not
    # populate a retired field between the preservation guard and its DROP.
    connection.execute(
        sa.text(
            "LOCK TABLE temporary_access_grants, runs, product_briefs, requirement_coverages "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    observations = (
        connection.execute(
            sa.text(
                "SELECT id FROM temporary_access_grants WHERE observed_at IS NOT NULL "
                "OR observation_id IS NOT NULL OR slot_clear_since IS NOT NULL "
                "OR slot_clear_readings IS DISTINCT FROM 0 OR reopened_at IS NOT NULL "
                "ORDER BY id LIMIT 10"
            )
        )
        .scalars()
        .all()
    )
    invalid_runs = (
        connection.execute(
            sa.text(
                "SELECT id FROM runs WHERE type IS NULL "
                "OR type NOT IN ('engineering', 'deploy', 'qa') OR status IS NULL "
                "OR status NOT IN ('queued', 'running', 'completed', 'failed', 'cancelled') "
                "ORDER BY id LIMIT 10"
            )
        )
        .scalars()
        .all()
    )
    # Both preflights precede every schema or data mutation. Do not discard
    # historical observations or guess the meaning of an unknown Run value.
    problems = []
    if observations:
        problems.append(
            f"retired temporary-access observations on grant ids: {', '.join(observations)}"
        )
    if invalid_runs:
        problems.append(f"invalid Run type/status on run ids: {', '.join(invalid_runs)}")
    if problems:
        raise RuntimeError(
            f"Migration {revision} refuses to change the schema; "
            + "; ".join(problems)
            + ". Showing up to 10 ids per condition; "
            "review and preserve or correct these records first."
        )

    for column in OBSERVATION_COLUMNS:
        op.drop_column("temporary_access_grants", column)
    for table in TIMESTAMP_TABLES:
        # Keep every known date. A missing counterpart inherits the known one;
        # when both are absent, record this transaction's time, not an invented
        # historical creation time. PostgreSQL evaluates both RHSs from the old row.
        timestamps = sa.table(table, sa.column("created_at"), sa.column("updated_at"))
        created_at, updated_at = timestamps.c.created_at, timestamps.c.updated_at
        connection.execute(
            timestamps.update()
            .where(sa.or_(created_at.is_(None), updated_at.is_(None)))
            .values(
                created_at=sa.func.coalesce(created_at, updated_at, sa.func.current_timestamp()),
                updated_at=sa.func.coalesce(updated_at, created_at, sa.func.current_timestamp()),
            )
        )
        for column in ("created_at", "updated_at"):
            op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=False)
    for name, condition in RUN_CHECKS:
        op.create_check_constraint(name, "runs", condition)


def downgrade() -> None:
    for name, _ in reversed(RUN_CHECKS):
        op.drop_constraint(name, "runs", type_="check")
    for table in TIMESTAMP_TABLES:
        for column in ("created_at", "updated_at"):
            op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=True)
    # The upgrade only dropped empty observations, so their original values are
    # recoverable. Backfilled dates remain useful and are never nulled on downgrade.
    for column in OBSERVATION_COLUMNS:
        if column == "slot_clear_readings":
            field = sa.Column(column, sa.Integer(), nullable=False, server_default="0")
        elif column == "observation_id":
            field = sa.Column(column, sa.String(length=255), nullable=True)
        else:
            field = sa.Column(column, sa.DateTime(timezone=True), nullable=True)
        op.add_column("temporary_access_grants", field)
