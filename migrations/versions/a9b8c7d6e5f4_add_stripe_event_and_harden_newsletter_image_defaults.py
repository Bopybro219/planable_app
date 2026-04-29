"""add stripe event table and harden newsletter/image defaults

Revision ID: a9b8c7d6e5f4
Revises: e8f1a2b3c4d5
Create Date: 2026-04-29 20:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a9b8c7d6e5f4"
down_revision = "e8f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "stripe_event" not in existing_tables:
        op.create_table(
            "stripe_event",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("stripe_event_id", sa.String(length=255), nullable=False),
            sa.Column("event_type", sa.String(length=120), nullable=False),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("processing_error", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("stripe_event_id"),
        )
        op.create_index("ix_stripe_event_created_at", "stripe_event", ["created_at"], unique=False)
        op.create_index("ix_stripe_event_event_type", "stripe_event", ["event_type"], unique=False)
        op.create_index("ix_stripe_event_processed_at", "stripe_event", ["processed_at"], unique=False)
        op.create_index("ix_stripe_event_stripe_event_id", "stripe_event", ["stripe_event_id"], unique=True)

    if "newsletter_subscriber" in existing_tables:
        op.execute(
            sa.text(
                "UPDATE newsletter_subscriber "
                "SET status = 'pending' "
                "WHERE status IS NULL OR status NOT IN ('pending', 'subscribed', 'unsubscribed')"
            )
        )
        with op.batch_alter_table("newsletter_subscriber", schema=None) as batch_op:
            batch_op.alter_column("status", existing_type=sa.String(length=30), server_default="pending")

    if "place_image" in existing_tables:
        op.execute(sa.text("UPDATE place_image SET is_approved = false WHERE is_approved IS NULL"))
        with op.batch_alter_table("place_image", schema=None) as batch_op:
            batch_op.alter_column("is_approved", existing_type=sa.Boolean(), server_default=sa.false())


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "place_image" in existing_tables:
        with op.batch_alter_table("place_image", schema=None) as batch_op:
            batch_op.alter_column("is_approved", existing_type=sa.Boolean(), server_default=sa.true())

    if "newsletter_subscriber" in existing_tables:
        with op.batch_alter_table("newsletter_subscriber", schema=None) as batch_op:
            batch_op.alter_column("status", existing_type=sa.String(length=30), server_default="subscribed")

    if "stripe_event" in existing_tables:
        op.drop_index("ix_stripe_event_stripe_event_id", table_name="stripe_event")
        op.drop_index("ix_stripe_event_processed_at", table_name="stripe_event")
        op.drop_index("ix_stripe_event_event_type", table_name="stripe_event")
        op.drop_index("ix_stripe_event_created_at", table_name="stripe_event")
        op.drop_table("stripe_event")
