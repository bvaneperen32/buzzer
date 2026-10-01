"""
Live-session fixes (docs/plans/fix-live-session-test-cleanup.md): the room's Redis status once a
game starts (B2), and what a real host disconnect and rejoin do to a started game — the lock,
the timer pause, the grace period, a manual lock kept, and an old socket's close arriving after
the new socket's join. Also an answer sent again after a reconnect, while the first attempt is
still in the server or after it failed: it counts once.

Everything a test creates is deleted by exact id afterwards, guests and course included, by the
suite's ownership tracking (conftest.py, "Test data").

The one fixed wait in here is the host's absence itself (the scenario, not a synchronization).
Where a test asserts that nothing happened, it first waits for the server's own log line saying
it handled the close.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ..ledger import token_subject
from .conftest import _REPO_ROOT, db_settings, mint_guest_email, redis_cli
from .engine.socket_client import TestSocketClient as SocketClient


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def backend_log(since: str) -> str:
    """The backend's log since an RFC 3339 time, without colour codes. Uses
    BUZZER_TEST_BACKEND_LOGS (default `docker compose logs --no-log-prefix backend`)."""
    base = shlex.split(
        os.environ.get(
            "BUZZER_TEST_BACKEND_LOGS", "docker compose logs --no-log-prefix backend"
        )
    )
    result = subprocess.run(
        [*base, "--since", since],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"backend logs unavailable: {result.stderr.strip()}")
    return _ANSI.sub("", result.stdout)


async def wait_logged(
    event: str, sid: str | None, since: str, timeout: float = 15.0
) -> None:
    """Wait until the backend has logged `event` (for socket `sid`, if given)."""
    deadline = time.monotonic() + timeout
    while True:
        if any(
            event in line and (sid is None or f"sid={sid}" in line)
            for line in backend_log(since).splitlines()
        ):
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"The backend never logged {event} for {sid}")
        await asyncio.sleep(0.2)


class LiveEnv:
    def __init__(self, base_url: str, admin_token: str):
        self.base_url = base_url
        self.token = admin_token
        self.http = httpx.Client(base_url=base_url, timeout=15.0)
        self.tag = uuid.uuid4().hex[:6]
        self.clients: list[SocketClient] = []

    def ok(self, method: str, path: str, status: int, **kwargs) -> dict:
        r = self.http.request(
            method, path, headers={"Authorization": f"Bearer {self.token}"}, **kwargs
        )
        assert r.status_code == status, f"{method} {path}: {r.status_code} {r.text}"
        return r.json() if r.content else {}

    def game(self, *, time_limit: int = 60, questions: int = 1) -> tuple[int, int]:
        course = self.ok(
            "POST",
            "/api/admin/courses",
            201,
            json={"name": f"Live session {self.tag}", "semester": "Live session tests"},
        )
        game = self.ok(
            "POST",
            "/api/admin/games",
            201,
            json={"title": f"Live session {self.tag}", "course_id": course["id"]},
        )
        for n in range(questions):
            self.ok(
                "POST",
                f"/api/admin/games/{game['id']}/questions",
                201,
                json={
                    "type": "true_false",
                    "grading_type": "COMPLETENESS",
                    "prompt": f"Question {n + 1}",
                    "points_value": 1,
                    "time_limit_seconds": time_limit,
                },
            )
        return course["id"], game["id"]

    def room(self, course_id: int, game_id: int) -> str:
        data = self.ok(
            "POST",
            "/api/game/rooms",
            201,
            json={"course_id": course_id, "game_id": game_id},
        )
        return data["room_code"]

    def room_state(self, code: str) -> dict:
        return json.loads(redis_cli("GET", f"room:{code}"))

    async def join(
        self, token: str, code: str, role: str, label: str
    ) -> tuple[SocketClient, dict]:
        client = SocketClient(self.base_url, token, label)
        self.clients.append(client)
        await client.connect()
        await client.emit("join_room", {"room_code": code, "role": role})
        return client, await client.wait_for("sync_state", timeout=15)

    async def host(self, code: str, label: str = "host") -> tuple[SocketClient, dict]:
        return await self.join(self.token, code, "HOST", label)

    async def player(self, code: str, name: str) -> SocketClient:
        email = mint_guest_email(
            f"live{name}"
        )  # so the suite deletes this guest afterwards
        r = self.http.post(
            "/api/auth/guest",
            json={"display_name": name, "email": email, "room_code": code},
        )
        assert r.status_code == 200, r.text
        client, _ = await self.join(r.json()["access_token"], code, "PLAYER", name)
        return client

    async def close(self) -> None:
        for client in self.clients:
            await client.disconnect()
        self.http.close()


@pytest.fixture()
async def live(docker_stack, base_url: str, admin_token: str):
    try:
        redis_cli("PING")
    except RuntimeError as exc:
        pytest.skip(f"these tests read the room from Redis: {exc}")
    env = LiveEnv(base_url, admin_token)
    yield env
    await env.close()


def no_event(client: SocketClient, *events: str) -> bool:
    return all(client._queues[event].empty() for event in events)


async def start(env: LiveEnv, *, time_limit: int = 60, questions: int = 1):
    course_id, game_id = env.game(time_limit=time_limit, questions=questions)
    code = env.room(course_id, game_id)
    host, _ = await env.host(code)
    player = await env.player(code, "Pat")
    await host.emit("host_advance", {})
    await player.wait_for("new_question", timeout=15)
    return code, host, player


# ---------------------------------------------------------------------------
# B2: the room's status in Redis
# ---------------------------------------------------------------------------


async def test_a_started_game_is_in_progress_in_redis(live):
    """The start used to write the room's pre-start copy back, so Redis said LOBBY for the
    whole game (and the host-disconnect handling, gated on IN_PROGRESS, never ran)."""
    code, host, player = await start(live, questions=2)
    assert live.room_state(code)["status"] == "IN_PROGRESS"

    await host.emit("host_advance", {})  # results
    await host.wait_for("question_results", timeout=15)
    assert live.room_state(code)["status"] == "IN_PROGRESS"
    await host.emit("host_advance", {})  # question 2
    await player.wait_for("new_question", timeout=15)
    assert live.room_state(code)["status"] == "IN_PROGRESS"


# ---------------------------------------------------------------------------
# A real host disconnect, and the host's return
# ---------------------------------------------------------------------------


async def test_host_disconnect_locks_pauses_and_the_rejoin_resumes(live):
    code, host, player = await start(live, time_limit=6)

    await host.disconnect()
    locked = await player.wait_for("question_locked", timeout=10)
    gone = await player.wait_for("host_disconnected", timeout=10)
    remaining = locked["remainingSeconds"]
    assert 4.0 < remaining <= 6.0
    abandon_in = datetime.fromisoformat(gone["abandonAt"]) - datetime.now(timezone.utc)
    assert timedelta(seconds=290) < abandon_in <= timedelta(seconds=300)
    room = live.room_state(code)
    assert room["status"] == "IN_PROGRESS"
    assert (
        room["question_locked"] is True
        and room["question_locked_by_disconnect"] is True
    )
    assert room["timer_remaining_seconds"] == pytest.approx(remaining)

    # Answers are refused while the host is away — as a rejection, not an error.
    qid = room["question_ids"][0]
    await player.emit(
        "submit_answer", {"question_id": qid, "answer_data": {"selectedValue": True}}
    )
    rejected = await player.wait_for("answer_rejected", timeout=10)
    assert rejected == {
        "questionId": qid,
        "reason": "locked",
        "message": "Question is locked",
    }
    assert no_event(player, "error", "answer_received")

    # A phone that reconnects while the host is away learns it from its sync_state.
    _, sync = await live.join(player.token, code, "PLAYER", "Pat again")
    assert sync["hostDisconnected"] is True

    await asyncio.sleep(2.5)  # the host is away for a while (the scenario)

    host, sync = await live.host(code, "host again")
    rejoined = time.monotonic()
    assert sync["questionLocked"] is False
    unlocked = await player.wait_for("question_unlocked", timeout=10)
    assert unlocked["remainingSeconds"] == pytest.approx(remaining)
    await player.wait_for("host_reconnected", timeout=10)
    _, sync = await live.join(player.token, code, "PLAYER", "Pat once more")
    assert sync["hostDisconnected"] is False
    room = live.room_state(code)
    assert (
        room["question_locked"] is False and "question_locked_by_disconnect" not in room
    )

    # The time the host was away didn't count: the countdown resumes with what was left.
    ended = await host.wait_for("answer_phase_ended", timeout=remaining + 5)
    elapsed = time.monotonic() - rejoined
    assert ended["questionId"] == qid
    assert remaining - 0.5 <= elapsed <= remaining + 1.5, (remaining, elapsed)
    assert (await player.wait_for("question_locked", timeout=5))[
        "remainingSeconds"
    ] == 0


async def test_a_manual_lock_survives_the_host_disconnect_and_rejoin(live):
    code, host, player = await start(live)
    await host.emit("host_lock_question", {})
    remaining = (await player.wait_for("question_locked", timeout=10))[
        "remainingSeconds"
    ]

    await host.disconnect()
    await player.wait_for("host_disconnected", timeout=10)
    assert no_event(player, "question_locked")  # already locked: nothing more to pause
    room = live.room_state(code)
    assert (
        room["question_locked"] is True and "question_locked_by_disconnect" not in room
    )

    host, sync = await live.host(code, "host again")
    assert sync["questionLocked"] is True  # the state after the rejoin: still locked
    assert sync["timerRemainingSeconds"] == pytest.approx(remaining)
    assert live.room_state(code)["question_locked"] is True
    # The phones hear the host is back, though the question stays locked.
    await player.wait_for("host_reconnected", timeout=10)
    assert no_event(player, "question_unlocked")

    await host.emit("host_lock_question", {})  # the host unlocks it by hand
    unlocked = await player.wait_for("question_unlocked", timeout=10)
    assert unlocked["remainingSeconds"] == pytest.approx(remaining)


async def test_an_old_host_socket_closing_after_the_rejoin_changes_nothing(live):
    """A dropped connection can reach the server after the host's new socket has joined. That
    late close must not lock the question or start the grace period; the next real close does."""
    code, old, player = await start(live)
    new, _ = await live.host(code, "host again")
    since = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    old_sid = old.sio.get_sid()

    await old.disconnect()
    await wait_logged("host_socket_closed_host_still_seated", old_sid, since)
    assert no_event(player, "question_locked", "host_disconnected")
    assert live.room_state(code)["question_locked"] is False

    await new.disconnect()  # now the host is really gone
    await player.wait_for("question_locked", timeout=10)
    await player.wait_for("host_disconnected", timeout=10)
    await live.host(code, "host back")  # cancels the grace period
    await player.wait_for("question_unlocked", timeout=10)


async def test_late_answers_are_rejected_and_real_failures_stay_errors(live):
    """An answer that arrives after its question moved on is a rejection the player's screen
    survives. A malformed request or a refused join is still an error."""
    code, host, player = await start(live, questions=2)
    qid = live.room_state(code)["question_ids"][0]
    await host.emit("host_advance", {})  # results: the question has moved on
    await host.wait_for("question_results", timeout=10)
    await player.wait_for("question_results", timeout=10)

    await player.emit(
        "submit_answer", {"question_id": qid, "answer_data": {"selectedValue": True}}
    )
    assert await player.wait_for("answer_rejected", timeout=10) == {
        "questionId": qid,
        "reason": "closed",
        "message": "No active question",
    }

    await host.emit("host_advance", {})  # question 2
    q2 = (await player.wait_for("new_question", timeout=10))["questionId"]
    await player.emit(
        "submit_answer", {"question_id": qid, "answer_data": {"selectedValue": True}}
    )
    assert await player.wait_for("answer_rejected", timeout=10) == {
        "questionId": qid,
        "reason": "closed",
        "message": "Question ID does not match the active question",
    }
    assert no_event(player, "error", "answer_received")

    await player.emit("submit_answer", {"question_id": str(q2), "answer_data": {}})
    assert (await player.wait_for("error", timeout=10))[
        "message"
    ] == "question_id must be an integer"

    await host.emit("host_advance", {})  # results
    await host.wait_for("question_results", timeout=10)
    await host.emit("host_advance", {})  # game over
    await host.wait_for("game_over", timeout=10)
    client = SocketClient(live.base_url, player.token, "late joiner")
    live.clients.append(client)
    await client.connect()
    await client.emit("join_room", {"room_code": code, "role": "PLAYER"})
    refused = await client.wait_for("error", timeout=10)
    assert refused["message"] == "This game is not accepting players"
    assert no_event(client, "answer_rejected", "sync_state")


# ---------------------------------------------------------------------------
# An answer sent again after a reconnect counts once
# ---------------------------------------------------------------------------
#
# A phone whose connection drops before the server's reply arrives sends its answer again once
# it has re-joined, if its sync_state says the server has none yet. The first attempt may still
# be inside the server then, or may have failed there. These tests hold the first attempt at a
# known point: an answer's INSERT share-locks its question's row (the foreign key check), so
# while the test holds that row FOR UPDATE, the server's recording of any answer to the
# question waits at its INSERT, before the player is marked answered or scored.


def mysql_connect(*, autocommit: bool):
    """A connection as the backend's own MySQL account, which can also see (and interrupt) the
    backend's statements. Skips with the reason if MySQL can't be reached."""
    try:
        import pymysql
    except ImportError:
        pytest.skip("PyMySQL isn't installed (tests/integration/requirements.txt)")
    settings = db_settings()
    try:
        return pymysql.connect(
            host=settings["host"],
            port=settings["port"],
            user=settings["user"],
            password=settings["password"],
            database=settings["database"],
            connect_timeout=5,
            read_timeout=30,
            autocommit=autocommit,
        )
    except pymysql.MySQLError as exc:
        pytest.skip(
            f"these tests hold a MySQL row lock: can't reach MySQL at {settings['host']}:"
            f"{settings['port']} as {settings['user']} ({exc}); set BUZZER_TEST_DB_*"
        )


class QuestionRowLock:
    """Holds a question's row FOR UPDATE until the block ends (or `release()`)."""

    def __init__(self, question_id: int):
        self.question_id = question_id
        self.conn = mysql_connect(autocommit=False)
        self.watch = mysql_connect(autocommit=True)

    def __enter__(self) -> QuestionRowLock:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM questions WHERE id = %s FOR UPDATE", (self.question_id,)
            )
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
        self.watch.close()

    def release(self) -> None:
        if self.conn.open:
            self.conn.rollback()
            self.conn.close()

    def waiting_inserts(self) -> list[int]:
        """The backend's connections whose INSERT into session_scores is running now."""
        with self.watch.cursor() as cur:
            cur.execute(
                "SELECT id FROM information_schema.processlist "
                "WHERE db = DATABASE() AND info LIKE 'INSERT INTO session_scores%%'"
            )
            return [row[0] for row in cur.fetchall()]

    async def inserts_waiting(self, count: int, timeout: float = 15.0) -> list[int]:
        deadline = time.monotonic() + timeout
        while True:
            ids = self.waiting_inserts()
            if len(ids) == count:
                return ids
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"{len(ids)} answer INSERT(s) running in the backend, expected {count}"
                )
            await asyncio.sleep(0.05)

    def interrupt(self, connection_id: int) -> None:
        """KILL QUERY: the backend's statement fails, as a database error would fail it."""
        with self.watch.cursor() as cur:
            cur.execute("KILL QUERY %s", (connection_id,))


