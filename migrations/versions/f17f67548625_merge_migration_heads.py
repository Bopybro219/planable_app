"""Merge migration heads

Revision ID: f17f67548625
Revises: b1a2c3d4e5f6, c4d5e6f7a8b9
Create Date: 2026-04-29 12:20:51.457074

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f17f67548625'
down_revision = ('b1a2c3d4e5f6', 'c4d5e6f7a8b9')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
