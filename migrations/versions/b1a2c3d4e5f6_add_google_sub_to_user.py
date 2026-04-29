"""add google_sub to user

Revision ID: b1a2c3d4e5f6
Revises: 7f9c2e1a4b6d
Create Date: 2026-04-29 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b1a2c3d4e5f6"
down_revision = "7f9c2e1a4b6d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user", sa.Column("google_sub", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_user_google_sub"), "user", ["google_sub"], unique=True)


def downgrade():
    op.drop_index(op.f("ix_user_google_sub"), table_name="user")
    op.drop_column("user", "google_sub")
