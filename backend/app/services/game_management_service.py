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

T7 (docs/plans/t7-question-types.md §2.5, §2.6, §2.9): every Spot It write persists
the merged config with the image's trusted current dimensions, and a stamp sent
with it is an attempted confirmation (409 when stale, 422 when the grid doesn't
fit). While a session of the game is live, a Sort It or Spot It question is
frozen except for its prompt, time limit and (Sort It) prompt image — compared
on the stored values after the write, so a refusal rolls back.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

import bleach
import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.exceptions import ConflictError, NotFoundError, UnprocessableError
from ..models.course import Course
from ..models.game import Game, Question, UserGameAccess
from ..models.image import Image
from ..models.session import SessionScore
from ..models.user import User
from ..schemas.admin import GameCreate, GameUpdate, QuestionCreate, QuestionUpdate
from ..schemas.image import LiveSessionItem
from . import bundle_service, game_service, image_service, sort_it, spot_it
from .bundle_service import GameMeta, QuestionFields
from .game_lock import fresh, lock_game
from .image_refs import CANVAS_KEY, OPTIONS_KEY, image_refs, is_image_id, map_image_refs

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
    questions = sorted(result.scalars().all(), key=image_service.question_order)
    # T7: each Spot It question's review status, from a plain read of its image (the list
    # takes no locks); every other type is false / [].
    spot = [
        q for q in questions if q.type == spot_it.TYPE and isinstance(q.config, dict)
    ]
    images = await image_service.images_for_refs(
        db, game_id, [q.config.get(CANVAS_KEY) for q in spot]
    )
    for question in questions:
        reasons: list[str] = []
        if question.type == spot_it.TYPE:
            config = question.config if isinstance(question.config, dict) else {}
            image_id = config.get(CANVAS_KEY)
            image = images.get(image_id) if is_image_id(image_id) else None
            info = (
                spot_it.ImageInfo(image.id, image.version, image.width, image.height)
                if image is not None
                else None
            )
            reasons = spot_it.review_status(question.config, question.answer_data, info)
        _attach_review_status(question, reasons)
    return questions


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


# ---------------------------------------------------------------------------
# T7: Spot It confirmation and dimensions, the live freeze, review status
# ---------------------------------------------------------------------------

T7_TYPES = (sort_it.TYPE, spot_it.TYPE)

_LIVE_FIX = (
    "To change it now, the session's host can finish the game or delete the session from "
    "Play (deleting discards its partial scores); admins can delete sessions from Sessions."
)


def _live_message(lead_in: str, live: Sequence[LiveSessionItem]) -> str:
    """The freeze's 409: a T7 lead-in, T8's room list (T8's own message is unchanged), the
    fix (T7 spec §2.6)."""
    return f"{lead_in} Live: {image_service.live_rooms_text(live)}. {_LIVE_FIX}"


def _image_info(meta: image_service.ImageMeta | None) -> spot_it.ImageInfo | None:
    return (
        spot_it.ImageInfo(meta.id, meta.version, meta.width, meta.height)
        if meta
        else None
    )


def _attach_review_status(question: Question, reasons: list[str]) -> Question:
    """Set the computed (never stored) fields `QuestionResponse` reports."""
    question.needs_review = bool(reasons)
    question.review_reasons = list(reasons)
    return question


def _stale_stamp_message(stamp: dict, config: dict, version: int) -> str:
    checked = (
        f"image {stamp['image_id']} v{stamp['image_version']}, "
        f"{stamp['cols']}×{stamp['rows']}"
    )
    cols, rows = spot_it.grid(config)
    image = (
        ""
        if stamp["image_id"] == config[CANVAS_KEY]
        else f"image {config[CANVAS_KEY]} "
    )
    return (
        "The image or grid changed after the accepted cells were checked "
        f"(checked: {checked}; now: {image}v{version}, {cols}×{rows}). "
        "Review the cells and save again."
    )


