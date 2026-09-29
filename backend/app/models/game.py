from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base

if TYPE_CHECKING:
    from .image import Image
    from .session import GameSession, SessionScore
    from .user import User, UserGameAccess


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=sa.text("''")
    )
    max_players: Mapped[int] = mapped_column(Integer, default=150, nullable=False)
    # NULL = unassigned: games created before migration 004 have no course.
    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("courses.id", name="fk_games_course_id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # passive_deletes: the database's ON DELETE CASCADE removes the questions. Loading them
    # first (ordered) to delete them one by one fails in MySQL's sort for a large question.
    questions: Mapped[list[Question]] = relationship(
        "Question",
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="Question.order_index",
        passive_deletes=True,
    )
    sessions: Mapped[list[GameSession]] = relationship(
        "GameSession", back_populates="game"
    )
    user_access: Mapped[list[UserGameAccess]] = relationship(
        "UserGameAccess", back_populates="game", cascade="all, delete-orphan"
    )
    # passive_deletes: deleting a game leaves its images to the database's ON DELETE CASCADE,
    # so SQLAlchemy never loads them (or their blobs) just to delete them.
    images: Mapped[list[Image]] = relationship(
        "Image",
        back_populates="game",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Image.id",
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    grading_type: Mapped[str] = mapped_column(
        Enum("ACCURACY", "COMPLETENESS", name="grading_type"), nullable=False
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    answer_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    time_limit_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    points_value: Mapped[float] = mapped_column(sa.Float, default=1.0, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    game: Mapped[Game] = relationship("Game", back_populates="questions")
    scores: Mapped[list[SessionScore]] = relationship(
        "SessionScore", back_populates="question"
    )


class UserGameAccess(Base):
    __tablename__ = "user_game_access"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )

    user: Mapped[User] = relationship("User", back_populates="game_access")
    game: Mapped[Game] = relationship("Game", back_populates="user_access")
