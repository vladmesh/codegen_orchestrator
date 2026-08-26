"""Add one-time promo codes that arm initial engineering budgets.

Revision ID: f6e7d8c9b0a1
Revises: a6b7c8d9e0f1
Create Date: 2026-08-25 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f6e7d8c9b0a1"
down_revision: str | None = "a6b7c8d9e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=255), nullable=False),
        sa.Column("credits_microusd", sa.BigInteger(), nullable=False),
        sa.Column("attempt_reservation_microusd", sa.BigInteger(), nullable=False),
        sa.Column("redeemed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("credits_microusd >= 0", name="ck_promo_code_credits"),
        sa.CheckConstraint(
            "attempt_reservation_microusd > 0", name="ck_promo_code_attempt_reservation"
        ),
        sa.ForeignKeyConstraint(["redeemed_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("redeemed_by_user_id", name="uq_promo_code_redeemed_by_user"),
    )
    op.create_index(
        "uq_promo_codes_normalized_code", "promo_codes", [sa.text("upper(code)")], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_promo_codes_normalized_code", table_name="promo_codes")
    op.drop_table("promo_codes")
