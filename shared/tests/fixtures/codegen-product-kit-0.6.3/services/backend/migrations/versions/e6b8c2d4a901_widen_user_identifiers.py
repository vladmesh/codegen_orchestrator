"""Widen persisted user identifiers without changing identity or admission semantics.

Revision ID: e6b8c2d4a901
Revises: d4a7b2c9e1f0
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "e6b8c2d4a901"
down_revision = "d4a7b2c9e1f0"
branch_labels = None
depends_on = None

_USER_COLUMNS = (("users", "id"), ("user_channels", "user_id"), ("settings", "subject_id"))


def upgrade() -> None:
    for table, column in _USER_COLUMNS:
        op.alter_column(
            table, column, existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=False
        )
    # ALTER COLUMN keeps the serial default/ownership but does not widen the sequence itself.
    op.execute("ALTER SEQUENCE users_id_seq AS BIGINT")


def downgrade() -> None:
    # Keep writes out between the overflow check and narrowing. PostgreSQL DDL and
    # Alembic's version update share a transaction, so every refusal leaves head intact.
    op.execute("LOCK TABLE users, user_channels, settings IN ACCESS EXCLUSIVE MODE")
    connection = op.get_bind()
    for table, column in _USER_COLUMNS:
        high = connection.scalar(
            sa.text(
                f"SELECT EXISTS (SELECT 1 FROM {table} "
                f"WHERE {column} < -2147483648 OR {column} > 2147483647)"
            )
        )
        if high:
            raise RuntimeError(f"Cannot downgrade {table}.{column}: values exceed int32")
    sequence_value = connection.scalar(sa.text("SELECT last_value FROM users_id_seq"))
    if sequence_value is not None and sequence_value > 2147483647:
        raise RuntimeError("Cannot downgrade users_id_seq: sequence exceeds int32")
    op.execute("ALTER SEQUENCE users_id_seq AS INTEGER")
    for table, column in reversed(_USER_COLUMNS):
        op.alter_column(
            table, column, existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=False
        )
