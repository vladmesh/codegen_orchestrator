"""Add the canonical engineering attempt ledger.

Revision ID: 8d2c5e6f7a8b
Revises: 7c19ab4de20f
Create Date: 2026-08-24 10:54:32.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "8d2c5e6f7a8b"
down_revision: str | None = "7c19ab4de20f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "engineering_attempt_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=320), nullable=False),
        sa.Column("run_id", sa.String(length=255), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("story_id", sa.String(length=255), nullable=True),
        sa.Column("task_id", sa.String(length=255), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("owner_attribution", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=True),
        sa.Column("cost_source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Hard project deletion retains accounting history.  Parent links are
        # detached by PostgreSQL instead of deleting or rewriting the row.
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.CheckConstraint("role = 'engineering'", name="ck_engineering_attempt_role"),
        sa.CheckConstraint(
            "(cost_source = 'unknown' AND cost_microusd IS NULL) OR "
            "(cost_source = 'provider_reported' AND cost_microusd IS NOT NULL "
            "AND provider IS NOT NULL)",
            name="ck_engineering_attempt_cost_provenance",
        ),
        sa.CheckConstraint(
            "owner_attribution IN ('resolved', 'unknown')",
            name="ck_engineering_attempt_owner_attribution",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("run_id"),
    )
    for column in ("project_id", "story_id", "task_id", "user_id", "occurred_at"):
        op.create_index(
            f"ix_engineering_attempt_ledger_{column}", "engineering_attempt_ledger", [column]
        )

    # Old Float observations cannot prove their source or preserve exact money.
    # They remain unknown, while provider/model/usage facts and project ownership
    # are retained. A missing project has explicitly unknown ownership.
    op.execute(
        """
        INSERT INTO engineering_attempt_ledger (
            id, idempotency_key, run_id, project_id, story_id, task_id, user_id,
            owner_attribution, role, occurred_at, provider, model, input_tokens,
            output_tokens, total_tokens, cost_microusd, cost_source
        )
        SELECT uuid_generate_v4(), 'engineering-run:' || r.id, r.id, r.project_id,
               r.story_id, r.task_id, p.owner_id,
               CASE WHEN p.owner_id IS NULL THEN 'unknown' ELSE 'resolved' END,
               'engineering', COALESCE(r.completed_at, r.started_at, r.created_at),
               r.agent_profile->>'provider', r.agent_profile->>'model', r.input_tokens,
               r.output_tokens, r.total_tokens, NULL, 'unknown'
        FROM runs r
        LEFT JOIN projects p ON p.id = r.project_id
        WHERE r.type = 'engineering'
          AND r.status IN ('completed', 'failed', 'cancelled')
        """
    )
    op.execute(
        """
        CREATE FUNCTION engineering_attempt_ledger_reject_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND NEW.id = OLD.id
               AND NEW.idempotency_key = OLD.idempotency_key
               AND NEW.created_at = OLD.created_at
               AND NEW.updated_at = OLD.updated_at
               AND NEW.user_id IS NOT DISTINCT FROM OLD.user_id
               AND NEW.owner_attribution = OLD.owner_attribution
               AND NEW.role = OLD.role
               AND NEW.occurred_at = OLD.occurred_at
               AND NEW.provider IS NOT DISTINCT FROM OLD.provider
               AND NEW.model IS NOT DISTINCT FROM OLD.model
               AND NEW.input_tokens IS NOT DISTINCT FROM OLD.input_tokens
               AND NEW.output_tokens IS NOT DISTINCT FROM OLD.output_tokens
               AND NEW.total_tokens IS NOT DISTINCT FROM OLD.total_tokens
               AND NEW.cache_read_tokens IS NOT DISTINCT FROM OLD.cache_read_tokens
               AND NEW.cache_write_tokens IS NOT DISTINCT FROM OLD.cache_write_tokens
               AND NEW.cost_microusd IS NOT DISTINCT FROM OLD.cost_microusd
               AND NEW.cost_source = OLD.cost_source
               AND (NEW.run_id IS NOT DISTINCT FROM OLD.run_id OR NEW.run_id IS NULL)
               AND (NEW.project_id IS NOT DISTINCT FROM OLD.project_id OR NEW.project_id IS NULL)
               AND (NEW.story_id IS NOT DISTINCT FROM OLD.story_id OR NEW.story_id IS NULL)
               AND (NEW.task_id IS NOT DISTINCT FROM OLD.task_id OR NEW.task_id IS NULL)
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'engineering_attempt_ledger is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER engineering_attempt_ledger_append_only
        BEFORE UPDATE OR DELETE ON engineering_attempt_ledger
        FOR EACH ROW EXECUTE FUNCTION engineering_attempt_ledger_reject_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("engineering_attempt_ledger")
    op.execute("DROP FUNCTION engineering_attempt_ledger_reject_mutation()")
