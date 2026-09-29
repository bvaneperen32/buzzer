from __future__ import annotations

from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from ..services.image_refs import CANVAS_KEY, OPTIONS_KEY, PROMPT_KEY, is_image_id


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------


class CourseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=255)
    semester: str = Field(..., min_length=1, max_length=50)


class CourseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=255)
    semester: str | None = Field(None, min_length=1, max_length=50)


class CourseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    semester: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


class RosterEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    course_id: int
    netid: str
    full_name: str
    email: str
    is_active: bool
    imported_at: datetime


class RosterUploadResult(BaseModel):
    imported: int
    updated: int
    deactivated: int
    errors: list[str]


class RosterEntryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_active: bool | None = None
    netid: str | None = Field(None, min_length=1, max_length=100)
    full_name: str | None = Field(None, min_length=1, max_length=255)
    email: EmailStr | None = None


class RosterRowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    netid: str = Field(..., min_length=1, max_length=100)
    full_name: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., min_length=1, max_length=255)


class RosterImportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[RosterRowIn] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


class GameCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., min_length=1, max_length=255)
    description: str = Field("", max_length=5000)
    max_players: int = Field(150, ge=1, le=500)


class GameCreateRequest(GameCreate):
    # GameCreate also validates the `game` section of import bundles, which stay
    # portable across databases — so the course id lives only on the request body.
    course_id: int = Field(..., gt=0)


class GameUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=5000)
    max_players: int | None = Field(None, ge=1, le=500)


class AdminGameUpdate(GameUpdate):
    # Admin only: a course can be set or changed, never cleared.
    course_id: int | None = Field(None, gt=0)


class GameResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    description: str
    max_players: int
    course_id: int | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


class QuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(
        ..., pattern="^(multiple_choice|true_false|fill_in_the_blank|multi_select)$"
    )
    grading_type: str = Field(..., pattern="^(ACCURACY|COMPLETENESS)$")
    prompt: str = Field(..., min_length=1, max_length=2000)
    config: dict = Field(default_factory=dict)
    answer_data: dict = Field(default_factory=dict)
    time_limit_seconds: int = Field(30, ge=2, le=300)
    points_value: float = Field(1.0, ge=0, le=100000)
    order_index: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def validate_structure(self) -> QuestionCreate:
        if self.type == "multiple_choice":
            opts = self.config.get("options")
            if not isinstance(opts, list) or len(opts) < 2:
                raise ValueError(
                    "multiple_choice config must have 'options' list with at least 2 items"
                )
            if self.grading_type == "ACCURACY":
                pts = self.answer_data.get("answer_points")
                if not isinstance(pts, list) or len(pts) != len(opts):
                    raise ValueError(
                        "ACCURACY multiple_choice answer_data must have 'answer_points' list matching options length"
                    )
                if any(not isinstance(p, (int, float)) or p < 0 for p in pts):
                    raise ValueError(
                        "answer_points values must be non-negative numbers"
                    )
        if self.type == "true_false" and self.grading_type == "ACCURACY":
            pts = self.answer_data.get("answer_points")
            if not isinstance(pts, dict) or set(pts.keys()) != {"true", "false"}:
                raise ValueError(
                    "ACCURACY true_false answer_data must have 'answer_points' with 'true' and 'false' keys"
                )
        if self.type == "fill_in_the_blank" and self.grading_type == "ACCURACY":
            answers = self.answer_data.get("acceptedAnswers")
            if not isinstance(answers, list) or len(answers) == 0:
                raise ValueError(
                    "ACCURACY fill_in_the_blank answer_data must have 'acceptedAnswers' list with at least one item"
                )
            if any(not isinstance(a, str) or not a.strip() for a in answers):
                raise ValueError("acceptedAnswers entries must be non-empty strings")
            pts = self.answer_data.get("answerPoints")
            if not isinstance(pts, list) or len(pts) != len(answers):
                raise ValueError(
                    "ACCURACY fill_in_the_blank answer_data must have 'answerPoints' list matching acceptedAnswers length"
                )
            if any(not isinstance(p, (int, float)) or p < 0 for p in pts):
                raise ValueError("answerPoints values must be non-negative numbers")
            edit_dist = self.answer_data.get("editDistance", 0)
            if not isinstance(edit_dist, int) or edit_dist < 0:
                raise ValueError("editDistance must be a non-negative integer")
        if self.type == "multi_select":
            opts = self.config.get("options")
            if not isinstance(opts, list) or len(opts) < 2:
                raise ValueError(
                    "multi_select config must have 'options' list with at least 2 items"
                )
            if self.grading_type == "ACCURACY":
                pts = self.answer_data.get("answer_points")
                if not isinstance(pts, list) or len(pts) != len(opts):
                    raise ValueError(
                        "ACCURACY multi_select answer_data must have 'answer_points' list matching options length"
                    )
                if any(not isinstance(p, (int, float)) for p in pts):
                    raise ValueError("answer_points values must be numbers")
        _validate_image_refs(self.type, self.config)
        return self


