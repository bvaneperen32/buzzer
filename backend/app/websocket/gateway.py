"""
Phase 5 — python-socketio WebSocket gateway.

Handles all real-time game communication between Host and Player clients.

Architecture notes:
- Each game room maps to a socket.io room named by its 6-char room code.
- Host sockets additionally join a "{room_code}:host" room for host-only events.
- Each player's socket joins a "user:{user_id}" room so the server can send
  personalised payloads (e.g. their score/rank) without exposing other players'
  data to the whole room.
- In-memory dicts track per-sid context, question timers, and host-abandon tasks.
  These are per-process; with the Redis manager, socket.io routes events correctly
  across instances while local task bookkeeping stays on the owning process.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import socketio
import structlog

from ..config import settings
from ..database import AsyncSessionLocal
from ..models.game import Question
from ..models.session import GameSession
from ..redis_client import get_redis
from ..services import game_service, image_service
from ..services import state_service as state
from ..services.auth_service import get_user_by_id
from . import events as E
from .middleware import authenticate_socket

logger = structlog.get_logger()

_HOST_GRACE_SECONDS = 300  # 5-minute host-disconnect grace period

# For the session's recorded host after an admin has revoked their HOST grant.
_HOST_ACCESS_LOST = "You no longer have HOST access to this course"

# ---------------------------------------------------------------------------
# Server instantiation
# ---------------------------------------------------------------------------

# Use the Redis pubsub manager only in production (multi-instance scaling).
# In development the in-memory manager is simpler and more reliable — the
# AsyncRedisManager's pubsub listener drops connections under idle load, which
# silently breaks room-targeted emits.
_client_manager = (
    None if settings.is_development else socketio.AsyncRedisManager(settings.REDIS_URL)
)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=settings.cors_origins_list,
    client_manager=_client_manager,
    logger=False,
    engineio_logger=False,
)
socket_app = socketio.ASGIApp(sio, socketio_path="socket.io")

# ---------------------------------------------------------------------------
# Per-process in-memory state
# ---------------------------------------------------------------------------

# sid → {user_id, role, room_code, session_id}
_sid_ctx: dict[str, dict[str, Any]] = {}

# session_id → asyncio.Task (host disconnect abandon countdown)
_abandon_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]

# session_id → asyncio.Task (question time-limit countdown)
_timer_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _db():
    """Async DB session with auto-commit / auto-rollback."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _host_room(room_code: str) -> str:
    return f"{room_code}:host"


def _user_room(user_id: str) -> str:
    return f"user:{user_id}"


async def _question_payload(db, q: Question, number: int, total: int) -> dict:
    """Build the client-safe NEW_QUESTION payload (no answer_data).

    `images` carries small links for the question's option images only (T8): it goes to
    every phone, and phones never get prompt or canvas images. `config` passes through
    unchanged, so a phone treats an option id missing from `images` as no image.
    """
    payload: dict = {
        "questionId": q.id,
        "type": q.type,
        "gradingType": q.grading_type,
        "prompt": q.prompt,
        "config": q.config,
        "timeLimitSeconds": q.time_limit_seconds,
        "pointsValue": q.points_value,
        "orderIndex": q.order_index,
        "questionNumber": number,
        "totalQuestions": total,
    }
    if q.type == "fill_in_the_blank":
        payload["editDistance"] = q.answer_data.get("editDistance", 0)
    payload["images"] = await image_service.question_player_images(
        db, q, datetime.now(timezone.utc)
    )
    return payload


async def _host_question_payload(db, current_q: dict | None) -> dict | None:
    """Convert the compact Redis current-question record into a full client payload."""
    if not current_q:
        return None
    question = await db.get(Question, current_q["question_id"])
    if not question:
        return None
    payload = await _question_payload(
        db, question, current_q["question_number"], current_q["total_questions"]
    )
    payload["startedAt"] = current_q[
        "started_at"
    ]  # ISO timestamp so client can compute remaining time
    return payload


def _answer_reveal(q: Question) -> dict:
    """
    Derive a client-safe correct-answer reveal from answer_data.
    answer_data is NEVER forwarded directly — only derived facts are sent.
    """
    if q.grading_type == "COMPLETENESS":
        return {"type": "completeness"}
    if q.type == "multiple_choice":
        pts: list[int] = q.answer_data.get("answer_points", [])
        correct = [i for i, p in enumerate(pts) if p >= q.points_value]
        return {"type": "multiple_choice", "correctIndices": correct}
    if q.type == "true_false":
        pts_map: dict = q.answer_data.get("answer_points", {})
        correct_true = pts_map.get("true", 0) >= q.points_value
        return {"type": "true_false", "correctValue": correct_true}
    if q.type == "fill_in_the_blank":
        return {
            "type": "fill_in_the_blank",
            "acceptedAnswers": q.answer_data.get("acceptedAnswers", []),
            "editDistance": q.answer_data.get("editDistance", 0),
        }
    if q.type == "multi_select":
        return {
            "type": "multi_select",
            "answerPoints": q.answer_data.get("answer_points", []),
        }
    return {}


