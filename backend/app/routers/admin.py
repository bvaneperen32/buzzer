from __future__ import annotations

import io
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.dependencies import require_admin
from ..common.exceptions import ConflictError, NotFoundError
from ..database import get_db
from ..models.course import Course, CourseRoster, UserCourseAccess
from ..models.game import Game, Question, UserGameAccess
from ..models.session import GameSession, SessionScore
from ..models.user import User
from ..schemas.admin import (
    AdminGameUpdate,
    AdminSessionItem,
    CourseAccessGrant,
    CourseCreate,
    CourseResponse,
    CourseUpdate,
    GameAccessGrant,
    GameCreateRequest,
    GameResponse,
    QuestionCreate,
    QuestionReorder,
    QuestionResponse,
    QuestionUpdate,
    RosterEntryPatch,
    RosterEntryResponse,
    RosterImportPayload,
    RosterUploadResult,
    UserCreate,
    UserResponse,
    UserUpdate,
    UserWithAccessResponse,
)
from ..services import game_management_service, game_service, roster_service
from ..services.auth_service import hash_password
from ..services.export_service import build_canvas_csv, build_session_csv
from ..services.report_service import build_session_report
from ..services.roster_service import process_roster_csv, process_roster_rows

router = APIRouter(prefix="/admin", tags=["admin"])
logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------