def recorded(session_id: str, user_id: str, question_id: int) -> dict:
    """What the server holds for one player's answer: its MySQL rows (and their points), and in
    Redis the player's running score, the answered mark and the answer distribution."""
    conn = mysql_connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(points_awarded), 0) FROM session_scores "
                "WHERE session_id = %s AND user_id = %s AND question_id = %s",
                (session_id, user_id, question_id),
            )
            rows, points = cur.fetchone()
    finally:
        conn.close()
    player = json.loads(redis_cli("GET", f"session:{session_id}:player:{user_id}"))
    flat = redis_cli("HGETALL", f"session:{session_id}:dist:{question_id}").splitlines()
    answered = redis_cli(
        "SISMEMBER", f"session:{session_id}:answered:{question_id}", user_id
    )
    return {
        "rows": rows,
        "points": float(points),
        "score": player["score"],
        "dist": {flat[i]: int(flat[i + 1]) for i in range(0, len(flat), 2)},
        "answered": answered == "1",
    }


COUNTED_ONCE = {
    "rows": 1,
    "points": 1.0,
    "score": 1,
    "dist": {"true": 1},
    "answered": True,
}


NOTHING = {"rows": 0, "points": 0.0, "score": 0, "dist": {}, "answered": False}


@contextmanager
def redis_refusing_counts():
    """Deny the backend's Redis user HINCRBY, which counting an answer needs (its distribution),
    until the block ends. `apply_answer`'s MULTI/EXEC is then refused as a whole (EXECABORT):
    nothing of the answer is counted in Redis, though its row is committed in MySQL."""
    redis_cli("ACL", "SETUSER", "default", "-hincrby")
    try:
        yield
    finally:
        redis_cli("ACL", "SETUSER", "default", "+hincrby")


