"""
Unit tests for recording an answer exactly once, and for results that count exactly the answers
that counted (docs/plans/fix-live-session-test-cleanup.md §3.13, §3.14): an answer sent again
while its first attempt is still in the server or after it failed there, and Show Results
while an answer is stuck being recorded.

The gateway's handlers and `game_service.record_answer` run unchanged. MySQL's committed rows
and Redis's answer state are an in-memory store whose steps — the row's commit, and the Redis
count (`state.apply_answer`) — a test can pause or make fail, which a live stack can't do on
demand. tests/integration/test_live_session.py covers the same paths over real sockets, MySQL
and Redis; these pin the failure points and interleavings it can't reach.

Run with:
    cd tests/unit && PYTHONPATH=../../backend python -m pytest test_gateway_answers.py -v
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.game import Question
from app.models.session import GameSession
from app.websocket import events as E
from app.websocket import gateway as gw

ROOM = "ABCDEF"
HOST_ROOM = f"{ROOM}:host"
SESSION = "session-1"
QUESTION = 7
ANSWER = {"question_id": QUESTION, "answer_data": {"selectedValue": True}}


class StepFailed(Exception):
    """A step made to fail, as a database or Redis error would."""


class Store:
    """The committed session_scores rows, and Redis's answer state for the one open question
    (players p1 and p2)."""

    def __init__(self):
        self.rows: list = []  # committed SessionScore rows
        self.scores = {"p1": 0.0, "p2": 0.0}  # the players' running scores
        self.answered: set[str] = set()
        self.dist: Counter = Counter()
        self.room = {
            "status": "IN_PROGRESS",
            "session_id": SESSION,
            "question_phase": "QUESTION",
            "question_locked": False,
        }
        self.question = Question(
            id=QUESTION,
            game_id=1,
            type="true_false",
            grading_type="ACCURACY",
            prompt="Q",
            config={},
            answer_data={"answer_points": {"true": 1000, "false": 0}},
            points_value=1000,
            time_limit_seconds=30,
        )
        self.emitted: list[tuple[str, dict, str, dict]] = []
        self.paused: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}
        self.failing: set[str] = set()

    def pause(self, step: str) -> tuple[asyncio.Event, asyncio.Event]:
        """The next time `step` runs it waits: returns (reached, resume)."""
        self.paused[step] = (asyncio.Event(), asyncio.Event())
        return self.paused[step]

    async def step(self, name: str) -> None:
        if name in self.paused:
            reached, resume = self.paused.pop(name)
            reached.set()
            await resume.wait()
        if name in self.failing:
            self.failing.discard(name)
            raise StepFailed(name)

    def counted(self) -> dict:
        return {
            "rows": len(self.rows),
            "p1": self.scores["p1"],
            "answered": sorted(self.answered),
            "dist": dict(self.dist),
        }

    def sent(self, event: str, to: str) -> list[dict]:
        return [data for e, data, t, _ in self.emitted if (e, t) == (event, to)]

    def replies(self, sid: str) -> list[tuple[str, dict]]:
        return [(event, data) for event, data, to, _ in self.emitted if to == sid]


COUNTED_ONCE = {"rows": 1, "p1": 1000, "answered": ["p1"], "dist": {"true": 1}}
NOTHING = {"rows": 0, "p1": 0.0, "answered": [], "dist": {}}
ROW_ONLY = {"rows": 1, "p1": 0.0, "answered": [], "dist": {}}


class Db:
    """One `_db()` session: rows it adds reach the store only when it commits."""

    def __init__(self, store: Store):
        self.store = store
        self.pending: list = []

    def add(self, row) -> None:
        self.pending.append(row)

    async def commit(self) -> None:
        if self.pending:
            await self.store.step("commit")
            self.store.rows.extend(self.pending)
            self.pending = []

    async def rollback(self) -> None:
        self.pending = []

    async def get(self, model, _id):
        if model is GameSession:
            return GameSession(id=SESSION, status="IN_PROGRESS")
        return self.store.question if model is Question else None

    async def execute(self, _stmt):  # the results' read of the question's score rows
        rows = [r for r in self.store.rows if r.question_id == QUESTION]
        return SimpleNamespace(all=lambda: rows)


@pytest.fixture()
def store(monkeypatch) -> Store:
    s = Store()

    @asynccontextmanager
    async def db():  # as the real one: commit on the way out, roll back on an error
        session = Db(s)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    async def recorded_answer(_db, session_id, user_id, question_id):
        return next(
            (
                r
                for r in s.rows
                if (r.session_id, r.user_id, r.question_id)
                == (session_id, user_id, question_id)
            ),
            None,
        )

    async def apply_answer(_redis, _session, _question, user_id, points, dist_keys):
        await s.step("apply")  # a failure here is Redis failing before its EXEC
        if user_id in s.answered:
            return False
        s.scores[user_id] += points
        s.answered.add(user_id)
        s.dist.update(dist_keys)
        return True

    async def leaderboard(_db, _session):
        totals: Counter = Counter()
        for r in s.rows:
            totals[r.user_id] += r.points_awarded
        return [{"user_id": u, "score": p} for u, p in totals.most_common()]

    async def emit(event, data=None, to=None, **_):
        s.emitted.append(
            (event, data, to, s.counted())
        )  # with what was stored at the time

    async def set_room_state(_redis, _code, data):
        s.room = dict(data)

    started = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    state = {
        "has_answered": lambda _r, _s, _q, user_id: _async(user_id in s.answered),
        "apply_answer": apply_answer,
        "get_room_state": lambda *_: _async(dict(s.room)),
        "set_room_state": set_room_state,
        "get_current_question": lambda *_: _async(
            {"question_id": QUESTION, "started_at": started, "time_limit_seconds": 30}
        ),
        "get_player": lambda _r, _s, user_id: _async({"score": s.scores[user_id]}),
        "get_all_players": lambda *_: _async(
            [{"user_id": u, "score": p} for u, p in s.scores.items()]
        ),
        "get_answer_dist": lambda *_: _async(dict(s.dist)),
        "get_answered_count": lambda *_: _async(len(s.answered)),
        "get_player_count": lambda *_: _async(2),
        "all_players_answered": lambda *_: _async(len(s.answered) >= 2),
        "refresh_room_ttl": lambda *_: _async(None),
    }
    for name, fake in state.items():
        monkeypatch.setattr(gw.state, name, fake)
    monkeypatch.setattr(gw.game_service, "recorded_answer", recorded_answer)
    monkeypatch.setattr(gw.game_service, "get_leaderboard", leaderboard)
    monkeypatch.setattr(gw, "_db", db)
    monkeypatch.setattr(gw, "get_redis", lambda: _async(object()))
    monkeypatch.setattr(gw.sio, "emit", emit)
    for registry in ("_sid_ctx", "_timer_tasks", "_answer_turns", "_answers_in_flight"):
        monkeypatch.setattr(gw, registry, {})
    monkeypatch.setattr(gw, "_results_building", set())
    for sid, user, role in (
        ("phone", "p1", "PLAYER"),
        ("phone-again", "p1", "PLAYER"),  # the same player's socket after a reconnect
        ("other-phone", "p2", "PLAYER"),
        ("projector", "host", "HOST"),
    ):
        gw._sid_ctx[sid] = {
            "user_id": user,
            "role": role,
            "room_code": ROOM,
            "session_id": SESSION,
        }
    yield s
    # Nothing is left registered once every attempt has finished.
    assert gw._answer_turns == {} and gw._answers_in_flight == {}
    assert gw._results_building == set()


async def _async(value):
    return value


def submit(sid: str) -> asyncio.Future:
    return asyncio.ensure_future(gw.on_submit_answer(sid, ANSWER))


async def show_results() -> None:
    await gw._on_host_advance_impl("projector")


def results(store: Store) -> tuple[dict, dict, dict]:
    """The last results: the host's, p1's and p2's."""
    return (
        store.sent(E.QUESTION_RESULTS, HOST_ROOM)[-1],
        store.sent(E.QUESTION_RESULTS, "user:p1")[-1],
        store.sent(E.QUESTION_RESULTS, "user:p2")[-1],
    )


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


