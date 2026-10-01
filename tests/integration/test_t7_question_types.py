"""
T7 integration tests: Sort It and Spot It (docs/plans/t7-question-types.md §4.5).

Runs against the compose dev stack, directly on port 8000, like test_t8_images.py — whose
per-test environment (users, courses, games, images, rooms, sockets, cleanup), MySQL lock
harness and padding-question headroom helper this module reuses:

    cd tests/integration && pytest test_t7_question_types.py -v

Two kinds of test reach MySQL directly, with test_t8_images.py's BUZZER_TEST_DB_* settings:
- the lock tests, which hold an `images` or `questions` row lock from a test connection to
  force the order of two requests (RowLockHarness, below), and
- the planted-data tests (a stale stamp, a missing image), and read-backs of stored JSON.

The lock tests are required: if they skip because the database can't be reached, T7's
verification is incomplete, not passed.

Commit timing: question updates commit after the response is sent (get_db), so reads that
follow them poll with wait_until; question creates, image writes and room creation commit
before responding.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from html.parser import HTMLParser

import httpx
import pytest

from .conftest import create_guest_tokens, redis_cli, wait_until
from .engine.socket_client import TestSocketClient as SocketClient
from .test_t8_images import (
    _SOCKET_TIMEOUT,
    EXPORT_LIMIT,
    LockHarness,
    T8Env,
    bundle_image,
    bundle_v1,
    bundle_v2,
    db_connect,
    noise_jpeg,
    padding_question,
    plant_config,
    png,
    tf,
)

LANDSCAPE, PORTRAIT = (1600, 900), (900, 1600)

BOXES = ["Mammal", "Reptile", "Amphibian"]
CARDS = ["Whale", "Gecko", "Frog", "Bat", "Newt", "Tortoise", "Dolphin", "Salamander"]
KEY = [0, 1, 2, 0, 2, 1, 0, 2]


def cell(col: int, row: int) -> dict:
    return {"col": col, "row": row}


CELLS = [cell(4, 1), cell(4, 2)]
OMIT = object()


def landscape(color: str = "red") -> bytes:
    return png(color, LANDSCAPE)


def portrait(color: str = "blue") -> bytes:
    return png(color, PORTRAIT)


def stamp(image_id: int, version: int, cols: int, rows: int) -> dict:
    return {"image_id": image_id, "image_version": version, "cols": cols, "rows": rows}


def sort_body(
    *,
    boxes=BOXES,
    cards=CARDS,
    key=KEY,
    grading: str = "ACCURACY",
    points: float = 1000,
    prompt: str = "Sort by class",
    **config,
) -> dict:
    return {
        "type": "sort_into_boxes",
        "grading_type": grading,
        "prompt": prompt,
        "config": {"boxes": list(boxes), "cards": list(cards), **config},
        "answer_data": {} if key is None else {"correct_boxes": list(key)},
        "points_value": points,
        "time_limit_seconds": 30,
    }


def spot_body(
    image_id,
    *,
    cols: int = 6,
    rows: int = 4,
    cells=CELLS,
    grading: str = "ACCURACY",
    points: float = 1000,
    confirm=OMIT,
    prompt: str = "Find the lighthouse",
    **config,
) -> dict:
    """A Spot It question. `confirm` is the stamp to send: omitted by default, `None` sends
    an explicit `"confirmed_for": null`. `cells=None` omits accepted_cells."""
    answer_data: dict = {}
    if cells is not None:
        answer_data["accepted_cells"] = list(cells)
    if confirm is not OMIT:
        answer_data["confirmed_for"] = confirm
    return {
        "type": "find_in_image",
        "grading_type": grading,
        "prompt": prompt,
        "config": {"image_id": image_id, "grid": {"cols": cols, "rows": rows}, **config},
        "answer_data": answer_data,
        "points_value": points,
        "time_limit_seconds": 30,
    }


def message(r: httpx.Response) -> str:
    """A 4xx's text: the service's `message`, or a request-validation 422's joined `msg`s."""
    body = r.json()
    if "message" in body:
        return body["message"]
    return "; ".join(error.get("msg", "") for error in body.get("detail", []))


def blocked(*sentences: str) -> str:
    """The room gates' 409 (spec §2.5)."""
    return (
        "These Spot It questions need review before this game can run: "
        + " ".join(sentences)
        + " Open the game in the editor, fix each question, check its accepted cells, "
        "and confirm it."
    )


FREEZE_FIX = (
    "To change it now, the session's host can finish the game or delete the session from "
    "Play (deleting discards its partial scores); admins can delete sessions from Sessions."
)


def frozen(number: int) -> str:
    return (
        f"Q{number} can't be changed while a session of this game is live; only its prompt, "
        "time limit and (Sort It) prompt image can change until the session ends."
    )


def becomes_t7(number: int) -> str:
    return (
        f"Q{number} can't become a Sort It or Spot It question while a session of this game "
        "is live."
    )


UNCONFIRMED_CREATE = (
    "A Spot It question can't be added unconfirmed while a session of this game is live; "
    "check its accepted cells and confirm it first."
)


def too_dense_to_confirm(cols: int, rows: int, max_cols: int, max_rows: int) -> str:
    return (
        f"Can't confirm: the {cols}×{rows} grid is too dense for this image (at most "
        f"{max_cols}×{max_rows}). Change the grid, re-check the accepted cells, and confirm "
        "again."
    )


# ---------------------------------------------------------------------------
# Direct MySQL reads and plants
# ---------------------------------------------------------------------------


def db_query(sql: str, args=()) -> list[tuple]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())
    finally:
        conn.close()


def db_question(qid: int) -> dict:
    """The question as stored in MySQL (committed state)."""
    [(config, answer_data, points, prompt, limit)] = db_query(
        "SELECT config, answer_data, points_value, prompt, time_limit_seconds "
        "FROM questions WHERE id = %s",
        (qid,),
    )
    return {
        "config": json.loads(config),
        "answer_data": json.loads(answer_data),
        "points_value": points,
        "prompt": prompt,
        "time_limit_seconds": limit,
    }


def stored_when(qid: int, check, what: str) -> dict:
    """The stored question once `check(stored)` holds (updates commit after responding)."""
    return wait_until(
        lambda: (lambda stored: stored if check(stored) else None)(db_question(qid)),
        what=what,
    )


def plant_answer_data(qid: int, answer_data: dict) -> None:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE questions SET answer_data = %s WHERE id = %s",
                (json.dumps(answer_data), qid),
            )
        conn.commit()
    finally:
        conn.close()


def score_rows(session_id: str, question_id: int) -> int:
    [(count,)] = db_query(
        "SELECT COUNT(*) FROM session_scores WHERE session_id = %s AND question_id = %s",
        (session_id, question_id),
    )
    return count


def session_rows(game_id: int) -> int:
    [(count,)] = db_query("SELECT COUNT(*) FROM game_sessions WHERE game_id = %s", (game_id,))
    return count


def redis_rooms_for_game(game_id: int) -> list[str]:
    rooms = []
    for key in redis_cli("--scan", "--pattern", "room:*").split():
        raw = redis_cli("GET", key)
        try:
            state = json.loads(raw)
        except ValueError:
            continue
        if isinstance(state, dict) and state.get("game_id") == game_id:
            rooms.append(key)
    return rooms


# ---------------------------------------------------------------------------
# The lock harness, extended to images and questions rows (spec §4.5)
# ---------------------------------------------------------------------------


class RowLockHarness(LockHarness):
    """T8's harness plus a row lock on `images` or `questions`, and waits on any statement.

    Each wait pattern names the table, the row predicate and the lock clause; ids are
    interpolated literally, as T8's pattern already assumes of the rendered SQL. T8's class is
    unchanged, so its tests behave exactly as before.
    """

    TABLES = ("games", "images", "questions")

    def lock_row(self, table: str, row_id: int) -> None:
        assert table in self.TABLES, table
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {table} WHERE id = %s FOR UPDATE", (row_id,))

    def waiting_on(self, pattern: re.Pattern) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT INFO FROM information_schema.PROCESSLIST "
                "WHERE ID <> CONNECTION_ID() AND INFO IS NOT NULL"
            )
            return sum(1 for (info,) in cur.fetchall() if pattern.search(info))

    async def wait_for(
        self, pattern: re.Pattern, count: int = 1, timeout: float = 10.0
    ) -> None:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            seen = self.waiting_on(pattern)
            if seen >= count:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"Only {seen} of {count} statements matching {pattern.pattern!r} were seen "
            f"waiting within {timeout}s. PROCESSLIST shows only the connecting account's own "
            "connections: set BUZZER_TEST_DB_USER to the backend's MySQL account (app_user)."
        )


def lock_game_waits(game_id: int) -> re.Pattern:
    return re.compile(rf"FROM games\s+WHERE games\.id = {game_id}\s+FOR UPDATE", re.I)


def locked_image_waits(image_id: int, game_id: int) -> re.Pattern:
    return re.compile(
        rf"FROM images\s+WHERE images\.id = {image_id} AND images\.game_id = {game_id}"
        rf"\s+FOR UPDATE",
        re.I,
    )


def image_meta_waits(image_id: int, game_id: int) -> re.Pattern:
    return re.compile(
        rf"FROM images\s+WHERE images\.id = {image_id} AND images\.game_id = {game_id}"
        rf"\s+FOR SHARE",
        re.I,
    )


def get_question_waits(question_id: int, game_id: int) -> re.Pattern:
    return re.compile(
        rf"FROM questions\s+WHERE questions\.id = {question_id} AND "
        rf"questions\.game_id = {game_id}\s+FOR SHARE",
        re.I,
    )


def gate_questions_waits(game_id: int) -> re.Pattern:
    return re.compile(
        rf"FROM questions\s+WHERE questions\.game_id = {game_id}\s+FOR SHARE", re.I
    )


# ---------------------------------------------------------------------------
# Environment and sockets
# ---------------------------------------------------------------------------


