"""add user profile image filename

Revision ID: 7f9c2e1a4b6d
Revises: 2f3a1b7c9d4e
Create Date: 2026-04-28 12:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "7f9c2e1a4b6d"
down_revision = "2f3a1b7c9d4e"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("user")}

    with op.batch_alter_table("user", schema=None) as batch_op:
        if "profile_image_filename" not in existing_columns:
            batch_op.add_column(sa.Column("profile_image_filename", sa.String(length=255), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("user")}

    with op.batch_alter_table("user", schema=None) as batch_op:
        if "profile_image_filename" in existing_columns:
            batch_op.drop_column("profile_image_filename")
