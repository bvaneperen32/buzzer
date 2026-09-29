from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

import structlog
from rapidfuzz.distance import Levenshtein
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.exceptions import ConflictError, ForbiddenError, NotFoundError
from ..models.course import Course, CourseRoster, UserCourseAccess
from ..models.game import Game, Question, UserGameAccess
from ..models.session import GameSession, SessionScore
from ..models.user import User
from ..schemas.game import ScoreResult
from . import state_service as state
from .game_lock import fresh, lock_game

logger = structlog.get_logger()

# Character set from spec: unambiguous alphanumeric (no 0/O/1/I/L)
_ROOM_CHARSET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_MAX_CODE_ATTEMPTS = 5

# Shared by the guest endpoint (the primary check) and authorise_player (the backstop).
GUESTS_NOT_ALLOWED = "This game isn't open to guests — sign in with your school account"


# ---------------------------------------------------------------------------
# Room code generation
# ---------------------------------------------------------------------------


async def generate_room_code(redis: Redis) -> str:
    for _ in range(_MAX_CODE_ATTEMPTS):
        code = "".join(secrets.choice(_ROOM_CHARSET) for _ in range(6))
        if not await redis.exists(f"room:{code}"):
            return code
    raise ConflictError("Could not generate a unique room code — try again")


# ---------------------------------------------------------------------------
# Access validation helpers
# ---------------------------------------------------------------------------


async def has_host_grant(db: AsyncSession, user_id: str, course_id: int) -> bool:
    """A HOST grant is a user_course_access row with role HOST; a PLAYER row isn't one."""
    result = await db.execute(
        select(UserCourseAccess).where(
            UserCourseAccess.user_id == user_id,
            UserCourseAccess.course_id == course_id,
            UserCourseAccess.role == "HOST",
        )
    )
    return result.scalar_one_or_none() is not None


async def assert_host_can_use_course(
    db: AsyncSession, user: User, course_id: int
) -> None:
    """Admin can use any course; USER must have HOST role in user_course_access."""
    if user.role == "ADMIN":
        course = await db.get(Course, course_id)
        if not course:
            raise NotFoundError(f"Course {course_id} not found")
        return

    if not await has_host_grant(db, user.id, course_id):
        raise ForbiddenError("You do not have HOST access to this course")


async def user_can_use_game(db: AsyncSession, user: User, game_id: int) -> bool:
    """
    The game rule as a predicate, without raising: ADMIN (for an existing game), or
    HOST on the game's course *and* a user_game_access grant. An unassigned game
    (course_id NULL) passes for no non-admin.
    """
    game = await db.get(Game, game_id)
    if not game:
        return False
    if user.role == "ADMIN":
        return True
    if game.course_id is None:
        return False
    grant = await db.execute(
        select(UserGameAccess).where(
            UserGameAccess.user_id == user.id,
            UserGameAccess.game_id == game_id,
        )
    )
    return grant.scalar_one_or_none() is not None and await has_host_grant(
        db, user.id, game.course_id
    )


async def assert_host_can_use_game(db: AsyncSession, user: User, game_id: int) -> Game:
    """
    The game rule, for running a game and for editing it (see user_can_use_game).
    Admins get a 404 for a missing game; non-admins get the same 403 whether or not
    the game exists.
    """
    game = await db.get(Game, game_id)
    if user.role == "ADMIN" and not game:
        raise NotFoundError(f"Game {game_id} not found")
    if not await user_can_use_game(db, user, game_id):
        raise ForbiddenError("You do not have access to this game")
    return game


async def user_can_host_session(
    db: AsyncSession, user: User, session: GameSession
) -> bool:
    """
    The session-host check behind downloads, session delete, and the socket HOST
    join. host_user_id is a historical record, not a permission: it counts only
    alongside a live HOST grant on the session's course.
    """
    if user.role == "ADMIN":
        return True
    if session.host_user_id != user.id:
        return False
    return await has_host_grant(db, user.id, session.course_id)


