"""
Game and question authoring — every write to `games` and `questions`.

Both the admin router and the host router call these functions, so prompt
sanitization, the course requirement, and question validation live in exactly
one place. Access checks are the caller's job (the `game_service` assertions);
nothing here decides who may do what.
"""

from __future__ import annotations

import json

import bleach
import structlog
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.exceptions import ConflictError, NotFoundError, UnprocessableError
from ..models.course import Course
from ..models.game import Game, Question, UserGameAccess
from ..models.session import SessionScore
from ..models.user import User
from ..schemas.admin import GameCreate, GameUpdate, QuestionCreate, QuestionUpdate

logger = structlog.get_logger()

PROMPT_TAGS = ["b", "i", "br", "u"]
SUPPORTED_IMPORT_VERSION = 1


def sanitize_prompt(text: str) -> str:
    """The only place prompts are sanitized. Load-bearing: report_service renders
    prompts as raw HTML, so a write path that skips this is a stored-XSS hole."""
    return bleach.clean(text, tags=PROMPT_TAGS, attributes={}, strip=True)


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


async def create_game(
    db: AsyncSession, *, meta: GameCreate, course_id: int, creator: User
) -> Game:
    course = await db.get(Course, course_id)
    if not course:
        raise NotFoundError(f"Course {course_id} not found")

    game = Game(
        title=meta.title,
        description=meta.description,
        max_players=meta.max_players,
        course_id=course_id,
    )
    db.add(game)
    await db.flush()
    await db.refresh(game)

    # Admins already pass every game rule, so only a non-admin creator needs a
    # grant to use the game they just made.
    if creator.role != "ADMIN":
        db.add(UserGameAccess(user_id=creator.id, game_id=game.id))

    logger.info("game_created", game_id=game.id, title=game.title, course_id=course_id)
    return game


async def update_game(
    db: AsyncSession, game: Game, changes: GameUpdate, *, course_id: int | None = None
) -> Game:
    # course_id comes in separately (admin path only) so that it goes through the
    # existence check here — never read it off `changes` with a plain setattr.
    if course_id is not None and course_id != game.course_id:
        if not await db.get(Course, course_id):
            raise NotFoundError(f"Course {course_id} not found")
        game.course_id = course_id

    if changes.title is not None:
        game.title = changes.title
    if changes.description is not None:
        game.description = changes.description
    if changes.max_players is not None:
        game.max_players = changes.max_players
    return game


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


async def _get_question(db: AsyncSession, game_id: int, question_id: int) -> Question:
    result = await db.execute(
        select(Question).where(Question.id == question_id, Question.game_id == game_id)
    )
    question = result.scalar_one_or_none()
    if not question:
        raise NotFoundError(f"Question {question_id} not found in game {game_id}")
    return question


async def list_questions(db: AsyncSession, game_id: int) -> list[Question]:
    result = await db.execute(
        select(Question)
        .where(Question.game_id == game_id)
        .order_by(Question.order_index)
    )
    return list(result.scalars().all())


async def create_question(
    db: AsyncSession, game_id: int, body: QuestionCreate
) -> Question:
    # Auto-assign order_index if not specified
    if body.order_index is None:
        result = await db.execute(
            select(Question.order_index)
            .where(Question.game_id == game_id)
            .order_by(Question.order_index.desc())
            .limit(1)
        )
        max_idx = result.scalar_one_or_none()
        order_index = (max_idx + 1) if max_idx is not None else 0
    else:
        order_index = body.order_index

    question = Question(
        game_id=game_id,
        type=body.type,
        grading_type=body.grading_type,
        prompt=sanitize_prompt(body.prompt),
        config=body.config,
        answer_data=body.answer_data,
        time_limit_seconds=body.time_limit_seconds,
        points_value=body.points_value,
        order_index=order_index,
    )
    db.add(question)
    await db.flush()
    await db.refresh(question)
    return question