async def test_a_retry_overlapping_the_first_attempt_counts_once(live):
    """The first attempt is held before it marks the player answered. The phone's connection
    drops, it re-joins, its sync_state says no answer yet, and it sends the answer again. Once
    the first attempt carries on, the answer is persisted, scored and counted in the
    distribution once, and the retry is told the server already has it."""
    code, host, player = await start(live)
    room = live.room_state(code)
    session_id, qid = room["session_id"], room["question_ids"][0]
    user_id = token_subject(player.token)
    answer = {"question_id": qid, "answer_data": {"selectedValue": True}}

    with QuestionRowLock(qid) as lock:
        await player.emit("submit_answer", answer)
        await lock.inserts_waiting(1)  # the first attempt, held at its INSERT
        await player.disconnect()  # the phone's connection drops before the reply
        retry, sync = await live.join(player.token, code, "PLAYER", "Pat again")
        assert not sync["hasAnswered"]  # so the phone sends its answer again
        since = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
        await retry.emit("submit_answer", answer)
        # The retry is in the server, waiting for the held attempt's outcome.
        await wait_logged(
            "submit_answer_waits_for_earlier_attempt", retry.sio.get_sid(), since
        )
    # The row lock is released: the first attempt carries on, then the retry.
    reply = await retry.wait_for("answer_received", timeout=15)
    await host.emit("host_advance", {})  # results
    results = await host.wait_for("question_results", timeout=15)

    assert recorded(session_id, user_id, qid) == COUNTED_ONCE
    assert (results["totalAnswered"], results["answerDistribution"]) == (1, {"true": 1})
    assert reply == {"questionId": qid, "alreadyAnswered": True}
    assert no_event(retry, "answer_rejected", "error")