async def assert_can_download_session(
    db: AsyncSession, user: User, session_id: str
) -> GameSession:
    session = await db.get(GameSession, session_id)
    if not session:
        raise NotFoundError(f"Session {session_id} not found")
    # Host check before status, so a non-owner can't learn the session's status.
    if not await user_can_host_session(db, user, session):
        raise ForbiddenError("Only the session's host can download it")
    # Applies to admins too; the admin router's own download endpoints are the
    # any-status path.
    if session.status != "COMPLETED":
        raise ConflictError("Downloads are available once the session has completed")
    return session


async def assert_can_delete_session(
    db: AsyncSession, user: User, session_id: str
) -> GameSession:
    session = await db.get(GameSession, session_id)
    if not session:
        raise NotFoundError(f"Session {session_id} not found")
    if not await user_can_host_session(db, user, session):
        raise ForbiddenError("Only the session host can delete this session")
    return session


# ---------------------------------------------------------------------------
# Game-grant side effects — grants never outlive what makes them effective
# ---------------------------------------------------------------------------


async def revoke_game_grants_for_course(
    db: AsyncSession, user_id: str, course_id: int
) -> int:
    """User lost HOST on course_id: drop their grants for that course's games, so a
    later, unrelated HOST grant can't silently revive them."""
    result = await db.execute(
        delete(UserGameAccess).where(
            UserGameAccess.user_id == user_id,
            UserGameAccess.game_id.in_(
                select(Game.id).where(Game.course_id == course_id)
            ),
        )
    )
    logger.info(
        "game_grants_revoked_host_lost",
        user_id=user_id,
        course_id=course_id,
        count=result.rowcount,
    )
    return result.rowcount


async def revoke_game_grants_without_host(
    db: AsyncSession, game_id: int, course_id: int
) -> int:
    """Game moved to course_id: drop grants on it held by users who don't HOST that
    course. Grants of the new course's hosts stay and remain effective."""
    hosts = select(UserCourseAccess.user_id).where(
        UserCourseAccess.course_id == course_id,
        UserCourseAccess.role == "HOST",
    )
    result = await db.execute(
        delete(UserGameAccess).where(
            UserGameAccess.game_id == game_id,
            UserGameAccess.user_id.not_in(hosts),
        )
    )
    logger.info(
        "game_grants_revoked_game_moved",
        game_id=game_id,
        course_id=course_id,
        count=result.rowcount,
    )
    return result.rowcount


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------


async def list_sessions_for(
    db: AsyncSession, user: User, status: str | None
) -> list[dict]:
    """
    AdminSessionItem-shaped rows, newest first. Admin: every session, optionally
    filtered by status. Anyone else: only COMPLETED sessions they hosted on a
    course they still HOST (what the download rule lets them fetch); `status`
    is ignored.
    """
    player_counts = (
        select(
            SessionScore.session_id,
            func.count(SessionScore.user_id.distinct()).label("player_count"),
        )
        .group_by(SessionScore.session_id)
        .subquery()
    )
    # host_user_id is nullable and a LOBBY session has no scores, so both of
    # those joins must be outer or such sessions vanish from the list.
    query = (
        select(
            GameSession,
            Game.title,
            Course.name,
            Course.semester,
            User.display_name,
            User.username,
            User.netid,
            player_counts.c.player_count,
        )
        .join(Game, Game.id == GameSession.game_id)
        .join(Course, Course.id == GameSession.course_id)
        .outerjoin(User, User.id == GameSession.host_user_id)
        .outerjoin(player_counts, player_counts.c.session_id == GameSession.id)
        .order_by(GameSession.created_at.desc())
    )
    if user.role == "ADMIN":
        if status:
            query = query.where(GameSession.status == status)
    else:
        query = query.where(
            GameSession.host_user_id == user.id,
            GameSession.status == "COMPLETED",
            GameSession.course_id.in_(
                select(UserCourseAccess.course_id).where(
                    UserCourseAccess.user_id == user.id,
                    UserCourseAccess.role == "HOST",
                )
            ),
        )

    rows = await db.execute(query)
    return [
        {
            "session_id": s.id,
            "room_code": s.room_code,
            "status": s.status,
            "game_id": s.game_id,
            "game_title": game_title,
            "course_id": s.course_id,
            "course_name": course_name,
            "course_semester": course_semester,
            "host_display_name": display_name or username or netid,
            "created_at": s.created_at,
            "completed_at": s.completed_at,
            "player_count": player_count or 0,
        }
        for (
            s,
            game_title,
            course_name,
            course_semester,
            display_name,
            username,
            netid,
            player_count,
        ) in rows.all()
    ]


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


