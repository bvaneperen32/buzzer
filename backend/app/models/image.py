from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CHAR, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base

if TYPE_CHECKING:
    from .game import Game


class Image(Base):
    """One image in a game's library. Questions refer to it by id inside their `config`."""

    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE", name="fk_images_game_id"),
        nullable=False,
        index=True,
    )
    # Of `data`, detected from the bytes; never taken from the client.
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Explicitly MEDIUMBLOB: SQLAlchemy's default binary type becomes a 64 KB BLOB. Deferred,
    # so listing a library never reads image bytes.
    data: Mapped[bytes] = mapped_column(MEDIUMBLOB, nullable=False, deferred=True)
    small_data: Mapped[bytes] = mapped_column(MEDIUMBLOB, nullable=False, deferred=True)
    # Of the full image, after orientation.
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # Display-only original basename. Never sent in socket payloads or image responses.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    # Increases by 1 on every replace; links carry it, so a replace makes new URLs.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Set by the database's NOW(), the same clock as game_sessions.created_at.
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    game: Mapped[Game] = relationship("Game", back_populates="images")