async def test_a_failed_recording_counts_once(live):
    """An attempt that fails inside the server gets no reply, and the answer still ends up
    counted exactly once.

    1. Its INSERT fails (interrupted, as a database error would end it). Nothing is left, and
       the retry after the phone's reconnect records it.
    2. Redis refuses to count it after its row is committed, and the phone doesn't reconnect.
       Show Results counts the stored answer before publishing, so the results agree on it —
       answered, points, total, rank and distribution — and a later retry is only
       acknowledged."""
    code, host, player = await start(live, questions=2)
    room = live.room_state(code)
    session_id, (q1, q2) = room["session_id"], room["question_ids"]
    user_id = token_subject(player.token)
    since = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()

    # 1. It fails before its commit.
    answer = {"question_id": q1, "answer_data": {"selectedValue": True}}
    with QuestionRowLock(q1) as lock:
        await player.emit("submit_answer", answer)
        [attempt] = await lock.inserts_waiting(1)
        lock.interrupt(attempt)
        await wait_logged("submit_answer_failed", player.sio.get_sid(), since)
    assert recorded(session_id, user_id, q1) == NOTHING
    assert no_event(player, "answer_received", "answer_rejected", "error")

    await player.disconnect()
    player, sync = await live.join(player.token, code, "PLAYER", "Pat again")
    assert not sync["hasAnswered"]
    # The open question, sent again to a re-joining player with no answer (the late-join push).
    assert (await player.wait_for("new_question", timeout=15))["questionId"] == q1
    await player.emit("submit_answer", answer)
    reply = await player.wait_for("answer_received", timeout=15)
    assert reply["pointsAwarded"] == 1 and "alreadyAnswered" not in reply
    assert recorded(session_id, user_id, q1) == COUNTED_ONCE

    await host.emit("host_advance", {})  # results
    await player.wait_for("question_results", timeout=15)
    await host.emit("host_advance", {})  # question 2
    assert (await player.wait_for("new_question", timeout=15))["questionId"] == q2

    # 2. It fails after its commit, and the phone doesn't reconnect.
    answer = {"question_id": q2, "answer_data": {"selectedValue": True}}
    with redis_refusing_counts():
        await player.emit("submit_answer", answer)
        await wait_logged("submit_answer_failed", player.sio.get_sid(), since)
    # The row alone (the running score, 1, is question 1's).
    assert recorded(session_id, user_id, q2) == {
        "rows": 1,
        "points": 1.0,
        "score": 1,
        "dist": {},
        "answered": False,
    }
    assert no_event(player, "answer_received", "answer_rejected", "error")

    await host.emit("host_advance", {})  # results: the stored answer is counted first
    results = await host.wait_for("question_results", timeout=15)
    mine = await player.wait_for("question_results", timeout=15)
    assert (results["totalAnswered"], results["answerDistribution"]) == (1, {"true": 1})
    counted = (
        mine["answered"],
        mine["yourPoints"],
        mine["yourScore"],
        mine["yourRank"],
    )
    assert counted == (True, 1, 2, 1)
    once = {"rows": 1, "points": 1.0, "score": 2, "dist": {"true": 1}, "answered": True}
    assert recorded(session_id, user_id, q2) == once

    await player.emit("submit_answer", answer)  # sent again later: acknowledged only
    reply = await player.wait_for("answer_received", timeout=15)
    assert reply == {"questionId": q2, "alreadyAnswered": True}
    assert recorded(session_id, user_id, q2) == once