async def create_room(
    db: AsyncSession,
    redis: Redis,
    host: User,
    game_id: int,
    course_id: int,
    max_rooms: int = 50,
    allow_guests: bool = True,
) -> GameSession:
    # Validate access
    await assert_host_can_use_course(db, host, course_id)
    await assert_host_can_use_game(db, host, game_id)
    # The game row lock serializes room creation with image replace and delete (and with
    # T7's confirmation checks, which go right after the course check): a replace either
    # commits before this room exists or sees it live and is refused. It also refreshes the
    # Game the access check loaded, so a course move committed while we waited is seen.
    game = await lock_game(db, game_id)
    # Applies to admins too — so an unassigned game can't run until it's assigned.
    if game.course_id != course_id:
        raise ConflictError("This game belongs to a different course")

    # Enforce global room limit — count only LOBBY/IN_PROGRESS rooms.
    # COMPLETED/ABANDONED rooms may linger in Redis briefly for reconnection
    # but do not consume a concurrent-room slot.
    all_room_keys = await redis.keys("room:*")
    room_count = 0
    for key in all_room_keys:
        key_str = key if isinstance(key, str) else key.decode()
        if len(key_str) != 11:  # "room:" (5 chars) + 6-char code
            continue
        room_state_data = await state.get_room_state(redis, key_str[5:])
        if room_state_data and room_state_data.get("status") in (
            "LOBBY",
            "IN_PROGRESS",
        ):
            room_count += 1
    if room_count >= max_rooms:
        raise ConflictError(f"Maximum of {max_rooms} concurrent rooms reached")

    room_code = await generate_room_code(redis)

    session = GameSession(
        id=str(uuid.uuid4()),
        room_code=room_code,
        game_id=game_id,
        course_id=course_id,
        host_user_id=host.id,
        status="LOBBY",
        allow_guests=allow_guests,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)

    # Store room state in Redis
    await state.set_room_state(
        redis,
        room_code,
        {
            "session_id": session.id,
            "game_id": game_id,
            "course_id": course_id,
            "host_user_id": host.id,
            "status": "LOBBY",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    logger.info(
        "room_created",
        room_code=room_code,
        session_id=session.id,
        host_id=host.id,
        game_id=game_id,
        course_id=course_id,
    )
    return session


async def get_session_by_code(
    db: AsyncSession, redis: Redis, room_code: str
) -> tuple[GameSession, dict | None]:
    """
    Return (session, redis_state). redis_state is None if Redis has no record
    (session may still be in MySQL as COMPLETED/ABANDONED).
    """
    redis_state = await state.get_room_state(redis, room_code)

    result = await db.execute(
        select(GameSession)
        .where(GameSession.room_code == room_code)
        .options(selectinload(GameSession.game))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise NotFoundError(f"Room '{room_code}' not found or has expired")

    return session, redis_state


async def start_game(
    db: AsyncSession, redis: Redis, session: GameSession
) -> list[Question] | None:
    """Transition LOBBY → IN_PROGRESS. Returns the ordered question list, or None when a
    concurrent Start already moved the session to IN_PROGRESS ("already started"), which
    the caller ignores."""
    # Serialized with image and question writes and room creation for this game. The
    # reads after the lock are fresh: a plain read would reuse the snapshot taken before
    # the wait, or return the session object the caller loaded earlier, unrefreshed.
    await lock_game(db, session.game_id)
    session = (
        await db.execute(
            fresh(
                select(GameSession).where(GameSession.id == session.id), exclusive=True
            )
        )
    ).scalar_one()
    if session.status == "IN_PROGRESS":
        return None
    if session.status != "LOBBY":
        raise ConflictError(f"Cannot start a game in '{session.status}' state")

    # Sorted in Python, not by SQL: MySQL's sort fails for a question row over its 256 KB sort
    # buffer (T8 spec §4.6). (order_index, id) is the editor's order.
    result = await db.execute(
        fresh(select(Question).where(Question.game_id == session.game_id))
    )
    questions = sorted(result.scalars().all(), key=lambda q: (q.order_index, q.id))
    if not questions:
        raise ConflictError(
            "This game has no questions — add questions before starting"
        )

    session.status = "IN_PROGRESS"
    # Commit immediately so concurrent on_submit_answer handlers in separate DB
    # sessions see IN_PROGRESS rather than the unflushed LOBBY status.
    await db.commit()

    # Update Redis status
    room_state = await state.get_room_state(redis, session.room_code)
    if room_state:
        room_state["status"] = "IN_PROGRESS"
        await state.set_room_state(redis, session.room_code, room_state)

    logger.info("game_started", session_id=session.id, question_count=len(questions))
    return list(questions)


async def complete_game(db: AsyncSession, redis: Redis, session: GameSession) -> None:
    session.status = "COMPLETED"
    session.completed_at = datetime.now(timezone.utc)
    # Commit so player join attempts racing with game_over see COMPLETED.
    await db.commit()
    logger.info("game_completed", session_id=session.id)
    # Keep Redis state 10 more minutes for reconnection, then let it expire naturally


async def abandon_game(db: AsyncSession, redis: Redis, session: GameSession) -> None:
    session.status = "ABANDONED"
    session.completed_at = datetime.now(timezone.utc)
    await db.flush()
    await state.delete_room_state(redis, session.room_code, session.id)
    logger.info("game_abandoned", session_id=session.id)


# ---------------------------------------------------------------------------
# Player authorisation at join time
# ---------------------------------------------------------------------------


async def authorise_player(db: AsyncSession, user: User, session: GameSession) -> str:
    """
    Check whether the player is permitted to join this session.
    Returns the display name to use for the player.
    Raises ForbiddenError / NotFoundError if not authorised.
    """
    if user.role == "GUEST":
        # Backstop for guests who skip /auth/guest with a still-valid guest token.
        if not session.allow_guests:
            raise ForbiddenError(GUESTS_NOT_ALLOWED)
        return user.display_name or "Guest"

    if user.role == "ADMIN":
        return user.display_name or user.username or "Admin"

    # OAuth2 user — check course roster
    if user.netid:
        result = await db.execute(
            select(CourseRoster).where(
                CourseRoster.course_id == session.course_id,
                CourseRoster.netid == user.netid,
                CourseRoster.is_active == True,  # noqa: E712
            )
        )
        roster_entry = result.scalar_one_or_none()
        if roster_entry:
            return roster_entry.full_name  # first name from CSV

    # Local account with explicit PLAYER role for this course
    result = await db.execute(
        select(UserCourseAccess).where(
            UserCourseAccess.user_id == user.id,
            UserCourseAccess.course_id == session.course_id,
            UserCourseAccess.role == "PLAYER",
        )
    )
    if result.scalar_one_or_none():
        return user.display_name or user.username or "Player"

    # Local account with HOST role can also join as player
    result = await db.execute(
        select(UserCourseAccess).where(
            UserCourseAccess.user_id == user.id,
            UserCourseAccess.course_id == session.course_id,
            UserCourseAccess.role == "HOST",
        )
    )
    if result.scalar_one_or_none():
        return user.display_name or user.username or "Host"

    raise ForbiddenError("You are not enrolled in the course roster for this game")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def calculate_score(question: Question, answer_data: dict) -> ScoreResult:
    """
    Calculate points for a player's answer.
    COMPLETENESS: any answer = full points, no answer = 0
    ACCURACY:     points come from answer_data.answer_points per selected option
    """
    if not answer_data:
        return ScoreResult(points_awarded=0, is_correct=False)

    if question.grading_type == "COMPLETENESS":
        return ScoreResult(points_awarded=question.points_value, is_correct=True)

    # ACCURACY
    if question.type == "multiple_choice":
        selected = answer_data.get("selectedIndex")
        if selected is None:
            return ScoreResult(points_awarded=0, is_correct=False)
        pts_list: list[float] = question.answer_data.get("answer_points", [])
        if not (0 <= selected < len(pts_list)):
            return ScoreResult(points_awarded=0, is_correct=False)
        points = pts_list[selected]
        return ScoreResult(
            points_awarded=points,
            is_correct=(points == question.points_value),
        )

    if question.type == "true_false":
        selected = answer_data.get("selectedValue")
        if selected is None:
            return ScoreResult(points_awarded=0, is_correct=False)
        pts_map: dict = question.answer_data.get("answer_points", {})
        key = "true" if selected else "false"
        points = pts_map.get(key, 0)
        return ScoreResult(
            points_awarded=points,
            is_correct=(points == question.points_value),
        )

    if question.type == "fill_in_the_blank":
        text = " ".join((answer_data.get("text") or "").lower().split())
        if not text:
            return ScoreResult(points_awarded=0, is_correct=False)
        accepted = [
            " ".join(a.lower().split())
            for a in question.answer_data.get("acceptedAnswers", [])
        ]
        answer_pts: list[float] = question.answer_data.get("answerPoints", [])
        max_dist = int(question.answer_data.get("editDistance", 0))
        best: float = 0.0
        for i, a in enumerate(accepted):
            if Levenshtein.distance(text, a) <= max_dist:
                pts = answer_pts[i] if i < len(answer_pts) else question.points_value
                best = max(best, pts)
        return ScoreResult(
            points_awarded=best,
            is_correct=(best >= question.points_value),
        )

    if question.type == "multi_select":
        selected = answer_data.get("selectedIndices")
        if not isinstance(selected, list):
            return ScoreResult(points_awarded=0, is_correct=False)
        pts_list: list[float] = question.answer_data.get("answer_points", [])
        raw = sum(
            pts_list[i]
            for i in selected
            if isinstance(i, int) and 0 <= i < len(pts_list)
        )
        score = max(0.0, raw)
        return ScoreResult(
            points_awarded=score,
            is_correct=(score >= question.points_value and question.points_value > 0),
        )

    return ScoreResult(points_awarded=0, is_correct=False)


async def record_answer(
    db: AsyncSession,
    redis: Redis,
    session_id: str,
    user_id: str,
    question: Question,
    answer_data: dict,
    answer_time_ms: int | None,
) -> ScoreResult:
    """
    Score an answer, write it to MySQL immediately, and update Redis score.
    Returns the score result.
    """
    result = calculate_score(question, answer_data)

    # Persist to MySQL immediately (per-question persistence guarantee)
    score_row = SessionScore(
        session_id=session_id,
        user_id=user_id,
        question_id=question.id,
        points_awarded=result.points_awarded,
        answer_time_ms=answer_time_ms,
        is_correct=result.is_correct,
        answer_data=answer_data or None,
    )
    db.add(score_row)
    await db.flush()

    # Update Redis running score and answered set
    await state.update_player_score(redis, session_id, user_id, result.points_awarded)
    await state.mark_answered(redis, session_id, question.id, user_id)

    # Increment per-option distribution counter for the results bar chart
    dist_key: str | None = None
    if question.type == "multiple_choice":
        idx = answer_data.get("selectedIndex")
        if idx is not None:
            dist_key = str(idx)
    elif question.type == "true_false":
        val = answer_data.get("selectedValue")
        if val is not None:
            dist_key = "true" if val else "false"
    elif question.type == "fill_in_the_blank":
        text = " ".join((answer_data.get("text") or "").lower().split())
        if text:
            dist_key = text
    elif question.type == "multi_select":
        indices = answer_data.get("selectedIndices")
        if isinstance(indices, list):
            for idx in indices:
                if isinstance(idx, int):
                    await state.increment_answer_dist(
                        redis, session_id, question.id, str(idx)
                    )
    if dist_key is not None:
        await state.increment_answer_dist(redis, session_id, question.id, dist_key)

    return result


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


async def get_leaderboard(db: AsyncSession, session_id: str) -> list[dict]:
    """
    Aggregate session_scores from MySQL to produce a leaderboard.
    Used for QUESTION_RESULTS and GAME_OVER broadcasts.
    """
    from sqlalchemy import func as sqlfunc

    rows = await db.execute(
        select(
            SessionScore.user_id,
            sqlfunc.sum(SessionScore.points_awarded).label("total_score"),
        )
        .where(SessionScore.session_id == session_id)
        .group_by(SessionScore.user_id)
        .order_by(sqlfunc.sum(SessionScore.points_awarded).desc())
    )
    return [{"user_id": r.user_id, "score": float(r.total_score or 0)} for r in rows]


async def get_player_question_summary(
    db: AsyncSession, session_id: str, user_id: str
) -> list[dict]:
    """
    Per-question breakdown for a single player — used in GAME_OVER payload.
    """
    rows = await db.execute(
        select(
            Question.id.label("question_id"),
            Question.prompt,
            Question.points_value,
            Question.type.label("q_type"),
            Question.grading_type,
            Question.config,
            Question.answer_data.label("q_answer_data"),
            SessionScore.points_awarded,
            SessionScore.answer_time_ms,
            SessionScore.answer_data.label("player_answer"),
        )
        .join(GameSession, GameSession.game_id == Question.game_id)
        .outerjoin(
            SessionScore,
            (SessionScore.question_id == Question.id)
            & (SessionScore.session_id == session_id)
            & (SessionScore.user_id == user_id),
        )
        .where(GameSession.id == session_id)
        .order_by(Question.order_index)
    )

    result = []
    for r in rows:
        q_type = r.q_type
        grading_type = r.grading_type
        q_ans = r.q_answer_data or {}
        pts_val = r.points_value

        if grading_type == "COMPLETENESS":
            reveal: dict = {"type": "completeness"}
        elif q_type == "multiple_choice":
            pts = q_ans.get("answer_points", [])
            correct_indices = [i for i, p in enumerate(pts) if p >= pts_val]
            reveal = {"type": "multiple_choice", "correctIndices": correct_indices}
        elif q_type == "true_false":
            pts_map = q_ans.get("answer_points", {})
            correct_true = pts_map.get("true", 0) >= pts_val
            reveal = {"type": "true_false", "correctValue": correct_true}
        elif q_type == "fill_in_the_blank":
            reveal = {
                "type": "fill_in_the_blank",
                "acceptedAnswers": q_ans.get("acceptedAnswers", []),
                "editDistance": q_ans.get("editDistance", 0),
            }
        elif q_type == "multi_select":
            reveal = {
                "type": "multi_select",
                "answerPoints": q_ans.get("answer_points", []),
            }
        else:
            reveal = {}

        result.append(
            {
                "questionId": r.question_id,
                "prompt": r.prompt,
                "type": q_type,
                "gradingType": grading_type,
                "config": r.config or {},
                "pointsAwarded": r.points_awarded or 0,
                "maxPoints": pts_val,
                "answerTimeMs": r.answer_time_ms,
                "playerAnswer": r.player_answer,
                "answerReveal": reveal,
            }
        )

    return result


async def get_host_question_summary(
    db: AsyncSession, session_id: str, total_players: int
) -> list[dict]:
    """Per-question breakdown for the host game-over screen.
    Returns prompt, answer reveal, distribution, and correct/answered counts."""
    from collections import defaultdict

    rows = await db.execute(
        select(
            Question.id.label("question_id"),
            Question.order_index,
            Question.prompt,
            Question.type.label("q_type"),
            Question.grading_type,
            Question.config,
            Question.answer_data.label("q_answer_data"),
            Question.points_value,
            SessionScore.user_id,
            SessionScore.points_awarded,
            SessionScore.answer_data.label("player_answer"),
            SessionScore.answer_time_ms,
            SessionScore.is_correct,
        )
        .join(GameSession, GameSession.game_id == Question.game_id)
        .outerjoin(
            SessionScore,
            (SessionScore.question_id == Question.id)
            & (SessionScore.session_id == session_id),
        )
        .where(GameSession.id == session_id)
        .order_by(Question.order_index)
    )

    questions_meta: dict[int, dict] = {}
    questions_order: list[int] = []
    scores_by_q: dict[int, list[dict]] = defaultdict(list)

    for r in rows:
        qid = r.question_id
        if qid not in questions_meta:
            questions_meta[qid] = {
                "order_index": r.order_index,
                "prompt": r.prompt,
                "type": r.q_type,
                "grading_type": r.grading_type,
                "config": r.config or {},
                "answer_data": r.q_answer_data or {},
                "points_value": r.points_value,
            }
            questions_order.append(qid)
        if r.user_id is not None:
            scores_by_q[qid].append(
                {
                    "player_answer": r.player_answer,
                    "is_correct": bool(r.is_correct),
                    "answer_time_ms": r.answer_time_ms,
                }
            )

    result = []
    for qnum, qid in enumerate(questions_order, start=1):
        q = questions_meta[qid]
        scores = scores_by_q[qid]
        q_type = q["type"]
        q_ans = q["answer_data"]
        pts_val = q["points_value"]
        grading_type = q["grading_type"]

        if grading_type == "COMPLETENESS":
            reveal: dict = {"type": "completeness"}
        elif q_type == "multiple_choice":
            pts_list = q_ans.get("answer_points", [])
            reveal = {
                "type": "multiple_choice",
                "correctIndices": [i for i, p in enumerate(pts_list) if p >= pts_val],
            }
        elif q_type == "true_false":
            pts_map = q_ans.get("answer_points", {})
            reveal = {
                "type": "true_false",
                "correctValue": pts_map.get("true", 0) >= pts_val,
            }
        elif q_type == "fill_in_the_blank":
            reveal = {
                "type": "fill_in_the_blank",
                "acceptedAnswers": q_ans.get("acceptedAnswers", []),
                "editDistance": q_ans.get("editDistance", 0),
            }
        elif q_type == "multi_select":
            reveal = {
                "type": "multi_select",
                "answerPoints": q_ans.get("answer_points", []),
            }
        else:
            reveal = {}

        dist: dict[str, int] = {}
        correct_count = 0
        total_time = 0
        time_count = 0
        for s in scores:
            ans = s["player_answer"]
            if ans:
                if q_type == "multiple_choice":
                    idx = ans.get("selectedIndex")
                    if idx is not None:
                        k = str(idx)
                        dist[k] = dist.get(k, 0) + 1
                elif q_type == "true_false":
                    val = ans.get("selectedValue")
                    if val is not None:
                        k = "true" if val else "false"
                        dist[k] = dist.get(k, 0) + 1
                elif q_type == "fill_in_the_blank":
                    text = (ans.get("text") or "").strip().lower()
                    if text:
                        dist[text] = dist.get(text, 0) + 1
                elif q_type == "multi_select":
                    indices = ans.get("selectedIndices")
                    if isinstance(indices, list):
                        for idx in indices:
                            if isinstance(idx, int):
                                k = str(idx)
                                dist[k] = dist.get(k, 0) + 1
            if s["is_correct"]:
                correct_count += 1
            if s["answer_time_ms"] is not None:
                total_time += s["answer_time_ms"]
                time_count += 1

        result.append(
            {
                "questionId": qid,
                "questionNumber": qnum,
                "prompt": q["prompt"],
                "type": q_type,
                "gradingType": grading_type,
                "config": q["config"],
                "pointsValue": pts_val,
                "answerReveal": reveal,
                "answerDistribution": dist,
                "totalAnswered": len(scores),
                "totalPlayers": total_players,
                "correctCount": correct_count,
                "avgAnswerTimeMs": round(total_time / time_count)
                if time_count
                else None,
            }
        )

    return result
