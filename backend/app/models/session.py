from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base

if TYPE_CHECKING:
    from .course import Course
    from .game import Game, Question
    from .user import User


class GameSession(Base):
    __tablename__ = "game_sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_code: Mapped[str] = mapped_column(
        String(6), unique=True, index=True, nullable=False
    )
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), nullable=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    host_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Enum("LOBBY", "IN_PROGRESS", "COMPLETED", "ABANDONED", name="session_status"),
        default="LOBBY",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    allow_guests: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa.true()
    )

    game: Mapped[Game] = relationship("Game", back_populates="sessions")
    course: Mapped[Course] = relationship("Course", back_populates="sessions")
    host: Mapped[User | None] = relationship("User", back_populates="hosted_sessions")
    scores: Mapped[list[SessionScore]] = relationship(
        "SessionScore", back_populates="session", cascade="all, delete-orphan"
    )


class SessionScore(Base):
    __tablename__ = "session_scores"

    __table_args__ = (Index("ix_scores_session_user", "session_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("game_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), nullable=False)
    points_awarded: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    answer_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    answer_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped[GameSession] = relationship("GameSession", back_populates="scores")
    user: Mapped[User] = relationship("User", back_populates="scores")
    question: Mapped[Question] = relationship("Question", back_populates="scores")