def _too_dense_to_confirm_message(cols: int, rows: int, width: int, height: int) -> str:
    max_cols, max_rows = spot_it.max_grid(width, height)
    if max_rows < spot_it.MIN_GRID or max_cols < spot_it.MIN_GRID:
        text = spot_it.too_dense_text(cols, rows, width, height).removeprefix(
            "its image"
        )
        return (
            f"Can't confirm: this image{text}. Choose or replace the image, then confirm "
            "again."
        )
    return (
        f"Can't confirm: the {cols}×{rows} grid is too dense for this image (at most "
        f"{max_cols}×{max_rows}). Change the grid, re-check the accepted cells, and confirm "
        "again."
    )


def _spot_it_write(
    config: dict,
    answer_data: dict,
    meta: image_service.ImageMeta,
    *,
    answer_data_sent: bool,
) -> tuple[dict, dict]:
    """(config, answer_data) to persist for a Spot It write (T7 spec §2.5 "Writes"), on plain
    values after every locking read.

    - The config always gets the image's trusted current `width`/`height`.
    - When `answer_data` was sent (always on create), its stamp decides: absent or null → no
      stamp (a null key is removed, so stored data never holds `confirmed_for: null`); an
      object → an attempted confirmation, which must match the current image version and grid
      (else 409) and then fit the image (else 422). Without a sent `answer_data`, the stored
      one — stamp included — is carried unchanged and never stamp-validated.
    """
    written_config = {**config, "width": meta.width, "height": meta.height}
    if not answer_data_sent:
        return written_config, answer_data
    sent_stamp = answer_data.get("confirmed_for")
    if sent_stamp is None:
        return written_config, {
            key: value for key, value in answer_data.items() if key != "confirmed_for"
        }
    cols, rows = spot_it.grid(config)  # validate_structure has checked the grid
    if sent_stamp != spot_it.stamp_for(config[CANVAS_KEY], meta.version, cols, rows):
        raise ConflictError(_stale_stamp_message(sent_stamp, config, meta.version))
    if not spot_it.grid_fits(cols, rows, meta.width, meta.height):
        raise UnprocessableError(
            _too_dense_to_confirm_message(cols, rows, meta.width, meta.height)
        )
    return written_config, answer_data


def _frozen_fields(fields: QuestionFields) -> tuple:
    """What a live session freezes on a Sort It or Spot It question (T7 spec §2.6): its type,
    grading, points, whole answer key (with the stamp), and answer surface."""
    config = fields.config if isinstance(fields.config, dict) else {}
    if fields.type == sort_it.TYPE:
        surface: tuple = (config.get("boxes"), config.get("cards"))
    elif fields.type == spot_it.TYPE:
        surface = (config.get(CANVAS_KEY), config.get("grid"))
    else:
        surface = ()
    return (
        fields.type,
        fields.grading_type,
        fields.points_value,
        fields.answer_data,
        surface,
    )


def _question_number(inputs: image_service.ExportInputs, question_id: int) -> int:
    """The question's 1-based position in the game's order ("Q3")."""
    for number, (qid, _, _) in enumerate(inputs.questions, start=1):
        if qid == question_id:
            return number
    return len(inputs.questions) + 1


async def _spot_it_meta(
    db: AsyncSession, game_id: int, config: dict
) -> image_service.ImageMeta:
    """The canvas image's current metadata, read fresh under the game lock. The library check
    has already passed, so the image exists."""
    meta = await image_service.get_image_meta(
        db, game_id, config[CANVAS_KEY], locking=True
    )
    if meta is None:  # unreachable after assert_in_library; a clear error, not a crash
        raise UnprocessableError(
            f"Image {config[CANVAS_KEY]} isn't in this game's library"
        )
    return meta