async def _emit_error(sid: str, message: str) -> None:
    await sio.emit(E.ERROR, {"message": message}, to=sid)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def _question_timer_task(
    session_id: str,
    room_code: str,
    question_id: int,
    seconds: float,
) -> None:
    """Fire ANSWER_PHASE_ENDED to the host when the question time limit expires,
    then auto-lock the question after a short pause."""
    try:
        await asyncio.sleep(seconds)
        redis = await get_redis()
        answered = await state.get_answered_count(redis, session_id, question_id)
        total = await state.get_player_count(redis, session_id)
        await sio.emit(
            E.ANSWER_PHASE_ENDED,
            {
                "questionId": question_id,
                "answeredCount": answered,
                "totalPlayers": total,
            },
            to=_host_room(room_code),
        )
        logger.info(
            "question_timer_expired", session_id=session_id, question_id=question_id
        )

        # Brief pause so any in-flight answers can land, then auto-lock
        await asyncio.sleep(0.5)
        room_state = await state.get_room_state(redis, room_code)
        if (
            room_state
            and room_state.get("question_phase") == "QUESTION"
            and not room_state.get("question_locked")
        ):
            current_q = await state.get_current_question(redis, session_id)
            if current_q and current_q["question_id"] == question_id:
                room_state["question_locked"] = True
                room_state["timer_remaining_seconds"] = 0.0
                await state.set_room_state(redis, room_code, room_state)
                await sio.emit(
                    E.QUESTION_LOCKED,
                    {"questionId": question_id, "remainingSeconds": 0.0},
                    to=room_code,
                )
                logger.info(
                    "question_auto_locked",
                    session_id=session_id,
                    question_id=question_id,
                )
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception(
            "question_timer_task_error", session_id=session_id, question_id=question_id
        )
    finally:
        # Only remove our own entry. task.cancel() is async — the CancelledError
        # is delivered at the next yield, which may be after a replacement task has
        # already been stored under the same session_id. A blind pop() here would
        # evict the new task, making it impossible to cancel later and allowing
        # this timer to fire ANSWER_PHASE_ENDED for the wrong question.
        if _timer_tasks.get(session_id) is asyncio.current_task():
            _timer_tasks.pop(session_id, None)


async def _auto_unlock_for_host(
    redis, session_id: str, room_code: str, room_state: dict
) -> None:
    """Unlock a question that was auto-locked because the host disconnected.
    Only fires when room_state carries question_locked_by_disconnect=True.
    Mutates room_state in-place and persists it."""
    if not room_state.get("question_locked_by_disconnect"):
        return

    remaining: float = room_state.pop("timer_remaining_seconds", 0.0)
    room_state.pop("question_locked_by_disconnect", None)
    room_state["question_locked"] = False

    current_q = await state.get_current_question(redis, session_id)

    if current_q and remaining > 0:
        virtual_start = datetime.now(timezone.utc) - timedelta(
            seconds=current_q["time_limit_seconds"] - remaining
        )
        await state.set_current_question(
            redis,
            session_id,
            current_q["question_id"],
            virtual_start,
            current_q["time_limit_seconds"],
            current_q["question_number"],
            current_q["total_questions"],
        )
        task = asyncio.ensure_future(
            _question_timer_task(
                session_id, room_code, current_q["question_id"], remaining
            )
        )
        _timer_tasks[session_id] = task

    await state.set_room_state(redis, room_code, room_state)

    if current_q:
        await sio.emit(
            E.QUESTION_UNLOCKED,
            {"questionId": current_q["question_id"], "remainingSeconds": remaining},
            to=room_code,
        )
        logger.info(
            "question_auto_unlocked_host_reconnect",
            session_id=session_id,
            remaining_seconds=remaining,
        )


async def _host_abandon_task(session_id: str, room_code: str) -> None:
    """Abandon the game if the host does not reconnect within the grace period."""
    try:
        await asyncio.sleep(_HOST_GRACE_SECONDS)
        async with _db() as db:
            redis = await get_redis()
            session = await db.get(GameSession, session_id)
            if session and session.status == "IN_PROGRESS":
                await game_service.abandon_game(db, redis, session)
                await sio.emit(E.GAME_ABANDONED, {}, to=room_code)
                logger.warning("game_abandoned_host_timeout", session_id=session_id)
    except asyncio.CancelledError:
        pass
    finally:
        _abandon_tasks.pop(session_id, None)


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


def _client_ip(environ: dict) -> str:
    """Extract real client IP from the environ python-socketio passes to connect handlers.

    python-socketio uses a WSGI-style environ dict (HTTP_* headers, REMOTE_ADDR)
    even in ASGI mode. Fall back to ASGI scope fields in case that changes.
    """
    # WSGI-style: nginx sets X-Real-IP which becomes HTTP_X_REAL_IP
    for key in ("HTTP_X_FORWARDED_FOR", "HTTP_X_REAL_IP"):
        val = environ.get(key, "")
        if val:
            return val.split(",")[0].strip()
    # WSGI-style direct address
    remote = environ.get("REMOTE_ADDR", "")
    if remote:
        return remote
    # ASGI scope fallback: headers as list of (bytes, bytes)
    for name, value in environ.get("headers", []):
        name_b = name if isinstance(name, bytes) else name.encode()
        if name_b.lower() in (b"x-forwarded-for", b"x-real-ip"):
            val = value.decode() if isinstance(value, bytes) else value
            return val.split(",")[0].strip()
    # ASGI scope fallback: client tuple
    client = environ.get("client")
    if client:
        return client[0]
    return "unknown"


