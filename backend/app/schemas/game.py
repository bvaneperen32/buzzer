from __future__ import annotations


from pydantic import BaseModel, ConfigDict, field_validator


# ---------------------------------------------------------------------------
# Client-safe question — answer_data and grading_type never exposed
# ---------------------------------------------------------------------------


class QuestionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type: str
    prompt: str
    config: dict
    time_limit_seconds: int
    points_value: float
    order_index: int


# ---------------------------------------------------------------------------
# Room / session
# ---------------------------------------------------------------------------


class RoomCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_id: int
    course_id: int
    allow_guests: bool = True

    @field_validator("game_id", "course_id")
    @classmethod
    def positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v


class RoomCreateResponse(BaseModel):
    room_code: str
    session_id: str


class ActiveSessionItem(BaseModel):
    session_id: str
    room_code: str
    status: str
    game_title: str
    course_name: str
    course_semester: str


class RoomInfoResponse(BaseModel):
    session_id: str
    room_code: str
    status: str
    game_title: str
    course_id: int
    course_name: str
    course_semester: str
    question_count: int


# ---------------------------------------------------------------------------
# Host resource lists
# ---------------------------------------------------------------------------


class MyCourseItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    semester: str
    role: str  # HOST (always, for this endpoint)


class MyGameItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    description: str
    max_players: int
    # All three are None for an unassigned game (only admins are ever sent one).
    course_id: int | None = None
    course_name: str | None = None
    course_semester: str | None = None


# ---------------------------------------------------------------------------
# Scoring result (internal, not a response schema)
# ---------------------------------------------------------------------------


class ScoreResult(BaseModel):
    points_awarded: float
    is_correct: bool
