"""add nullable stripe lifecycle fields to user

Revision ID: 2f3a1b7c9d4e
Revises: 60890b0d6a4b
Create Date: 2026-04-28 11:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2f3a1b7c9d4e"
down_revision = "60890b0d6a4b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stripe_customer_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("stripe_subscription_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("subscription_status", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("subscription_current_period_end", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("subscription_cancel_at_period_end", sa.Boolean(), nullable=True))
        batch_op.create_index(batch_op.f("ix_user_stripe_customer_id"), ["stripe_customer_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_user_stripe_subscription_id"), ["stripe_subscription_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_user_subscription_status"), ["subscription_status"], unique=False)


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_subscription_status"))
        batch_op.drop_index(batch_op.f("ix_user_stripe_subscription_id"))
        batch_op.drop_index(batch_op.f("ix_user_stripe_customer_id"))
        batch_op.drop_column("subscription_cancel_at_period_end")
        batch_op.drop_column("subscription_current_period_end")
        batch_op.drop_column("subscription_status")
        batch_op.drop_column("stripe_subscription_id")
        batch_op.drop_column("stripe_customer_id")