class RecordingClient(SocketClient):
    """A socket that also records every event it receives, tracked or not."""

    def __init__(self, *args, **kwargs):
        self.received: list[tuple[str, object]] = []
        super().__init__(*args, **kwargs)

    def _register_handlers(self):
        for event in list(self._queues):

            @self.sio.on(event)
            async def _handler(data=None, _ev=event):
                self.received.append((_ev, data))
                await self._queues[_ev].put(data or {})

        @self.sio.on("*")
        async def _any(event, *args):
            self.received.append((event, args[0] if args else None))


class T7Env(T8Env):
    def put_question(self, game_id: int, qid: int, body: dict, token=None) -> httpx.Response:
        return self.req(
            "PUT",
            f"/api/host/games/{game_id}/questions/{qid}",
            token or self.admin_token,
            json=body,
        )

    def put_ok(self, game_id: int, qid: int, body: dict) -> dict:
        r = self.put_question(game_id, qid, body)
        assert r.status_code == 200, r.text
        return r.json()

    def try_room(self, course_id: int, game_id: int, token=None) -> httpx.Response:
        r = self.req(
            "POST",
            "/api/game/rooms",
            token or self.admin_token,
            json={"course_id": course_id, "game_id": game_id},
        )
        if r.status_code == 201:
            self.session_ids.append(r.json()["session_id"])
        return r

    def report(self, session_id: str, token=None) -> str:
        r = self.req("GET", f"/api/host/sessions/{session_id}/report", token or self.admin_token)
        assert r.status_code == 200, r.text
        return r.text

    async def recording_socket(self, token: str, room_code: str, role: str, label: str):
        client = RecordingClient(self.base_url, token, label)
        self.sockets.append(client)
        await client.connect()
        await client.emit("join_room", {"room_code": room_code, "role": role})
        await client.wait_for("sync_state", timeout=_SOCKET_TIMEOUT)
        return client


@pytest.fixture()
def t7(docker_stack, base_url: str, admin_token: str):
    # Async tests close their own sockets (t7.close_sockets() in a finally).
    env = T7Env(base_url, admin_token)
    yield env
    env.cleanup()


async def join_players(t7: T7Env, room_code: str, n: int) -> tuple[list, list[str]]:
    tokens = create_guest_tokens(t7.base_url, room_code, n)
    players = [
        await t7.socket(token, room_code, "PLAYER", f"player{i}")
        for i, token in enumerate(tokens)
    ]
    return players, tokens


async def advance_to_question(host, players) -> tuple[dict, list[dict]]:
    await host.emit("host_advance", {})
    payload = await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
    received = [await p.wait_for("new_question", timeout=_SOCKET_TIMEOUT) for p in players]
    assert all(p["questionId"] == payload["questionId"] for p in received)
    return payload, received