_WS_RATE_LIMIT = 10  # max connections per window
_WS_RATE_WINDOW = 10  # seconds


@sio.event
async def connect(sid: str, _environ: dict, auth: dict | None) -> bool | None:
    """
    Authenticate the connecting socket.  The client must pass a valid JWT:
        const socket = io({ auth: { token: accessToken } });
    Returns False to reject the connection.
    """
    # Rate-limit WebSocket connections per IP using Redis (production only —
    # in dev all traffic shares a Docker-internal IP, which would trip the limit).
    # Bypass: if STRESS_TEST_KEY is set and the auth dict carries a matching
    # stress_key, skip rate limiting for this connection.
    if not settings.is_development:
        _stress_bypass = (
            bool(settings.STRESS_TEST_KEY)
            and isinstance(auth, dict)
            and auth.get("stress_key") == settings.STRESS_TEST_KEY
        )
        if not _stress_bypass:
            try:
                ip = _client_ip(_environ)
                redis = await get_redis()
                rate_key = f"ws_rate:{ip}"
                count = await redis.incr(rate_key)
                if count == 1:
                    await redis.expire(rate_key, _WS_RATE_WINDOW)
                if count > _WS_RATE_LIMIT:
                    logger.warning("socket_rate_limited", ip=ip, count=count)
                    return False
            except Exception:
                # Redis unavailable — skip rate limit and let JWT auth decide.
                logger.warning(
                    "ws_rate_limit_redis_error",
                    reason="Redis unavailable, skipping rate limit",
                )

    async with _db() as db:
        try:
            user = await authenticate_socket(auth, db)
        except ValueError as exc:
            logger.warning("socket_auth_failed", reason=str(exc))
            return False

    await sio.save_session(sid, {"user_id": user.id})
    logger.info("socket_connected", sid=sid, user_id=user.id)


@sio.event
async def disconnect(sid: str) -> None:
    ctx = _sid_ctx.pop(sid, None)
    if not ctx:
        return

    room_code = ctx["room_code"]
    session_id = ctx["session_id"]
    user_id = ctx["user_id"]
    role = ctx["role"]

    redis = await get_redis()

    if role == "HOST":
        room_state = await state.get_room_state(redis, room_code)
        if room_state and room_state.get("status") == "IN_PROGRESS":
            # Auto-lock the active question so the timer pauses while the host is away
            if room_state.get("question_phase") == "QUESTION" and not room_state.get(
                "question_locked"
            ):
                current_q = await state.get_current_question(redis, session_id)
                if current_q:
                    started_at = datetime.fromisoformat(current_q["started_at"])
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                    remaining = max(0.0, current_q["time_limit_seconds"] - elapsed)

                    timer = _timer_tasks.pop(session_id, None)
                    if timer:
                        timer.cancel()

                    room_state["question_locked"] = True
                    room_state["timer_remaining_seconds"] = remaining
                    room_state["question_locked_by_disconnect"] = True
                    await state.set_room_state(redis, room_code, room_state)

                    await sio.emit(
                        E.QUESTION_LOCKED,
                        {
                            "questionId": current_q["question_id"],
                            "remainingSeconds": remaining,
                        },
                        to=room_code,
                    )
                    logger.info(
                        "question_auto_locked_host_disconnect",
                        session_id=session_id,
                        remaining_seconds=remaining,
                    )

            abandon_at = (
                datetime.now(timezone.utc) + timedelta(seconds=_HOST_GRACE_SECONDS)
            ).isoformat()
            await sio.emit(E.HOST_DISCONNECTED, {"abandonAt": abandon_at}, to=room_code)
            task = asyncio.create_task(_host_abandon_task(session_id, room_code))
            _abandon_tasks[session_id] = task
            logger.warning("host_disconnected_grace_started", session_id=session_id)
    else:
        await state.set_player_connected(redis, session_id, user_id, False)
        player_count = await state.get_player_count(redis, session_id)
        await sio.emit(
            E.PLAYER_LEFT,
            {"userId": user_id, "playerCount": player_count},
            to=_host_room(room_code),
        )
        logger.info("player_disconnected", user_id=user_id, room=room_code)


# ---------------------------------------------------------------------------
# JOIN_ROOM — initial join
# ---------------------------------------------------------------------------


