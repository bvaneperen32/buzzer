"""
Game and question authoring — every write to `games` and `questions`.

Both the admin router and the host router call these functions, so prompt
sanitization, the course requirement, and question validation live in exactly
one place. Access checks are the caller's job (the `game_service` assertions);
nothing here decides who may do what.

T8: every write that can change a game's export (question create and update,
title/description/max-players update) and every reorder first takes the game
row lock, reads fresh under it, and checks image references before writing.
The export rule measures game metadata before it is assigned, but questions
after they are written and read back (see `_as_stored`); a refusal raises, and
the request's transaction rolls back. Import checks the whole bundle before
creating any row, then measures the game it created the same way.
"""

from __future__ import annotations

import json
from typing import Any

import bleach
import structlog
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.exceptions import ConflictError, NotFoundError, UnprocessableError
from ..models.course import Course
from ..models.game import Game, Question, UserGameAccess
from ..models.image import Image
from ..models.session import SessionScore
from ..models.user import User
from ..schemas.admin import GameCreate, GameUpdate, QuestionCreate, QuestionUpdate
from . import bundle_service, game_service, image_service
from .bundle_service import GameMeta, QuestionFields
from .game_lock import fresh, lock_game
from .image_refs import OPTIONS_KEY, image_refs, map_image_refs

logger = structlog.get_logger()

PROMPT_TAGS = ["b", "i", "br", "u"]
SUPPORTED_IMPORT_VERSIONS = (
    bundle_service.PLAIN_VERSION,
    bundle_service.IMAGES_VERSION,
)


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
    # The lock refreshes `game` (the object the access check returned) before anything is
    # assigned to it, so the export rule sees current values. Strings and an int, which MySQL
    # (strict mode) stores exactly, so they can be measured before they're assigned.
    await lock_game(db, game.id)
    new_meta = GameMeta(
        title=game.title if changes.title is None else changes.title,
        description=game.description
        if changes.description is None
        else changes.description,
        max_players=game.max_players
        if changes.max_players is None
        else changes.max_players,
    )
    if new_meta != GameMeta(game.title, game.description, game.max_players):
        inputs = await image_service.export_inputs(
            db, game.id, with_data=False, locking=True
        )
        image_service.check_export_change(inputs, meta=new_meta)

    # course_id comes in separately (admin path only) so that it goes through the
    # existence check and grant revocation here — never read it off `changes` with a
    # plain setattr.
    if course_id is not None and course_id != game.course_id:
        if not await db.get(Course, course_id):
            raise NotFoundError(f"Course {course_id} not found")
        game.course_id = course_id
        await game_service.revoke_game_grants_without_host(db, game.id, course_id)

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


async def _get_question(
    db: AsyncSession, game_id: int, question_id: int, *, locking: bool = False
) -> Question:
    stmt = select(Question).where(
        Question.id == question_id, Question.game_id == game_id
    )
    if locking:
        stmt = fresh(stmt)
    question = (await db.execute(stmt)).scalar_one_or_none()
    if not question:
        raise NotFoundError(f"Question {question_id} not found in game {game_id}")
    return question


async def list_questions(db: AsyncSession, game_id: int) -> list[Question]:
    # Sorted in Python (a large question breaks MySQL's sort; see
    # image_service.question_order). id breaks order_index ties, so the list, question
    # numbers ("Q3") and export agree.
    result = await db.execute(select(Question).where(Question.game_id == game_id))
    return sorted(result.scalars().all(), key=image_service.question_order)


async def _as_stored(db: AsyncSession, question: Question) -> QuestionFields:
    """A question just written, flushed and read back: its fields exactly as export will read
    them, for the export rule.

    MySQL can store a question longer than it was sent, so measuring the request's values
    isn't enough. `points_value` is a single-precision FLOAT (1e-40 reads back as 9.99995e-41),
    and the JSON columns turn integers outside 64 bits into doubles and don't parse every
    double exactly. Reading the row back measures whatever the database did. The read is of
    this transaction's own write, so it's current without a lock.
    """
    await db.flush()
    await db.refresh(question)
    return QuestionFields(
        type=question.type,
        grading_type=question.grading_type,
        prompt=question.prompt,
        config=question.config,
        answer_data=question.answer_data,
        time_limit_seconds=question.time_limit_seconds,
        points_value=question.points_value,
    )