async def next_of(client, *events: str) -> tuple[str, dict]:
    """The first of `events` to arrive, and its payload."""
    tasks = {
        asyncio.create_task(client.wait_for(event, timeout=_SOCKET_TIMEOUT)): event
        for event in events
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    task = done.pop()
    return tasks[task], task.result()


async def submit(player, question_id: int, answer) -> tuple[str, dict]:
    await player.emit("submit_answer", {"question_id": question_id, "answer_data": answer})
    return await next_of(player, "answer_received", "error")


async def show_results(host, players) -> tuple[dict, list[dict]]:
    await host.emit("host_advance", {})
    host_results = await host.wait_for("question_results", timeout=_SOCKET_TIMEOUT)
    results = [await p.wait_for("question_results", timeout=_SOCKET_TIMEOUT) for p in players]
    return host_results, results


async def finish(host, players) -> tuple[dict, list[dict]]:
    await host.emit("host_advance", {})
    host_over = await host.wait_for("game_over", timeout=_SOCKET_TIMEOUT)
    overs = [await p.wait_for("game_over", timeout=_SOCKET_TIMEOUT) for p in players]
    return host_over, overs


def placements(key: list[int], k: int, right: int, wrong: int) -> dict:
    """An answer placing the first `right` cards correctly, the next `wrong` in the next box
    over, and leaving the rest out."""
    answer: list[int | None] = []
    for i, correct in enumerate(key):
        if i < right:
            answer.append(correct)
        elif i < right + wrong:
            answer.append((correct + 1) % k)
        else:
            answer.append(None)
    return {"placements": answer}


def sort_distribution(answers: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for answer in answers:
        for i, box in enumerate(answer["placements"]):
            key = f"{i}:{'none' if box is None else box}"
            dist[key] = dist.get(key, 0) + 1
    return dist


# ---------------------------------------------------------------------------
# Validation (spec §2.3, §2.4)
# ---------------------------------------------------------------------------


def test_structural_validation_on_create_and_update(t7):
    _, game_id = t7.setup_game()
    _, other_game = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    foreign = t7.upload_ok(other_game, landscape("green"))["id"]

    creates = [
        (sort_body(key=[0, 1, 3, 0, 2, 1, 0, 2]), "Card 3's correct box must be a box index"),
        (sort_body(key=[0, True, 2, 0, 2, 1, 0, 2]), "Card 2's correct box must be a box index"),
        (spot_body(image, cols=1), "config.grid.cols must be a whole number from 2 to 10"),
        (spot_body(image, rows=11), "config.grid.rows must be a whole number from 2 to 10"),
        (spot_body(image, cells=[cell(4, 1), cell(4, 1)]), "Accepted cell E2 is listed twice"),
        (spot_body(image, cells=[{"col": True, "row": 1}]), "must use whole numbers"),
        (spot_body(image, cells=[cell(6, 0)]), "Accepted cell 1 is outside the 6×4 grid"),
        (spot_body(foreign), f"Image {foreign} isn't in this game's library"),
        (spot_body(None), "config.image_id is required"),
        (spot_body(image, prompt_image_id=image), "config.prompt_image_id must be absent"),
    ]
    for body, text in creates:
        r = t7.create_question(game_id, body)
        assert r.status_code == 422, (body, r.text)
        assert text in message(r), message(r)
    no_image = spot_body(image)
    del no_image["config"]["image_id"]
    r = t7.create_question(game_id, no_image)
    assert r.status_code == 422 and "config.image_id is required" in message(r)
    assert t7.questions(game_id) == []

    sort_q = t7.question_ok(game_id, sort_body())
    spot_q = t7.question_ok(game_id, spot_body(image, confirm=stamp(image, 1, 6, 4)))
    before = t7.questions(game_id)
    grid = {"cols": 6, "rows": 4}
    updates = [
        # A removed box that correct_boxes still uses.
        (sort_q, {"config": {"boxes": BOXES[:2], "cards": CARDS}},
         "Card 3's correct box must be a box index from 0 to 1"),
        (sort_q, {"answer_data": {"correct_boxes": [*KEY[:7], False]}}, "Card 8's correct box"),
        # A grid shrunk under an accepted cell.
        (spot_q, {"config": {"image_id": image, "grid": {"cols": 4, "rows": 4}}},
         "Accepted cell 1 is outside the 4×4 grid"),
        (spot_q, {"config": {"image_id": image, "grid": {"cols": 11, "rows": 4}}},
         "config.grid.cols must be a whole number from 2 to 10"),
        (spot_q, {"config": {"image_id": image, "grid": {"cols": 6, "rows": 1}}},
         "config.grid.rows must be a whole number from 2 to 10"),
        (spot_q, {"answer_data": {"accepted_cells": [cell(0, 0), cell(0, 0)]}},
         "Accepted cell A1 is listed twice"),
        (spot_q, {"answer_data": {"accepted_cells": [{"col": 0, "row": False}]}},
         "must use whole numbers"),
        (spot_q, {"config": {"image_id": foreign, "grid": grid}},
         f"Image {foreign} isn't in this game's library"),
        (spot_q, {"config": {"grid": grid}}, "config.image_id is required"),
        (spot_q, {"config": {"image_id": image, "grid": grid, "prompt_image_id": image}},
         "config.prompt_image_id must be absent or null"),
    ]  # fmt: skip
    for qid, body, text in updates:
        r = t7.put_question(game_id, qid, body)
        assert r.status_code == 422, (body, r.text)
        assert text in message(r), message(r)
    assert t7.questions(game_id) == before  # refused updates change nothing


# ---------------------------------------------------------------------------
# Density gates confirmation and play, not saving (spec §2.4, §2.5)
# ---------------------------------------------------------------------------


def test_density_gates_confirmation_not_saving(t7):
    course_id, game_id = t7.setup_game()
    wide = t7.upload_ok(game_id, landscape())["id"]  # at most 10×6
    tall = t7.upload_ok(game_id, portrait())["id"]  # at most 6×10
    strip = t7.upload_ok(game_id, png("gray", (1200, 200)))["id"]  # 6:1, fits no grid

    for image, cols, rows, limits in ((wide, 10, 7, (10, 6)), (tall, 7, 10, (6, 10))):
        cells = [cell(cols - 1, rows - 1), cell(0, 0)]
        r = t7.create_question(game_id, spot_body(image, cols=cols, rows=rows, cells=cells))
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["needs_review"] is True
        assert created["review_reasons"] == ["grid_too_dense", "not_confirmed"]
        assert created["config"]["grid"] == {"cols": cols, "rows": rows}
        assert created["answer_data"] == {"accepted_cells": cells}
        expected = too_dense_to_confirm(cols, rows, *limits)
        # An attempted confirmation, on update and on create.
        r = t7.put_question(
            game_id,
            created["id"],
            {"answer_data": {"accepted_cells": cells, "confirmed_for": stamp(image, 1, cols, rows)}},
        )
        assert r.status_code == 422 and message(r) == expected, r.text
        r = t7.create_question(
            game_id,
            spot_body(image, cols=cols, rows=rows, cells=cells, confirm=stamp(image, 1, cols, rows)),
        )
        assert r.status_code == 422 and message(r) == expected, r.text

    r = t7.create_question(game_id, spot_body(strip, cols=2, rows=2, cells=[cell(1, 1)]))
    assert r.status_code == 201, r.text
    assert r.json()["review_reasons"] == ["grid_too_dense", "not_confirmed"]
    strip_q = r.json()["id"]
    for cols in (2, 3):  # every confirmation attempt fails: no grid fits a 6:1 image
        r = t7.put_question(
            game_id,
            strip_q,
            {
                "config": {"image_id": strip, "grid": {"cols": cols, "rows": 2}},
                "answer_data": {"accepted_cells": [cell(1, 1)], "confirmed_for": stamp(strip, 1, cols, 2)},
            },
        )  # fmt: skip
        assert r.status_code == 422, r.text
        assert message(r) == (
            "Can't confirm: this image is too wide for any grid (crop it to at most 11:2). "
            "Choose or replace the image, then confirm again."
        )

    assert len(t7.questions(game_id)) == 3  # the three unconfirmed drafts saved
    r = t7.try_room(course_id, game_id)
    assert r.status_code == 409, r.text
    assert message(r) == blocked(
        "Q1 — the 10×7 grid is too dense for its image (at most 10×6); its accepted cells "
        "haven't been checked.",
        "Q2 — the 7×10 grid is too dense for its image (at most 6×10); its accepted cells "
        "haven't been checked.",
        "Q3 — its image is too wide for any grid (crop it to at most 11:2); its accepted "
        "cells haven't been checked.",
    )
    assert session_rows(game_id) == 0


# ---------------------------------------------------------------------------
# Confirmation and the server-owned dimensions (spec §2.4, §2.5)
# ---------------------------------------------------------------------------


def test_confirmation_writes_server_dimensions(t7):
    _, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    for bogus in ({"width": "wide", "height": -5}, {"width": -1, "height": [2]}, {}):
        r = t7.create_question(game_id, spot_body(image, confirm=stamp(image, 1, 6, 4), **bogus))
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["needs_review"] is False and created["review_reasons"] == []
        assert (created["config"]["width"], created["config"]["height"]) == LANDSCAPE
        stored = db_question(created["id"])["config"]
        assert (stored["width"], stored["height"]) == LANDSCAPE

    # A grid-only PUT carries the stamp, which no longer matches the grid.
    qid = created["id"]
    r = t7.put_question(
        game_id,
        qid,
        {"config": {"image_id": image, "grid": {"cols": 5, "rows": 4}, "width": 1, "height": 1}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_reasons"] == ["grid_changed"]
    stored = stored_when(qid, lambda s: s["config"]["grid"]["cols"] == 5, "the grid change")
    assert (stored["config"]["width"], stored["config"]["height"]) == LANDSCAPE
    assert stored["answer_data"]["confirmed_for"] == stamp(image, 1, 6, 4)
    assert t7.question(game_id, qid)["review_reasons"] == ["grid_changed"]


# ---------------------------------------------------------------------------
# Stamp cases (spec §2.5): not sent, omitted/null, supplied
# ---------------------------------------------------------------------------


def test_stamp_cases_on_create_and_update(t7):
    _, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    current = stamp(image, 1, 6, 4)

    # Case 2 on create: an omitted and a null stamp both store no key.
    for body in (spot_body(image), spot_body(image, confirm=None)):
        r = t7.create_question(game_id, body)
        assert r.status_code == 201, r.text
        assert r.json()["review_reasons"] == ["not_confirmed"]
        assert db_question(r.json()["id"])["answer_data"] == {"accepted_cells": CELLS}

    # Case 2 on update: the same, from a confirmed question.
    qid = t7.question_ok(game_id, spot_body(image, confirm=current))
    for sent in ({"accepted_cells": CELLS}, {"accepted_cells": CELLS, "confirmed_for": None}):
        confirmed = t7.put_ok(game_id, qid, {"answer_data": {"accepted_cells": CELLS, "confirmed_for": current}})
        assert confirmed["needs_review"] is False
        stored_when(qid, lambda s: "confirmed_for" in s["answer_data"], "the stamp")
        r = t7.put_question(game_id, qid, {"answer_data": sent})
        assert r.status_code == 200, r.text
        assert r.json()["review_reasons"] == ["not_confirmed"]
        stored = stored_when(qid, lambda s: "confirmed_for" not in s["answer_data"], "no stamp")
        assert stored["answer_data"] == {"accepted_cells": CELLS}  # no null key survives

    # Case 1: answer_data not sent — the stored stamp is carried.
    t7.put_ok(game_id, qid, {"answer_data": {"accepted_cells": CELLS, "confirmed_for": current}})
    r = t7.put_question(game_id, qid, {"prompt": "Where is it?", "answer_data": None})
    assert r.status_code == 200 and r.json()["needs_review"] is False, r.text
    stored = stored_when(qid, lambda s: s["prompt"] == "Where is it?", "the prompt")
    assert stored["answer_data"]["confirmed_for"] == current

    # A replace makes the carried stamp stale: case 1 still saves (not stamp-validated).
    assert t7.replace(game_id, image, landscape("navy")).status_code == 200
    r = t7.put_question(game_id, qid, {"prompt": "Where is it now?"})
    assert r.status_code == 200, r.text
    assert r.json()["review_reasons"] == ["image_replaced"]
    stored = stored_when(qid, lambda s: s["prompt"] == "Where is it now?", "the prompt")
    assert stored["answer_data"]["confirmed_for"] == current

    # Case 3: a supplied stamp equal to the stale stored one → 409; so is another stale one.
    stale = {"answer_data": {"accepted_cells": CELLS, "confirmed_for": current}}
    r = t7.put_question(game_id, qid, stale)
    assert r.status_code == 409, r.text
    assert message(r) == (
        "The image or grid changed after the accepted cells were checked (checked: image "
        f"{image} v1, 6×4; now: v2, 6×4). Review the cells and save again."
    )
    r = t7.put_question(
        game_id, qid, {"answer_data": {"accepted_cells": CELLS, "confirmed_for": stamp(image + 1000, 2, 6, 4)}}
    )
    assert r.status_code == 409 and f"now: image {image} v2, 6×4" in message(r), r.text
    r = t7.create_question(game_id, spot_body(image, confirm=current))
    assert r.status_code == 409 and "(checked: image" in message(r), r.text

    # A supplied current stamp on a too-dense grid → 422, on update and create.
    dense = {
        "config": {"image_id": image, "grid": {"cols": 10, "rows": 7}},
        "answer_data": {"accepted_cells": CELLS, "confirmed_for": stamp(image, 2, 10, 7)},
    }
    r = t7.put_question(game_id, qid, dense)
    assert r.status_code == 422 and message(r) == too_dense_to_confirm(10, 7, 10, 6), r.text
    r = t7.create_question(game_id, spot_body(image, cols=10, rows=7, confirm=stamp(image, 2, 10, 7)))
    assert r.status_code == 422 and message(r) == too_dense_to_confirm(10, 7, 10, 6), r.text
    assert db_question(qid)["answer_data"]["confirmed_for"] == current  # refusals change nothing


def test_reconfirmation_after_same_shape_replace(t7):
    _, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    qid = t7.question_ok(game_id, spot_body(image, confirm=stamp(image, 1, 6, 4)))
    assert t7.replace(game_id, image, landscape("navy")).status_code == 200

    loaded = t7.question(game_id, qid)
    assert loaded["review_reasons"] == ["image_replaced"]
    # The editor's tick-only save: every loaded field, with a fresh stamp for version 2.
    body = {
        field: loaded[field]
        for field in ("type", "grading_type", "prompt", "config", "time_limit_seconds", "points_value")
    }
    body["answer_data"] = {**loaded["answer_data"], "confirmed_for": stamp(image, 2, 6, 4)}
    r = t7.put_question(game_id, qid, body)
    assert r.status_code == 200, r.text
    assert r.json()["needs_review"] is False and r.json()["review_reasons"] == []
    stored = stored_when(
        qid, lambda s: s["answer_data"]["confirmed_for"]["image_version"] == 2, "the new stamp"
    )
    assert stored["answer_data"]["confirmed_for"] == stamp(image, 2, 6, 4)


async def test_stamp_only_reconfirmation_refreshes_dimensions(t7):
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    cells = [cell(1, 1), cell(4, 4)]
    qid = t7.question_ok(
        game_id, spot_body(image, cols=6, rows=6, cells=cells, confirm=stamp(image, 1, 6, 6))
    )
    assert t7.replace(game_id, image, portrait()).status_code == 200
    assert t7.question(game_id, qid)["review_reasons"] == ["image_replaced"]

    # Only answer_data: the same cells and a fresh stamp — no config in the request.
    r = t7.put_question(
        game_id, qid, {"answer_data": {"accepted_cells": cells, "confirmed_for": stamp(image, 2, 6, 6)}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["needs_review"] is False
    stored = stored_when(qid, lambda s: s["config"]["width"] == 900, "the refreshed dimensions")
    assert stored["config"] == {
        "image_id": image,
        "grid": {"cols": 6, "rows": 6},
        "width": 900,
        "height": 1600,
    }

    room_code, _ = t7.create_room(course_id, game_id)
    try:
        host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
        players, _ = await join_players(t7, room_code, 1)
        _, [payload] = await advance_to_question(host, players)
        assert (payload["config"]["width"], payload["config"]["height"]) == (900, 1600)
    finally:
        await t7.close_sockets()


# ---------------------------------------------------------------------------
# Review reasons end to end; the 10×6 regression; the start backstop (spec §2.5, §2.9)
# ---------------------------------------------------------------------------


async def test_review_reasons_end_to_end(t7):
    course_id, game_id = t7.setup_game()
    a = t7.upload_ok(game_id, landscape())["id"]
    b = t7.upload_ok(game_id, landscape("green"))["id"]
    q1 = t7.question_ok(game_id, spot_body(a, confirm=stamp(a, 1, 6, 4)))
    t7.question_ok(
        game_id, spot_body(a, cols=10, rows=6, cells=[cell(9, 0)], confirm=stamp(a, 1, 10, 6))
    )

    # A partial PUT changing the image and the grid together, with the stamp carried.
    r = t7.put_question(game_id, q1, {"config": {"image_id": b, "grid": {"cols": 5, "rows": 4}}})
    assert r.status_code == 200, r.text
    assert r.json()["review_reasons"] == ["image_changed", "grid_changed"]
    t7.put_ok(game_id, q1, {"answer_data": {"accepted_cells": CELLS, "confirmed_for": stamp(b, 1, 5, 4)}})

    # A session, so there is a report to rebuild.
    room_code, session_id = t7.create_room(course_id, game_id)
    try:
        await t7.play_to_completion(room_code, 2)
    finally:
        await t7.close_sockets()

    # A missing image (planted: no application write can produce it) and a replace.
    plant_config(q1, {"image_id": 99999999, "grid": {"cols": 5, "rows": 4}, "width": 1600, "height": 900})
    assert t7.replace(game_id, a, portrait()).status_code == 200
    reasons = {q["id"]: q["review_reasons"] for q in t7.questions(game_id)}
    assert reasons[q1] == ["image_missing"]
    r = t7.try_room(course_id, game_id)
    assert r.status_code == 409, r.text
    assert message(r) == blocked(
        "Q1 — its image is missing.",
        "Q2 — the 10×6 grid is too dense for its image (at most 6×10); its image was replaced "
        "after the cells were checked.",
    )
    html = t7.report(session_id)
    assert "[image missing]" in html


async def test_landscape_grid_replaced_with_portrait(t7):
    """The spec's regression: a confirmed 10×6 landscape question whose image becomes
    portrait before reconfirmation (§4.5)."""
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape(), filename="harbour.png")["id"]
    cells = [cell(9, 0), cell(9, 1), cell(3, 2)]  # column 9 is J
    qid = t7.question_ok(
        game_id, spot_body(image, cols=10, rows=6, cells=cells, confirm=stamp(image, 1, 10, 6))
    )
    assert t7.replace(game_id, image, portrait(), filename="harbour.png").status_code == 200

    loaded = t7.question(game_id, qid)
    assert loaded["review_reasons"] == ["grid_too_dense", "image_replaced"]
    assert loaded["config"]["grid"] == {"cols": 10, "rows": 6}
    assert loaded["answer_data"] == {"accepted_cells": cells, "confirmed_for": stamp(image, 1, 10, 6)}
    r = t7.try_room(course_id, game_id)
    assert r.status_code == 409, r.text
    assert message(r) == blocked(
        "Q1 — the 10×6 grid is too dense for its image (at most 6×10); its image was replaced "
        "after the cells were checked."
    )

    first = t7.export(game_id)
    [exported] = json.loads(first)["questions"]
    assert exported["config"] == {"image_id": "img-1", "grid": {"cols": 10, "rows": 6}}
    assert exported["answer_data"] == {"accepted_cells": cells}  # no confirmed, no stamp

    r = t7.import_bundle(course_id, first)
    assert r.status_code == 201, r.text
    imported_game = r.json()["game_id"]
    [imported] = t7.questions(imported_game)
    assert imported["config"]["grid"] == {"cols": 10, "rows": 6}
    assert imported["answer_data"] == {"accepted_cells": cells}
    assert (imported["config"]["width"], imported["config"]["height"]) == (900, 1600)
    assert imported["review_reasons"] == ["grid_too_dense", "not_confirmed"]
    assert t7.export(imported_game) == first  # byte-identical
    r = t7.try_room(course_id, imported_game)
    assert r.status_code == 409 and message(r) == blocked(
        "Q1 — the 10×6 grid is too dense for its image (at most 6×10); its accepted cells "
        "haven't been checked."
    )

    # The repair, on the imported game and on the original one.
    fixed = [cell(5, 0), cell(5, 1), cell(3, 2)]
    for game, question, version in (
        (imported_game, imported["id"], 1),
        (game_id, qid, 2),
    ):
        canvas = t7.question(game, question)["config"]["image_id"]
        r = t7.put_question(
            game, question,
            {"answer_data": {"accepted_cells": cells, "confirmed_for": stamp(canvas, version, 10, 6)}},
        )  # fmt: skip
        assert r.status_code == 422 and message(r) == too_dense_to_confirm(10, 6, 6, 10), r.text
        r = t7.put_question(
            game, question, {"config": {"image_id": canvas, "grid": {"cols": 6, "rows": 10}}}
        )
        assert r.status_code == 422, r.text
        assert "Accepted cell 1 is outside the 6×10 grid" in message(r)
        r = t7.put_question(
            game, question,
            {
                "config": {"image_id": canvas, "grid": {"cols": 6, "rows": 10}},
                "answer_data": {"accepted_cells": fixed, "confirmed_for": stamp(canvas, version, 6, 10)},
            },
        )  # fmt: skip
        assert r.status_code == 200, r.text
        assert r.json()["needs_review"] is False
        room_code, _ = t7.create_room(course_id, game)  # waits on the update's commit
        try:
            host = await t7.socket(t7.admin_token, room_code, "HOST", f"host{game}")
            await host.emit("host_advance", {})
            payload = await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
            assert payload["questionId"] == question
            assert payload["config"]["grid"] == {"cols": 6, "rows": 10}
        finally:
            await t7.close_sockets()


async def test_start_backstop(t7):
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    qid = t7.question_ok(game_id, spot_body(image, confirm=stamp(image, 1, 6, 4)))
    room_code, _ = t7.create_room(course_id, game_id)
    # Planted after room creation (the live freeze makes this unreachable through the API).
    plant_answer_data(qid, {"accepted_cells": CELLS, "confirmed_for": stamp(image, 7, 6, 4)})
    try:
        host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
        await host.emit("host_advance", {})
        error = await host.wait_for("error", timeout=_SOCKET_TIMEOUT)
        assert error["message"] == blocked(
            "Q1 — its image was replaced after the cells were checked."
        )
    finally:
        await t7.close_sockets()
    assert t7.room_status(room_code) == "LOBBY"


# ---------------------------------------------------------------------------
# The live freeze (spec §2.6)
# ---------------------------------------------------------------------------

T8_LIVE = re.compile(
    r"^Images can't be replaced while a session of this game is live: (?P<rooms>.+)\. "
    r"The session's host can rejoin and finish it, or delete it from Play \(deleting "
    r"discards its partial scores\)\. Anyone else can ask that host or an admin; admins "
    r"can delete sessions from Sessions\.$"
)


def live_rooms(t7: T7Env, game_id: int, image_id: int) -> str:
    """T8's room list, from T8's own (unchanged) image live-guard message."""
    r = t7.replace(game_id, image_id, landscape("olive"))
    assert r.status_code == 409, r.text
    match = T8_LIVE.match(message(r))
    assert match, message(r)
    return match["rooms"]


@pytest.mark.parametrize("phase", ["LOBBY", "IN_PROGRESS"])
async def test_live_freeze(t7, phase):
    course_id, game_id = t7.setup_game()
    a = t7.upload_ok(game_id, landscape())["id"]
    b = t7.upload_ok(game_id, landscape("green"))["id"]
    sort_q = t7.question_ok(game_id, sort_body())
    spot_q = t7.question_ok(game_id, spot_body(a, confirm=stamp(a, 1, 6, 4)))
    tf_q = t7.question_ok(game_id, tf(prompt="Water is wet"))
    room_code, session_id = t7.create_room(course_id, game_id)
    try:
        host = players = None
        if phase == "IN_PROGRESS":
            host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
            players, _ = await join_players(t7, room_code, 1)
            await advance_to_question(host, players)  # Q1 (Sort It) is open
        rooms = live_rooms(t7, game_id, a)
        assert f"room {room_code} ({phase}, hosted by " in rooms

        def live_409(lead_in: str) -> str:
            return f"{lead_in} Live: {rooms}. {FREEZE_FIX}"

        spot_grid = {"cols": 6, "rows": 4}
        edits = [
            (sort_q, 1, {"type": "multiple_choice", "config": {"options": ["a", "b"]},
                         "answer_data": {"answer_points": [1000, 0]}}),
            (sort_q, 1, {"type": "find_in_image", "config": {"image_id": a, "grid": spot_grid},
                         "answer_data": {"accepted_cells": CELLS, "confirmed_for": stamp(a, 1, 6, 4)}}),
            (sort_q, 1, {"grading_type": "COMPLETENESS"}),
            (sort_q, 1, {"points_value": 999}),
            (sort_q, 1, {"answer_data": {"correct_boxes": [1, *KEY[1:]]}}),
            (sort_q, 1, {"config": {"boxes": ["Mammals", *BOXES[1:]], "cards": CARDS}}),
            (sort_q, 1, {"config": {"boxes": BOXES, "cards": ["Orca", *CARDS[1:]]}}),
            (spot_q, 2, {"answer_data": {"accepted_cells": [cell(0, 0)], "confirmed_for": stamp(a, 1, 6, 4)}}),
            (spot_q, 2, {"answer_data": {"accepted_cells": CELLS, "confirmed_for": None}}),
            (spot_q, 2, {"config": {"image_id": b, "grid": spot_grid}}),
            (spot_q, 2, {"config": {"image_id": a, "grid": {"cols": 5, "rows": 4}}}),
            (spot_q, 2, {"grading_type": "COMPLETENESS"}),
            (spot_q, 2, {"points_value": 500}),
        ]  # fmt: skip
        before, meter = t7.questions(game_id), t7.meter(game_id)
        for qid, number, body in edits:
            r = t7.put_question(game_id, qid, body)
            assert r.status_code == 409, (body, r.text)
            assert message(r) == live_409(frozen(number)), message(r)
            assert t7.questions(game_id) == before and t7.meter(game_id) == meter

        to_t7 = {"type": "sort_into_boxes", "config": {"boxes": BOXES, "cards": CARDS},
                 "answer_data": {"correct_boxes": KEY}}  # fmt: skip
        r = t7.put_question(game_id, tf_q, to_t7)
        assert r.status_code == 409 and message(r) == live_409(becomes_t7(3)), r.text
        r = t7.create_question(game_id, spot_body(a))
        assert r.status_code == 409 and message(r) == live_409(UNCONFIRMED_CREATE), r.text
        assert t7.questions(game_id) == before and t7.meter(game_id) == meter
        r = t7.create_question(game_id, spot_body(a, confirm=stamp(a, 1, 6, 4)))
        assert r.status_code == 201, r.text  # a confirmed Spot It question is allowed

        if phase == "IN_PROGRESS":
            # The open question is still scored against its original key.
            event, received = await submit(players[0], sort_q, {"placements": KEY})
            assert event == "answer_received", received
            assert received["pointsAwarded"] == 1000 and received["isCorrect"] is True
    finally:
        await t7.close_sockets()

    # Once the session has ended, the same edits succeed.
    assert t7.admin("DELETE", f"/api/game/sessions/{session_id}").status_code in (200, 204)
    t7.session_ids.remove(session_id)
    for qid, body in (
        (sort_q, {"answer_data": {"correct_boxes": [1, *KEY[1:]]}}),
        (sort_q, {"config": {"boxes": ["Mammals", *BOXES[1:]], "cards": CARDS}}),
        (sort_q, {"points_value": 999}),
        (spot_q, {"answer_data": {"accepted_cells": CELLS, "confirmed_for": None}}),
        (spot_q, {"config": {"image_id": a, "grid": {"cols": 5, "rows": 4}}}),
        (tf_q, to_t7),
    ):
        assert t7.put_question(game_id, qid, body).status_code == 200, body
    assert t7.create_question(game_id, spot_body(a)).status_code == 201


@pytest.mark.parametrize("phase", ["LOBBY", "IN_PROGRESS"])
async def test_prompt_only_live_edits(t7, phase):
    """Editor-shaped round trips (GET, then PUT every loaded field) change only the prompt or
    the time limit while live; changing any frozen detail on the way back is refused."""
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    confirmed = stamp(image, 1, 6, 4)
    out_of_order = [cell(4, 2), cell(1, 0), cell(3, 1)]
    variants = {
        "sort accuracy 687.5": sort_body(points=687.5),
        "sort accuracy 708.333": sort_body(points=708.333),
        "sort completeness with key": sort_body(grading="COMPLETENESS", points=0.1),
        "sort completeness without key": sort_body(grading="COMPLETENESS", key=None),
        "sort bom label": sort_body(cards=["Whale", "Gecko", "﻿Frog", "Bat"], key=[0, 1, 2, 0]),
        "spot accuracy out of order": spot_body(image, cells=out_of_order, points=708.333, confirm=confirmed),
        "spot completeness absent": spot_body(image, cells=None, grading="COMPLETENESS", confirm=confirmed),
        "spot completeness empty": spot_body(image, cells=[], grading="COMPLETENESS", confirm=confirmed),
    }  # fmt: skip
    ids = {name: t7.question_ok(game_id, body) for name, body in variants.items()}
    room_code, _ = t7.create_room(course_id, game_id)
    fields = ("type", "grading_type", "prompt", "config", "answer_data", "time_limit_seconds",
              "points_value")  # fmt: skip
    try:
        if phase == "IN_PROGRESS":
            host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
            await host.emit("host_advance", {})
            await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)

        for name, qid in ids.items():
            loaded = t7.question(game_id, qid)
            stored_before = db_question(qid)
            for change in ({"prompt": f"{name}, reworded"}, {"time_limit_seconds": 45}):
                body = {field: loaded[field] for field in fields} | change
                r = t7.put_question(game_id, qid, body)
                assert r.status_code == 200, (name, r.text)
                [(field, value)] = change.items()
                stored = stored_when(qid, lambda s: s[field] == value, f"{name}: {field}")
                assert {k: v for k, v in stored.items() if k != field} == {
                    k: v for k, v in stored_before.items() if k != field
                }, name
                loaded, stored_before = t7.question(game_id, qid), stored

        # The same round trip with a frozen detail changed is refused, changing nothing.
        spot = ids["spot accuracy out of order"]
        refused = [
            (spot, {"answer_data": {"accepted_cells": sorted(out_of_order, key=lambda c: (c["row"], c["col"])), "confirmed_for": confirmed}}),
            (spot, {"answer_data": {"accepted_cells": out_of_order, "confirmed_for": None}}),
            (ids["sort completeness without key"], {"answer_data": {"correct_boxes": KEY}}),
            (ids["sort completeness with key"], {"answer_data": {}}),
            (ids["spot completeness absent"], {"answer_data": {"accepted_cells": [], "confirmed_for": confirmed}}),
            (ids["spot completeness empty"], {"answer_data": {"confirmed_for": confirmed}}),
        ]  # fmt: skip
        for qid, change in refused:
            loaded = t7.question(game_id, qid)
            body = {field: loaded[field] for field in fields} | change
            r = t7.put_question(game_id, qid, body)
            assert r.status_code == 409, (change, r.text)
            assert t7.question(game_id, qid) == loaded
    finally:
        await t7.close_sockets()
    # The U+FEFF survived every round trip.
    assert db_question(ids["sort bom label"])["config"]["cards"][2] == "﻿Frog"


# ---------------------------------------------------------------------------
# Lock tests with forced orderings (spec §4.5)
# ---------------------------------------------------------------------------


def lock_setup(t7: T7Env) -> tuple[int, int, int, int, list[dict]]:
    """A confirmed ACCURACY Spot It question, 10×6 grid, on a 1600×900 image."""
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    cells = [cell(9, 0), cell(3, 2)]
    qid = t7.question_ok(
        game_id, spot_body(image, cols=10, rows=6, cells=cells, confirm=stamp(image, 1, 10, 6))
    )
    return course_id, game_id, image, qid, cells


def _client(t7: T7Env) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=t7.base_url,
        timeout=30.0,
        headers={"Authorization": f"Bearer {t7.admin_token}"},
    )


async def test_lock_replace_first_room_reads_the_new_image(t7):
    course_id, game_id, image, _, _ = lock_setup(t7)
    harness = RowLockHarness()
    try:
        harness.lock_row("images", image)
        async with _client(t7) as client:
            replace = asyncio.create_task(
                client.put(
                    f"/api/host/games/{game_id}/images/{image}",
                    files={"file": ("tall.png", portrait(), "image/png")},
                )
            )
            await harness.wait_for(locked_image_waits(image, game_id))
            room = asyncio.create_task(
                client.post("/api/game/rooms", json={"course_id": course_id, "game_id": game_id})
            )
            await harness.wait_for(lock_game_waits(game_id))
            assert not replace.done() and not room.done()
            harness.release()
            replace_r = await asyncio.wait_for(replace, 30)
            room_r = await asyncio.wait_for(room, 30)
    finally:
        harness.close()
    assert replace_r.status_code == 200, replace_r.text
    assert t7.image(game_id, image)["version"] == 2
    assert room_r.status_code == 409, room_r.text
    assert message(room_r) == blocked(
        "Q1 — the 10×6 grid is too dense for its image (at most 6×10); its image was replaced "
        "after the cells were checked."
    )
    assert session_rows(game_id) == 0
    assert redis_rooms_for_game(game_id) == []


async def test_lock_room_first_replace_is_refused(t7):
    course_id, game_id, image, _, _ = lock_setup(t7)
    harness = RowLockHarness()
    try:
        harness.lock_row("images", image)
        async with _client(t7) as client:
            room = asyncio.create_task(
                client.post("/api/game/rooms", json={"course_id": course_id, "game_id": game_id})
            )
            await harness.wait_for(image_meta_waits(image, game_id))
            replace = asyncio.create_task(
                client.put(
                    f"/api/host/games/{game_id}/images/{image}",
                    files={"file": ("tall.png", portrait(), "image/png")},
                )
            )
            await harness.wait_for(lock_game_waits(game_id))
            assert not replace.done() and not room.done()
            harness.release()
            room_r = await asyncio.wait_for(room, 30)
            replace_r = await asyncio.wait_for(replace, 30)
    finally:
        harness.close()
    assert room_r.status_code == 201, room_r.text
    t7.session_ids.append(room_r.json()["session_id"])
    assert replace_r.status_code == 409, replace_r.text
    assert f"room {room_r.json()['room_code']} (LOBBY" in message(replace_r)
    assert t7.image(game_id, image)["version"] == 1


async def test_lock_unconfirming_update_first(t7):
    course_id, game_id, _, qid, cells = lock_setup(t7)
    harness = RowLockHarness()
    try:
        harness.lock_row("questions", qid)
        async with _client(t7) as client:
            update = asyncio.create_task(
                client.put(
                    f"/api/host/games/{game_id}/questions/{qid}",
                    json={"answer_data": {"accepted_cells": cells, "confirmed_for": None}},
                )
            )
            await harness.wait_for(get_question_waits(qid, game_id))
            room = asyncio.create_task(
                client.post("/api/game/rooms", json={"course_id": course_id, "game_id": game_id})
            )
            await harness.wait_for(lock_game_waits(game_id))
            assert not update.done() and not room.done()
            harness.release()
            update_r = await asyncio.wait_for(update, 30)
            room_r = await asyncio.wait_for(room, 30)
    finally:
        harness.close()
    assert update_r.status_code == 200, update_r.text
    assert update_r.json()["review_reasons"] == ["not_confirmed"]
    assert room_r.status_code == 409, room_r.text
    assert message(room_r) == blocked("Q1 — its accepted cells haven't been checked.")
    assert session_rows(game_id) == 0


async def test_lock_room_first_unconfirming_update_is_frozen(t7):
    course_id, game_id, image, qid, cells = lock_setup(t7)
    harness = RowLockHarness()
    try:
        harness.lock_row("questions", qid)
        async with _client(t7) as client:
            room = asyncio.create_task(
                client.post("/api/game/rooms", json={"course_id": course_id, "game_id": game_id})
            )
            await harness.wait_for(gate_questions_waits(game_id))
            update = asyncio.create_task(
                client.put(
                    f"/api/host/games/{game_id}/questions/{qid}",
                    json={"answer_data": {"accepted_cells": cells, "confirmed_for": None}},
                )
            )
            await harness.wait_for(lock_game_waits(game_id))
            assert not update.done() and not room.done()
            harness.release()
            room_r = await asyncio.wait_for(room, 30)
            update_r = await asyncio.wait_for(update, 30)
    finally:
        harness.close()
    assert room_r.status_code == 201, room_r.text
    t7.session_ids.append(room_r.json()["session_id"])
    assert update_r.status_code == 409, update_r.text
    text = message(update_r)
    assert text.startswith(frozen(1) + f" Live: room {room_r.json()['room_code']} (LOBBY")
    assert text.endswith(FREEZE_FIX)
    assert db_question(qid)["answer_data"]["confirmed_for"] == stamp(image, 1, 10, 6)


# ---------------------------------------------------------------------------
# Gameplay over sockets (spec §2.3, §2.4, §2.7)
# ---------------------------------------------------------------------------


async def test_sort_it_gameplay(t7):
    course_id, game_id = t7.setup_game()
    boxes = {2: ["Land", "Sea"], 3: BOXES, 4: [*BOXES, "Bird"]}
    keys = {k: [i % k for i in range(8)] for k in (2, 3, 4)}
    expected_partial = {2: 625.0, 3: 687.5, 4: 708.3333333333334}  # 6 right, 1 wrong, 1 out
    qids = {k: t7.question_ok(game_id, sort_body(boxes=boxes[k], key=keys[k])) for k in keys}
    comp = t7.question_ok(game_id, sort_body(grading="COMPLETENESS", key=None))
    room_code, session_id = t7.create_room(course_id, game_id)
    live: dict[int, dict] = {}
    try:
        host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
        players, _ = await join_players(t7, room_code, 4)
        full, partial, floor, rogue = players
        for k in (2, 3, 4):
            qid, key = qids[k], keys[k]
            payload, received = await advance_to_question(host, players)
            assert payload["questionId"] == qid
            order = payload["cardOrder"]
            assert sorted(order) == list(range(8)) and order != list(range(8))
            assert all(p["cardOrder"] == order for p in received)
            assert "correct_boxes" not in json.dumps(payload)

            for bad in (
                {"placements": [True, *key[1:]]},
                {"placements": key, "extra": 1},
                {"placements": [k, *key[1:]]},
                [0, 1],
                {"placements": [None] * 8},
                {"placements": key[:7]},
            ):
                event, _ = await submit(rogue, qid, bad)
                assert event == "error", bad
            assert score_rows(session_id, qid) == 0

            answers = {
                full: {"placements": key},
                partial: placements(key, k, 6, 1),
                floor: placements(key, k, 0, 8),
            }
            for player, answer in answers.items():
                event, got = await submit(player, qid, answer)
                assert event == "answer_received", got
                if player is full:
                    assert got["pointsAwarded"] == 1000 and got["isCorrect"] is True
                elif player is partial:
                    assert got["pointsAwarded"] == pytest.approx(expected_partial[k])
                    assert got["isCorrect"] is False
                else:
                    assert got["pointsAwarded"] == 0 and got["isCorrect"] is False
            assert score_rows(session_id, qid) == 3

            host_results, results = await show_results(host, players)
            reveal = {"type": "sort_into_boxes", "correctBoxes": key}
            assert host_results["answerReveal"] == reveal
            assert all(result["answerReveal"] == reveal for result in results)
            allowed = {f"{i}:{b}" for i in range(8) for b in [*map(str, range(k)), "none"]}
            dist = host_results["answerDistribution"]
            assert set(dist) <= allowed
            assert dist == sort_distribution(list(answers.values()))
            live[qid] = dist
            assert results[1]["yourPoints"] == pytest.approx(expected_partial[k], rel=1e-6)

        await advance_to_question(host, players)
        event, got = await submit(rogue, comp, {"placements": [None, 2, *([None] * 6)]})
        assert event == "answer_received" and got["pointsAwarded"] == 1000 and got["isCorrect"]
        event, _ = await submit(full, comp, {"placements": [None] * 8})
        assert event == "error"
        host_results, _ = await show_results(host, players)
        assert host_results["answerReveal"] == {"type": "completeness"}
        live[comp] = host_results["answerDistribution"]

        host_over, _ = await finish(host, players)
        summary = {item["questionId"]: item for item in host_over["questionSummary"]}
        for qid, dist in live.items():
            assert summary[qid]["answerDistribution"] == dist
    finally:
        await t7.close_sockets()


async def test_spot_it_gameplay(t7):
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    confirmed = stamp(image, 1, 6, 4)
    forward = t7.question_ok(game_id, spot_body(image, cells=[cell(4, 1), cell(4, 2)], confirm=confirmed))
    backward = t7.question_ok(game_id, spot_body(image, cells=[cell(4, 2), cell(4, 1)], confirm=confirmed))
    comp = t7.question_ok(
        game_id, spot_body(image, cells=None, grading="COMPLETENESS", confirm=confirmed)
    )
    room_code, session_id = t7.create_room(course_id, game_id)
    live: dict[int, dict] = {}
    try:
        host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
        players, _ = await join_players(t7, room_code, 3)
        hit, miss, rogue = players
        for qid in (forward, backward):
            payload, received = await advance_to_question(host, players)
            assert payload["questionId"] == qid
            assert "accepted_cells" not in json.dumps(received)
            for bad in (
                {"col": True, "row": 1},
                {"col": 1, "row": 1, "x": 0},
                {"col": 6, "row": 0},
                {"col": 0, "row": 4},
                {"col": "1", "row": 1},
                [1, 1],
            ):
                event, _ = await submit(rogue, qid, bad)
                assert event == "error", bad
            assert score_rows(session_id, qid) == 0
            event, got = await submit(hit, qid, cell(4, 2))
            assert event == "answer_received" and got["pointsAwarded"] == 1000
            assert got["isCorrect"] is True
            event, got = await submit(miss, qid, cell(0, 0))
            assert event == "answer_received" and got["pointsAwarded"] == 0
            assert got["isCorrect"] is False
            host_results, results = await show_results(host, players)
            reveal = {"type": "find_in_image", "acceptedCells": [cell(4, 1), cell(4, 2)]}
            assert host_results["answerReveal"] == reveal  # row-major, whatever the storage
            assert all(result["answerReveal"] == reveal for result in results)
            assert host_results["answerDistribution"] == {"4,2": 1, "0,0": 1}
            live[qid] = host_results["answerDistribution"]

        await advance_to_question(host, players)
        event, got = await submit(miss, comp, cell(5, 3))
        assert event == "answer_received" and got["pointsAwarded"] == 1000 and got["isCorrect"]
        host_results, _ = await show_results(host, players)
        assert host_results["answerReveal"] == {"type": "completeness"}
        live[comp] = host_results["answerDistribution"]

        host_over, _ = await finish(host, players)
        summary = {item["questionId"]: item for item in host_over["questionSummary"]}
        for qid, dist in live.items():
            assert summary[qid]["answerDistribution"] == dist
    finally:
        await t7.close_sockets()


# ---------------------------------------------------------------------------
# Self-contained results and image isolation (spec §2.7)
# ---------------------------------------------------------------------------

# Every type's per-player results. `answered` joined them after T7, when the phone's "No answer"
# moved to the server's record for every type (docs/plans/fix-live-session-test-cleanup.md §3.14);
# a T7 payload's fields as a whole are unchanged.
EXISTING_RESULT_FIELDS = {
    "questionId",
    "answerReveal",
    "yourPoints",
    "yourScore",
    "yourRank",
    "playerCount",
    "answered",
}
T7_RESULT_FIELDS = {
    "questionType",
    "gradingType",
    "pointsValue",
    "yourAnswer",
    "yourIsCorrect",
}


async def test_self_contained_results(t7):
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    confirmed = stamp(image, 1, 6, 4)
    partial = placements(KEY, 3, 6, 1)
    # (question body, answered player's answer, reconnecting player's answer, expectations)
    cases = [
        (sort_body(), {"placements": KEY}, partial,
         {"type": "sort_into_boxes", "correctBoxes": KEY}, (1000, True), (687.5, False)),
        (sort_body(grading="COMPLETENESS"), partial, {"placements": KEY},
         {"type": "completeness"}, (1000, True), (1000, True)),
        (spot_body(image, confirm=confirmed), cell(4, 2), cell(0, 0),
         {"type": "find_in_image", "acceptedCells": CELLS}, (1000, True), (0, False)),
        (spot_body(image, cells=None, grading="COMPLETENESS", confirm=confirmed), cell(1, 1),
         cell(2, 2), {"type": "completeness"}, (1000, True), (1000, True)),
    ]  # fmt: skip
    qids = [t7.question_ok(game_id, case[0]) for case in cases]
    tf_q = t7.question_ok(game_id, tf())
    room_code, _ = t7.create_room(course_id, game_id)
    try:
        host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
        players, tokens = await join_players(t7, room_code, 3)
        answered, unanswered, reconnecting = players
        for qid, (body, answer, reconnect_answer, reveal, full, other) in zip(qids, cases):
            payload, _ = await advance_to_question(host, players)
            assert payload["questionId"] == qid
            assert (await submit(answered, qid, answer))[0] == "answer_received"
            assert (await submit(reconnecting, qid, reconnect_answer))[0] == "answer_received"
            # A new socket for the reconnecting player, before results: no question content.
            await reconnecting.disconnect()
            reconnecting = await t7.socket(tokens[2], room_code, "PLAYER", f"again{qid}")
            players[2] = reconnecting
            host_results, results = await show_results(host, players)
            assert reconnecting._queues["new_question"].empty()
            expectations = [
                (answer, full),
                (None, (0, False)),
                (reconnect_answer, other),
            ]
            for result, (sent, (points, correct)) in zip(results, expectations):
                assert set(result) == EXISTING_RESULT_FIELDS | T7_RESULT_FIELDS, result
                assert result["questionId"] == qid
                assert result["answerReveal"] == reveal == host_results["answerReveal"]
                assert result["questionType"] == body["type"]
                assert result["gradingType"] == body["grading_type"]
                assert result["pointsValue"] == 1000
                assert result["answered"] is (sent is not None)
                assert result["yourAnswer"] == sent
                assert result["yourIsCorrect"] is correct
                assert result["yourPoints"] == pytest.approx(points)
        # Other types' results carry none of T7's fields; nobody answered this one.
        await advance_to_question(host, players)
        _, results = await show_results(host, players)
        assert all(set(result) == EXISTING_RESULT_FIELDS for result in results)
        assert [result["answered"] for result in results] == [False, False, False]
        assert tf_q == results[0]["questionId"]
    finally:
        await t7.close_sockets()


async def test_phones_never_receive_the_spot_it_image(t7):
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    qid = t7.question_ok(game_id, spot_body(image, confirm=stamp(image, 1, 6, 4)))
    room_code, session_id = t7.create_room(course_id, game_id)
    [token] = create_guest_tokens(t7.base_url, room_code, 1)
    try:
        host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
        phone = await t7.recording_socket(token, room_code, "PLAYER", "phone")
        _, [payload] = await advance_to_question(host, [phone])
        assert payload["images"] == {}
        assert payload["config"]["image_id"] == image  # an id, never a link
        await submit(phone, qid, cell(4, 1))
        await show_results(host, [phone])
        await finish(host, [phone])
        assert phone.received
        for event, data in phone.received:
            assert "/api/images/" not in json.dumps(data), event
    finally:
        await t7.close_sockets()
    links = t7.session_links(session_id).json()["images"]
    assert str(image) in links  # the projector gets the canvas image


# ---------------------------------------------------------------------------
# Export and import (spec §2.9)
# ---------------------------------------------------------------------------


def test_export_import_round_trips(t7):
    course_id, game_id = t7.setup_game()

    # Sort It alone: version 1, byte-identical round trip.
    sort_game = t7.create_game(course_id)
    t7.question_ok(sort_game, sort_body(points=708.333))
    t7.question_ok(sort_game, sort_body(grading="COMPLETENESS", key=None))
    first = t7.export(sort_game)
    assert json.loads(first)["version"] == 1
    r = t7.import_bundle(course_id, first)
    assert r.status_code == 201, r.text
    assert t7.export(r.json()["game_id"]) == first

    # Spot It: version 2, confirmed only where confirmed, never width/height.
    image = t7.upload_ok(game_id, landscape())["id"]
    confirmed = stamp(image, 1, 6, 6)
    cells = [cell(4, 2), cell(1, 0)]
    t7.question_ok(game_id, spot_body(image, cols=6, rows=6, cells=cells, confirm=confirmed))
    t7.question_ok(
        game_id,
        spot_body(image, cols=6, rows=6, cells=None, grading="COMPLETENESS", confirm=confirmed),
    )
    t7.question_ok(game_id, spot_body(image, cols=6, rows=6, cells=[], grading="COMPLETENESS"))
    first = t7.export(game_id)
    bundle = json.loads(first)
    assert bundle["version"] == 2
    assert [q["answer_data"] for q in bundle["questions"]] == [
        {"accepted_cells": cells, "confirmed": True},
        {"confirmed": True},
        {"accepted_cells": []},
    ]
    assert all(set(q["config"]) == {"image_id", "grid"} for q in bundle["questions"])
    r = t7.import_bundle(course_id, first)
    assert r.status_code == 201, r.text
    imported = r.json()["game_id"]
    assert [q["review_reasons"] for q in t7.questions(imported)] == [[], [], ["not_confirmed"]]
    assert t7.export(imported) == first

    # Replaced (landscape → portrait) and not reconfirmed: no confirmed flags; imports needing
    # review with the portrait's dimensions; re-exports identically.
    assert t7.replace(game_id, image, portrait()).status_code == 200
    first = t7.export(game_id)
    assert all("confirmed" not in q["answer_data"] for q in json.loads(first)["questions"])
    r = t7.import_bundle(course_id, first)
    assert r.status_code == 201, r.text
    imported = r.json()["game_id"]
    for q in t7.questions(imported):
        assert q["review_reasons"] == ["not_confirmed"]
        assert (q["config"]["width"], q["config"]["height"]) == (900, 1600)
    assert t7.export(imported) == first


def spot_bundle_question(**answer_data) -> dict:
    return {
        "type": "find_in_image",
        "grading_type": "ACCURACY",
        "prompt": "Find it",
        "config": {"image_id": "img-1", "grid": {"cols": 6, "rows": 4}},
        "answer_data": {"accepted_cells": CELLS, **answer_data},
        "time_limit_seconds": 30,
        "points_value": 1000,
    }


def test_import_refusals(t7):
    course_id, _ = t7.setup_game()
    games_before = t7.game_count()
    image = [bundle_image("img-1", landscape())]
    cases = [
        (bundle_v2(image, [spot_bundle_question(confirmed_for=stamp(1, 1, 6, 4))]),
         "Question 1: bundles record confirmation as answer_data.confirmed"),
        (bundle_v2(image, [spot_bundle_question(confirmed="yes")]),
         "Question 1: answer_data.confirmed must be true or false"),
        (bundle_v2(image, [spot_bundle_question(confirmed=1)]),
         "Question 1: answer_data.confirmed must be true or false"),
    ]  # fmt: skip
    dense = spot_bundle_question(confirmed=True)
    dense["config"]["grid"] = {"cols": 10, "rows": 7}
    cases.append(
        (bundle_v2(image, [dense]),
         'Question 1 is marked confirmed, but the 10×7 grid is too dense for its image (at most '
         '10×6); remove "confirmed" or fix the grid')
    )  # fmt: skip
    strip = spot_bundle_question(confirmed=True)
    strip["config"]["grid"] = {"cols": 2, "rows": 2}
    strip["answer_data"]["accepted_cells"] = [cell(1, 1)]
    cases.append(
        (bundle_v2([bundle_image("img-1", png("gray", (1200, 200)))], [strip]),
         'Question 1 is marked confirmed, but its image is too wide for any grid (crop it to at '
         'most 11:2); remove "confirmed" or change the image')
    )  # fmt: skip
    v1 = spot_bundle_question()
    v1["config"]["image_id"] = 5
    cases.append((bundle_v1([v1]), "Question 1: version 1 bundles can't reference images"))
    v1_no_image = spot_bundle_question()
    del v1_no_image["config"]["image_id"]
    cases.append((bundle_v1([v1_no_image]), "config.image_id is required"))
    for raw, text in cases:
        r = t7.import_bundle(course_id, raw)
        assert r.status_code == 422, r.text
        assert text in message(r), message(r)
    assert t7.game_count() == games_before

    # An unconfirmed too-dense question imports, needing review.
    dense["answer_data"].pop("confirmed")
    r = t7.import_bundle(course_id, bundle_v2(image, [dense]))
    assert r.status_code == 201, r.text
    [q] = t7.questions(r.json()["game_id"])
    assert q["review_reasons"] == ["grid_too_dense", "not_confirmed"]
    assert q["config"]["grid"] == {"cols": 10, "rows": 7}


# ---------------------------------------------------------------------------
# The export-size boundary (spec §4.5)
# ---------------------------------------------------------------------------


class Boundary:
    """A game held near the export limit with one padding question (test_t8_images.py's
    approach): four large images, then plain-ASCII padding sized to the byte."""

    def __init__(self, t7: T7Env, course_id: int, game_id: int):
        self.t7, self.course_id, self.game_id = t7, course_id, game_id
        for seed in range(4):
            t7.upload_ok(game_id, noise_jpeg((1400, 1400), 100, seed), description=f"Big {seed}")
        self.pad = 10
        self.pad_q = t7.question_ok(game_id, padding_question(self.pad))

    def meter(self) -> int:
        return self.t7.meter(self.game_id)

    def set_headroom(self, headroom: int) -> None:
        length = self.pad + (EXPORT_LIMIT - headroom - self.meter())
        r = self.t7.put_question(
            self.game_id, self.pad_q, {"config": {"pad": "x" * length}}
        )
        assert r.status_code == 200, r.text
        self.pad = length
        wait_until(
            lambda: self.meter() == EXPORT_LIMIT - headroom,
            timeout=30,
            what=f"headroom {headroom}",
        )

    def refused(self, r: httpx.Response) -> None:
        assert r.status_code == 409, r.text
        assert f"over the export limit of {EXPORT_LIMIT:,} bytes" in message(r), message(r)

    def confirmed_question(self, name: str = "a.png") -> tuple[dict, int]:
        """A fresh image and a confirmed 6×6 question on it. Frees headroom first: at the
        limit, the upload itself would be refused."""
        self.set_headroom(64 * 1024)
        image = self.t7.upload_ok(self.game_id, landscape(), filename=name)
        qid = self.t7.question_ok(
            self.game_id,
            spot_body(image["id"], cols=6, rows=6, cells=[cell(1, 1)], confirm=stamp(image["id"], 1, 6, 6)),
        )  # fmt: skip
        return image, qid

    def reconfirm(self, image_id: int, qid: int, version: int) -> httpx.Response:
        """Reconfirm the question. An accepted update commits only after its response, and
        set_headroom sizes the padding from a meter read, so this returns only once the
        question reads back as the response stored it: the meter then includes the change.
        (Without this, a meter read could miss the "confirmed" bytes and size the padding
        that many bytes too long.)"""
        r = self.t7.put_question(
            self.game_id,
            qid,
            {"answer_data": {"accepted_cells": [cell(1, 1)], "confirmed_for": stamp(image_id, version, 6, 6)}},
        )  # fmt: skip
        if r.status_code == 200:
            wait_until(
                lambda: self.t7.question(self.game_id, qid) == r.json(),
                what="the reconfirmation",
            )
        return r


def test_export_size_boundary(t7):
    course_id, game_id = t7.setup_game()
    game = Boundary(t7, course_id, game_id)

    # c: the bytes "confirmed": true adds, measured before any padding.
    image_id = t7.upload_ok(game_id, landscape(), filename="a.png")["id"]
    qid = t7.question_ok(game_id, spot_body(image_id, cols=6, rows=6, cells=[cell(1, 1)]))
    unconfirmed = t7.meter(game_id)
    assert game.reconfirm(image_id, qid, 1).status_code == 200
    wait_until(lambda: t7.meter(game_id) != unconfirmed, what="the confirmed flag")
    c = t7.meter(game_id) - unconfirmed
    assert c == len(',\n        "confirmed": true')

    # Replaced (a 6×6 grid fits both shapes): unconfirmed, at headroom 0 → exactly the limit.
    assert t7.replace(game_id, image_id, portrait(), filename="a.png").status_code == 200
    game.set_headroom(0)
    exported = t7.export(game_id)
    assert len(exported) == EXPORT_LIMIT == 41_943_040 == t7.meter(game_id)
    r = t7.import_bundle(course_id, exported)
    assert r.status_code == 201, r.text
    assert t7.export(r.json()["game_id"]) == exported

    # Reconfirming adds c bytes: refused at headroom c − 1, accepted at c, landing exactly.
    game.set_headroom(c - 1)
    before = t7.question(game_id, qid)
    game.refused(game.reconfirm(image_id, qid, 2))
    assert t7.question(game_id, qid) == before
    game.set_headroom(c)
    r = game.reconfirm(image_id, qid, 2)
    assert r.status_code == 200 and r.json()["needs_review"] is False, r.text
    wait_until(lambda: t7.meter(game_id) == EXPORT_LIMIT, what="the reconfirmation")

    # The pinned replacement boundary: the same bytes under a filename d bytes longer adds
    # +d and drops "confirmed" (−c). Each case starts from a fresh confirmed setup at
    # headroom 0.
    for d, accepted in ((c, True), (c // 2, True), (c + 1, False)):
        image, qid = game.confirmed_question()
        game.set_headroom(0)
        name = "a" * (1 + d) + ".png"
        r = t7.replace(game_id, image["id"], landscape(), filename=name)
        stored = t7.image(game_id, image["id"])
        if not accepted:
            game.refused(r)
            assert (stored["version"], stored["filename"]) == (1, "a.png")
            assert t7.meter(game_id) == EXPORT_LIMIT
            continue
        assert r.status_code == 200, r.text
        assert (stored["version"], stored["filename"]) == (2, name)
        assert t7.meter(game_id) == EXPORT_LIMIT - c + d
        if d == c:
            assert len(t7.export(game_id)) == EXPORT_LIMIT == t7.meter(game_id)
            # Follow-on: at the limit, a tick-only reconfirmation (+c) is refused …
            assert t7.question(game_id, qid)["review_reasons"] == ["image_replaced"]
            before = t7.question(game_id, qid)
            game.refused(game.reconfirm(image["id"], qid, 2))
            assert t7.question(game_id, qid) == before
            # … and succeeds once at least c bytes are freed.
            game.set_headroom(c)
            r = game.reconfirm(image["id"], qid, 2)
            assert r.status_code == 200 and r.json()["needs_review"] is False, r.text


# ---------------------------------------------------------------------------
# The report (spec §2.8)
# ---------------------------------------------------------------------------


class Report(HTMLParser):
    """Tags, attributes and text of a downloaded report, as a browser would parse it."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.chunks: list[str] = []
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "tr":
            self.rows.append([])
        elif tag == "td":
            self._cell = []

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self.rows[-1].append("".join(self._cell))
            self._cell = None

    def handle_data(self, data):
        self.chunks.append(data)
        if self._cell is not None:
            self._cell.append(data)

    @property
    def text(self) -> str:
        return "".join(self.chunks)


async def play_report_game(t7: T7Env, room_code: str, qids: list[int], answers: list[list]):
    """Players answer each question as scripted (None = no answer); returns after game over."""
    host = await t7.socket(t7.admin_token, room_code, "HOST", "host")
    players, _ = await join_players(t7, room_code, len(answers[0]))
    for qid, per_player in zip(qids, answers):
        await advance_to_question(host, players)
        for player, answer in zip(players, per_player):
            if answer is not None:
                assert (await submit(player, qid, answer))[0] == "answer_received"
        await show_results(host, players)
    await finish(host, players)
    wait_until(lambda: t7.room_status(room_code) == "COMPLETED", what="completion")


async def test_report(t7):
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape(), description="Harbour at dusk")["id"]
    boxes, cards, key = BOXES, ["Whale", "Gecko", "Frog", "Bat"], [0, 1, 2, 0]
    sort_q = t7.question_ok(game_id, sort_body(cards=cards, key=key, points=708.333))
    spot_q = t7.question_ok(game_id, spot_body(image, points=2.55, confirm=stamp(image, 1, 6, 4)))
    tf_q = t7.question_ok(game_id, tf())
    host_id, host = t7.make_host("reporter", course_id, game_id)
    room_code, session_id = t7.create_room(course_id, game_id, token=host)
    answers = [
        [{"placements": key}, {"placements": [0, 2, None, 0]}],  # 1 of 2 all correct
        [cell(4, 1), cell(4, 1)],
        [{"selectedValue": True}, None],
    ]
    try:
        await play_report_game(t7, room_code, [sort_q, spot_q, tf_q], answers)
    finally:
        await t7.close_sockets()

    html = t7.report(session_id, host)
    report = Report(html)
    # The Sort It table, in the author's card order.
    table = [row for row in report.rows if len(row) == 5]
    assert table == [
        ["Whale", "Mammal", "100%", "—", "0%"],
        ["Gecko", "Reptile", "50%", "Amphibian", "0%"],
        ["Frog", "Amphibian", "50%", "—", "50%"],
        ["Bat", "Mammal", "100%", "—", "0%"],
    ]
    assert "50% all cards correct" in report.text
    # The Spot It heatmap: the small copy, counts, the Correct outlines.
    [svg] = [attrs for tag, attrs in report.tags if tag == "svg"]
    assert svg["aria-label"] == "Answer heatmap: Harbour at dusk"
    [href] = [attrs["href"] for tag, attrs in report.tags if tag == "image"]
    assert href.startswith("data:image/webp;base64,")
    assert "2 of 2 answered" in report.text
    assert report.text.count("Correct") >= 3  # two outlined cells + the legend
    # The q-timing labels: T7 through the T7 rule, the existing type as before.
    timings = [
        " ".join(text.split())  # the separator's spaces are typographic
        for text in re.findall(r'<span class="q-timing">([^<]*)</span>', html)
    ]
    assert timings == ["30s · 708.3 pts", "30s · 2.6 pts", "30s · 1 pt"]
    # Aggregate only: the second player's partial Sort It score (708.333 × 3/8 = 265.62…)
    # appears nowhere.
    assert "265.6" not in html
    assert "TestPlayer" not in html

    # Without game access: the withheld text in the heatmap, no small copy, no description.
    t7.revoke_game(host_id, game_id)
    report = Report(t7.report(session_id, host))
    assert not [attrs for tag, attrs in report.tags if tag == "image"]
    assert "[image withheld — you no longer have access to this game]" in " ".join(
        report.text.split()
    )
    assert "Harbour at dusk" not in report.text


async def test_report_escaping(t7):
    course_id, game_id = t7.setup_game()
    script_card = "<script>alert(1)</script>"
    img_box = '"><img src=x onerror=alert(1)>'
    description = 'The "harbour" at <dusk> & dawn'
    image = t7.upload_ok(game_id, landscape(), description=description)["id"]
    sort_q = t7.question_ok(
        game_id,
        sort_body(boxes=[img_box, "Reptile", "Amphibian"], cards=[script_card, "Gecko", "Frog", "Bat"],
                  key=[0, 1, 2, 0]),
    )  # fmt: skip
    spot_q = t7.question_ok(game_id, spot_body(image, confirm=stamp(image, 1, 6, 4)))
    room_code, session_id = t7.create_room(course_id, game_id)
    try:
        await play_report_game(
            t7, room_code, [sort_q, spot_q], [[{"placements": [0, 1, 2, 0]}], [cell(4, 1)]]
        )
    finally:
        await t7.close_sockets()

    html = t7.report(session_id)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in html
    assert "The &quot;harbour&quot; at &lt;dusk&gt; &amp; dawn" in html
    report = Report(html)
    names = [tag for tag, _ in report.tags]
    assert "script" not in names and "img" not in names
    for tag, attrs in report.tags:
        assert not [name for name in attrs if name.startswith("on")], (tag, attrs)
    assert script_card in report.text and img_box in report.text


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_access(t7):
    course_id, game_id = t7.setup_game()
    image = t7.upload_ok(game_id, landscape())["id"]
    _, no_grant = t7.make_host("nogrant", course_id)
    room_code, _ = t7.create_room(course_id, game_id)
    [guest] = create_guest_tokens(t7.base_url, room_code, 1)
    for token in (no_grant, guest):
        for body in (sort_body(), spot_body(image, confirm=stamp(image, 1, 6, 4))):
            r = t7.create_question(game_id, body, token=token)
            assert r.status_code == 403, r.text
    assert t7.questions(game_id) == []
