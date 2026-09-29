from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.dependencies import get_current_user, require_user
from ..common.exceptions import ForbiddenError, NotFoundError
from ..config import settings
from ..database import get_db
from ..models.course import Course, UserCourseAccess
from ..models.game import Game, Question, UserGameAccess
from ..models.session import GameSession
from ..models.user import User
from ..redis_client import get_redis
from ..schemas.game import (
    ActiveSessionItem,
    MyCourseItem,
    MyGameItem,
    RoomCreateRequest,
    RoomCreateResponse,
    RoomInfoResponse,
)
from ..schemas.image import SessionImages
from ..services import game_service, image_service
from ..services import state_service as state

router = APIRouter(prefix="/game", tags=["game"])
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Host resource discovery
# ---------------------------------------------------------------------------


@router.get("/my-courses", response_model=list[MyCourseItem])
async def my_courses(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """
    Returns courses the authenticated user can host.
    ADMIN sees all courses; USER sees courses with HOST role in user_course_access.
    """
    if user.role == "ADMIN":
        result = await db.execute(
            select(Course).order_by(Course.semester.desc(), Course.name)
        )
        return [
            {"id": c.id, "name": c.name, "semester": c.semester, "role": "HOST"}
            for c in result.scalars().all()
        ]

    result = await db.execute(
        select(Course, UserCourseAccess)
        .join(UserCourseAccess, UserCourseAccess.course_id == Course.id)
        .where(
            UserCourseAccess.user_id == user.id,
            UserCourseAccess.role == "HOST",
        )
        .order_by(Course.semester.desc(), Course.name)
    )
    return [
        {"id": c.id, "name": c.name, "semester": c.semester, "role": uca.role}
        for c, uca in result.all()
    ]


@router.get("/my-games", response_model=list[MyGameItem])
async def my_games(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """
    Returns games the authenticated user can run and edit.
    ADMIN sees all games, including unassigned ones; USER sees games they hold a
    grant for on a course they HOST (so unassigned games never match).
    """
    query = (
        select(Game, Course.name, Course.semester)
        .outerjoin(Course, Course.id == Game.course_id)
        .order_by(Course.name, Course.semester, Game.title)
    )
    if user.role != "ADMIN":
        query = (
            query.join(UserGameAccess, UserGameAccess.game_id == Game.id)
            .join(UserCourseAccess, UserCourseAccess.course_id == Game.course_id)
            .where(
                UserGameAccess.user_id == user.id,
                UserCourseAccess.user_id == user.id,
                UserCourseAccess.role == "HOST",
            )
        )

    result = await db.execute(query)
    return [
        {
            "id": g.id,
            "title": g.title,
            "description": g.description,
            "max_players": g.max_players,
            "course_id": g.course_id,
            "course_name": course_name,
            "course_semester": course_semester,
        }
        for g, course_name, course_semester in result.all()
    ]


@router.get("/my-active-sessions", response_model=list[ActiveSessionItem])
async def my_active_sessions(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """Returns LOBBY/IN_PROGRESS sessions hosted by the current user."""
    result = await db.execute(
        select(GameSession)
        .where(
            GameSession.host_user_id == user.id,
            GameSession.status.in_(["LOBBY", "IN_PROGRESS"]),
        )
        .order_by(GameSession.created_at.desc())
    )
    sessions = result.scalars().all()

    rows = []
    for s in sessions:
        # Offer only rooms the socket would let this user rejoin as HOST.
        if not await game_service.user_can_host_session(db, user, s):
            continue
        game = await db.get(Game, s.game_id)
        course = await db.get(Course, s.course_id)
        rows.append(
            {
                "session_id": s.id,
                "room_code": s.room_code,
                "status": s.status,
                "game_title": game.title if game else "Unknown",
                "course_name": course.name if course else "Unknown",
                "course_semester": course.semester if course else "",
            }
        )
    return rows


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
) -> None:
    """Permanently delete a session and all associated scores. Admin, or the session's
    host while they still HOST its course."""
    session = await game_service.assert_can_delete_session(db, user, session_id)

    # Wipe Redis state (room key, player keys, question key, answered sets, dist hashes)
    await state.delete_room_state(redis, session.room_code, session_id)

    # Delete from MySQL — cascade="all, delete-orphan" removes session_scores automatically
    await db.delete(session)
    await db.commit()

    logger.info("session_deleted", session_id=session_id, deleted_by=user.id)


# ---------------------------------------------------------------------------
# Room management
# ---------------------------------------------------------------------------


@router.post("/rooms", response_model=RoomCreateResponse, status_code=201)
async def create_room(
    body: RoomCreateRequest,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
) -> dict:
    session = await game_service.create_room(
        db=db,
        redis=redis,
        host=user,
        game_id=body.game_id,
        course_id=body.course_id,
        max_rooms=settings.MAX_ROOMS,
        allow_guests=body.allow_guests,
    )
    # Commit before responding (T8): the host only receives a room code once the session is
    # visible to the image live guard, and the game row lock create_room took is released
    # promptly rather than after the response.
    await db.commit()
    return {"room_code": session.room_code, "session_id": session.id}


@router.get("/rooms/{room_code}/ping")
async def ping_room(
    room_code: str,
    redis=Depends(get_redis),
) -> dict:
    """Public endpoint — no auth required. Confirms a room is joinable."""
    from ..services.state_service import get_room_state
    from ..common.exceptions import NotFoundError as _NF, ConflictError as _CE

    state = await get_room_state(redis, room_code.upper())
    if not state:
        raise _NF(f"Room '{room_code}' not found")
    if state.get("status") not in ("LOBBY", "IN_PROGRESS"):
        raise _CE("This game has already ended")
    return {"status": state.get("status")}


@router.get("/rooms/{room_code}", response_model=RoomInfoResponse)
async def get_room(
    room_code: str,
    _: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
) -> dict:
    """Get session metadata for reconnection. Any authenticated user can call this."""
    session, _ = await game_service.get_session_by_code(db, redis, room_code.upper())

    # Fetch related records for the response
    game = await db.get(Game, session.game_id)
    course = await db.get(Course, session.course_id)

    q_count_result = await db.execute(
        select(Question).where(Question.game_id == session.game_id)
    )
    question_count = len(q_count_result.scalars().all())

    return {
        "session_id": session.id,
        "room_code": session.room_code,
        "status": session.status,
        "game_title": game.title if game else "Unknown",
        "course_id": session.course_id,
        "course_name": course.name if course else "Unknown",
        "course_semester": course.semester if course else "",
        "question_count": question_count,
    }


@router.get("/sessions/{session_id}/images", response_model=SessionImages)
async def session_images(
    session_id: str,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionImages:
    """The projector's link map (T8): full and small links for every image of the session's
    game. Needs both the session-host check and the current game rule —
    user_can_host_session alone checks only HOST on the course the session ran in, so a
    host who lost the game grant, or whose game moved course, would still get links."""
    session = await db.get(GameSession, session_id)
    if not session:
        raise NotFoundError(f"Session {session_id} not found")
    if not (
        await game_service.user_can_host_session(db, user, session)
        and await game_service.user_can_use_game(db, user, session.game_id)
    ):
        raise ForbiddenError("You do not have access to this session's images")
    # Doesn't depend on the Redis room: a projector refreshed late on the game-over screen
    # still gets links, and anyone passing both checks could read the library anyway.
    return await image_service.session_links(db, session, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Post-game guest reconciliation (host-facing)
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/guests")
async def list_session_guests(
    session_id: str,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    """List unmerged guest players from a completed session."""
    from ..models.session import SessionScore
    from ..models.user import User as UserModel

    session = await db.get(GameSession, session_id)
    if not session:
        raise NotFoundError(f"Session {session_id} not found")

    # Only the session host or an admin can see this
    if user.role != "ADMIN" and session.host_user_id != user.id:
        from ..common.exceptions import ForbiddenError

        raise ForbiddenError("Only the session host can view guest players")

    result = await db.execute(
        select(UserModel)
        .join(SessionScore, SessionScore.user_id == UserModel.id)
        .where(
            SessionScore.session_id == session_id,
            UserModel.role == "GUEST",
        )
        .distinct()
    )
    guests = result.scalars().all()
    return [
        {
            "id": g.id,
            "display_name": g.display_name,
            "email": g.email,
        }
        for g in guests
    ]


@router.post("/sessions/{session_id}/merge-guest", status_code=204)
async def merge_guest_for_session(
    session_id: str,
    body: dict,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Host-initiated guest merge. Delegates to the same logic as the admin endpoint."""
    from ..models.session import SessionScore
    from ..models.user import User as UserModel

    session = await db.get(GameSession, session_id)
    if not session:
        raise NotFoundError(f"Session {session_id} not found")
    if user.role != "ADMIN" and session.host_user_id != user.id:
        from ..common.exceptions import ForbiddenError

        raise ForbiddenError("Only the session host can merge guest players")

    guest_user_id: str = body.get("guest_user_id", "")
    target_netid: str = body.get("target_netid", "")

    if not guest_user_id or not target_netid:
        from ..common.exceptions import ConflictError

        raise ConflictError("guest_user_id and target_netid are required")

    guest = await db.get(UserModel, guest_user_id)
    if not guest or guest.role != "GUEST":
        raise NotFoundError(f"Guest user {guest_user_id} not found")

    real_result = await db.execute(
        select(UserModel).where(UserModel.netid == target_netid.lower())
    )
    real_user = real_result.scalar_one_or_none()
    if not real_user:
        raise NotFoundError(f"No user with netid '{target_netid}' found")

    scores_result = await db.execute(
        select(SessionScore).where(SessionScore.user_id == guest_user_id)
    )
    for score in scores_result.scalars().all():
        score.user_id = real_user.id

    await db.delete(guest)
    logger.info(
        "guest_merged_by_host",
        guest_id=guest_user_id,
        real_user_id=real_user.id,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Session score export
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/export")
async def export_session_scores(
    session_id: str,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Download a completed session's scores as raw CSV. Same download rule as
    /api/host/sessions/{id}/export, so this can't be a looser path around it."""
    await game_service.assert_can_download_session(db, user, session_id)

    from ..services.export_service import build_session_csv

    filename, content = await build_session_csv(db, session_id)

    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
