"""Store capability previews and the technical plan beside a Product Brief revision.

Forward only in effect: both new brief columns are nullable and default to absent, so
every stored revision keeps its meaning, and a brief that relies on no previewed
capability never has a plan.
"""

from alembic import op
import sqlalchemy as sa

revision = "a7c3e9f1b5d8"
down_revision = "f1a3c5e7b9d2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "capability_previews",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("requests", sa.JSON(), nullable=False),
        sa.Column("product", sa.JSON(), nullable=False),
        sa.Column("technical", sa.JSON(), nullable=False),
    )
    op.create_index("ix_capability_previews_project_id", "capability_previews", ["project_id"])
    op.add_column(
        "product_briefs",
        sa.Column(
            "capability_preview_id",
            sa.String(length=64),
            sa.ForeignKey("capability_previews.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_product_briefs_capability_preview_id", "product_briefs", ["capability_preview_id"]
    )
    op.add_column("product_briefs", sa.Column("capability_plan", sa.JSON(), nullable=True))
    op.create_check_constraint(
        "ck_product_briefs_capability_plan",
        "product_briefs",
        "(capability_preview_id IS NULL) = (capability_plan IS NULL)",
    )


def downgrade():
    op.drop_constraint("ck_product_briefs_capability_plan", "product_briefs", type_="check")
    op.drop_column("product_briefs", "capability_plan")
    op.drop_index("ix_product_briefs_capability_preview_id", table_name="product_briefs")
    op.drop_column("product_briefs", "capability_preview_id")
    op.drop_index("ix_capability_previews_project_id", table_name="capability_previews")
    op.drop_table("capability_previews")