@sio.on(E.JOIN_ROOM)
async def on_join_room(sid: str, data: dict) -> None:
    """
    data = {"room_code": str, "role": "HOST" | "PLAYER"}
    Validates access, registers the socket in the appropriate rooms, and
    emits SYNC_STATE back to the joining client.
    """
    session_data = await sio.get_session(sid)
    user_id: str = session_data.get("user_id", "")

    room_code = str(data.get("room_code", "")).upper().strip()
    role = str(data.get("role", "")).upper()

    if not room_code or role not in ("HOST", "PLAYER"):
        await _emit_error(sid, "Invalid join_room payload")
        return

    async with _db() as db:
        redis = await get_redis()
        user = await get_user_by_id(db, user_id)
        if not user:
            await _emit_error(sid, "User not found")
            return

        try:
            session, room_state = await game_service.get_session_by_code(
                db, redis, room_code
            )
        except Exception as exc:
            await _emit_error(sid, str(exc))
            return

        if role == "HOST":
            # host_user_id alone isn't enough: the recorded host must still hold
            # HOST on the course (ADMIN keeps its override).
            if not await game_service.user_can_host_session(db, user, session):
                await _emit_error(
                    sid,
                    _HOST_ACCESS_LOST
                    if user.id == session.host_user_id
                    else "Not authorised as host for this session",
                )
                return

            # Cancel any pending abandon grace period
            task = _abandon_tasks.pop(session.id, None)
            if task:
                task.cancel()

            await sio.enter_room(sid, room_code)
            await sio.enter_room(sid, _host_room(room_code))
            _sid_ctx[sid] = {
                "user_id": user.id,
                "role": "HOST",
                "room_code": room_code,
                "session_id": session.id,
            }

            if room_state:
                room_state["host_sid"] = sid
                await state.set_room_state(redis, room_code, room_state)
                await _auto_unlock_for_host(redis, session.id, room_code, room_state)

            players = await state.get_all_players(redis, session.id)
            current_q = await state.get_current_question(redis, session.id)
            current_q_payload = await _host_question_payload(db, current_q)
            question_locked = (
                room_state.get("question_locked", False) if room_state else False
            )
            timer_remaining = (
                room_state.get("timer_remaining_seconds") if room_state else None
            )

            await sio.emit(
                E.SYNC_STATE,
                {
                    "role": "HOST",
                    "status": session.status,
                    "questionPhase": room_state.get("question_phase")
                    if room_state
                    else None,
                    "questionIndex": room_state.get("question_index", -1)
                    if room_state
                    else -1,
                    "players": players,
                    "playerCount": len(players),
                    "currentQuestion": current_q_payload,
                    "questionLocked": question_locked,
                    "timerRemainingSeconds": timer_remaining,
                },
                to=sid,
            )
            logger.info("host_joined", sid=sid, room=room_code, session_id=session.id)

        else:  # PLAYER
            if session.status not in ("LOBBY", "IN_PROGRESS"):
                await _emit_error(sid, "This game is not accepting players")
                return

            try:
                display_name = await game_service.authorise_player(db, user, session)
            except Exception as exc:
                await _emit_error(sid, str(exc))
                return

            existing = await state.get_player(redis, session.id, user.id)
            if existing:
                await state.set_player_connected(redis, session.id, user.id, True)
            else:
                await state.add_player(
                    redis,
                    session.id,
                    user.id,
                    display_name,
                    is_guest=(user.role == "GUEST"),
                )

            await sio.enter_room(sid, room_code)
            await sio.enter_room(sid, _user_room(user.id))
            _sid_ctx[sid] = {
                "user_id": user.id,
                "role": "PLAYER",
                "room_code": room_code,
                "session_id": session.id,
            }

            player_count = await state.get_player_count(redis, session.id)
            player_data = await state.get_player(redis, session.id, user.id)
            current_q = await state.get_current_question(redis, session.id)

            # Notify host of the new player
            await sio.emit(
                E.PLAYER_JOINED,
                {
                    "userId": user.id,
                    "displayName": display_name,
                    "playerCount": player_count,
                },
                to=_host_room(room_code),
            )

            has_answered = False
            if current_q:
                has_answered = await state.has_answered(
                    redis, session.id, current_q["question_id"], user.id
                )

            question_phase = room_state.get("question_phase") if room_state else None
            question_locked = (
                room_state.get("question_locked", False) if room_state else False
            )

            await sio.emit(
                E.SYNC_STATE,
                {
                    "role": "PLAYER",
                    "status": session.status,
                    "playerCount": player_count,
                    "yourScore": player_data.get("score", 0) if player_data else 0,
                    "currentQuestion": current_q,
                    "hasAnswered": has_answered,
                    "questionLocked": question_locked,
                },
                to=sid,
            )

            # Late-join: if a question is actively open and not locked, push it to
            # this player with the remaining time so they can answer immediately.
            if (
                session.status == "IN_PROGRESS"
                and question_phase == "QUESTION"
                and not question_locked
                and current_q
                and not has_answered
            ):
                question = await db.get(Question, current_q["question_id"])
                if question:
                    started_at = datetime.fromisoformat(current_q["started_at"])
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    elapsed = int(
                        (datetime.now(timezone.utc) - started_at).total_seconds()
                    )
                    remaining = max(5, current_q["time_limit_seconds"] - elapsed)
                    payload = await _question_payload(
                        db,
                        question,
                        current_q["question_number"],
                        current_q["total_questions"],
                    )
                    payload["timeLimitSeconds"] = remaining
                    await sio.emit(E.NEW_QUESTION, payload, to=sid)

            await state.refresh_room_ttl(redis, room_code)
            logger.info("player_joined", sid=sid, room=room_code, user_id=user.id)


# ---------------------------------------------------------------------------
# REJOIN_ROOM — reconnection after network interruption
# ---------------------------------------------------------------------------


