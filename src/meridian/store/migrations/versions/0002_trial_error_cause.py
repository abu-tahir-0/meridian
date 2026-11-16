"""Keep why a trial was a harness error, not only that it was one.

The result file already carries `error_cause`; the trial row did not, so stored
history could not be broken down by cause.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: migration 0001 applies the current schema.sql, which already
    # carries the column on a fresh database. On an existing one, this adds it.
    op.execute("ALTER TABLE trial ADD COLUMN IF NOT EXISTS error_cause TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE trial DROP COLUMN IF EXISTS error_cause")
