"""Image libraries: an images table, one library per game

Revision ID: 005
Revises: 004
Create Date: 2026-09-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer, nullable=False),
        sa.Column("content_type", sa.String(32), nullable=False),
        # MEDIUMBLOB (up to 16 MiB): the generic binary type would be a 64 KB BLOB.
        sa.Column("data", mysql.MEDIUMBLOB(), nullable=False),
        sa.Column("small_data", mysql.MEDIUMBLOB(), nullable=False),
        sa.Column("width", sa.Integer, nullable=False),
        sa.Column("height", sa.Integer, nullable=False),
        sa.Column("byte_size", sa.Integer, nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("replaced_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
    )
    # Index before the FK so MySQL reuses it instead of adding its own.
    op.create_index("ix_images_game_id", "images", ["game_id"])
    # ON DELETE CASCADE: deleting a game removes its images without loading them.
    op.create_foreign_key(
        "fk_images_game_id",
        "images",
        "games",
        ["game_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # The FK must go before the index it depends on.
    op.drop_constraint("fk_images_game_id", "images", type_="foreignkey")
    op.drop_index("ix_images_game_id", table_name="images")
    op.drop_table("images")
