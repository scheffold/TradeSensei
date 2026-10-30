"""Add the generic file-import account type.

Revision ID: 9e52a7c13d64
Revises: c4a8d2e9f301
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9e52a7c13d64"
down_revision: str | None = "c4a8d2e9f301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the enum value outside a PostgreSQL transaction block."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE accounttype ADD VALUE IF NOT EXISTS 'generic'")


def downgrade() -> None:
    """Keep the enum value when reverting application support."""
    # PostgreSQL cannot safely remove an enum value that existing rows may use.
    pass