async def create_question(
    db: AsyncSession, game_id: int, body: QuestionCreate
) -> Question:
    # The router has already validated the body as QuestionCreate (reference shapes too).
    await lock_game(db, game_id)
    await image_service.assert_in_library(
        db, game_id, [ref.image_id for ref in image_refs(body.config)]
    )
    inputs = await image_service.export_inputs(
        db, game_id, with_data=False, locking=True
    )

    # From the same fresh list, so two creates that waited on the lock get distinct indexes.
    if body.order_index is None:
        order_index = max((index for _, index, _ in inputs.questions), default=-1) + 1
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
    # The export rule on the question as stored. A refusal raises before the router commits,
    # so the insert rolls back.
    image_service.check_export_change(
        inputs, questions=[*inputs.fields, await _as_stored(db, question)]
    )
    return question


async def update_question(
    db: AsyncSession, game_id: int, question_id: int, body: QuestionUpdate
) -> Question:
    await lock_game(db, game_id)
    # Fresh (shared, populate_existing): the merge must start from the latest committed
    # question, not the pre-lock snapshot or an unrefreshed object.
    question = await _get_question(db, game_id, question_id, locking=True)

    # An explicit null means "leave unchanged", so drop nulls along with unset fields.
    changes = body.model_dump(exclude_unset=True, exclude_none=True)

    # QuestionUpdate only checks fields one at a time. Re-run the full structural
    # validation POST applies, against the question as it would be after this edit.
    merged = {field: getattr(question, field) for field in QuestionCreate.model_fields}
    merged.update(changes)
    try:
        validated = QuestionCreate(**merged)
    except ValidationError as exc:
        raise UnprocessableError("; ".join(e["msg"] for e in exc.errors())) from exc

    await image_service.assert_in_library(
        db, game_id, [ref.image_id for ref in image_refs(validated.config)]
    )
    inputs = await image_service.export_inputs(
        db, game_id, with_data=False, locking=True
    )

    # Every locking read is done, so only now assign (populate_existing would overwrite
    # unflushed changes). Then the export rule on the question as stored; a refusal raises,
    # and get_db rolls the update back.
    if "prompt" in changes:
        changes["prompt"] = sanitize_prompt(changes["prompt"])
    for field, value in changes.items():
        setattr(question, field, value)
    stored = await _as_stored(db, question)
    image_service.check_export_change(
        inputs,
        questions=[
            stored if qid == question_id else fields
            for qid, _, fields in inputs.questions
        ],
    )
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
    # Under the game lock, from a fresh list: a create that committed while this waited
    # makes the id sets differ, which is the existing 409.
    await lock_game(db, game_id)
    result = await db.execute(
        fresh(select(Question).where(Question.game_id == game_id))
    )
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
    """Version 1 (unchanged bytes) for a game with no images, version 2 otherwise. The one
    builder and serializer in bundle_service also measure the export for the export rule."""
    inputs = await image_service.export_inputs(
        db, game.id, with_data=True, locking=False
    )
    bundle = bundle_service.build_bundle(
        inputs.meta, inputs.fields, inputs.images, include_data=True
    )

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in game.title)
    filename = f"{safe_title}.json"
    return filename, bundle_service.serialize(bundle)