# ---------------------------------------------------------------------------
# Results count exactly the answers the server accepted
# ---------------------------------------------------------------------------


async def test_results_wait_for_answers_still_being_recorded(live):
    """Show Results closes the question, then waits for the answers still being recorded, so
    the results count exactly the answers acknowledged.

    1. The answer finishes within the wait. It is counted, and the phone hears its
       acknowledgement before the results. An answer sent after the request is refused as late.
    2. It is still being recorded when the wait (10 s) runs out. Nothing is published: the room
       is told the question closed, and the host that the results are delayed. Once the answer
       finishes, showing the results again counts it."""
    code, host, player = await start(live, questions=2)
    other = await live.player(code, "Lee")
    await other.wait_for("new_question", timeout=15)  # the late-join question
    q1, q2 = live.room_state(code)["question_ids"]
    since = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()

    # 1. It finishes within the wait.
    answer = {"question_id": q1, "answer_data": {"selectedValue": True}}
    with QuestionRowLock(q1) as lock:
        await player.emit("submit_answer", answer)
        await lock.inserts_waiting(1)  # still being recorded...
        await host.emit("host_advance", {})  # ...when the host asks for the results
        await wait_logged("question_results_wait_for_answers", None, since)
        await other.emit("submit_answer", answer)
        late = await other.wait_for("answer_rejected", timeout=10)
    results = await host.wait_for("question_results", timeout=15)
    mine = await player.wait_for("question_results", timeout=15)
    theirs = await other.wait_for("question_results", timeout=15)
    ack = await player.wait_for("answer_received", timeout=15)

    assert (results["totalAnswered"], results["answerDistribution"]) == (1, {"true": 1})
    assert player.order.index("answer_received") < player.order.index(
        "question_results"
    )
    assert ack["pointsAwarded"] == 1
    assert (mine["answered"], mine["yourPoints"]) == (True, 1)
    assert late == {
        "questionId": q1,
        "reason": "locked",
        "message": "Question is locked",
    }
    assert (theirs["answered"], theirs["yourPoints"]) == (False, 0)

    # 2. It is still being recorded when the wait runs out.
    await host.emit("host_advance", {})  # question 2
    assert (await player.wait_for("new_question", timeout=15))["questionId"] == q2
    answer = {"question_id": q2, "answer_data": {"selectedValue": True}}
    with QuestionRowLock(q2) as lock:
        await player.emit("submit_answer", answer)
        await lock.inserts_waiting(1)
        await host.emit("host_advance", {})
        delayed = await host.wait_for("results_delayed", timeout=20)
        assert delayed == {"questionId": q2, "answersPending": 1}
        assert (await other.wait_for("question_locked", timeout=5))["questionId"] == q2
        assert no_event(host, "question_results")
        assert no_event(player, "question_results", "answer_received")
    ack = await player.wait_for("answer_received", timeout=15)  # it finishes
    await host.emit("host_advance", {})  # the host shows the results again
    results = await host.wait_for("question_results", timeout=15)
    mine = await player.wait_for("question_results", timeout=15)
    assert (results["totalAnswered"], results["answerDistribution"]) == (1, {"true": 1})
    assert (mine["answered"], mine["yourPoints"], ack["pointsAwarded"]) == (True, 1, 1)


