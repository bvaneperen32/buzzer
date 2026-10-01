"""
Unit tests for the gateway's host disconnect and rejoin (docs/plans/fix-live-session-test-cleanup.md
§3.4): the lock, timer pause and grace period a real disconnect starts, what a rejoin undoes and
what it keeps, and the order of a disconnect against a rejoin — including the two interleavings
a live stack can't be made to produce on demand.

Redis state, socket.io and the database are in-memory fakes; the gateway's own handlers run
unchanged.

Run with:
    cd tests/unit && PYTHONPATH=../../backend python -m pytest test_gateway_host_presence.py -v
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.websocket import events as E
from app.websocket import gateway as gw

ROOM = "ABCDEF"
SESSION = "session-1"
QUESTION = 7


class FakeState:
    """The Redis records the host paths read and write: the room and the current question."""

    def __init__(self):
        self.rooms: dict[str, dict] = {}
        self.questions: dict[str, dict] = {}
        self.room_read: asyncio.Event | None = (
            None  # set on each room read, when a test waits
        )
        self.room_gate: asyncio.Event | None = None  # when set, room reads wait for it

    async def get_room_state(self, _redis, code):
        if self.room_read is not None:
            self.room_read.set()
        if self.room_gate is not None:
            await self.room_gate.wait()
        room = self.rooms.get(code)
        return dict(room) if room else None

    async def set_room_state(self, _redis, code, data):
        self.rooms[code] = dict(data)

    async def get_current_question(self, _redis, session_id):
        q = self.questions.get(session_id)
        return dict(q) if q else None

    async def set_current_question(
        self, _redis, session_id, qid, started_at, limit, number, total
    ):
        self.questions[session_id] = {
            "question_id": qid,
            "started_at": started_at.isoformat(),
            "time_limit_seconds": limit,
            "question_number": number,
            "total_questions": total,
        }


@pytest.fixture()
def world(monkeypatch):
    """A started game with question 7 open for 30 s (started 10 s ago), and no sockets."""
    fake = FakeState()
    for name in (
        "get_room_state",
        "set_room_state",
        "get_current_question",
        "set_current_question",
    ):
        monkeypatch.setattr(gw.state, name, getattr(fake, name))
    emitted: list[tuple[str, dict, str]] = []

    async def emit(event, data=None, to=None, **_):
        emitted.append((event, data, to))

    monkeypatch.setattr(gw.sio, "emit", emit)
    monkeypatch.setattr(gw.sio, "enter_room", AsyncMock())
    monkeypatch.setattr(gw, "get_redis", AsyncMock(return_value=object()))
    for registry in (
        "_sid_ctx",
        "_abandon_tasks",
        "_timer_tasks",
        "_host_presence_locks",
    ):
        monkeypatch.setattr(gw, registry, {})

    fake.rooms[ROOM] = {
        "status": "IN_PROGRESS",
        "session_id": SESSION,
        "question_phase": "QUESTION",
        "question_locked": False,
    }
    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    fake.questions[SESSION] = {
        "question_id": QUESTION,
        "started_at": started.isoformat(),
        "time_limit_seconds": 30,
        "question_number": 1,
        "total_questions": 1,
    }
    fake.emitted = emitted
    yield fake
    for task in [*gw._abandon_tasks.values(), *gw._timer_tasks.values()]:
        task.cancel()


def seat_record(sid: str) -> None:
    """A host socket as JOIN_ROOM leaves it in _sid_ctx."""
    gw._sid_ctx[sid] = {
        "user_id": "host",
        "role": "HOST",
        "room_code": ROOM,
        "session_id": SESSION,
    }


async def seat(sid: str) -> dict | None:
    return await gw._seat_host(sid, "host", SESSION, ROOM, object())


def events(world, name: str) -> list[dict]:
    return [data for event, data, _ in world.emitted if event == name]


# ---------------------------------------------------------------------------
# A disconnect, then a rejoin
# ---------------------------------------------------------------------------


async def test_a_host_disconnect_locks_and_pauses_and_the_rejoin_undoes_it(world):
    """The close locks the open question, pauses its timer (keeping the time left) and starts
    the grace period. The rejoin unlocks it, resumes the countdown where it was, ends the grace
    period and tells the phones the host is back."""
    seat_record("old")
    timer = asyncio.ensure_future(asyncio.sleep(3600))
    gw._timer_tasks[SESSION] = timer

    await gw.disconnect("old")
    await asyncio.sleep(0)  # let the cancellation land

    room = world.rooms[ROOM]
    assert room["question_locked"] is True
    assert room["question_locked_by_disconnect"] is True
    assert room["timer_remaining_seconds"] == pytest.approx(20, abs=1)
    assert timer.cancelled() and SESSION not in gw._timer_tasks
    [locked] = events(world, E.QUESTION_LOCKED)
    assert locked["remainingSeconds"] == room["timer_remaining_seconds"]
    [gone] = events(world, E.HOST_DISCONNECTED)
    abandon_in = datetime.fromisoformat(gone["abandonAt"]) - datetime.now(timezone.utc)
    assert (
        gw._HOST_GRACE_SECONDS - 5
        < abandon_in.total_seconds()
        <= gw._HOST_GRACE_SECONDS
    )
    abandon = gw._abandon_tasks[SESSION]
    assert not abandon.done()
    remaining = room["timer_remaining_seconds"]

    room = await seat("new")
    await asyncio.sleep(0)  # the cancellation reaches the grace task, inside its sleep

    assert room["question_locked"] is False
    assert (
        "question_locked_by_disconnect" not in room
        and "timer_remaining_seconds" not in room
    )
    assert room["host_sid"] == "new" and world.rooms[ROOM] == room
    assert abandon.done() and SESSION not in gw._abandon_tasks
    assert events(world, E.GAME_ABANDONED) == []
    [unlocked] = events(world, E.QUESTION_UNLOCKED)
    assert unlocked["remainingSeconds"] == remaining
    assert events(world, E.HOST_RECONNECTED) == [
        {}
    ]  # the phones drop "Host disconnected"
    assert not gw._timer_tasks[SESSION].done()  # the resumed countdown
    # The virtual start keeps "now - started_at == time_limit - remaining" (the lock/unlock invariant).
    started = datetime.fromisoformat(world.questions[SESSION]["started_at"])
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    assert elapsed == pytest.approx(30 - remaining, abs=0.5)


async def test_rejoin_keeps_a_lock_the_host_set_by_hand(world):
    world.rooms[ROOM].update(question_locked=True, timer_remaining_seconds=12.5)
    seat_record("old")

    await gw.disconnect("old")
    assert events(world, E.QUESTION_LOCKED) == []  # already locked: nothing to pause
    assert len(events(world, E.HOST_DISCONNECTED)) == 1

    room = await seat("new")
    assert room["question_locked"] is True and room["timer_remaining_seconds"] == 12.5
    assert events(world, E.QUESTION_UNLOCKED) == []
    assert SESSION not in gw._timer_tasks
    # The host is back even though the question stays locked: the phones must hear it.
    assert events(world, E.HOST_RECONNECTED) == [{}]


async def test_rejoin_after_the_time_ran_out_keeps_the_question_locked(world):
    started = datetime.now(timezone.utc) - timedelta(seconds=45)  # a 30 s question
    world.questions[SESSION]["started_at"] = started.isoformat()
    seat_record("old")

    await gw.disconnect("old")
    assert world.rooms[ROOM]["timer_remaining_seconds"] == 0.0

    room = await seat("new")
    assert room["question_locked"] is True
    assert room["timer_remaining_seconds"] == 0.0
    assert "question_locked_by_disconnect" not in room
    assert events(world, E.QUESTION_UNLOCKED) == []
    assert (
        SESSION not in gw._timer_tasks
    )  # no timer reopened for a question whose time is up


async def test_lobby_disconnect_does_nothing(world):
    world.rooms[ROOM].update(status="LOBBY", question_phase=None)
    seat_record("old")

    await gw.disconnect("old")

    assert world.emitted == [] and SESSION not in gw._abandon_tasks


# ---------------------------------------------------------------------------
# Ordering: the old socket's close against the new socket's join
# ---------------------------------------------------------------------------


async def test_other_host_sockets_keep_the_game_hosted(world):
    """Joins that follow no announced absence say nothing: a first join, and a second (admin)
    host's. While another host socket is seated, a close changes nothing either: the late
    close of a socket a reconnect replaced (a dropped connection may only be noticed at the
    server's ping timeout, after the new socket joined), or the admin leaving. It locks
    nothing and starts no grace period. The last host socket's close is the disconnect."""
    seat_record("old")  # replaced by "new"; its close arrives late
    await seat("new")
    await seat("admin")

    await gw.disconnect("old")
    await gw.disconnect("admin")

    assert world.emitted == []
    assert world.rooms[ROOM]["question_locked"] is False
    assert SESSION not in gw._abandon_tasks
    await gw.disconnect("new")  # the last host socket
    assert len(events(world, E.HOST_DISCONNECTED)) == 1


async def test_a_rejoin_arriving_during_the_disconnect_handling_waits_for_it(world):
    """The disconnect is suspended inside its Redis read when the rejoin starts. Unserialized,
    the rejoin would read the room still unlocked and change nothing, and the disconnect would
    then lock the question and start the grace period with the host connected."""
    seat_record("old")
    world.room_read, world.room_gate = asyncio.Event(), asyncio.Event()
    leaving = asyncio.ensure_future(gw.disconnect("old"))
    await world.room_read.wait()  # the disconnect is inside its read of the room

    world.room_read = None
    joining = asyncio.ensure_future(seat("new"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not joining.done()  # held until the disconnect handling finishes

    world.room_gate.set()
    await leaving
    room = await joining
    await asyncio.sleep(0)

    assert (
        room["question_locked"] is False
        and world.rooms[ROOM]["question_locked"] is False
    )
    assert SESSION not in gw._abandon_tasks
    assert [event for event, _, _ in world.emitted] == [
        E.QUESTION_LOCKED,
        E.HOST_DISCONNECTED,
        E.QUESTION_UNLOCKED,
        E.HOST_RECONNECTED,
    ]


@pytest.mark.parametrize(
    "held", [E.QUESTION_LOCKED, E.HOST_DISCONNECTED], ids=["lock-sent", "absence-sent"]
)
async def test_a_player_joining_during_the_disconnect_broadcast_sees_the_host_gone(
    world, monkeypatch, held
):
    """A player joins while the disconnect is being announced (the broadcast is held at one of
    its two room-wide emits). Its sync_state must match what the room is being told: the host
    gone and the question locked. Otherwise the phone would clear the banner the broadcast
    raised, with the host still away."""
    seat_record("old")
    sending, release = asyncio.Event(), asyncio.Event()
    record = gw.sio.emit  # the world's recorder

    async def emit(event, data=None, to=None, **kw):
        await record(event, data, to=to, **kw)
        if event == held and to == ROOM:
            sending.set()
            await release.wait()

    class Session:
        id = SESSION
        status = "IN_PROGRESS"
        host_user_id = "host"

    class Player:
        id = "p1"
        role = "GUEST"

    async def by_code(_db, _redis, code):
        return Session(), await world.get_room_state(None, code)

    @asynccontextmanager
    async def db():
        yield object()

    monkeypatch.setattr(gw.sio, "emit", emit)
    monkeypatch.setattr(
        gw.sio, "get_session", AsyncMock(return_value={"user_id": "p1"})
    )
    monkeypatch.setattr(gw, "_db", db)
    monkeypatch.setattr(gw, "get_user_by_id", AsyncMock(return_value=Player()))
    monkeypatch.setattr(gw.game_service, "get_session_by_code", by_code)
    monkeypatch.setattr(
        gw.game_service, "authorise_player", AsyncMock(return_value="Pat")
    )
    for name, value in (
        ("get_player", {"score": 0}),
        ("set_player_connected", None),
        ("get_player_count", 1),
        ("has_answered", 0),
        ("refresh_room_ttl", None),
    ):
        monkeypatch.setattr(gw.state, name, AsyncMock(return_value=value))

    leaving = asyncio.ensure_future(gw.disconnect("old"))
    await sending.wait()  # the disconnect is announcing itself
    await gw.on_join_room("phone", {"room_code": ROOM, "role": "PLAYER"})
    release.set()
    await leaving

    [sync] = [data for event, data, to in world.emitted if event == E.SYNC_STATE]
    assert sync["hostDisconnected"] is True
    assert sync["questionLocked"] is True
    # The grace period itself is unchanged: announced, registered, cancelled by the rejoin.
    [gone] = events(world, E.HOST_DISCONNECTED)
    abandon_in = datetime.fromisoformat(gone["abandonAt"]) - datetime.now(timezone.utc)
    assert (
        gw._HOST_GRACE_SECONDS - 5
        < abandon_in.total_seconds()
        <= gw._HOST_GRACE_SECONDS
    )
    grace = gw._abandon_tasks[SESSION]
    await seat("new")
    await asyncio.sleep(
        0
    )  # the cancellation reaches the task, inside its grace-period sleep
    assert grace.done() and SESSION not in gw._abandon_tasks
    assert (
        events(world, E.GAME_ABANDONED) == []
        and len(events(world, E.HOST_RECONNECTED)) == 1
    )


async def test_a_disconnect_arriving_during_the_rejoin_sees_the_new_host(world):
    seat_record("old")
    world.room_read, world.room_gate = asyncio.Event(), asyncio.Event()
    joining = asyncio.ensure_future(seat("new"))
    await (
        world.room_read.wait()
    )  # the rejoin holds the lock, inside its read of the room

    world.room_read = None
    leaving = asyncio.ensure_future(gw.disconnect("old"))
    for _ in range(5):
        await asyncio.sleep(0)
    world.room_gate.set()
    await joining
    await leaving

    assert world.rooms[ROOM]["question_locked"] is False
    assert world.emitted == [] and SESSION not in gw._abandon_tasks


# ---------------------------------------------------------------------------
# The grace period
# ---------------------------------------------------------------------------


@pytest.fixture()
def quick_grace(monkeypatch):
    monkeypatch.setattr(gw, "_HOST_GRACE_SECONDS", 0.05)
    abandoned: list[str] = []

    class Session:
        id = SESSION
        status = "IN_PROGRESS"

    class Db:
        async def get(self, _model, _id):
            return Session()

    @asynccontextmanager
    async def db():
        yield Db()

    async def abandon_game(_db, _redis, session):
        abandoned.append(session.id)

    monkeypatch.setattr(gw, "_db", db)
    monkeypatch.setattr(gw.game_service, "abandon_game", abandon_game)
    return abandoned


async def test_the_grace_period_ends_the_game_when_the_host_stays_away(
    world, quick_grace
):
    seat_record("old")
    await gw.disconnect("old")
    await gw._abandon_tasks[SESSION]

    assert quick_grace == [SESSION]
    assert events(world, E.GAME_ABANDONED) == [{}]
    assert SESSION not in gw._abandon_tasks


async def test_a_cancelled_grace_task_leaves_its_replacement_registered(
    world, quick_grace
):
    """Disconnect, rejoin, disconnect again: the first task's cleanup runs after the second
    task is stored, and must not evict it (or no later rejoin could cancel it)."""
    seat_record("a")
    await gw.disconnect("a")
    first = gw._abandon_tasks[SESSION]
    await asyncio.sleep(0)  # the first task is now inside its grace-period sleep
    await seat("b")  # cancels it; its cleanup runs at the next turn of the loop
    await gw.disconnect("b")
    second = gw._abandon_tasks[SESSION]
    assert second is not first and not first.done()

    await asyncio.sleep(0)  # the first task's cancellation and cleanup
    assert first.done()
    assert gw._abandon_tasks.get(SESSION) is second
    await seat("c")  # the replacement is still registered, so this rejoin can cancel it
    assert SESSION not in gw._abandon_tasks
    await asyncio.sleep(0.1)  # past the (shortened) grace period
    assert second.done()
    assert quick_grace == []  # the game was never abandoned with its host back


# ---------------------------------------------------------------------------
# A late answer is rejected, not an error
# ---------------------------------------------------------------------------


@pytest.fixture()
def player(world, monkeypatch):
    """A seated player socket with no answer recorded yet; the handler's database session is
    used only to look for a stored answer (none)."""

    @asynccontextmanager
    async def db():
        yield object()

    monkeypatch.setattr(gw, "_db", db)
    monkeypatch.setattr(gw.state, "has_answered", AsyncMock(return_value=False))
    monkeypatch.setattr(
        gw.game_service, "recorded_answer", AsyncMock(return_value=None)
    )
    gw._sid_ctx["phone"] = {
        "user_id": "p1",
        "role": "PLAYER",
        "room_code": ROOM,
        "session_id": SESSION,
    }
    return world


async def submit(question_id: int = QUESTION) -> None:
    await gw.on_submit_answer("phone", {"question_id": question_id, "answer_data": {}})


@pytest.mark.parametrize(
    ("room", "question_id", "reason", "message"),
    [
        ({"question_locked": True}, QUESTION, "locked", "Question is locked"),
        ({"question_phase": "RESULTS"}, QUESTION, "closed", "No active question"),
        (
            {},
            QUESTION + 1,
            "closed",
            "Question ID does not match the active question",
        ),
    ],
    ids=["locked", "results-shown", "moved-on"],
)
async def test_a_late_answer_is_rejected_not_an_error(
    player, room, question_id, reason, message
):
    player.rooms[ROOM].update(room)

    await submit(question_id)

    assert [(event, to) for event, _, to in player.emitted] == [
        (E.ANSWER_REJECTED, "phone")
    ]
    assert events(player, E.ANSWER_REJECTED) == [
        {"questionId": question_id, "reason": reason, "message": message}
    ]


async def test_a_lost_room_is_still_an_error(player):
    """The room itself gone (a session failure) stays an ERROR, as do malformed requests."""
    del player.rooms[ROOM]
    await submit()
    await gw.on_submit_answer("phone", {"question_id": "7", "answer_data": {}})

    assert [event for event, _, _ in player.emitted] == [E.ERROR, E.ERROR]
    assert events(player, E.ANSWER_REJECTED) == []