@router.get("/courses", response_model=list[CourseResponse])
async def list_courses(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[Course]:
    result = await db.execute(
        select(Course).order_by(Course.semester.desc(), Course.name)
    )
    return result.scalars().all()


@router.post("/courses", response_model=CourseResponse, status_code=201)
async def create_course(
    body: CourseCreate,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Course:
    course = Course(name=body.name, semester=body.semester)
    db.add(course)
    await db.flush()
    await db.refresh(course)
    await db.commit()
    logger.info("course_created", course_id=course.id, name=body.name)
    return course


@router.get("/courses/{course_id}", response_model=CourseResponse)
async def get_course(
    course_id: int,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Course:
    course = await db.get(Course, course_id)
    if not course:
        raise NotFoundError(f"Course {course_id} not found")
    return course


@router.put("/courses/{course_id}", response_model=CourseResponse)
async def update_course(
    course_id: int,
    body: CourseUpdate,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Course:
    course = await db.get(Course, course_id)
    if not course:
        raise NotFoundError(f"Course {course_id} not found")
    if body.name is not None:
        course.name = body.name
    if body.semester is not None:
        course.semester = body.semester
    return course


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


@router.get("/courses/{course_id}/roster", response_model=list[RosterEntryResponse])
async def list_roster(
    course_id: int,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CourseRoster]:
    return await roster_service.list_roster(db, course_id)


@router.post("/courses/{course_id}/roster", response_model=RosterUploadResult)
async def upload_roster(
    course_id: int,
    file: Annotated[UploadFile, File(description="Canvas gradebook CSV export")],
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RosterUploadResult:
    course = await db.get(Course, course_id)
    if not course:
        raise NotFoundError(f"Course {course_id} not found")
    content = await file.read()
    return await process_roster_csv(db, course_id, content)


@router.post("/courses/{course_id}/roster/import", response_model=RosterUploadResult)
async def import_roster_rows(
    course_id: int,
    payload: RosterImportPayload,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RosterUploadResult:
    course = await db.get(Course, course_id)
    if not course:
        raise NotFoundError(f"Course {course_id} not found")
    rows = [
        {"netid": r.netid, "full_name": r.full_name, "email": r.email}
        for r in payload.rows
    ]
    result = await process_roster_rows(db, course_id, rows)
    await db.commit()
    return result


@router.patch(
    "/courses/{course_id}/roster/{roster_id}", response_model=RosterEntryResponse
)
async def patch_roster_entry(
    course_id: int,
    roster_id: int,
    body: RosterEntryPatch,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CourseRoster:
    return await roster_service.patch_roster_entry(db, course_id, roster_id, body)


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


@router.get("/games", response_model=list[GameResponse])
async def list_games(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[Game]:
    result = await db.execute(select(Game).order_by(Game.title))
    return result.scalars().all()


@router.post("/games", response_model=GameResponse, status_code=201)
async def create_game(
    body: GameCreateRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Game:
    game = await game_management_service.create_game(
        db, meta=body, course_id=body.course_id, creator=admin
    )
    await db.commit()
    return game


@router.get("/games/{game_id}", response_model=GameResponse)
async def get_game(
    game_id: int,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Game:
    game = await db.get(Game, game_id)
    if not game:
        raise NotFoundError(f"Game {game_id} not found")
    return game


@router.put("/games/{game_id}", response_model=GameResponse)
async def update_game(
    game_id: int,
    body: AdminGameUpdate,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Game:
    game = await db.get(Game, game_id)
    if not game:
        raise NotFoundError(f"Game {game_id} not found")
    return await game_management_service.update_game(
        db, game, body, course_id=body.course_id
    )


@router.delete("/games/{game_id}", status_code=204)
async def delete_game(
    game_id: int,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    game = await db.get(Game, game_id)
    if not game:
        raise NotFoundError(f"Game {game_id} not found")
    # Nullify game_sessions references before delete — game_sessions.game_id is
    # NOT NULL so SQLAlchemy's default SET-NULL cascade would fail without this.
    from sqlalchemy import delete as sa_delete, select as sa_select
    from ..models.session import GameSession as _GS, SessionScore as _SS

    session_ids = (
        (await db.execute(sa_select(_GS.id).where(_GS.game_id == game_id)))
        .scalars()
        .all()
    )
    if session_ids:
        await db.execute(sa_delete(_SS).where(_SS.session_id.in_(session_ids)))
    await db.execute(sa_delete(_GS).where(_GS.game_id == game_id))
    await db.delete(game)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


@router.get("/games/{game_id}/questions", response_model=list[QuestionResponse])
async def list_questions(
    game_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[Question]:
    await game_service.assert_host_can_use_game(db, admin, game_id)
    return await game_management_service.list_questions(db, game_id)


@router.post(
    "/games/{game_id}/questions", response_model=QuestionResponse, status_code=201
)
async def create_question(
    game_id: int,
    body: QuestionCreate,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Question:
    await game_service.assert_host_can_use_game(db, admin, game_id)
    question = await game_management_service.create_question(db, game_id, body)
    await db.commit()
    return question


@router.put("/games/{game_id}/questions/{question_id}", response_model=QuestionResponse)
async def update_question(
    game_id: int,
    question_id: int,
    body: QuestionUpdate,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Question:
    await game_service.assert_host_can_use_game(db, admin, game_id)
    return await game_management_service.update_question(db, game_id, question_id, body)


@router.delete("/games/{game_id}/questions/{question_id}", status_code=204)
async def delete_question(
    game_id: int,
    question_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await game_service.assert_host_can_use_game(db, admin, game_id)
    await game_management_service.delete_question(db, game_id, question_id)


@router.post("/games/{game_id}/questions/reorder", status_code=204)
async def reorder_questions(
    game_id: int,
    body: QuestionReorder,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await game_service.assert_host_can_use_game(db, admin, game_id)
    await game_management_service.reorder_questions(db, game_id, body.order)


@router.get("/games/{game_id}/export")
async def export_game(
    game_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    game = await game_service.assert_host_can_use_game(db, admin, game_id)
    filename, content = await game_management_service.export_game(db, game)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/games/import", status_code=201)
async def import_game(
    file: Annotated[UploadFile, File(description="buzzer/game JSON bundle")],
    course_id: Annotated[int, Form(description="Course the imported game belongs to")],
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    game = await game_management_service.import_game(
        db, await file.read(), course_id=course_id, creator=admin
    )
    await db.commit()
    return {"game_id": game.id}


# ---------------------------------------------------------------------------
# Users (local accounts)
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[User]:
    result = await db.execute(
        select(User).where(User.role != "GUEST").order_by(User.role, User.username)
    )
    return result.scalars().all()


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreate,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    # Enforce unique username
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise ConflictError(f"Username '{body.username}' is already taken")

    user = User(
        id=str(uuid.uuid4()),
        username=body.username,
        display_name=body.display_name,
        email=str(body.email) if body.email else None,
        password_hash=hash_password(body.password),
        role="USER",
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    await db.commit()
    logger.info("local_user_created", user_id=user.id, username=body.username)
    return user


@router.get("/users/guests", response_model=list[UserResponse])
async def list_guests(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[User]:
    result = await db.execute(
        select(User).where(User.role == "GUEST").order_by(User.created_at.desc())
    )
    return result.scalars().all()


@router.get("/users/{user_id}", response_model=UserWithAccessResponse)
async def get_user(
    user_id: str,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError(f"User {user_id} not found")

    ca_result = await db.execute(
        select(UserCourseAccess).where(UserCourseAccess.user_id == user_id)
    )
    ga_result = await db.execute(
        select(UserGameAccess).where(UserGameAccess.user_id == user_id)
    )

    course_access = [
        {"course_id": ca.course_id, "role": ca.role} for ca in ca_result.scalars().all()
    ]
    game_access = [ga.game_id for ga in ga_result.scalars().all()]

    return {
        **{
            c: getattr(user, c)
            for c in [
                "id",
                "username",
                "netid",
                "display_name",
                "email",
                "role",
                "created_at",
                "last_login",
            ]
        },
        "course_access": course_access,
        "game_access": game_access,
    }


@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdate,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError(f"User {user_id} not found")
    if body.username is not None:
        existing = await db.execute(
            select(User).where(User.username == body.username, User.id != user_id)
        )
        if existing.scalar_one_or_none():
            raise ConflictError(f"Username '{body.username}' is already taken")
        user.username = body.username
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.email is not None:
        user.email = str(body.email)
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    if body.role is not None:
        user.role = body.role
    return user


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError(f"User {user_id} not found")
    if user.role == "ADMIN":
        raise ConflictError("Cannot delete an admin account")
    # Delete scores first — the session_scores.user_id column is NOT NULL so
    # SQLAlchemy's default SET-NULL cascade would fail without this.
    from sqlalchemy import delete as sa_delete
    from ..models.session import SessionScore

    await db.execute(sa_delete(SessionScore).where(SessionScore.user_id == user_id))
    await db.delete(user)


# ---------------------------------------------------------------------------
# Access management
# ---------------------------------------------------------------------------


@router.post("/users/{user_id}/course-access", status_code=204)
async def grant_course_access(
    user_id: str,
    body: CourseAccessGrant,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError(f"User {user_id} not found")
    course = await db.get(Course, body.course_id)
    if not course:
        raise NotFoundError(f"Course {body.course_id} not found")

    result = await db.execute(
        select(UserCourseAccess).where(
            UserCourseAccess.user_id == user_id,
            UserCourseAccess.course_id == body.course_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        if existing.role == "HOST" and body.role == "PLAYER":
            await game_service.revoke_game_grants_for_course(
                db, user_id, body.course_id
            )
        existing.role = body.role  # update role if access already exists
    else:
        db.add(
            UserCourseAccess(user_id=user_id, course_id=body.course_id, role=body.role)
        )


@router.delete("/users/{user_id}/course-access/{course_id}", status_code=204)
async def revoke_course_access(
    user_id: str,
    course_id: int,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    result = await db.execute(
        select(UserCourseAccess).where(
            UserCourseAccess.user_id == user_id,
            UserCourseAccess.course_id == course_id,
        )
    )
    access = result.scalar_one_or_none()
    if not access:
        raise NotFoundError(
            f"No course access record for user {user_id} / course {course_id}"
        )
    if access.role == "HOST":
        await game_service.revoke_game_grants_for_course(db, user_id, course_id)
    await db.delete(access)


@router.post("/users/{user_id}/game-access", status_code=204)
async def grant_game_access(
    user_id: str,
    body: GameAccessGrant,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError(f"User {user_id} not found")
    game = await db.get(Game, body.game_id)
    if not game:
        raise NotFoundError(f"Game {body.game_id} not found")
    # A grant only takes effect alongside HOST on the game's course, so refuse
    # one that would be dead on arrival.
    if game.course_id is None:
        raise ConflictError("Assign this game to a course before granting access")
    if not await game_service.has_host_grant(db, user_id, game.course_id):
        raise ConflictError(
            "User must be a HOST of this game's course before being granted the game"
        )

    result = await db.execute(
        select(UserGameAccess).where(
            UserGameAccess.user_id == user_id,
            UserGameAccess.game_id == body.game_id,
        )
    )
    if not result.scalar_one_or_none():
        db.add(UserGameAccess(user_id=user_id, game_id=body.game_id))


@router.delete("/users/{user_id}/game-access/{game_id}", status_code=204)
async def revoke_game_access(
    user_id: str,
    game_id: int,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    result = await db.execute(
        select(UserGameAccess).where(
            UserGameAccess.user_id == user_id,
            UserGameAccess.game_id == game_id,
        )
    )
    access = result.scalar_one_or_none()
    if not access:
        raise NotFoundError(
            f"No game access record for user {user_id} / game {game_id}"
        )
    await db.delete(access)


# ---------------------------------------------------------------------------
# Guest merge
# ---------------------------------------------------------------------------


@router.post("/users/merge-guest", status_code=204)
async def merge_guest(
    body: dict,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    guest_user_id: str = body.get("guest_user_id", "")
    target_netid: str = body.get("target_netid", "")

    if not guest_user_id or not target_netid:
        raise ConflictError("guest_user_id and target_netid are required")

    guest = await db.get(User, guest_user_id)
    if not guest or guest.role != "GUEST":
        raise NotFoundError(f"Guest user {guest_user_id} not found")

    normalized_netid = target_netid.strip().lower()

    # Find the real user by netid
    result = await db.execute(select(User).where(User.netid == normalized_netid))
    real_user = result.scalar_one_or_none()

    from ..models.session import SessionScore

    if real_user:
        # Real user already has an account — re-attribute scores and delete the guest record
        scores_result = await db.execute(
            select(SessionScore).where(SessionScore.user_id == guest_user_id)
        )
        for score in scores_result.scalars().all():
            score.user_id = real_user.id
        await db.delete(guest)
        logger.info("guest_merged", guest_id=guest_user_id, real_user_id=real_user.id)
    else:
        # No account yet for this netid (student hasn't logged in via OAuth2) —
        # promote the guest record in-place so scores are retained without re-attribution
        guest.netid = normalized_netid
        guest.role = "USER"
        logger.info("guest_promoted", guest_id=guest_user_id, netid=normalized_netid)


# ---------------------------------------------------------------------------
# Sessions (admin view)
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=list[AdminSessionItem])
async def list_sessions(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(
        None, description="Filter by status (LOBBY, IN_PROGRESS, COMPLETED, ABANDONED)"
    ),
) -> list[dict]:
    """List all game sessions across all hosts, newest first."""
    query = select(GameSession).order_by(GameSession.created_at.desc())
    if status:
        query = query.where(GameSession.status == status)
    result = await db.execute(query)
    sessions = result.scalars().all()

    rows = []
    for s in sessions:
        game = await db.get(Game, s.game_id)
        course = await db.get(Course, s.course_id)
        host = await db.get(User, s.host_user_id) if s.host_user_id else None

        count_result = await db.execute(
            select(func.count(SessionScore.user_id.distinct())).where(
                SessionScore.session_id == s.id
            )
        )
        player_count = count_result.scalar_one() or 0

        rows.append(
            {
                "session_id": s.id,
                "room_code": s.room_code,
                "status": s.status,
                "game_id": s.game_id,
                "game_title": game.title if game else "Unknown",
                "course_id": s.course_id,
                "course_name": course.name if course else "Unknown",
                "course_semester": course.semester if course else "",
                "host_display_name": (host.display_name or host.username or host.netid)
                if host
                else None,
                "created_at": s.created_at,
                "completed_at": s.completed_at,
                "player_count": player_count,
            }
        )
    return rows


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    format: str = Query("canvas", description="'canvas' or 'raw'"),
    title: str | None = Query(
        None, description="Assignment column title (Canvas format)"
    ),
    sis_domain: str = Query(
        "", description="Domain appended to netid for SIS Login ID (e.g. wisc.edu)"
    ),
    roster_only: bool = Query(
        True, description="Exclude players without a netid (guests, local accounts)"
    ),
    per_question: bool = Query(
        False, description="One column per question instead of total"
    ),
) -> StreamingResponse:
    """Download session scores as CSV. Supports Canvas gradebook import format."""
    session = await db.get(GameSession, session_id)
    if not session:
        raise NotFoundError(f"Session {session_id} not found")

    if format == "canvas":
        filename, content = await build_canvas_csv(
            db,
            session_id,
            assignment_title=title,
            sis_domain=sis_domain,
            roster_only=roster_only,
            per_question=per_question,
        )
    else:
        filename, content = await build_session_csv(db, session_id)

    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sessions/{session_id}/report")
async def session_report(
    session_id: str,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Download a standalone HTML report for a session (aggregate stats, no PII)."""
    session = await db.get(GameSession, session_id)
    if not session:
        raise NotFoundError(f"Session {session_id} not found")
    filename, content = await build_session_report(db, session_id)
    return Response(
        content=content,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
