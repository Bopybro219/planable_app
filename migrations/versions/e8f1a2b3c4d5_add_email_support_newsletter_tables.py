"""add email support and newsletter tables

Revision ID: e8f1a2b3c4d5
Revises: f17f67548625
Create Date: 2026-04-29 16:30:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "e8f1a2b3c4d5"
down_revision = "f17f67548625"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "contact_message" not in existing_tables:
        op.create_table(
            "contact_message",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("subject", sa.String(length=255), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="new"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("handled_by_user_id", sa.Integer(), nullable=True),
            sa.Column("handled_at", sa.DateTime(), nullable=True),
            sa.Column("reply_sent_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["handled_by_user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_contact_message_email", "contact_message", ["email"], unique=False)
        op.create_index("ix_contact_message_status", "contact_message", ["status"], unique=False)
        op.create_index("ix_contact_message_created_at", "contact_message", ["created_at"], unique=False)
        op.create_index("ix_contact_message_handled_by_user_id", "contact_message", ["handled_by_user_id"], unique=False)

    if "newsletter_subscriber" not in existing_tables:
        op.create_table(
            "newsletter_subscriber",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("subscribed_at", sa.DateTime(), nullable=True),
            sa.Column("unsubscribed_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="subscribed"),
            sa.Column("source", sa.String(length=120), nullable=True),
            sa.Column("consent_text", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("email"),
        )
        op.create_index("ix_newsletter_subscriber_email", "newsletter_subscriber", ["email"], unique=True)
        op.create_index("ix_newsletter_subscriber_status", "newsletter_subscriber", ["status"], unique=False)
        op.create_index("ix_newsletter_subscriber_created_at", "newsletter_subscriber", ["created_at"], unique=False)

    if "email_event" not in existing_tables:
        op.create_table(
            "email_event",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("event_key", sa.String(length=255), nullable=False),
            sa.Column("category", sa.String(length=80), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("related_type", sa.String(length=80), nullable=True),
            sa.Column("related_id", sa.String(length=120), nullable=True),
            sa.Column("recipient_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_key"),
        )
        op.create_index("ix_email_event_event_key", "email_event", ["event_key"], unique=True)
        op.create_index("ix_email_event_category", "email_event", ["category"], unique=False)
        op.create_index("ix_email_event_user_id", "email_event", ["user_id"], unique=False)
        op.create_index("ix_email_event_created_at", "email_event", ["created_at"], unique=False)

    if "newsletter_draft" not in existing_tables:
        op.create_table(
            "newsletter_draft",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("subject", sa.String(length=255), nullable=False),
            sa.Column("body_text", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_newsletter_draft_status", "newsletter_draft", ["status"], unique=False)
        op.create_index("ix_newsletter_draft_created_by_user_id", "newsletter_draft", ["created_by_user_id"], unique=False)
        op.create_index("ix_newsletter_draft_created_at", "newsletter_draft", ["created_at"], unique=False)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "newsletter_draft" in existing_tables:
        op.drop_index("ix_newsletter_draft_created_at", table_name="newsletter_draft")
        op.drop_index("ix_newsletter_draft_created_by_user_id", table_name="newsletter_draft")
        op.drop_index("ix_newsletter_draft_status", table_name="newsletter_draft")
        op.drop_table("newsletter_draft")

    if "email_event" in existing_tables:
        op.drop_index("ix_email_event_created_at", table_name="email_event")
        op.drop_index("ix_email_event_user_id", table_name="email_event")
        op.drop_index("ix_email_event_category", table_name="email_event")
        op.drop_index("ix_email_event_event_key", table_name="email_event")
        op.drop_table("email_event")

    if "newsletter_subscriber" in existing_tables:
        op.drop_index("ix_newsletter_subscriber_created_at", table_name="newsletter_subscriber")
        op.drop_index("ix_newsletter_subscriber_status", table_name="newsletter_subscriber")
        op.drop_index("ix_newsletter_subscriber_email", table_name="newsletter_subscriber")
        op.drop_table("newsletter_subscriber")

    if "contact_message" in existing_tables:
        op.drop_index("ix_contact_message_handled_by_user_id", table_name="contact_message")
        op.drop_index("ix_contact_message_created_at", table_name="contact_message")
        op.drop_index("ix_contact_message_status", table_name="contact_message")
        op.drop_index("ix_contact_message_email", table_name="contact_message")
        op.drop_table("contact_message")