@sio.on(E.REJOIN_ROOM)
async def on_rejoin_room(sid: str, data: dict) -> None:
    """
    data = {"room_code": str}
    Re-establishes room membership using the JWT already validated at connect time.
    Role is inferred with the session-host check (game_service.user_can_host_session).
    """
    session_data = await sio.get_session(sid)
    user_id: str = session_data.get("user_id", "")

    room_code = str(data.get("room_code", "")).upper().strip()
    if not room_code or not user_id:
        await _emit_error(sid, "Invalid rejoin_room payload")
        return

    async with _db() as db:
        redis = await get_redis()
        user = await get_user_by_id(db, user_id)
        if not user:
            await _emit_error(sid, "User not found")
            return

        try:
            session, room_state = await game_service.get_session_by_code(
                db, redis, room_code
            )
        except Exception as exc:
            await _emit_error(sid, str(exc))
            return

        is_host = await game_service.user_can_host_session(db, user, session)
        if not is_host and user.id == session.host_user_id:
            # A recorded host who has lost HOST is refused rather than seated as a
            # player — this path must not become a way around the JOIN_ROOM check.
            await _emit_error(sid, _HOST_ACCESS_LOST)
            return

        if is_host:
            task = _abandon_tasks.pop(session.id, None)
            if task:
                task.cancel()
                logger.info("host_reconnected_abandon_cancelled", session_id=session.id)

            await sio.enter_room(sid, room_code)
            await sio.enter_room(sid, _host_room(room_code))
            _sid_ctx[sid] = {
                "user_id": user.id,
                "role": "HOST",
                "room_code": room_code,
                "session_id": session.id,
            }

            if room_state:
                room_state["host_sid"] = sid
                await state.set_room_state(redis, room_code, room_state)
                await _auto_unlock_for_host(redis, session.id, room_code, room_state)

            players = await state.get_all_players(redis, session.id)
            current_q = await state.get_current_question(redis, session.id)
            current_q_payload = await _host_question_payload(db, current_q)
            question_locked = (
                room_state.get("question_locked", False) if room_state else False
            )
            timer_remaining = (
                room_state.get("timer_remaining_seconds") if room_state else None
            )

            await sio.emit(
                E.SYNC_STATE,
                {
                    "role": "HOST",
                    "status": session.status,
                    "questionPhase": room_state.get("question_phase")
                    if room_state
                    else None,
                    "questionIndex": room_state.get("question_index", -1)
                    if room_state
                    else -1,
                    "players": players,
                    "playerCount": len(players),
                    "currentQuestion": current_q_payload,
                    "questionLocked": question_locked,
                    "timerRemainingSeconds": timer_remaining,
                },
                to=sid,
            )
            logger.info("host_rejoined", sid=sid, room=room_code, session_id=session.id)

        else:
            player_data = await state.get_player(redis, session.id, user.id)
            if not player_data:
                # Redis was flushed — re-authorise and re-add the player
                try:
                    display_name = await game_service.authorise_player(
                        db, user, session
                    )
                except Exception as exc:
                    await _emit_error(sid, str(exc))
                    return
                await state.add_player(
                    redis,
                    session.id,
                    user.id,
                    display_name,
                    is_guest=(user.role == "GUEST"),
                )
                player_data = await state.get_player(redis, session.id, user.id)
            else:
                await state.set_player_connected(redis, session.id, user.id, True)

            await sio.enter_room(sid, room_code)
            await sio.enter_room(sid, _user_room(user.id))
            _sid_ctx[sid] = {
                "user_id": user.id,
                "role": "PLAYER",
                "room_code": room_code,
                "session_id": session.id,
            }

            current_q = await state.get_current_question(redis, session.id)
            has_answered = False
            if current_q:
                has_answered = await state.has_answered(
                    redis, session.id, current_q["question_id"], user.id
                )

            player_count = await state.get_player_count(redis, session.id)
            question_locked = (
                room_state.get("question_locked", False) if room_state else False
            )

            await sio.emit(
                E.SYNC_STATE,
                {
                    "role": "PLAYER",
                    "status": session.status,
                    "playerCount": player_count,
                    "yourScore": player_data.get("score", 0) if player_data else 0,
                    "currentQuestion": current_q,
                    "hasAnswered": has_answered,
                    "questionLocked": question_locked,
                },
                to=sid,
            )

        await state.refresh_room_ttl(redis, room_code)
        logger.info(
            "client_rejoined", sid=sid, room=room_code, user_id=user_id, is_host=is_host
        )


# ---------------------------------------------------------------------------
# HOST_ADVANCE — host drives all phase transitions
# ---------------------------------------------------------------------------


@sio.on(E.HOST_ADVANCE)
async def on_host_advance(sid: str, _data: dict) -> None:
    """
    The host sends HOST_ADVANCE to move the game forward.

    State machine:
      question_phase == None (LOBBY)  →  start game, emit NEW_QUESTION (phase → QUESTION)
      question_phase == "QUESTION"    →  show results, emit QUESTION_RESULTS (phase → RESULTS)
      question_phase == "RESULTS"     →  next question or end game (phase → QUESTION | COMPLETED)
    """
    try:
        await _on_host_advance_impl(sid)
    except Exception:
        logger.exception("host_advance_unhandled_error", sid=sid)


