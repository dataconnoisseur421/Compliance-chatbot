"""add documents.search_scope

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(
            sa.Column("search_scope", sa.String(length=10), nullable=False, server_default="core")
        )
        batch_op.create_check_constraint(
            op.f("ck_documents_search_scope_valid"), "search_scope IN ('core', 'extended')"
        )


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_constraint(op.f("ck_documents_search_scope_valid"), type_="check")
        batch_op.drop_column("search_scope")