# The reference keys are defined once, in services/image_refs.py (which imports nothing);
# these are the shape rules on them (T8 spec §2.7). Whether an id is in the game's library is
# checked by image_service.assert_in_library, under the game row lock.
_IMAGE_OPTION_TYPES = ("multiple_choice", "multi_select")


def _validate_image_refs(question_type: str, config: dict) -> None:
    prompt_image = config.get(PROMPT_KEY)
    if prompt_image is not None and not is_image_id(prompt_image):
        raise ValueError(
            f"config.{PROMPT_KEY} must be an image id (a positive integer)"
        )
    # T7 allows it on its canvas type; a legacy null is tolerated everywhere.
    if config.get(CANVAS_KEY) is not None:
        raise ValueError(
            f"config.{CANVAS_KEY} is only allowed on canvas question types"
        )

    option_images = config.get(OPTIONS_KEY)
    if option_images is None:
        # Text-only options are validated exactly as before T8.
        return
    if question_type not in _IMAGE_OPTION_TYPES:
        raise ValueError(
            f"config.{OPTIONS_KEY} is only allowed on multiple_choice and multi_select "
            "questions"
        )
    options = config.get("options")
    if (
        not isinstance(option_images, list)
        or not isinstance(options, list)
        or len(option_images) != len(options)
    ):
        raise ValueError(
            f"config.{OPTIONS_KEY} must be a list with one entry per option"
        )
    if any(entry is not None and not is_image_id(entry) for entry in option_images):
        raise ValueError(
            f"config.{OPTIONS_KEY} entries must be image ids (positive integers) or null"
        )
    for index, (caption, image) in enumerate(zip(options, option_images)):
        letter = chr(ord("A") + index) if index < 26 else str(index + 1)
        if not isinstance(caption, str):
            raise ValueError(
                f"Option {letter}: options must be text when a question has option images"
            )
        if not caption.strip() and image is None:
            raise ValueError(f"Option {letter} needs a caption or an image")


class QuestionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str | None = Field(
        None, pattern="^(multiple_choice|true_false|fill_in_the_blank|multi_select)$"
    )
    grading_type: str | None = Field(None, pattern="^(ACCURACY|COMPLETENESS)$")
    prompt: str | None = Field(None, min_length=1, max_length=2000)
    config: dict | None = None
    answer_data: dict | None = None
    time_limit_seconds: int | None = Field(None, ge=2, le=300)
    points_value: float | None = Field(None, ge=0, le=100000)
    order_index: int | None = Field(None, ge=0)


class QuestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    game_id: int
    type: str
    grading_type: str
    prompt: str
    config: dict
    answer_data: dict
    time_limit_seconds: int
    points_value: float
    order_index: int
    created_at: datetime


class QuestionReorder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: list[int] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Users (local accounts)
# ---------------------------------------------------------------------------


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=72)
    email: EmailStr | None = None

    @field_validator("username")
    @classmethod
    def lower_username(cls, v: str) -> str:
        return v.lower()


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str | None = Field(
        None, min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    display_name: str | None = Field(None, min_length=1, max_length=255)
    email: EmailStr | None = None
    password: str | None = Field(None, min_length=8, max_length=72)
    role: str | None = Field(None, pattern="^(USER|ADMIN)$")

    @field_validator("username")
    @classmethod
    def lower_username(cls, v: str | None) -> str | None:
        return v.lower() if v is not None else None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str | None
    netid: str | None
    display_name: str | None
    email: str | None
    role: str
    created_at: datetime
    last_login: datetime | None


# ---------------------------------------------------------------------------
# Access management
# ---------------------------------------------------------------------------


class CourseAccessGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_id: int
    role: str = Field(..., pattern="^(HOST|PLAYER)$")


class GameAccessGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_id: int


class UserWithAccessResponse(UserResponse):
    course_access: list[dict]
    game_access: list[int]


# ---------------------------------------------------------------------------
# Sessions (admin view)
# ---------------------------------------------------------------------------


class AdminSessionItem(BaseModel):
    model_config = ConfigDict(from_attributes=False)
    session_id: str
    room_code: str
    status: str
    game_id: int
    game_title: str
    course_id: int
    course_name: str
    course_semester: str
    host_display_name: str | None
    created_at: datetime
    completed_at: datetime | None
    player_count: int