ACCEPTED = (
    E.ANSWER_RECEIVED,
    {
        "questionId": QUESTION,
        "isCorrect": True,
        "pointsAwarded": 1000,
        "totalScore": 1000,
    },
)
ALREADY = (E.ANSWER_RECEIVED, {"questionId": QUESTION, "alreadyAnswered": True})


# ---------------------------------------------------------------------------
# An answer sent again counts once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "held",
    ["commit", "apply"],
    ids=["before-its-row-is-committed", "after-its-row-before-it-is-counted"],
)
async def test_a_retry_during_the_first_attempt_waits_for_it_and_counts_once(
    store, held
):
    """The first attempt is held before it marks the player answered; the reconnected phone's
    retry arrives. The retry waits for the first attempt, then finds its answer and is
    acknowledged without scoring: one row, one score, one distribution count. The first
    attempt's own acknowledgement comes only once its answer is stored and counted."""
    reached, resume = store.pause(held)
    first = submit("phone")
    await reached.wait()
    retry = submit("phone-again")
    await settle()
    assert not retry.done()  # waiting for the first attempt's outcome
    assert gw._answer_turns[(SESSION, QUESTION, "p1")][1] == 2

    resume.set()
    await asyncio.gather(first, retry)

    assert store.counted() == COUNTED_ONCE
    [(event, data, _to, stored_then)] = [e for e in store.emitted if e[2] == "phone"]
    assert (event, data) == ACCEPTED and stored_then == COUNTED_ONCE
    assert store.replies("phone-again") == [ALREADY]