async def create_question(
    db: AsyncSession, game_id: int, body: QuestionCreate, redis: Redis
) -> Question:
    # The router has already validated the body as QuestionCreate (reference shapes too).
    await lock_game(db, game_id)
    await image_service.assert_in_library(
        db, game_id, [ref.image_id for ref in image_refs(body.config)]
    )
    # T7 locking reads, all before any assignment (T8's fresh-read rule).
    meta = live = None
    if body.type == spot_it.TYPE:
        meta = await _spot_it_meta(db, game_id, body.config)
        live = await image_service.live_sessions(db, redis, game_id, locking=True)
    inputs = await image_service.export_inputs(
        db, game_id, with_data=False, locking=True
    )

    config, answer_data = body.config, body.answer_data
    reasons: list[str] = []
    if meta is not None:
        config, answer_data = _spot_it_write(
            config, answer_data, meta, answer_data_sent=True
        )
        reasons = spot_it.review_status(config, answer_data, _image_info(meta))
        if live and reasons:
            # A LOBBY session would include it at start, which the gate would then refuse.
            raise ConflictError(
                _live_message(
                    "A Spot It question can't be added unconfirmed while a session of this "
                    "game is live; check its accepted cells and confirm it first.",
                    live,
                )
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
        config=config,
        answer_data=answer_data,
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
    return _attach_review_status(question, reasons)


async def update_question(
    db: AsyncSession,
    game_id: int,
    question_id: int,
    body: QuestionUpdate,
    redis: Redis,
) -> Question:
    await lock_game(db, game_id)
    # Fresh (shared, populate_existing): the merge must start from the latest committed
    # question, not the pre-lock snapshot or an unrefreshed object.
    question = await _get_question(db, game_id, question_id, locking=True)
    # T7: the stored question as plain values, before anything is assigned — the live
    # freeze compares it with the question as stored after the write.
    before = QuestionFields(
        type=question.type,
        grading_type=question.grading_type,
        prompt=question.prompt,
        config=copy.deepcopy(question.config),
        answer_data=copy.deepcopy(question.answer_data),
        time_limit_seconds=question.time_limit_seconds,
        points_value=question.points_value,
    )

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
    # T7 locking reads, all before any assignment (T8's fresh-read rule).
    meta = None
    if validated.type == spot_it.TYPE:
        meta = await _spot_it_meta(db, game_id, validated.config)
    live = []
    if before.type in T7_TYPES or validated.type in T7_TYPES:
        live = await image_service.live_sessions(db, redis, game_id, locking=True)
    inputs = await image_service.export_inputs(
        db, game_id, with_data=False, locking=True
    )

    # T7 checks on plain values.
    number = _question_number(inputs, question_id)
    # Another type becoming a T7 type; a T7 question's own type change is a frozen-field
    # change, refused below with the frozen-field lead-in.
    if live and validated.type in T7_TYPES and before.type not in T7_TYPES:
        raise ConflictError(
            _live_message(
                f"Q{number} can't become a Sort It or Spot It question while a session "
                "of this game is live.",
                live,
            )
        )
    if meta is not None:
        # Every Spot It write persists the merged config with trusted dimensions, even
        # when the request omitted `config` (T7 spec §2.5 Writes, step 2).
        changes["config"], answer_data = _spot_it_write(
            validated.config,
            validated.answer_data,
            meta,
            answer_data_sent="answer_data" in changes,
        )
        if "answer_data" in changes:
            changes["answer_data"] = answer_data

    # Every locking read is done, so only now assign (populate_existing would overwrite
    # unflushed changes). Then the export rule on the question as stored; a refusal raises,
    # and get_db rolls the update back.
    if "prompt" in changes:
        changes["prompt"] = sanitize_prompt(changes["prompt"])
    for field, value in changes.items():
        setattr(question, field, value)
    stored = await _as_stored(db, question)
    if (
        live
        and before.type in T7_TYPES
        and _frozen_fields(stored) != _frozen_fields(before)
    ):
        raise ConflictError(
            _live_message(
                f"Q{number} can't be changed while a session of this game is live; only "
                "its prompt, time limit and (Sort It) prompt image can change until the "
                "session ends.",
                live,
            )
        )
    image_service.check_export_change(
        inputs,
        questions=[
            stored if qid == question_id else fields
            for qid, _, fields in inputs.questions
        ],
    )
    reasons = (
        spot_it.review_status(stored.config, stored.answer_data, _image_info(meta))
        if meta is not None
        else []
    )
    return _attach_review_status(question, reasons)


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


def _take_bundle_confirmation(q: dict, label: str) -> tuple[dict, bool]:
    """A bundle's Spot It question without its `answer_data.confirmed` flag, and the flag.

    Bundles are version-free, so they record confirmation as a boolean (absent = false); the
    stamp is rebuilt against the imported image (T7 spec §2.9). A stamp in a bundle is refused.
    """
    answer_data = q.get("answer_data")
    if not isinstance(answer_data, dict):
        return q, False  # QuestionCreate reports the malformed answer_data
    if "confirmed_for" in answer_data:
        raise UnprocessableError(
            f"{label}: bundles record confirmation as answer_data.confirmed"
        )
    if "confirmed" not in answer_data:
        return q, False
    confirmed = answer_data["confirmed"]
    if type(confirmed) is not bool:
        raise UnprocessableError(
            f"{label}: answer_data.confirmed must be true or false"
        )
    rest = {key: value for key, value in answer_data.items() if key != "confirmed"}
    return {**q, "answer_data": rest}, confirmed


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
    # T7: which Spot It questions the bundle marks confirmed, by question index.
    confirmed_in_bundle: dict[int, bool] = {}
    for i, q in enumerate(questions_raw):
        label = f"Question {i + 1}"
        if not isinstance(q, dict):
            raise UnprocessableError(f"{label} invalid: must be an object")
        if q.get("type") == spot_it.TYPE:
            q, confirmed_in_bundle[i] = _take_bundle_confirmation(q, label)
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

    # T7, before any write: a Spot It question marked confirmed is an attempted confirmation,
    # so its grid must fit its (processed) image. Unconfirmed questions keep any structurally
    # valid grid; nothing is resized or remapped (T7 spec §2.9).
    for i, q in enumerate(validated_questions):
        if q.type == spot_it.TYPE and confirmed_in_bundle.get(i):
            image = processed[q.config[CANVAS_KEY] - 1]
            cols, rows = spot_it.grid(q.config)
            if not spot_it.grid_fits(cols, rows, image.width, image.height):
                max_cols, max_rows = spot_it.max_grid(image.width, image.height)
                fix = (
                    "change the image"
                    if min(max_cols, max_rows) < spot_it.MIN_GRID
                    else "fix the grid"
                )
                raise UnprocessableError(
                    f"Question {i + 1} is marked confirmed, but "
                    f"{spot_it.too_dense_text(cols, rows, image.width, image.height)}; "
                    f'remove "confirmed" or {fix}'
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
        config = map_image_refs(q.config, lambda value: real_ids[value - 1])
        answer_data = q.answer_data
        if q.type == spot_it.TYPE:
            # Dimensions rebuilt from the processed image (any sent ones are discarded), and
            # a confirmed question stamped against the new image at version 1.
            position = q.config[CANVAS_KEY]
            image = processed[position - 1]
            config = {**config, "width": image.width, "height": image.height}
            answer_data = dict(q.answer_data)
            if confirmed_in_bundle.get(idx):
                cols, rows = spot_it.grid(config)
                answer_data["confirmed_for"] = spot_it.stamp_for(
                    real_ids[position - 1], 1, cols, rows
                )
        db.add(
            Question(
                game_id=game.id,
                type=q.type,
                grading_type=q.grading_type,
                prompt=sanitize_prompt(q.prompt),
                config=config,
                answer_data=answer_data,
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
