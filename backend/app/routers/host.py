"""
/api/host/* — host management: authoring games and questions, rosters, and
completed-session downloads, for the courses a host HOSTs.

Every endpoint takes a real account (require_user) and then applies the course,
game, or download rule from game_service. Those rules let ADMIN through, so admins
do host work here too. Game and question writes go through
game_management_service, which the admin router shares.

Deliberately absent: game delete (admin only), course and game listing (use
/api/game/my-courses and /api/game/my-games), and session delete (the existing
DELETE /api/game/sessions/{id}).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.dependencies import require_user
from ..database import get_db
from ..models.course import CourseRoster
from ..models.game import Game, Question
from ..models.user import User
from ..redis_client import get_redis
from ..schemas.admin import (
    AdminSessionItem,
    GameCreateRequest,
    GameResponse,
    GameUpdate,
    QuestionCreate,
    QuestionReorder,
    QuestionResponse,
    QuestionUpdate,
    RosterEntryPatch,
    RosterEntryResponse,
    RosterImportPayload,
    RosterUploadResult,
)
from ..schemas.image import DescriptionPatch, ImageItem, LibraryResponse
from ..services import (
    bundle_service,
    game_management_service,
    game_service,
    image_service,
    roster_service,
)
from ..services.export_service import build_canvas_csv, build_session_csv
from ..services.report_service import build_session_report

router = APIRouter(prefix="/host", tags=["host"])

# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


@router.post("/games", response_model=GameResponse, status_code=201)
async def create_game(
    body: GameCreateRequest,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Game:
    await game_service.assert_host_can_use_course(db, user, body.course_id)
    game = await game_management_service.create_game(
        db, meta=body, course_id=body.course_id, creator=user
    )
    await db.commit()
    return game


# Declared before the /games/{game_id} routes so "import" is never read as an id.
@router.post("/games/import", status_code=201)
async def import_game(
    file: Annotated[UploadFile, File(description="buzzer/game JSON bundle")],
    course_id: Annotated[int, Form(description="Course the imported game belongs to")],
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    # Access first: the file isn't read or parsed for a course the user can't use.
    await game_service.assert_host_can_use_course(db, user, course_id)
    raw = await image_service.read_capped(file, bundle_service.IMPORT_LIMIT_BYTES)
    game = await game_management_service.import_game(
        db, raw, course_id=course_id, creator=user
    )
    await db.commit()
    return {"game_id": game.id}


@router.get("/games/{game_id}", response_model=GameResponse)
async def get_game(
    game_id: int,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Game:
    return await game_service.assert_host_can_use_game(db, user, game_id)


@router.put("/games/{game_id}", response_model=GameResponse)
async def update_game(
    game_id: int,
    body: GameUpdate,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Game:
    # GameUpdate forbids extra fields, so a course_id here is a 422: only admins
    # move games between courses (PUT /api/admin/games/{id}).
    game = await game_service.assert_host_can_use_game(db, user, game_id)
    return await game_management_service.update_game(db, game, body)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


@router.get("/games/{game_id}/questions", response_model=list[QuestionResponse])
async def list_questions(
    game_id: int,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[Question]:
    await game_service.assert_host_can_use_game(db, user, game_id)
    return await game_management_service.list_questions(db, game_id)


@router.post(
    "/games/{game_id}/questions", response_model=QuestionResponse, status_code=201
)
async def create_question(
    game_id: int,
    body: QuestionCreate,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Question:
    await game_service.assert_host_can_use_game(db, user, game_id)
    question = await game_management_service.create_question(db, game_id, body)
    await db.commit()
    return question


@router.put("/games/{game_id}/questions/{question_id}", response_model=QuestionResponse)
async def update_question(
    game_id: int,
    question_id: int,
    body: QuestionUpdate,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Question:
    await game_service.assert_host_can_use_game(db, user, game_id)
    return await game_management_service.update_question(db, game_id, question_id, body)


@router.delete("/games/{game_id}/questions/{question_id}", status_code=204)
async def delete_question(
    game_id: int,
    question_id: int,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await game_service.assert_host_can_use_game(db, user, game_id)
    await game_management_service.delete_question(db, game_id, question_id)


@router.post("/games/{game_id}/questions/reorder", status_code=204)
async def reorder_questions(
    game_id: int,
    body: QuestionReorder,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await game_service.assert_host_can_use_game(db, user, game_id)
    await game_management_service.reorder_questions(db, game_id, body.order)


@router.get("/games/{game_id}/export")
async def export_game(
    game_id: int,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    game = await game_service.assert_host_can_use_game(db, user, game_id)
    filename, content = await game_management_service.export_game(db, game)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Image library (T8). Every endpoint applies the game rule; an image of another
# game is a 404. The writes commit before responding, so a commit failure is an
# error response rather than arriving after a success.
# ---------------------------------------------------------------------------


@router.get("/games/{game_id}/images", response_model=LibraryResponse)
async def list_images(
    game_id: int,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
) -> LibraryResponse:
    await game_service.assert_host_can_use_game(db, user, game_id)
    return await image_service.list_library(
        db, redis, game_id, datetime.now(timezone.utc)
    )


@router.post("/games/{game_id}/images", response_model=ImageItem, status_code=201)
async def upload_image(
    game_id: int,
    file: Annotated[UploadFile, File(description="PNG, JPEG, WebP or GIF image")],
    description: Annotated[str, Form(description="What the image shows (alt text)")],
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ImageItem:
    # FastAPI has already received the body (nginx limits its size); nothing below reads
    # the spooled file until access and the cheap checks have passed.
    await game_service.assert_host_can_use_game(db, user, game_id)
    cleaned = image_service.clean_description(description)
    await image_service.precheck_library(db, game_id)
    processed = await image_service.process_upload(file)
    image = await image_service.add_image(
        db, game_id, processed, image_service.clean_filename(file.filename), cleaned
    )
    await db.commit()
    return await image_service.library_item(
        db, game_id, image, datetime.now(timezone.utc)
    )


@router.put("/games/{game_id}/images/{image_id}", response_model=ImageItem)
async def replace_image(
    game_id: int,
    image_id: int,
    file: Annotated[UploadFile, File(description="PNG, JPEG, WebP or GIF image")],
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
) -> ImageItem:
    await game_service.assert_host_can_use_game(db, user, game_id)
    processed = await image_service.process_upload(file)
    image = await image_service.replace_image(
        db,
        redis,
        game_id,
        image_id,
        processed,
        image_service.clean_filename(file.filename),
    )
    await db.commit()
    return await image_service.library_item(
        db, game_id, image, datetime.now(timezone.utc)
    )


@router.patch("/games/{game_id}/images/{image_id}", response_model=ImageItem)
async def edit_image_description(
    game_id: int,
    image_id: int,
    body: DescriptionPatch,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ImageItem:
    await game_service.assert_host_can_use_game(db, user, game_id)
    image = await image_service.set_description(
        db, game_id, image_id, image_service.clean_description(body.description)
    )
    await db.commit()
    return await image_service.library_item(
        db, game_id, image, datetime.now(timezone.utc)
    )


@router.delete("/games/{game_id}/images/{image_id}", status_code=204)
async def delete_image(
    game_id: int,
    image_id: int,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis=Depends(get_redis),
) -> None:
    await game_service.assert_host_can_use_game(db, user, game_id)
    await image_service.delete_image(db, redis, game_id, image_id)
    await db.commit()


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


@router.get("/courses/{course_id}/roster", response_model=list[RosterEntryResponse])
async def list_roster(
    course_id: int,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CourseRoster]:
    await game_service.assert_host_can_use_course(db, user, course_id)
    return await roster_service.list_roster(db, course_id)


@router.post("/courses/{course_id}/roster/import", response_model=RosterUploadResult)
async def import_roster_rows(
    course_id: int,
    payload: RosterImportPayload,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RosterUploadResult:
    await game_service.assert_host_can_use_course(db, user, course_id)
    rows = [
        {"netid": r.netid, "full_name": r.full_name, "email": r.email}
        for r in payload.rows
    ]
    result = await roster_service.process_roster_rows(db, course_id, rows)
    await db.commit()
    return result


@router.patch(
    "/courses/{course_id}/roster/{roster_id}", response_model=RosterEntryResponse
)
async def patch_roster_entry(
    course_id: int,
    roster_id: int,
    body: RosterEntryPatch,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CourseRoster:
    await game_service.assert_host_can_use_course(db, user, course_id)
    return await roster_service.patch_roster_entry(db, course_id, roster_id, body)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=list[AdminSessionItem])
async def list_sessions(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(
        None, description="Admins only (ignored otherwise): filter by status"
    ),
) -> list[dict]:
    """Admins: every session. Hosts: the COMPLETED sessions they hosted on courses
    they still HOST."""
    return await game_service.list_sessions_for(db, user, status)


@router.get("/sessions/{session_id}/report")
async def session_report(
    session_id: str,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Download a completed session's standalone HTML report."""
    session = await game_service.assert_can_download_session(db, user, session_id)
    # Downloading needs only the session check, but the report shows the game's *current*
    # images: embed them only for someone who still passes the game rule (T8 spec §2.14).
    filename, content = await build_session_report(
        db,
        session_id,
        include_images=await game_service.user_can_use_game(db, user, session.game_id),
    )
    return Response(
        content=content,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    user: Annotated[User, Depends(require_user)],
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
    """Download a completed session's scores as CSV (Canvas gradebook or raw)."""
    await game_service.assert_can_download_session(db, user, session_id)

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
