"""create_settings

Revision ID: 4d8c1f9a2b7e
Revises: 118f8b3895d8
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "4d8c1f9a2b7e"
down_revision = "118f8b3895d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.Enum("product", "user", name="setting_scope"), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(scope = 'product' AND subject_id = 0) OR (scope = 'user' AND subject_id > 0)",
            name="ck_settings_scope_subject",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", "scope", "subject_id", name="uq_settings_key_scope_subject"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.execute("DROP TYPE IF EXISTS setting_scope")
