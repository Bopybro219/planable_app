"""add place images table

Revision ID: 9c0a7f1d2e3b
Revises: 7f9c2e1a4b6d
Create Date: 2026-04-28 12:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9c0a7f1d2e3b"
down_revision = "7f9c2e1a4b6d"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("place_image"):
        op.create_table(
            "place_image",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("place_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("original_filename", sa.String(length=255), nullable=True),
            sa.Column("caption", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("is_approved", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.ForeignKeyConstraint(["place_id"], ["place.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("filename"),
        )
        op.create_index("ix_place_image_place_id", "place_image", ["place_id"], unique=False)
        op.create_index("ix_place_image_user_id", "place_image", ["user_id"], unique=False)
        op.create_index("ix_place_image_created_at", "place_image", ["created_at"], unique=False)
        op.create_index("ix_place_image_is_approved", "place_image", ["is_approved"], unique=False)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("place_image"):
        op.drop_index("ix_place_image_is_approved", table_name="place_image")
        op.drop_index("ix_place_image_created_at", table_name="place_image")
        op.drop_index("ix_place_image_user_id", table_name="place_image")
        op.drop_index("ix_place_image_place_id", table_name="place_image")
        op.drop_table("place_image")