async def _on_host_advance_impl(sid: str) -> None:
    ctx = _sid_ctx.get(sid)
    if not ctx or ctx["role"] != "HOST":
        return

    room_code = ctx["room_code"]
    session_id = ctx["session_id"]

    async with _db() as db:
        redis = await get_redis()
        session = await db.get(GameSession, session_id)
        room_state = await state.get_room_state(redis, room_code)

        if not session or not room_state:
            await _emit_error(sid, "Session state not found")
            return

        question_phase = room_state.get("question_phase")
        question_index: int = room_state.get("question_index", -1)
        question_ids: list[int] = room_state.get("question_ids", [])

        # Cancel any running question timer regardless of which phase we're in
        timer = _timer_tasks.pop(session_id, None)
        if timer:
            timer.cancel()

        # ── Phase: QUESTION → RESULTS ────────────────────────────────────────
        if question_phase == "QUESTION":
            current_q_state = await state.get_current_question(redis, session_id)
            if not current_q_state:
                await _emit_error(sid, "No current question found")
                return

            question = await db.get(Question, current_q_state["question_id"])
            if not question:
                await _emit_error(sid, "Question record missing")
                return

            reveal = _answer_reveal(question)
            answer_dist = await state.get_answer_dist(redis, session_id, question.id)
            leaderboard = await game_service.get_leaderboard(db, session_id)
            players = await state.get_all_players(redis, session_id)

            # Bulk-fetch per-question points for all players (single query)
            # Select only columns — not full ORM objects — to avoid triggering
            # the GameSession.scores backref which causes MissingGreenlet in
            # async SQLAlchemy when the collection hasn't been eager-loaded.
            from sqlalchemy import select as sa_select
            from ..models.session import SessionScore as _Score

            q_score_rows = await db.execute(
                sa_select(_Score.user_id, _Score.points_awarded).where(
                    _Score.session_id == session_id,
                    _Score.question_id == question.id,
                )
            )
            q_scores: dict[str, int] = {
                r.user_id: r.points_awarded for r in q_score_rows
            }

            # Bar-chart results → host (no player names)
            await sio.emit(
                E.QUESTION_RESULTS,
                {
                    "questionId": question.id,
                    "answerReveal": reveal,
                    "answerDistribution": answer_dist,
                    "totalAnswered": len(q_scores),
                    "totalPlayers": len(players),
                },
                to=_host_room(room_code),
            )

            # Personal result → every player (privacy: no other names/scores exposed)
            # Use the Redis player list so zero-score players aren't skipped.
            rank_map = {
                row["user_id"]: rank + 1 for rank, row in enumerate(leaderboard)
            }
            for player in players:
                uid = player["user_id"]
                await sio.emit(
                    E.QUESTION_RESULTS,
                    {
                        "questionId": question.id,
                        "answerReveal": reveal,
                        "yourPoints": q_scores.get(uid, 0),
                        "yourScore": player.get("score", 0),
                        "yourRank": rank_map.get(uid, len(players)),
                        "playerCount": len(players),
                    },
                    to=_user_room(uid),
                )

            room_state["question_phase"] = "RESULTS"
            room_state.pop("question_locked", None)
            room_state.pop("timer_remaining_seconds", None)
            room_state.pop("question_locked_by_disconnect", None)
            await state.set_room_state(redis, room_code, room_state)
            logger.info(
                "question_results_shown", session_id=session_id, question_id=question.id
            )
            return

        # ── Phase: RESULTS or None (Lobby) → advance/start ──────────────────
        if question_phase in (None, "RESULTS"):
            next_index = question_index + 1

            if question_phase is None:
                # Game has not started yet — transition LOBBY → IN_PROGRESS
                try:
                    questions = await game_service.start_game(db, redis, session)
                except Exception as exc:
                    await _emit_error(sid, str(exc))
                    return
                if questions is None:
                    # A concurrent Start (a double click) won the game lock and started the
                    # game; this advance read the lobby phase before that. Ignore it: no
                    # second start, no error.
                    logger.info("host_advance_already_started", session_id=session_id)
                    return
                question_ids = [q.id for q in questions]
                room_state["question_ids"] = question_ids
                next_index = 0

            if next_index >= len(question_ids):
                # All questions exhausted → end game
                await game_service.complete_game(db, redis, session)

                leaderboard = await game_service.get_leaderboard(db, session_id)
                players = await state.get_all_players(redis, session_id)

                # Compute max possible score (sum of all question point values)
                from sqlalchemy import select as sa_select2, func as sa_func
                from ..models.game import Question as _Question

                max_possible_result = await db.execute(
                    sa_select2(sa_func.sum(_Question.points_value)).where(
                        _Question.game_id == session.game_id
                    )
                )
                max_possible_score = int(max_possible_result.scalar_one() or 0)

                # Send anonymous score list + per-question summary to host
                host_q_summary = await game_service.get_host_question_summary(
                    db, session_id, len(players)
                )
                await sio.emit(
                    E.GAME_OVER,
                    {
                        "scores": [row["score"] for row in leaderboard],
                        "playerCount": len(players),
                        "maxPossibleScore": max_possible_score,
                        "questionSummary": host_q_summary,
                    },
                    to=_host_room(room_code),
                )

                # Per-player GAME_OVER with their personal summary
                # Iterate Redis player list so players with 0 points aren't skipped.
                final_rank_map = {
                    row["user_id"]: rank + 1 for rank, row in enumerate(leaderboard)
                }
                for player in players:
                    uid = player["user_id"]
                    summary = await game_service.get_player_question_summary(
                        db, session_id, uid
                    )
                    await sio.emit(
                        E.GAME_OVER,
                        {
                            "yourFinalScore": player.get("score", 0),
                            "yourFinalRank": final_rank_map.get(uid, len(players)),
                            "playerCount": len(players),
                            "questionSummary": summary,
                        },
                        to=_user_room(uid),
                    )

                room_state["question_phase"] = "COMPLETED"
                room_state["status"] = "COMPLETED"
                await state.set_room_state(redis, room_code, room_state)
                logger.info("game_completed_via_gateway", session_id=session_id)
                return

            # Load and broadcast the next question
            question = await db.get(Question, question_ids[next_index])
            if not question:
                await _emit_error(
                    sid, f"Question {question_ids[next_index]} not found in DB"
                )
                return

            room_state["question_index"] = next_index
            room_state["question_ids"] = question_ids
            room_state["question_phase"] = "QUESTION"
            room_state["question_locked"] = False
            await state.set_room_state(redis, room_code, room_state)

            started_at = datetime.now(timezone.utc)
            await state.set_current_question(
                redis,
                session_id,
                question.id,
                started_at,
                question.time_limit_seconds,
                next_index + 1,
                len(question_ids),
            )

            payload = await _question_payload(
                db, question, next_index + 1, len(question_ids)
            )
            _t_emit = datetime.now(timezone.utc)
            await sio.emit(E.NEW_QUESTION, payload, to=room_code)

            await state.refresh_room_ttl(redis, room_code)

            # Start question timer
            task = asyncio.create_task(
                _question_timer_task(
                    session_id, room_code, question.id, question.time_limit_seconds
                )
            )
            _timer_tasks[session_id] = task

            _log_kw: dict = {
                "session_id": session_id,
                "question_id": question.id,
                "index": next_index,
            }
            if settings.is_stress_test_mode:
                _log_kw["emit_at"] = _t_emit.isoformat()
                _log_kw["emit_ms"] = int(
                    (datetime.now(timezone.utc) - _t_emit).total_seconds() * 1000
                )
            logger.info("question_started", **_log_kw)


