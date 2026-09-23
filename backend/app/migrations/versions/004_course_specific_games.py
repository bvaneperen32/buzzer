"""Attach games to a course and let rooms refuse guests

Revision ID: 004
Revises: 003
Create Date: 2026-09-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable with no backfill: existing games stay unassigned (NULL). There is no
    # correct course for a game played in several courses, or never played.
    op.add_column("games", sa.Column("course_id", sa.Integer(), nullable=True))
    # Index before the FK so MySQL reuses it instead of adding its own.
    op.create_index("ix_games_course_id", "games", ["course_id"])
    op.create_foreign_key(
        "fk_games_course_id", "games", "courses", ["course_id"], ["id"]
    )
    op.add_column(
        "game_sessions",
        sa.Column(
            "allow_guests", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    op.drop_column("game_sessions", "allow_guests")
    # The FK must go before the index it depends on.
    op.drop_constraint("fk_games_course_id", "games", type_="foreignkey")
    op.drop_index("ix_games_course_id", table_name="games")
    op.drop_column("games", "course_id")