async def test_players_answer_at_the_same_time_without_waiting_for_each_other(store):
    """The turn is one player's at one question: another player's answer isn't held up."""
    reached, resume = store.pause("commit")
    first = submit("phone")
    await reached.wait()
    await submit("other-phone")  # completes while the first player's attempt is held
    assert [event for event, _ in store.replies("other-phone")] == [E.ANSWER_RECEIVED]
    resume.set()
    await first
    assert store.counted()["rows"] == 2 and store.counted()["answered"] == ["p1", "p2"]


@pytest.mark.parametrize(
    ("step", "left", "completed_by", "retry_reply"),
    [
        ("commit", NOTHING, "its retry", ACCEPTED),
        ("apply", ROW_ONLY, "its retry", ALREADY),
        ("apply", ROW_ONLY, "the results", ALREADY),
    ],
    ids=[
        "before-the-commit-then-retried",
        "after-the-commit-then-retried",
        "after-the-commit-then-results",
    ],
)
async def test_a_failed_attempt_counts_once_whoever_completes_it(
    store, step, left, completed_by, retry_reply
):
    """An attempt fails: before its row's commit (nothing is left), or after it, before Redis
    counted it (the row alone is left). Either way it gets no reply. What completes it —
    the player's retry, or, with no retry first, the results — counts it exactly once, and
    the results then agree on it everywhere: answered, points, total, rank, distribution."""
    store.failing.add(step)
    await submit("phone")  # fails: logged (submit_answer_failed), no reply
    assert store.counted() == left and store.replies("phone") == []

    if completed_by == "the results":
        await show_results()
        assert store.counted() == COUNTED_ONCE
    await submit("phone-again")
    assert store.replies("phone-again") == [retry_reply]
    assert store.counted() == COUNTED_ONCE

    if completed_by == "its retry":
        await show_results()
    host, mine, theirs = results(store)
    assert (host["totalAnswered"], host["answerDistribution"]) == (1, {"true": 1})
    assert (
        mine["answered"],
        mine["yourPoints"],
        mine["yourScore"],
        mine["yourRank"],
    ) == (
        True,
        1000,
        1000,
        1,
    )
    assert (theirs["answered"], theirs["yourScore"], theirs["yourRank"]) == (
        False,
        0,
        2,
    )


@pytest.mark.parametrize(
    "room",
    [{"question_locked": True}, {"question_phase": "RESULTS"}],
    ids=["after-the-lock", "after-results"],
)
async def test_an_answer_that_counted_is_acknowledged_not_rejected(store, room):
    """A retry of an answer the server already counted, arriving after the question locked or
    moved on, is acknowledged: never told it came too late, since it counted."""
    await submit("phone")
    store.room.update(room)
    await submit("phone-again")
    assert store.replies("phone-again") == [ALREADY]
    assert store.counted() == COUNTED_ONCE


# ---------------------------------------------------------------------------
# Results are never published while an answer can still change them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["completes", "fails"])
async def test_results_wait_for_a_stuck_answer_and_the_host_can_show_them_again(
    store, monkeypatch, outcome
):
    """An answer is stuck being recorded when the host asks for results, and stays stuck past
    the bounded wait. Nothing is published: the question stays closed (the room is told), and
    the host hears the results are delayed. A second Show Results or a lock toggle meanwhile
    changes nothing. The answer finally completes (or fails), and showing the results again
    publishes them, counting it (or not)."""
    monkeypatch.setattr(gw, "_ANSWER_DRAIN_SECONDS", 0.2)
    reached, resume = store.pause("commit")
    if outcome == "fails":
        store.failing.add("commit")
    recording = submit("phone")
    await reached.wait()

    first = asyncio.ensure_future(show_results())
    await settle()
    assert SESSION in gw._results_building  # waiting for the stuck answer
    await show_results()  # a double click: ignored
    await gw._on_lock_question_impl("projector")  # would reopen the question: ignored
    await first
    assert store.sent(E.QUESTION_RESULTS, HOST_ROOM) == []
    assert store.sent(E.RESULTS_DELAYED, HOST_ROOM) == [
        {"questionId": QUESTION, "answersPending": 1}
    ]
    [locked] = store.sent(E.QUESTION_LOCKED, ROOM)
    assert locked["questionId"] == QUESTION and 19 < locked["remainingSeconds"] <= 20
    assert store.room["question_phase"] == "QUESTION" and store.room["question_locked"]
    assert store.room["timer_remaining_seconds"] == locked["remainingSeconds"]

    resume.set()
    await recording  # it completes, or fails (logged, no reply)
    assert store.replies("phone") == ([ACCEPTED] if outcome == "completes" else [])

    await show_results()  # the host tries again
    host, mine, _ = results(store)
    counted = outcome == "completes"
    assert host["totalAnswered"] == int(counted)
    assert host["answerDistribution"] == ({"true": 1} if counted else {})
    assert (mine["answered"], mine["yourScore"]) == (counted, 1000 if counted else 0)
    assert store.room["question_phase"] == "RESULTS"
    assert len(store.sent(E.QUESTION_LOCKED, ROOM)) == 1  # it was already closed
