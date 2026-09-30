"""Allow QA executor spend in the canonical attempt ledger.

Revision ID: 6f2a9c4e8b1d
Revises: 5e9f2b3c4d6a
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6f2a9c4e8b1d"
down_revision: str | None = "5e9f2b3c4d6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_engineering_attempt_role", "engineering_attempt_ledger", type_="check")
    op.create_check_constraint(
        "ck_engineering_attempt_role",
        "engineering_attempt_ledger",
        "role IN ('engineering', 'qa')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_engineering_attempt_role", "engineering_attempt_ledger", type_="check")
    op.create_check_constraint(
        "ck_engineering_attempt_role", "engineering_attempt_ledger", "role = 'engineering'"
    )