# ---------------------------------------------------------------------------
# SUBMIT_ANSWER — player submits their answer
# ---------------------------------------------------------------------------


@sio.on(E.SUBMIT_ANSWER)
async def on_submit_answer(sid: str, data: dict) -> None:
    """
    data = {"question_id": int, "answer_data": dict, "answer_time_ms": int | None}
    Scores the answer, persists to MySQL, updates Redis, and notifies client + host.
    """
    ctx = _sid_ctx.get(sid)
    if not ctx or ctx["role"] != "PLAYER":
        return

    user_id = ctx["user_id"]
    room_code = ctx["room_code"]
    session_id = ctx["session_id"]

    question_id = data.get("question_id")
    answer_data = data.get("answer_data") or {}
    answer_time_ms = data.get("answer_time_ms")

    if not isinstance(question_id, int):
        await _emit_error(sid, "question_id must be an integer")
        return

    async with _db() as db:
        redis = await get_redis()

        room_state = await state.get_room_state(redis, room_code)
        if not room_state or room_state.get("question_phase") != "QUESTION":
            if settings.is_stress_test_mode:
                logger.warning(
                    "submit_answer_no_active_question",
                    sid=sid,
                    session_id=session_id,
                    phase=room_state.get("question_phase") if room_state else None,
                )
            await _emit_error(sid, "No active question")
            return

        if room_state.get("question_locked"):
            await _emit_error(sid, "Question is locked")
            return

        current_q_state = await state.get_current_question(redis, session_id)
        if not current_q_state or current_q_state["question_id"] != question_id:
            if settings.is_stress_test_mode:
                logger.warning(
                    "submit_answer_question_mismatch",
                    sid=sid,
                    session_id=session_id,
                    expected=current_q_state["question_id"]
                    if current_q_state
                    else None,
                    received=question_id,
                )
            await _emit_error(sid, "Question ID does not match the active question")
            return

        # Idempotent: acknowledge but don't double-score
        if await state.has_answered(redis, session_id, question_id, user_id):
            await sio.emit(
                E.ANSWER_RECEIVED,
                {"questionId": question_id, "alreadyAnswered": True},
                to=sid,
            )
            return

        session = await db.get(GameSession, session_id)
        if not session or session.status != "IN_PROGRESS":
            await _emit_error(sid, "Session is not in progress")
            return

        question = await db.get(Question, question_id)
        if not question:
            await _emit_error(sid, "Question not found")
            return

        if question.type == "multi_select":
            indices = answer_data.get("selectedIndices")
            if not isinstance(indices, list) or not all(
                isinstance(i, int) for i in indices
            ):
                await _emit_error(
                    sid,
                    "multi_select answer must include selectedIndices as a list of integers",
                )
                return
            num_opts = len(question.config.get("options", []))
            if any(not (0 <= i < num_opts) for i in indices):
                await _emit_error(
                    sid, "selectedIndices contains an out-of-bounds index"
                )
                return

        result = await game_service.record_answer(
            db, redis, session_id, user_id, question, answer_data, answer_time_ms
        )

        player_data = await state.get_player(redis, session_id, user_id)
        total_score = (
            player_data.get("score", 0) if player_data else result.points_awarded
        )

        # Notify the answering player
        await sio.emit(
            E.ANSWER_RECEIVED,
            {
                "questionId": question_id,
                "isCorrect": result.is_correct,
                "pointsAwarded": result.points_awarded,
                "totalScore": total_score,
            },
            to=sid,
        )

        # Notify host of updated answer count
        answered_count = await state.get_answered_count(redis, session_id, question_id)
        player_count = await state.get_player_count(redis, session_id)
        await sio.emit(
            E.ANSWER_STATUS,
            {
                "userId": user_id,
                "answeredCount": answered_count,
                "totalPlayers": player_count,
            },
            to=_host_room(room_code),
        )

        # If all players have answered, notify host immediately (cancel timer)
        if await state.all_players_answered(redis, session_id, question_id):
            timer = _timer_tasks.pop(session_id, None)
            if timer:
                timer.cancel()
            await sio.emit(
                E.ANSWER_PHASE_ENDED,
                {
                    "questionId": question_id,
                    "answeredCount": answered_count,
                    "totalPlayers": player_count,
                    "allAnswered": True,
                },
                to=_host_room(room_code),
            )

        await state.refresh_room_ttl(redis, room_code)
        _ans_log: dict = {
            "session_id": session_id,
            "user_id": user_id,
            "question_id": question_id,
            "is_correct": result.is_correct,
        }
        if settings.is_stress_test_mode:
            _ans_log["points_awarded"] = result.points_awarded
            _ans_log["answer_time_ms"] = answer_time_ms
        logger.info("answer_recorded", **_ans_log)