async def test_each_players_results_say_whether_their_answer_counted(live):
    """Every question type's per-player results carry `answered`, from the server's own record:
    an accepted answer is answered even when it scored nothing (a wrong option, a question worth
    0 points), and a refused or missing one isn't. The phone's "No answer" comes from this."""
    course = live.ok(
        "POST",
        "/api/admin/courses",
        201,
        json={"name": f"Live session {live.tag}", "semester": "Live session tests"},
    )
    game = live.ok(
        "POST",
        "/api/admin/games",
        201,
        json={"title": f"Live session {live.tag}", "course_id": course["id"]},
    )
    for question in (
        {
            "type": "multiple_choice",
            "grading_type": "ACCURACY",
            "prompt": "Closest to the Sun?",
            "config": {"options": ["Venus", "Mercury", "Mars", "Earth"]},
            "answer_data": {"answer_points": [0, 1000, 0, 0]},
            "points_value": 1000,
        },
        {
            "type": "true_false",
            "grading_type": "COMPLETENESS",
            "prompt": "Worth nothing",
            "points_value": 0,
        },
    ):
        live.ok(
            "POST",
            f"/api/admin/games/{game['id']}/questions",
            201,
            json={**question, "time_limit_seconds": 60},
        )
    code = live.room(course["id"], game["id"])
    host, _ = await live.host(code)
    ann, bob, cy, dee = [
        await live.player(code, name) for name in ("Ann", "Bob", "Cy", "Dee")
    ]
    players = {"Ann": ann, "Bob": bob, "Cy": cy, "Dee": dee}
    await host.emit("host_advance", {})
    q1 = (await ann.wait_for("new_question", timeout=15))["questionId"]

    async def answer(client: SocketClient, qid: int, data: dict, reply: str) -> dict:
        await client.emit("submit_answer", {"question_id": qid, "answer_data": data})
        return await client.wait_for(reply, timeout=10)

    assert (await answer(ann, q1, {"selectedIndex": 1}, "answer_received"))[
        "pointsAwarded"
    ] == 1000
    wrong = await answer(bob, q1, {"selectedIndex": 0}, "answer_received")
    assert (wrong["pointsAwarded"], wrong["isCorrect"]) == (0, False)
    await host.emit("host_lock_question", {})
    await cy.wait_for("question_locked", timeout=10)
    assert (await answer(cy, q1, {"selectedIndex": 1}, "answer_rejected"))[
        "reason"
    ] == "locked"
    await host.emit("host_advance", {})  # results
    results = {
        name: await c.wait_for("question_results", timeout=15)
        for name, c in players.items()
    }
    assert {name: (r["answered"], r["yourPoints"]) for name, r in results.items()} == {
        "Ann": (True, 1000),
        "Bob": (True, 0),  # answered: a wrong option scores 0 but still counted
        "Cy": (False, 0),  # refused as late
        "Dee": (False, 0),  # never answered
    }

    await host.emit("host_advance", {})  # question 2: completeness, worth 0 points
    q2 = (await ann.wait_for("new_question", timeout=15))["questionId"]
    assert (await answer(ann, q2, {"selectedValue": True}, "answer_received"))[
        "pointsAwarded"
    ] == 0
    await host.emit("host_advance", {})  # results
    results = {
        name: await c.wait_for("question_results", timeout=15)
        for name, c in players.items()
    }
    assert {name: r["answered"] for name, r in results.items()} == {
        "Ann": True,  # recorded, though it scored 0
        "Bob": False,
        "Cy": False,
        "Dee": False,
    }