async def update_question(
    db: AsyncSession, game_id: int, question_id: int, body: QuestionUpdate
) -> Question:
    question = await _get_question(db, game_id, question_id)

    # An explicit null means "leave unchanged", so drop nulls along with unset fields.
    changes = body.model_dump(exclude_unset=True, exclude_none=True)

    # QuestionUpdate only checks fields one at a time. Re-run the full structural
    # validation POST applies, against the question as it would be after this edit.
    merged = {field: getattr(question, field) for field in QuestionCreate.model_fields}
    merged.update(changes)
    try:
        QuestionCreate(**merged)
    except ValidationError as exc:
        raise UnprocessableError("; ".join(e["msg"] for e in exc.errors())) from exc

    if "prompt" in changes:
        changes["prompt"] = sanitize_prompt(changes["prompt"])
    for field, value in changes.items():
        setattr(question, field, value)
    return question


async def delete_question(db: AsyncSession, game_id: int, question_id: int) -> None:
    question = await _get_question(db, game_id, question_id)

    # session_scores.question_id is NOT NULL with no cascade, and deleting the scores
    # would rewrite past results — so an answered question can't be deleted.
    result = await db.execute(
        select(func.count(SessionScore.session_id.distinct())).where(
            SessionScore.question_id == question_id
        )
    )
    session_count = result.scalar_one()
    if session_count:
        raise ConflictError(
            f"This question has been answered in {session_count} session(s) and "
            "can't be deleted — edit it instead, or export the game and import a copy."
        )
    await db.delete(question)


async def reorder_questions(db: AsyncSession, game_id: int, order: list[int]) -> None:
    result = await db.execute(select(Question).where(Question.game_id == game_id))
    questions = {q.id: q for q in result.scalars().all()}

    if set(order) != set(questions.keys()):
        raise ConflictError(
            "order list must contain exactly the IDs of all questions in this game"
        )

    for idx, qid in enumerate(order):
        questions[qid].order_index = idx


# ---------------------------------------------------------------------------
# Import / export (buzzer/game bundles — portable, so no course in the file)
# ---------------------------------------------------------------------------


async def export_game(db: AsyncSession, game: Game) -> tuple[str, bytes]:
    questions = await list_questions(db, game.id)

    bundle = {
        "format": "buzzer/game",
        "version": SUPPORTED_IMPORT_VERSION,
        "game": {
            "title": game.title,
            "description": game.description,
            "max_players": game.max_players,
        },
        "questions": [
            {
                "type": q.type,
                "grading_type": q.grading_type,
                "prompt": q.prompt,
                "config": q.config,
                "answer_data": q.answer_data,
                "time_limit_seconds": q.time_limit_seconds,
                "points_value": q.points_value,
            }
            for q in questions
        ],
    }

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in game.title)
    filename = f"{safe_title}.json"
    return filename, json.dumps(bundle, indent=2, ensure_ascii=False).encode()


async def import_game(
    db: AsyncSession, raw: bytes, *, course_id: int, creator: User
) -> Game:
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UnprocessableError(f"Invalid JSON: {exc}") from exc

    if bundle.get("format") != "buzzer/game":
        raise UnprocessableError("Unrecognised file format")
    version = bundle.get("version")
    if version != SUPPORTED_IMPORT_VERSION:
        raise UnprocessableError(
            f"Unsupported version {version!r}; server supports version {SUPPORTED_IMPORT_VERSION}"
        )

    game_data = bundle.get("game", {})
    try:
        game_meta = GameCreate(**game_data)
    except Exception as exc:
        raise UnprocessableError(f"Invalid game metadata: {exc}") from exc

    questions_raw = bundle.get("questions", [])
    validated_questions: list[QuestionCreate] = []
    for i, q in enumerate(questions_raw):
        try:
            validated_questions.append(QuestionCreate(**q))
        except Exception as exc:
            raise UnprocessableError(f"Question {i + 1} invalid: {exc}") from exc

    game = await create_game(db, meta=game_meta, course_id=course_id, creator=creator)

    for idx, q in enumerate(validated_questions):
        db.add(
            Question(
                game_id=game.id,
                type=q.type,
                grading_type=q.grading_type,
                prompt=sanitize_prompt(q.prompt),
                config=q.config,
                answer_data=q.answer_data,
                time_limit_seconds=q.time_limit_seconds,
                points_value=q.points_value,
                order_index=idx,
            )
        )

    logger.info(
        "game_imported",
        game_id=game.id,
        title=game.title,
        question_count=len(validated_questions),
    )
    return game
