"""add manual entitlement fields to user

Revision ID: c4d5e6f7a8b9
Revises: 9c0a7f1d2e3b
Create Date: 2026-04-28 13:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c4d5e6f7a8b9"
down_revision = "9c0a7f1d2e3b"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("user")}

    with op.batch_alter_table("user", schema=None) as batch_op:
        if "manual_entitlement_enabled" not in existing_columns:
            batch_op.add_column(sa.Column("manual_entitlement_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "manual_entitlement_plan" not in existing_columns:
            batch_op.add_column(sa.Column("manual_entitlement_plan", sa.String(length=50), nullable=True))
        if "access_override_until" not in existing_columns:
            batch_op.add_column(sa.Column("access_override_until", sa.DateTime(), nullable=True))
        if "manual_entitlement_note" not in existing_columns:
            batch_op.add_column(sa.Column("manual_entitlement_note", sa.String(length=255), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("user")}

    with op.batch_alter_table("user", schema=None) as batch_op:
        if "manual_entitlement_note" in existing_columns:
            batch_op.drop_column("manual_entitlement_note")
        if "access_override_until" in existing_columns:
            batch_op.drop_column("access_override_until")
        if "manual_entitlement_plan" in existing_columns:
            batch_op.drop_column("manual_entitlement_plan")
        if "manual_entitlement_enabled" in existing_columns:
            batch_op.drop_column("manual_entitlement_enabled")