# ---------------------------------------------------------------------------
# HOST_LOCK_QUESTION — host closes answer submission for the current question
# ---------------------------------------------------------------------------


@sio.on(E.HOST_LOCK_QUESTION)
async def on_lock_question(sid: str, _data: dict) -> None:
    """
    Toggles question_locked in room state. Also pauses/resumes the server-side
    timer task so that locking stops the countdown and unlocking resumes it.
    """
    try:
        await _on_lock_question_impl(sid)
    except Exception:
        logger.exception("lock_question_unhandled_error", sid=sid)


async def _on_lock_question_impl(sid: str) -> None:
    ctx = _sid_ctx.get(sid)
    if not ctx or ctx["role"] != "HOST":
        return

    room_code = ctx["room_code"]
    session_id = ctx["session_id"]

    redis = await get_redis()
    room_state = await state.get_room_state(redis, room_code)

    if not room_state or room_state.get("question_phase") != "QUESTION":
        return

    current_q = await state.get_current_question(redis, session_id)
    if not current_q:
        return

    new_locked = not room_state.get("question_locked", False)
    room_state["question_locked"] = new_locked

    if new_locked:
        started_at = datetime.fromisoformat(current_q["started_at"])
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        remaining = max(0.0, current_q["time_limit_seconds"] - elapsed)
        room_state["timer_remaining_seconds"] = remaining

        timer = _timer_tasks.pop(session_id, None)
        if timer:
            timer.cancel()

        await state.set_room_state(redis, room_code, room_state)
        await sio.emit(
            E.QUESTION_LOCKED,
            {"questionId": current_q["question_id"], "remainingSeconds": remaining},
            to=room_code,
        )
        logger.info(
            "question_locked_by_host",
            session_id=session_id,
            question_id=current_q["question_id"],
            remaining_seconds=remaining,
        )
    else:
        remaining = room_state.pop("timer_remaining_seconds", 0.0)

        if remaining > 0:
            # Shift started_at forward so that the next lock recomputes remaining
            # correctly, regardless of how long this question has been paused.
            # Invariant: now - started_at == time_limit - remaining (active elapsed).
            virtual_start = datetime.now(timezone.utc) - timedelta(
                seconds=current_q["time_limit_seconds"] - remaining
            )
            await state.set_current_question(
                redis,
                session_id,
                current_q["question_id"],
                virtual_start,
                current_q["time_limit_seconds"],
                current_q["question_number"],
                current_q["total_questions"],
            )

        await state.set_room_state(redis, room_code, room_state)

        if remaining > 0:
            task = asyncio.ensure_future(
                _question_timer_task(
                    session_id, room_code, current_q["question_id"], remaining
                )
            )
            _timer_tasks[session_id] = task

        await sio.emit(
            E.QUESTION_UNLOCKED,
            {"questionId": current_q["question_id"], "remainingSeconds": remaining},
            to=room_code,
        )
        logger.info(
            "question_unlocked_by_host",
            session_id=session_id,
            question_id=current_q["question_id"],
            remaining_seconds=remaining,
        )