def _describe(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _has_image_reference(config: dict) -> bool:
    entries = config.get(OPTIONS_KEY)
    return any(
        ref is not None
        for ref in (config.get("prompt_image_id"), config.get("image_id"))
    ) or (
        entries is not None
        and (not isinstance(entries, list) or any(e is not None for e in entries))
    )


async def import_game(
    db: AsyncSession, raw: bytes, *, course_id: int, creator: User
) -> Game:
    """Import a version 1 or 2 bundle, in the T8 spec §2.15 order — the only definition of
    it. Everything up to step 7 runs before any row is created; step 8's export rule runs on
    the created rows, and the router commits once, so any failure creates nothing. The router
    has already applied the import limit."""
    # 1. Parse and version.
    try:
        bundle = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UnprocessableError(f"Invalid JSON: {exc}") from exc
    if (
        not isinstance(bundle, dict)
        or bundle.get("format") != bundle_service.BUNDLE_FORMAT
    ):
        raise UnprocessableError("Unrecognised file format")
    version = bundle.get("version")
    if type(version) is not int or version not in SUPPORTED_IMPORT_VERSIONS:
        raise UnprocessableError(
            f"Unsupported version {version!r}; server supports versions 1 and 2"
        )
    if version == bundle_service.PLAIN_VERSION and "images" in bundle:
        raise UnprocessableError("Version 1 bundles can't contain images")

    # 2. Game metadata.
    game_data = bundle.get("game", {})
    try:
        if not isinstance(game_data, dict):
            raise ValueError("'game' must be an object")
        game_meta = GameCreate(**game_data)
    except Exception as exc:
        raise UnprocessableError(f"Invalid game metadata: {exc}") from exc

    # 3. Image count, before touching any entry, so thousands of tiny images are refused
    #    without being processed.
    entries: list[image_service.BundleEntry] = []
    if version == bundle_service.IMAGES_VERSION:
        raw_images = bundle.get("images")
        if not isinstance(raw_images, list):
            raise UnprocessableError("A version 2 bundle's 'images' must be a list")
        image_service.check_budgets(len(raw_images), 0)
        # 4. Each entry, without decoding the image.
        entries = image_service.check_bundle_entries(raw_images)

    # 5. References and questions. Keys become provisional ids: the image's position + 1.
    provisional = {entry.key: position for position, entry in enumerate(entries, 1)}
    questions_raw = bundle.get("questions", [])
    if not isinstance(questions_raw, list):
        raise UnprocessableError("'questions' must be a list")
    validated_questions: list[QuestionCreate] = []
    for i, q in enumerate(questions_raw):
        label = f"Question {i + 1}"
        if not isinstance(q, dict):
            raise UnprocessableError(f"{label} invalid: must be an object")
        config = q.get("config")
        if isinstance(config, dict):
            entries_value = config.get(OPTIONS_KEY)
            if entries_value is not None and not isinstance(entries_value, list):
                raise UnprocessableError(
                    f"{label}: config.{OPTIONS_KEY} must be a list"
                )
            if version == bundle_service.PLAIN_VERSION:
                if _has_image_reference(config):
                    raise UnprocessableError(
                        f"{label}: version 1 bundles can't reference images"
                    )
            else:

                def to_provisional(value: Any, label: str = label) -> int:
                    if isinstance(value, str) and value in provisional:
                        return provisional[value]
                    raise UnprocessableError(
                        f"{label}: unknown image key {_describe(value)}"
                    )

                q = {**q, "config": map_image_refs(config, to_provisional)}
        try:
            validated_questions.append(QuestionCreate(**q))
        except Exception as exc:
            raise UnprocessableError(f"{label} invalid: {exc}") from exc

    # 6. The pipeline, one slot per image, against the import processing deadline. Its final
    #    deadline check runs before step 7, so nothing is written for an overdue import.
    processed = await image_service.process_bundle_images(
        entries, image_service.import_deadline()
    )

    # 7. The library budget, on the processed sizes (exactly what will be stored).
    image_service.check_budgets(len(processed), sum(p.byte_size for p in processed))

    # 8. Create the game, its images and its questions in this one transaction, then the
    #    export rule on the game as stored.
    game = await create_game(db, meta=game_meta, course_id=course_id, creator=creator)
    images = [
        Image(
            game_id=game.id,
            filename=entry.filename,
            description=entry.description,
            version=1,
            content_type=p.content_type,
            data=p.data,
            small_data=p.small_data,
            width=p.width,
            height=p.height,
            byte_size=p.byte_size,
            sha256=p.sha256,
        )
        for entry, p in zip(entries, processed)
    ]
    db.add_all(images)
    await db.flush()
    real_ids = [image.id for image in images]

    for idx, q in enumerate(validated_questions):
        db.add(
            Question(
                game_id=game.id,
                type=q.type,
                grading_type=q.grading_type,
                prompt=sanitize_prompt(q.prompt),
                config=map_image_refs(q.config, lambda value: real_ids[value - 1]),
                answer_data=q.answer_data,
                time_limit_seconds=q.time_limit_seconds,
                points_value=q.points_value,
                order_index=idx,
            )
        )
    await db.flush()
    # Measured as stored, like question writes (see _as_stored): a plain read, since every
    # row of this game is this transaction's own insert. A refusal raises before the router
    # commits, so the game, its images and its questions all roll back.
    stored = await image_service.export_inputs(
        db, game.id, with_data=False, locking=False
    )
    bundle_service.check_export(
        0, bundle_service.bundle_size(stored.meta, stored.fields, stored.images)
    )

    logger.info(
        "game_imported",
        game_id=game.id,
        title=game.title,
        question_count=len(validated_questions),
        image_count=len(images),
        bundle_version=version,
    )
    return game
