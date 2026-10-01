"""
T8 image support integration tests (docs/plans/t8-image-support.md §4.11).

Runs against the compose dev stack, directly on port 8000 (so without nginx's body limits —
test_t8_nginx_limits.py covers those):

    cd tests/integration && pytest test_t8_images.py -v

Each test builds its own users, courses, games and rooms through the API (the `t8` fixture)
and removes them afterwards; courses have no delete endpoint and are left as inert records.

Two kinds of test reach MySQL directly, with the settings below:
- the lock tests, which hold the game row lock from a test connection to prove that requests
  wait on it and then read fresh data (see "The lock tests" in §4.11), and
- the legacy-data tests, which plant configs the API would refuse.

    BUZZER_TEST_DB_HOST (127.0.0.1)   BUZZER_TEST_DB_PORT (3306)   BUZZER_TEST_DB_NAME (buzzer)
    BUZZER_TEST_DB_USER (app_user)    BUZZER_TEST_DB_PASSWORD (MYSQL_PASSWORD from .env)

The lock tests are required: if they skip because the database can't be reached, T8's
verification is incomplete, not passed.

Commit timing: get_db commits many writes *after* the response has been sent (question and
game updates, reorders, grants), so reads that follow them poll with wait_until. The image
endpoints and room creation commit before responding.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import pathlib
import random
import re
import struct
import time
import uuid
import warnings
import zlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from PIL import Image

from .conftest import (
    create_guest_tokens,
    db_settings,
    read_repo_env,
    redis_cli,
    wait_until,
)
from .engine.socket_client import TestSocketClient as SocketClient

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_PASSWORD = "t8-integration-pass"
_SOCKET_TIMEOUT = 15.0

MIB = 1024 * 1024
INPUT_LIMIT = 10 * MIB
IMAGE_COUNT_LIMIT = 50
LIBRARY_BUDGET = 24 * MIB
EXPORT_LIMIT = 40 * MIB


# ---------------------------------------------------------------------------
# Images, built in memory
# ---------------------------------------------------------------------------


def encode(img: Image.Image, fmt: str, **options) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, fmt, **options)
    return buffer.getvalue()


def png(color="red", size=(64, 48)) -> bytes:
    return encode(Image.new("RGB", size, color), "PNG")


def webp(color="green", size=(64, 48)) -> bytes:
    return encode(Image.new("RGB", size, color), "WEBP", quality=90)


def gif(size=(40, 30)) -> bytes:
    frames = [Image.new("RGB", size, c).convert("P") for c in ("red", "blue")]
    return encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100)


def jpeg_with_orientation(size=(40, 20), orientation=6) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    return encode(Image.new("RGB", size, "orange"), "JPEG", exif=exif)


def noise_jpeg(size=(800, 600), quality=90, seed=1) -> bytes:
    rng = random.Random(seed)
    img = Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))
    return encode(img, "JPEG", quality=quality)


def png_header(width: int, height: int) -> bytes:
    """A PNG whose header declares width × height, with no pixel data."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def truncated_png() -> bytes:
    data = encode(Image.frombytes("RGB", (200, 200), random.Random(3).randbytes(120000)), "PNG")
    return data[: len(data) // 2]


SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Direct MySQL access: the lock harness and planted legacy data
# ---------------------------------------------------------------------------


def db_connect():
    """A PyMySQL connection, autocommit off, with a 10 s lock wait. Skips the test (with the
    reason) if the database can't be reached — for a lock test that means verification is
    incomplete, not passed."""
    try:
        import pymysql
    except ImportError:
        pytest.skip("PyMySQL isn't installed (tests/integration/requirements.txt)")
    settings = db_settings()
    try:
        conn = pymysql.connect(
            host=settings["host"],
            port=settings["port"],
            user=settings["user"],
            password=settings["password"],
            database=settings["database"],
            connect_timeout=5,
            read_timeout=30,
            autocommit=False,
        )
    except pymysql.MySQLError as exc:
        pytest.skip(
            f"Can't reach MySQL at {settings['host']}:{settings['port']} as "
            f"{settings['user']} ({exc}); set BUZZER_TEST_DB_* — lock tests are required"
        )
    with conn.cursor() as cur:
        cur.execute("SET SESSION innodb_lock_wait_timeout = 10")
    return conn


class LockHarness:
    """Holds the game row lock from its own connection and watches for requests waiting on it.

    PROCESSLIST shows, without extra privileges, only the connections of the same MySQL
    account, so this must connect as the backend's account (the defaults above).
    """

    def __init__(self):
        self.conn = db_connect()

    def lock(self, game_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM games WHERE id = %s FOR UPDATE", (game_id,))

    def execute(self, sql: str, args=None) -> int:
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.lastrowid

    def waiting(self, game_id: int) -> int:
        """Other connections of this account running `SELECT … FROM games … FOR UPDATE` on
        this game — the backend's lock_game, blocked behind our lock."""
        pattern = re.compile(
            rf"FROM games\s+WHERE games\.id = {game_id}\s+FOR UPDATE", re.IGNORECASE
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT INFO FROM information_schema.PROCESSLIST "
                "WHERE ID <> CONNECTION_ID() AND INFO IS NOT NULL"
            )
            return sum(1 for (info,) in cur.fetchall() if pattern.search(info))

    async def wait_for_waiters(self, game_id: int, count: int, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            seen = self.waiting(game_id)
            if seen >= count:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"Only {seen} of {count} requests were seen waiting on game {game_id}'s lock within "
            f"{timeout}s. PROCESSLIST shows only the connecting account's own connections: "
            "set BUZZER_TEST_DB_USER to the backend's MySQL account (app_user)."
        )

    def release(self) -> None:
        self.conn.rollback()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.rollback()
        finally:
            self.conn.close()


def plant_config(question_id: int, config: dict) -> None:
    """Store a config the API would refuse (legacy data), committed."""
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE questions SET config = %s WHERE id = %s",
                (json.dumps(config), question_id),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-test environment: API helpers plus cleanup of everything a test creates
# ---------------------------------------------------------------------------


def mc(prompt="Which one?", options=("Paris", "Rome"), points=None, **config) -> dict:
    options = list(options)
    return {
        "type": "multiple_choice",
        "grading_type": "ACCURACY",
        "prompt": prompt,
        "config": {"options": options, **config},
        "answer_data": {"answer_points": points or [1] + [0] * (len(options) - 1)},
        "points_value": 1,
        "time_limit_seconds": 30,
    }


def ms(prompt="Pick all", options=("A", "B", "C"), **config) -> dict:
    options = list(options)
    return {
        "type": "multi_select",
        "grading_type": "ACCURACY",
        "prompt": prompt,
        "config": {"options": options, **config},
        "answer_data": {"answer_points": [1] * len(options)},
        "points_value": len(options),
        "time_limit_seconds": 30,
    }


def tf(prompt="True?", **config) -> dict:
    return {
        "type": "true_false",
        "grading_type": "ACCURACY",
        "prompt": prompt,
        "config": config,
        "answer_data": {"answer_points": {"true": 1, "false": 0}},
        "points_value": 1,
    }


def fitb(prompt="Name it", **config) -> dict:
    return {
        "type": "fill_in_the_blank",
        "grading_type": "ACCURACY",
        "prompt": prompt,
        "config": config,
        "answer_data": {"acceptedAnswers": ["x"], "answerPoints": [1]},
        "points_value": 1,
    }


class T8Env:
    def __init__(self, base_url: str, admin_token: str):
        self.base_url = base_url
        self.admin_token = admin_token
        self.tag = uuid.uuid4().hex[:8]
        self.http = httpx.Client(base_url=base_url, timeout=120.0)
        self.session_ids: list[str] = []
        self.game_ids: list[int] = []
        self.user_ids: list[str] = []
        self.sockets: list[SocketClient] = []

    # -- requests ------------------------------------------------------------

    def req(self, method: str, path: str, token: str | None = None, **kwargs):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.http.request(method, path, headers=headers, **kwargs)

    def admin(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self.req(method, path, self.admin_token, **kwargs)

    # -- users, courses, grants ----------------------------------------------

    def create_user(self, name: str) -> tuple[str, str]:
        username = f"t8-{name}-{self.tag}"
        r = self.admin(
            "POST",
            "/api/admin/users",
            json={"username": username, "display_name": f"T8 {name}", "password": _PASSWORD},
        )
        assert r.status_code == 201, r.text
        user_id = r.json()["id"]
        self.user_ids.append(user_id)
        r = self.req(
            "POST", "/api/auth/login", json={"username": username, "password": _PASSWORD}
        )
        assert r.status_code == 200, r.text
        return user_id, r.json()["access_token"]

    def create_course(self, label: str = "C") -> int:
        r = self.admin(
            "POST",
            "/api/admin/courses",
            json={"name": f"T8 {label} {self.tag}", "semester": "T8 Test"},
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def user_detail(self, user_id: str) -> dict:
        r = self.admin("GET", f"/api/admin/users/{user_id}")
        assert r.status_code == 200, r.text
        return r.json()

    def grant_course(self, user_id: str, course_id: int, role: str = "HOST") -> None:
        r = self.admin(
            "POST",
            f"/api/admin/users/{user_id}/course-access",
            json={"course_id": course_id, "role": role},
        )
        assert r.status_code == 204, r.text
        wait_until(
            lambda: {"course_id": course_id, "role": role}
            in self.user_detail(user_id)["course_access"],
            what=f"{role} on course {course_id}",
        )

    def grant_game(self, user_id: str, game_id: int) -> None:
        r = self.admin(
            "POST", f"/api/admin/users/{user_id}/game-access", json={"game_id": game_id}
        )
        assert r.status_code == 204, r.text
        wait_until(
            lambda: game_id in self.user_detail(user_id)["game_access"],
            what=f"grant on game {game_id}",
        )

    def revoke_game(self, user_id: str, game_id: int) -> None:
        r = self.admin("DELETE", f"/api/admin/users/{user_id}/game-access/{game_id}")
        assert r.status_code == 204, r.text
        wait_until(
            lambda: game_id not in self.user_detail(user_id)["game_access"],
            what=f"revoke of game {game_id}",
        )

    def make_host(self, name: str, course_id: int, game_id: int | None = None):
        user_id, token = self.create_user(name)
        self.grant_course(user_id, course_id, "HOST")
        if game_id is not None:
            self.grant_game(user_id, game_id)
        return user_id, token

    # -- games, questions, images --------------------------------------------

    def create_game(self, course_id: int, title: str | None = None) -> int:
        r = self.admin(
            "POST",
            "/api/admin/games",
            json={"title": title or f"T8 Game {self.tag}", "course_id": course_id},
        )
        assert r.status_code == 201, r.text
        self.game_ids.append(r.json()["id"])
        return r.json()["id"]

    def setup_game(self) -> tuple[int, int]:
        course_id = self.create_course()
        return course_id, self.create_game(course_id)

    def upload(
        self,
        game_id: int,
        data: bytes,
        description: str | None = "An image",
        filename: str = "image.png",
        token: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> httpx.Response:
        form = {} if description is None else {"description": description}
        return self.req(
            "POST",
            f"/api/host/games/{game_id}/images",
            token or self.admin_token,
            files={"file": (filename, data, content_type)},
            data=form,
        )

    def upload_ok(self, game_id: int, data: bytes, description="An image", **kw) -> dict:
        r = self.upload(game_id, data, description, **kw)
        assert r.status_code == 201, r.text
        return r.json()

    def replace(self, game_id: int, image_id: int, data: bytes, filename="new.png", token=None):
        return self.req(
            "PUT",
            f"/api/host/games/{game_id}/images/{image_id}",
            token or self.admin_token,
            files={"file": (filename, data, "application/octet-stream")},
        )

    def library(self, game_id: int, token: str | None = None) -> dict:
        r = self.req("GET", f"/api/host/games/{game_id}/images", token or self.admin_token)
        assert r.status_code == 200, r.text
        return r.json()

    def image(self, game_id: int, image_id: int) -> dict | None:
        return next(
            (i for i in self.library(game_id)["images"] if i["id"] == image_id), None
        )

    def meter(self, game_id: int) -> int:
        return self.library(game_id)["meters"]["export_bytes"]

    def create_question(self, game_id: int, body: dict, token=None) -> httpx.Response:
        return self.req(
            "POST", f"/api/host/games/{game_id}/questions", token or self.admin_token, json=body
        )

    def question_ok(self, game_id: int, body: dict) -> int:
        r = self.create_question(game_id, body)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def questions(self, game_id: int) -> list[dict]:
        r = self.admin("GET", f"/api/host/games/{game_id}/questions")
        assert r.status_code == 200, r.text
        return r.json()

    def question(self, game_id: int, question_id: int) -> dict | None:
        return next((q for q in self.questions(game_id) if q["id"] == question_id), None)

    def export(self, game_id: int, token=None) -> bytes:
        r = self.req("GET", f"/api/host/games/{game_id}/export", token or self.admin_token)
        assert r.status_code == 200, r.text
        return r.content

    def import_bundle(self, course_id: int, raw: bytes, token=None) -> httpx.Response:
        r = self.req(
            "POST",
            "/api/host/games/import",
            token or self.admin_token,
            data={"course_id": str(course_id)},
            files={"file": ("bundle.json", raw, "application/json")},
        )
        if r.status_code == 201:
            self.game_ids.append(r.json()["game_id"])
        return r

    def game_count(self) -> int:
        return len(self.admin("GET", "/api/admin/games").json())

    # -- rooms and play -------------------------------------------------------

    def create_room(self, course_id: int, game_id: int, token=None) -> tuple[str, str]:
        r = self.req(
            "POST",
            "/api/game/rooms",
            token or self.admin_token,
            json={"course_id": course_id, "game_id": game_id},
        )
        assert r.status_code == 201, r.text
        room_code, session_id = r.json()["room_code"], r.json()["session_id"]
        self.session_ids.append(session_id)
        return room_code, session_id

    def room_status(self, room_code: str) -> str:
        return self.admin("GET", f"/api/game/rooms/{room_code}").json()["status"]

    def session_links(self, session_id: str, token=None) -> httpx.Response:
        return self.req("GET", f"/api/game/sessions/{session_id}/images", token or self.admin_token)

    async def socket(self, token: str, room_code: str, role: str, label: str) -> SocketClient:
        client = SocketClient(self.base_url, token, label)
        self.sockets.append(client)
        await client.connect()
        await client.emit("join_room", {"room_code": room_code, "role": role})
        await client.wait_for("sync_state", timeout=_SOCKET_TIMEOUT)
        return client

    async def play_to_completion(self, room_code: str, questions: int, token=None) -> None:
        """Drive a session to COMPLETED with the host socket alone (no players needed)."""
        host = await self.socket(token or self.admin_token, room_code, "HOST", "host")
        for _ in range(questions):
            await host.emit("host_advance", {})
            await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
            await host.emit("host_advance", {})
            await host.wait_for("question_results", timeout=_SOCKET_TIMEOUT)
        await host.emit("host_advance", {})
        await host.wait_for("game_over", timeout=_SOCKET_TIMEOUT)
        await host.disconnect()
        wait_until(lambda: self.room_status(room_code) == "COMPLETED", what="completion")

    # -- cleanup -------------------------------------------------------------

    async def close_sockets(self) -> None:
        for client in self.sockets:
            try:
                await client.disconnect()
            except Exception:
                pass

    def cleanup(self) -> None:
        # Sessions first (also clears their Redis keys), then games (the admin delete
        # cascades to images), then users (sessions reference their host).
        for session_id in self.session_ids:
            self.admin("DELETE", f"/api/game/sessions/{session_id}")
        for game_id in self.game_ids:
            self.admin("DELETE", f"/api/admin/games/{game_id}")
        for game_id in self.game_ids:
            wait_until(
                lambda: self.admin("GET", f"/api/admin/games/{game_id}").status_code == 404,
                what=f"delete of game {game_id}",
            )
        for user_id in self.user_ids:
            self.admin("DELETE", f"/api/admin/users/{user_id}")
        self.http.close()


@pytest.fixture()
def t8(docker_stack, base_url: str, admin_token: str):
    # Async tests close their own sockets (t8.close_sockets() in a finally): they're bound to
    # the test's event loop, which is gone by the time this teardown runs.
    env = T8Env(base_url, admin_token)
    yield env
    env.cleanup()


def link_path(url: str) -> str:
    return url.split("?", 1)[0]


def fetch(env: T8Env, url: str, **kwargs) -> httpx.Response:
    return env.http.get(url, **kwargs)


# ---------------------------------------------------------------------------
# Upload and processing (spec §2.5)
# ---------------------------------------------------------------------------


def test_upload_accepts_each_format(t8):
    _, game_id = t8.setup_game()
    cases = [
        (png(), "image/png"),
        (noise_jpeg((120, 90)), "image/jpeg"),
        (webp(), "image/webp"),
        (gif(), "image/png"),  # GIF never conforms: its first frame becomes a PNG
    ]
    for data, content_type in cases:
        item = t8.upload_ok(game_id, data)
        assert item["content_type"] == content_type
        assert item["version"] == 1 and item["used_by"] == []
        full = fetch(t8, item["url"])
        assert full.status_code == 200 and full.headers["content-type"] == content_type


def test_exif_rotation_normalized(t8):
    _, game_id = t8.setup_game()
    item = t8.upload_ok(game_id, jpeg_with_orientation((40, 20), 6))
    assert (item["width"], item["height"]) == (20, 40)
    stored = Image.open(io.BytesIO(fetch(t8, item["url"]).content))
    assert stored.size == (20, 40)
    assert "exif" not in stored.info


def test_large_image_downscaled(t8):
    _, game_id = t8.setup_game()
    item = t8.upload_ok(game_id, png(size=(3200, 800)))
    assert (item["width"], item["height"]) == (1600, 400)
    assert Image.open(io.BytesIO(fetch(t8, item["url"]).content)).size == (1600, 400)
    # A small image is never scaled up.
    item = t8.upload_ok(game_id, png(size=(30, 20)))
    assert (item["width"], item["height"]) == (30, 20)


def test_conforming_bytes_stored_unchanged(t8):
    _, game_id = t8.setup_game()
    for data in (png(), noise_jpeg((300, 200)), webp()):
        item = t8.upload_ok(game_id, data)
        stored = fetch(t8, item["url"]).content
        assert stored == data
        assert item["byte_size"] == len(data)


def test_small_copy_is_webp_within_edge(t8):
    _, game_id = t8.setup_game()
    item = t8.upload_ok(game_id, png(size=(1600, 800)))
    small = fetch(t8, item["small_url"])
    assert small.status_code == 200
    assert small.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(small.content)).size == (400, 200)


def test_invalid_uploads_refused(t8):
    _, game_id = t8.setup_game()
    t8.upload_ok(game_id, png())
    before = t8.library(game_id)
    cases = [
        (SVG, 415),
        (b"just some text", 415),
        (b"", 415),
        (encode(Image.new("RGB", (8, 8)), "TIFF"), 415),
        (truncated_png(), 422),
        (png_header(8000, 6000), 422),  # 48 MP, over the pixel limit
        (b"\0" * (INPUT_LIMIT + 1), 413),
    ]
    for data, status in cases:
        r = t8.upload(game_id, data)
        assert r.status_code == status, (status, r.text)
        assert isinstance(r.json()["message"], str)
    # A missing or overlong description.
    assert t8.upload(game_id, png(), description=None).status_code == 422
    r = t8.upload(game_id, png(), description="x" * 301)
    assert r.status_code == 422 and "1–300" in r.json()["message"]
    assert t8.upload(game_id, png(), description="   ").status_code == 422
    after = t8.library(game_id)
    assert [i["id"] for i in after["images"]] == [i["id"] for i in before["images"]]
    assert after["meters"] == before["meters"]


def test_pixel_limit_checked_before_decoding(t8):
    _, game_id = t8.setup_game()
    r = t8.upload(game_id, png_header(8000, 6000))  # 48 MP: between the limit and Pillow's 80 MP
    assert r.status_code == 422, r.text
    assert r.json()["message"] == "This image is 48 MP; the limit is 40 MP. Resize it and upload again."


def test_pixel_limit_errors_are_mapped(t8):
    _, game_id = t8.setup_game()
    explicit = t8.upload(game_id, png_header(8000, 6000))
    bomb = t8.upload(game_id, png_header(10000, 9000))  # Pillow's own DecompressionBombError
    for r, mp in ((explicit, "48 MP"), (bomb, "90 MP")):
        assert r.status_code == 422, r.text
        assert mp in r.json()["message"] and "40 MP" in r.json()["message"]


def test_count_and_budget_limits(t8):
    course_id, game_id = t8.setup_game()
    for n in range(IMAGE_COUNT_LIMIT):
        t8.upload_ok(game_id, png(size=(8, 8 + n)))
    before = t8.library(game_id)
    assert before["meters"]["image_count"] == IMAGE_COUNT_LIMIT
    r = t8.upload(game_id, png())
    assert r.status_code == 409, r.text
    assert "51 images" in r.json()["message"] and "limit is 50" in r.json()["message"]
    assert t8.library(game_id)["meters"] == before["meters"]

    # The library budget, on a second game: one row planted near the budget (its byte_size
    # is what the budget counts), then a real upload that would cross it.
    game2 = t8.create_game(course_id)
    t8.upload_ok(game2, png())
    item = t8.library(game2)["images"][0]
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE images SET byte_size = %s WHERE id = %s",
                (LIBRARY_BUDGET - 1000, item["id"]),
            )
        conn.commit()
    finally:
        conn.close()
    before = t8.library(game2)
    r = t8.upload(game2, noise_jpeg((200, 200)))
    assert r.status_code == 409, r.text
    assert "library budget is 24 MiB" in r.json()["message"]
    assert t8.library(game2)["meters"] == before["meters"]


# ---------------------------------------------------------------------------
# Access (spec §2.8) and links (spec §2.9)
# ---------------------------------------------------------------------------


def test_library_access_rules(t8):
    course_id, game_id = t8.setup_game()
    other_course, other_game = t8.setup_game()
    item = t8.upload_ok(game_id, png())
    other_item = t8.upload_ok(other_game, png("blue"))

    _, no_grant = t8.make_host("nogrant", course_id)
    _, other_host = t8.make_host("other", other_course, other_game)
    room_code, _ = t8.create_room(course_id, game_id)
    [guest] = create_guest_tokens(t8.base_url, room_code, 1)

    for token in (no_grant, other_host, guest):
        assert t8.req("GET", f"/api/host/games/{game_id}/images", token).status_code == 403
        assert t8.upload(game_id, png(), token=token).status_code == 403
        r = t8.req(
            "PATCH", f"/api/host/games/{game_id}/images/{item['id']}", token,
            json={"description": "mine now"},
        )
        assert r.status_code == 403
        assert t8.req("DELETE", f"/api/host/games/{game_id}/images/{item['id']}", token).status_code == 403
    assert t8.req("GET", f"/api/host/games/{game_id}/images").status_code == 401

    # Admin allowed; another game's image is a 404 through this game's URL.
    assert t8.library(game_id)["images"][0]["id"] == item["id"]
    for method, kwargs in (
        ("PATCH", {"json": {"description": "x"}}),
        ("DELETE", {}),
        ("PUT", {"files": {"file": ("a.png", png(), "image/png")}}),
    ):
        r = t8.admin(method, f"/api/host/games/{game_id}/images/{other_item['id']}", **kwargs)
        assert r.status_code == 404, (method, r.text)
    assert t8.admin("GET", "/api/host/games/99999999/images").status_code == 404


def forged_image_token(image_id: int, version: int, variant: str, exp: datetime) -> str | None:
    """An image token signed with the stack's key from .env, or None if that's not possible."""
    try:
        from jose import jwt
    except ImportError:
        return None
    key = read_repo_env().get("JWT_PRIVATE_KEY")
    if not key:
        return None
    return jwt.encode(
        {"token_type": "image", "img": image_id, "v": version, "var": variant, "exp": exp},
        base64.b64decode(key).decode(),
        algorithm="RS256",
    )


async def test_links_verify_token_path_and_version(t8):
    course_id, game_id = t8.setup_game()
    item = t8.upload_ok(game_id, png())
    other = t8.upload_ok(game_id, png("blue"))
    url, small_url = item["url"], item["small_url"]
    token = url.split("t=", 1)[1]

    ok = fetch(t8, url)
    assert ok.status_code == 200
    assert ok.headers["x-content-type-options"] == "nosniff"
    assert ok.headers["content-disposition"] == "inline"
    cache = ok.headers["cache-control"]
    assert cache.startswith("private, max-age=") and cache.endswith(", immutable")
    max_age = int(cache.split("max-age=")[1].split(",")[0])
    assert 2 * 3600 - 120 <= max_age <= 3 * 3600  # valid for 2–3 hours
    expires = datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00"))
    assert expires.minute == 0 and expires.second == 0

    refused = [
        url[:-3] + ("AAA" if not url.endswith("AAA") else "BBB"),   # tampered
        url.replace("/full?", "/small?"),                            # wrong variant
        url.replace(f"/images/{item['id']}/", f"/images/{other['id']}/"),  # other image
        url.replace("/v1/", "/v2/"),                                 # version not in the token
        f"/api/images/{item['id']}/v1/full?t={t8.admin_token}",      # an access token
        f"/api/images/{item['id']}/v1/full",                         # no token
    ]
    for bad in refused:
        r = fetch(t8, bad)
        assert r.status_code == 403, (bad, r.status_code)
        assert "cache-control" not in r.headers
    expired = forged_image_token(
        item["id"], 1, "full", datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    if expired:
        assert fetch(t8, f"/api/images/{item['id']}/v1/full?t={expired}").status_code == 403
    else:
        warnings.warn(
            "Expired-link case not run: needs python-jose and JWT_PRIVATE_KEY in .env "
            "(the unit tests cover expiry)"
        )

    # An image token authenticates nothing else: not as a bearer token, not as socket auth.
    assert t8.req("GET", "/api/auth/me", token).status_code == 401
    assert t8.req("GET", f"/api/host/games/{game_id}/images", token).status_code == 401
    client = SocketClient(t8.base_url, token, "image-token")
    with pytest.raises(Exception):
        await client.connect()
    await client.disconnect()

    # Stable within the hour; a replace makes new URLs and the old ones 404.
    assert t8.image(game_id, item["id"])["url"] == url
    r = t8.replace(game_id, item["id"], png("green", (70, 50)))
    assert r.status_code == 200, r.text
    replaced = r.json()
    assert replaced["version"] == 2
    assert "/v2/full?" in replaced["url"] and replaced["url"] != url
    assert fetch(t8, url).status_code == 404
    assert fetch(t8, small_url).status_code == 404
    assert fetch(t8, replaced["url"]).status_code == 200
    # A deleted image's links are gone too.
    assert t8.admin("DELETE", f"/api/host/games/{game_id}/images/{other['id']}").status_code == 204
    assert fetch(t8, other["url"]).status_code == 404


# ---------------------------------------------------------------------------
# References (spec §2.7)
# ---------------------------------------------------------------------------


def bundle_v2(images: list[dict], questions: list[dict], **game) -> bytes:
    return json.dumps(
        {
            "format": "buzzer/game",
            "version": 2,
            "game": {"title": f"T8 import {uuid.uuid4().hex[:6]}", **game},
            "images": images,
            "questions": questions,
        }
    ).encode()


def bundle_image(key="img-1", data: bytes | None = None, **extra) -> dict:
    entry = {
        "key": key,
        "filename": f"{key}.png",
        "description": f"Image {key}",
        "data": base64.b64encode(data if data is not None else png()).decode(),
    }
    entry.update(extra)
    return entry


def bundle_v1(questions: list[dict], **extra) -> bytes:
    return json.dumps(
        {
            "format": "buzzer/game",
            "version": 1,
            "game": {"title": f"T8 v1 {uuid.uuid4().hex[:6]}"},
            "questions": questions,
            **extra,
        }
    ).encode()


def test_references_checked_on_every_write_path(t8):
    course_id, game_id = t8.setup_game()
    _, other_game = t8.setup_game()
    mine = t8.upload_ok(game_id, png())["id"]
    theirs = t8.upload_ok(other_game, png("blue"))["id"]

    # Create: same library only, with the same message for a missing and a foreign id.
    for foreign in (theirs, 99999999):
        r = t8.create_question(game_id, mc(prompt_image_id=foreign))
        assert r.status_code == 422, r.text
        assert r.json()["message"] == f"Image {foreign} isn't in this game's library"
        r = t8.create_question(game_id, mc(options=["", "B"], option_images=[foreign, None]))
        assert r.status_code == 422

    # prompt_image_id is accepted on all four types.
    for body in (mc(), tf(), fitb(), ms()):
        body["config"]["prompt_image_id"] = mine
        assert t8.create_question(game_id, body).status_code == 201, body["type"]

    # Update: checked against the merged question.
    qid = t8.question_ok(game_id, mc())
    r = t8.admin(
        "PUT",
        f"/api/host/games/{game_id}/questions/{qid}",
        json={"config": {"options": ["Paris", "Rome"], "prompt_image_id": theirs}},
    )
    assert r.status_code == 422 and "isn't in this game's library" in r.json()["message"]

    # Caption-or-image, and option_images only on the choice types.
    r = t8.create_question(game_id, mc(options=["Paris", " "], option_images=[mine, None]))
    assert r.status_code == 422 and "Option B needs a caption or an image" in r.text
    r = t8.create_question(game_id, mc(options=["", "B"], option_images=[mine, None]))
    assert r.status_code == 201
    for body in (tf(option_images=[mine]), fitb(option_images=[mine])):
        r = t8.create_question(game_id, body)
        assert r.status_code == 422 and "option_images is only allowed" in r.text

    # Import: a version 2 reference must be one of the bundle's own keys.
    r = t8.import_bundle(
        course_id, bundle_v2([bundle_image("a")], [mc(prompt_image_id="nope")])
    )
    assert r.status_code == 422 and 'unknown image key "nope"' in r.json()["message"]
    r = t8.import_bundle(course_id, bundle_v2([bundle_image("a")], [mc(prompt_image_id=mine)]))
    assert r.status_code == 422 and f"unknown image key {mine}" in r.json()["message"]


def test_option_rules_apply_only_with_option_images(t8):
    course_id, game_id = t8.setup_game()
    image = t8.upload_ok(game_id, png())["id"]
    # Text-only: blank options behave as before.
    qid = t8.question_ok(game_id, mc(options=["", "Rome"]))
    r = t8.create_question(game_id, mc(options=["Paris", ""], option_images=[image, None]))
    assert r.status_code == 422 and "Option B needs a caption or an image" in r.text
    # A merged update that adds option_images to a question with a blank option.
    r = t8.admin(
        "PUT",
        f"/api/host/games/{game_id}/questions/{qid}",
        json={"config": {"options": ["", "Rome"], "option_images": [None, image]}},
    )
    assert r.status_code == 422 and "Option A needs a caption or an image" in r.json()["message"]
    # The same content in a version 2 import.
    r = t8.import_bundle(
        course_id,
        bundle_v2([bundle_image("a")], [mc(options=["", "Rome"], option_images=[None, "a"])]),
    )
    assert r.status_code == 422 and "Option A needs a caption or an image" in r.json()["message"]


def test_image_id_refused_on_current_types(t8):
    course_id, game_id = t8.setup_game()
    image = t8.upload_ok(game_id, png())["id"]
    games_before = t8.game_count()
    for body in (mc(), tf(), fitb(), ms()):
        body["config"]["image_id"] = image
        r = t8.create_question(game_id, body)
        assert r.status_code == 422 and "only allowed on canvas question types" in r.text
    qid = t8.question_ok(game_id, mc())
    r = t8.admin(
        "PUT",
        f"/api/host/games/{game_id}/questions/{qid}",
        json={"config": {"options": ["Paris", "Rome"], "image_id": image}},
    )
    assert r.status_code == 422 and "canvas" in r.json()["message"]
    r = t8.import_bundle(course_id, bundle_v2([bundle_image("a")], [mc(image_id="a")]))
    assert r.status_code == 422 and "canvas" in r.json()["message"]
    r = t8.import_bundle(course_id, bundle_v1([mc(image_id=5)]))
    assert r.status_code == 422
    # A legacy null is tolerated.
    assert t8.create_question(game_id, mc(image_id=None)).status_code == 201
    assert t8.game_count() == games_before


def test_version1_bundle_with_image_refs_refused(t8):
    course_id, _ = t8.setup_game()
    games_before = t8.game_count()
    for body in (mc(prompt_image_id=5), mc(options=["", "B"], option_images=[5, None])):
        r = t8.import_bundle(course_id, bundle_v1([tf(), body]))
        assert r.status_code == 422
        assert r.json()["message"] == "Question 2: version 1 bundles can't reference images"
    # A version 2 bundle with an integer reference.
    r = t8.import_bundle(course_id, bundle_v2([bundle_image("a")], [mc(prompt_image_id=1)]))
    assert r.status_code == 422 and "unknown image key 1" in r.json()["message"]
    assert t8.game_count() == games_before


def test_text_only_version1_import_unchanged(t8):
    course_id, _ = t8.setup_game()
    body = mc(options=["", 7])
    r = t8.import_bundle(course_id, bundle_v1([body]))
    assert r.status_code == 201, r.text
    [stored] = t8.questions(r.json()["game_id"])
    assert stored["config"] == {"options": ["", 7]}
    assert stored["answer_data"] == body["answer_data"]


# ---------------------------------------------------------------------------
# Replace, delete, descriptions and live games (spec §2.13)
# ---------------------------------------------------------------------------


def test_replace_updates_every_use(t8):
    _, game_id = t8.setup_game()
    item = t8.upload_ok(game_id, png())
    q1 = t8.question_ok(game_id, mc(prompt_image_id=item["id"]))
    q2 = t8.question_ok(game_id, ms(options=["", "B", "C"], option_images=[item["id"], None, None]))
    r = t8.replace(game_id, item["id"], png("purple", (90, 60)))
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["id"] == item["id"] and new["version"] == 2
    assert (new["width"], new["height"]) == (90, 60)
    assert new["replaced_at"] is not None
    assert {(u["question_id"], u["role"]) for u in new["used_by"]} == {(q1, "prompt"), (q2, "option")}
    # Both questions still refer to the same id, which now resolves to the new bytes.
    stored = Image.open(io.BytesIO(fetch(t8, new["url"]).content))
    assert stored.size == (90, 60)
    assert t8.question(game_id, q1)["config"]["prompt_image_id"] == item["id"]


def test_replace_updates_filename_keeps_description(t8):
    _, game_id = t8.setup_game()
    item = t8.upload_ok(game_id, png(), description="The Eiffel Tower", filename="old.png")
    assert item["filename"] == "old.png"
    r = t8.replace(game_id, item["id"], png("blue"), filename="C:\\photos\\new tower.png")
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == "new tower.png"
    assert r.json()["description"] == "The Eiffel Tower"


def test_refused_replace_leaves_image_intact(t8):
    _, game_id = t8.setup_game()
    original = png()
    item = t8.upload_ok(game_id, original)
    before = t8.image(game_id, item["id"])
    for data, status in ((SVG, 415), (truncated_png(), 422), (png_header(10000, 9000), 422)):
        assert t8.replace(game_id, item["id"], data).status_code == status
    after = t8.image(game_id, item["id"])
    assert after == before
    assert fetch(t8, after["url"]).content == original


def test_delete_refused_while_referenced(t8):
    _, game_id = t8.setup_game()
    item = t8.upload_ok(game_id, png())
    t8.question_ok(game_id, tf())  # Q1: no image
    q2 = t8.question_ok(game_id, mc(prompt_image_id=item["id"]))  # Q2: prompt
    q3 = t8.question_ok(  # Q3: option B, an image-only option
        game_id, mc(options=["Paris", ""], option_images=[None, item["id"]])
    )
    path = f"/api/host/games/{game_id}/images/{item['id']}"
    r = t8.admin("DELETE", path)
    assert r.status_code == 409, r.text
    assert r.json()["message"].endswith("Used by Q2 (prompt), Q3 (option B).")
    assert t8.image(game_id, item["id"]) is not None

    # Once no question uses it, it can go.
    t8.admin(
        "PUT",
        f"/api/host/games/{game_id}/questions/{q2}",
        json={"config": {"options": ["Paris", "Rome"]}},
    )
    assert t8.admin("DELETE", f"/api/host/games/{game_id}/questions/{q3}").status_code == 204
    wait_until(lambda: t8.image(game_id, item["id"])["used_by"] == [], what="the uses to go")
    assert t8.admin("DELETE", path).status_code == 204
    assert t8.library(game_id)["images"] == []


def test_description_patch(t8):
    course_id, game_id = t8.setup_game()
    _, other_game = t8.setup_game()
    item = t8.upload_ok(game_id, png(), description="Old")
    other = t8.upload_ok(other_game, png())
    path = f"/api/host/games/{game_id}/images/{item['id']}"
    r = t8.admin("PATCH", path, json={"description": "  A red square  "})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "A red square"
    assert r.json()["url"] == item["url"] and r.json()["version"] == 1  # links unchanged
    for bad in ("", "   ", "x" * 301):
        assert t8.admin("PATCH", path, json={"description": bad}).status_code == 422
    assert t8.admin("PATCH", path, json={"description": "x", "extra": 1}).status_code == 422
    _, no_grant = t8.make_host("patcher", course_id)
    assert t8.req("PATCH", path, no_grant, json={"description": "x"}).status_code == 403
    r = t8.admin(
        "PATCH", f"/api/host/games/{game_id}/images/{other['id']}", json={"description": "x"}
    )
    assert r.status_code == 404
    # Allowed while a session is live.
    t8.create_room(course_id, game_id)
    r = t8.admin("PATCH", path, json={"description": "Edited live"})
    assert r.status_code == 200 and r.json()["description"] == "Edited live"


async def test_live_guard(t8):
    course_id, game_id = t8.setup_game()
    t8.question_ok(game_id, tf())
    item = t8.upload_ok(game_id, png())
    path = f"/api/host/games/{game_id}/images/{item['id']}"

    def assert_blocked(room_code: str):
        for r in (t8.replace(game_id, item["id"], png("blue")), t8.admin("DELETE", path)):
            assert r.status_code == 409, r.text
            message = r.json()["message"]
            assert f"room {room_code}" in message and "LOBBY" in message
            assert "hosted by" in message and "delete it from Play" in message
        library = t8.library(game_id)
        assert [s["room_code"] for s in library["live_sessions"]] == [room_code]
        # Uploads stay allowed while live.
        extra = t8.upload_ok(game_id, png("gray"))
        assert t8.admin("DELETE", f"/api/host/games/{game_id}/images/{extra['id']}").status_code == 409

    # 1. Deleting the session ends the block.
    room_code, session_id = t8.create_room(course_id, game_id)
    assert_blocked(room_code)
    assert t8.admin("DELETE", f"/api/game/sessions/{session_id}").status_code == 204
    t8.session_ids.remove(session_id)
    assert t8.replace(game_id, item["id"], png("blue")).status_code == 200

    # 2. So does completing it.
    room_code, _ = t8.create_room(course_id, game_id)
    assert_blocked(room_code)
    try:
        await t8.play_to_completion(room_code, 1)
    finally:
        await t8.close_sockets()
    assert t8.replace(game_id, item["id"], png("green")).status_code == 200

    # 3. And so does its Redis room disappearing (a long-abandoned lobby).
    room_code, _ = t8.create_room(course_id, game_id)
    assert_blocked(room_code)
    try:
        redis_cli("DEL", f"room:{room_code}")
    except RuntimeError as exc:
        pytest.skip(f"The 'Redis room gone' case needs redis-cli: {exc}")
    assert t8.library(game_id)["live_sessions"] == []
    assert t8.replace(game_id, item["id"], png("red")).status_code == 200


# ---------------------------------------------------------------------------
# What each screen receives (spec §2.10, §2.17)
# ---------------------------------------------------------------------------


async def test_players_receive_only_small_option_links(t8):
    course_id, game_id = t8.setup_game()
    prompt_image = t8.upload_ok(game_id, png("red"), description="The prompt")
    option_a = t8.upload_ok(game_id, png("blue"), description="Option A picture")
    option_b = t8.upload_ok(game_id, png("green"), description="Option B picture")
    t8.question_ok(
        game_id,
        mc(
            prompt_image_id=prompt_image["id"],
            options=["", "Bee"],
            option_images=[option_a["id"], option_b["id"]],
        ),
    )
    t8.question_ok(game_id, tf())
    room_code, session_id = t8.create_room(course_id, game_id)
    guest_a, guest_b = create_guest_tokens(t8.base_url, room_code, 2)
    try:
        host = await t8.socket(t8.admin_token, room_code, "HOST", "host")
        player = await t8.socket(guest_a, room_code, "PLAYER", "player")
        await host.emit("host_advance", {})
        payload = await player.wait_for("new_question", timeout=_SOCKET_TIMEOUT)

        images = payload["images"]
        assert set(images) == {str(option_a["id"]), str(option_b["id"])}
        for image_id, entry in images.items():
            assert f"/api/images/{image_id}/v1/small?t=" in entry["url"]
            assert set(entry) == {"url", "width", "height", "description"}
        assert images[str(option_a["id"])]["description"] == "Option A picture"
        assert "/full" not in json.dumps(payload)
        # config passes through unchanged, prompt image id included (ids aren't sensitive).
        assert payload["config"]["prompt_image_id"] == prompt_image["id"]
        # The guest can fetch its link, with no bearer token.
        small = fetch(t8, images[str(option_a["id"])]["url"])
        assert small.status_code == 200 and small.headers["content-type"] == "image/webp"

        # The host's session map has full and small links for every image.
        links = t8.session_links(session_id).json()["images"]
        assert set(links) == {str(i["id"]) for i in (prompt_image, option_a, option_b)}
        assert all("/full?" in entry["url"] and "/small?" in entry["small_url"] for entry in links.values())

        # A late-joining player who hasn't answered gets the question with fresh links.
        late = await t8.socket(guest_b, room_code, "PLAYER", "late")
        late_payload = await late.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
        assert set(late_payload["images"]) == set(images)
        assert fetch(t8, late_payload["images"][str(option_b["id"])]["url"]).status_code == 200

        # A text-only question carries an empty map.
        await host.emit("host_advance", {})
        await host.wait_for("question_results", timeout=_SOCKET_TIMEOUT)
        await host.emit("host_advance", {})
        second = await player.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
        assert second["images"] == {}
    finally:
        await t8.close_sockets()


def test_session_links_follow_current_game_access(t8):
    course_id, game_id = t8.setup_game()
    other_course = t8.create_course("Other")
    t8.question_ok(game_id, tf())
    t8.upload_ok(game_id, png())
    host_id, host = t8.make_host("sessionhost", course_id, game_id)
    room_code, session_id = t8.create_room(course_id, game_id, token=host)

    assert t8.session_links(session_id, host).status_code == 200
    t8.revoke_game(host_id, game_id)
    assert t8.session_links(session_id, host).status_code == 403
    t8.grant_game(host_id, game_id)
    assert t8.session_links(session_id, host).status_code == 200
    # Moving the game to a course the host doesn't HOST revokes the grant: 403 again.
    r = t8.admin("PUT", f"/api/admin/games/{game_id}", json={"course_id": other_course})
    assert r.status_code == 200, r.text
    wait_until(lambda: t8.session_links(session_id, host).status_code == 403, what="the move")
    assert t8.session_links(session_id).status_code == 200  # admin
    assert t8.session_links(session_id, host).status_code == 403
    assert t8.admin("GET", "/api/game/sessions/no-such-session/images").status_code == 404
    # Independent of the Redis room: still 200 once it's gone.
    try:
        redis_cli("DEL", f"room:{room_code}")
    except RuntimeError as exc:
        pytest.skip(f"The 'Redis room deleted' case needs redis-cli: {exc}")
    assert t8.session_links(session_id).status_code == 200


async def test_completed_session_links_still_served(t8):
    course_id, game_id = t8.setup_game()
    t8.question_ok(game_id, tf())
    t8.upload_ok(game_id, png())
    room_code, session_id = t8.create_room(course_id, game_id)
    try:
        await t8.play_to_completion(room_code, 1)
    finally:
        await t8.close_sockets()
    r = t8.session_links(session_id)
    assert r.status_code == 200 and len(r.json()["images"]) == 1


# ---------------------------------------------------------------------------
# The report (spec §2.14)
# ---------------------------------------------------------------------------


def data_uris(html: str) -> list[str]:
    return re.findall(r'src="(data:image/[a-z]+;base64,[A-Za-z0-9+/=]+)"', html)


async def test_report_embeds_small_copies(t8):
    course_id, game_id = t8.setup_game()
    prompt_image = t8.upload_ok(game_id, png("red"), description="Prompt <picture>")
    a = t8.upload_ok(game_id, png("blue"), description="Blue square")
    b = t8.upload_ok(game_id, png("green"), description="Green square")
    t8.question_ok(
        game_id,
        mc(prompt="MC", prompt_image_id=prompt_image["id"], options=["", "Bee"],
           option_images=[a["id"], b["id"]]),
    )
    t8.question_ok(
        game_id,
        ms(prompt="MS", options=["", "Cee", "Plain"], option_images=[a["id"], b["id"], None]),
    )
    t8.question_ok(game_id, tf(prompt="TF", prompt_image_id=b["id"]))
    # A replace before the room is created doesn't produce the note.
    assert t8.replace(game_id, a["id"], png("navy")).status_code == 200
    time.sleep(1.1)  # replaced_at and created_at have whole-second precision
    room_code, session_id = t8.create_room(course_id, game_id)
    try:
        await t8.play_to_completion(room_code, 3)
    finally:
        await t8.close_sockets()

    r = t8.admin("GET", f"/api/host/sessions/{session_id}/report")
    assert r.status_code == 200, r.text
    html = r.text
    uris = data_uris(html)
    # MC: prompt + 2 options; MS: 2 option thumbnails; TF: prompt.
    assert len(uris) == 6
    assert all(uri.startswith("data:image/webp;base64,") for uri in uris)
    for uri in uris:
        assert len(base64.b64decode(uri.split(",", 1)[1])) <= MIB
    assert 'alt="Prompt &lt;picture&gt;"' in html  # descriptions are escaped
    assert 'alt="Blue square"' in html and 'alt="Green square"' in html
    # The multi-select list: letter alone for the caption-less option, captions otherwise.
    assert '<ul class="opt-list">' in html
    assert '<span class="opt-letter">A</span><img' in html
    assert '<span class="opt-caption">Cee</span>' in html
    assert '<span class="opt-caption">Plain</span>' in html
    assert "Replaced after this session" not in html

    # A replace after the session: the report now shows the new image, with the note. (A real
    # replace can only follow a session's end, minutes after it was created; this scripted one
    # finishes within a second, so wait out the whole-second precision of the two timestamps.)
    time.sleep(1.1)
    assert t8.replace(game_id, b["id"], png("yellow")).status_code == 200
    html = t8.admin("GET", f"/api/host/sessions/{session_id}/report").text
    assert html.count("Replaced after this session — players saw an earlier version.") == 3
    assert len(data_uris(html)) == 6


async def test_report_withholds_images_without_game_access(t8):
    course_id, game_id = t8.setup_game()
    image = t8.upload_ok(game_id, png(), description="Secret picture")
    t8.question_ok(game_id, mc(prompt_image_id=image["id"]))
    host_id, host = t8.make_host("reporter", course_id, game_id)
    room_code, session_id = t8.create_room(course_id, game_id, token=host)
    try:
        await t8.play_to_completion(room_code, 1, token=host)
    finally:
        await t8.close_sockets()
    path = f"/api/host/sessions/{session_id}/report"
    html = t8.req("GET", path, host).text
    assert len(data_uris(html)) == 1

    t8.revoke_game(host_id, game_id)
    r = t8.req("GET", path, host)
    assert r.status_code == 200, r.text  # downloading needs only the session check
    assert data_uris(r.text) == []
    assert "[image withheld — you no longer have access to this game]" in r.text
    assert "Secret picture" not in r.text
    # Admins always pass the game rule, through either router.
    assert len(data_uris(t8.admin("GET", path).text)) == 1
    assert len(data_uris(t8.admin("GET", f"/api/admin/sessions/{session_id}/report").text)) == 1


# ---------------------------------------------------------------------------
# Export and import (spec §2.15)
# ---------------------------------------------------------------------------


def image_hashes(t8: T8Env, game_id: int) -> list[str]:
    return [sha256(fetch(t8, i["url"]).content) for i in t8.library(game_id)["images"]]


def test_export_import_cycles_preserve_bytes(t8):
    course_id, game_id = t8.setup_game()
    first = t8.upload_ok(game_id, noise_jpeg((300, 200)), description="Noise", filename="n.jpg")
    second = t8.upload_ok(game_id, png(), description="Red", filename="red.png")
    t8.upload_ok(game_id, webp(), description="Unused", filename="u.webp")  # unused: exported too
    t8.question_ok(
        game_id,
        mc(prompt_image_id=second["id"], options=["", "Rome"], option_images=[first["id"], None]),
    )
    t8.question_ok(game_id, tf(prompt="T & F"))

    bundle = t8.export(game_id)
    assert len(bundle) == t8.meter(game_id)  # the meter measures exactly what is exported
    parsed = json.loads(bundle)
    assert parsed["version"] == 2
    assert [i["key"] for i in parsed["images"]] == ["img-1", "img-2", "img-3"]
    assert all(set(i) == {"key", "filename", "description", "data"} for i in parsed["images"])
    hashes = image_hashes(t8, game_id)

    # Three generations: identical image bytes and byte-identical bundles.
    generation = game_id
    for _ in range(3):
        r = t8.import_bundle(course_id, bundle)
        assert r.status_code == 201, r.text
        generation = r.json()["game_id"]
        assert image_hashes(t8, generation) == hashes
        again = t8.export(generation)
        assert again == bundle
        bundle = again

    # A non-conforming image in a hand-built bundle is normalized once, then stays stable.
    raw = jpeg_with_orientation((60, 30), 6)
    r = t8.import_bundle(course_id, bundle_v2([bundle_image("rotated", raw)], [tf()]))
    assert r.status_code == 201, r.text
    normalized = t8.export(r.json()["game_id"])
    [entry] = json.loads(normalized)["images"]
    assert base64.b64decode(entry["data"]) != raw
    r = t8.import_bundle(course_id, normalized)
    assert r.status_code == 201, r.text
    assert t8.export(r.json()["game_id"]) == normalized

    # Unknown fields in an image entry are refused.
    r = t8.import_bundle(
        course_id, bundle_v2([bundle_image("a", content_type="image/png")], [tf()])
    )
    assert r.status_code == 422 and "Extra inputs are not permitted" in r.json()["message"]


def test_version1_samples_still_import(t8):
    course_id, _ = t8.setup_game()
    samples = sorted((_REPO_ROOT / "sample_games").glob("*.json"))
    assert samples
    for path in samples:
        raw = path.read_bytes()
        r = t8.import_bundle(course_id, raw)
        assert r.status_code == 201, (path.name, r.text)
        game_id = r.json()["game_id"]
        assert len(t8.questions(game_id)) == len(json.loads(raw)["questions"])
        assert json.loads(t8.export(game_id))["version"] == 1
        assert t8.library(game_id)["images"] == []


def test_bundle_images_field_edge_cases(t8):
    course_id, _ = t8.setup_game()
    games_before = t8.game_count()
    r = t8.import_bundle(course_id, bundle_v2({"a": 1}, [tf()]))  # type: ignore[arg-type]
    assert r.status_code == 422 and "must be a list" in r.json()["message"]
    r = t8.import_bundle(course_id, bundle_v1([tf()], images=[]))
    assert r.status_code == 422
    assert r.json()["message"] == "Version 1 bundles can't contain images"
    r = t8.import_bundle(
        course_id, json.dumps({"format": "buzzer/game", "version": 3, "game": {"title": "x"}}).encode()
    )
    assert r.status_code == 422
    assert t8.game_count() == games_before
    r = t8.import_bundle(course_id, bundle_v2([], [tf()]))
    assert r.status_code == 201, r.text
    game_id = r.json()["game_id"]
    assert t8.library(game_id)["images"] == []
    assert json.loads(t8.export(game_id))["version"] == 1  # an empty library exports as v1


def test_import_counts_images_before_decoding(t8):
    course_id, _ = t8.setup_game()
    entries = [bundle_image(f"k{n}") | {"data": "!!not base64!!"} for n in range(51)]
    r = t8.import_bundle(course_id, bundle_v2(entries, [tf()]))
    assert r.status_code == 409, r.text  # the count, not the base64 (422)
    assert "51 images" in r.json()["message"]


def test_import_checks_references_before_processing(t8):
    course_id, _ = t8.setup_game()
    undecodable = bundle_image("broken", b"not an image at all")
    r = t8.import_bundle(course_id, bundle_v2([undecodable], [mc(prompt_image_id="missing")]))
    assert r.status_code == 422, r.text
    assert 'unknown image key "missing"' in r.json()["message"]  # not the 415 for the image


def test_non_list_option_images_in_a_bundle_are_refused(t8):
    course_id, _ = t8.setup_game()
    body = mc()
    body["config"]["option_images"] = "img-1"
    r = t8.import_bundle(course_id, bundle_v2([bundle_image("img-1")], [body]))
    assert r.status_code == 422
    assert r.json()["message"] == "Question 1: config.option_images must be a list"


def test_game_delete_removes_images(t8):
    course_id, game_id = t8.setup_game()
    items = [t8.upload_ok(game_id, png(c)) for c in ("red", "blue")]
    t8.question_ok(game_id, mc(prompt_image_id=items[0]["id"]))
    assert t8.admin("DELETE", f"/api/admin/games/{game_id}").status_code == 204
    wait_until(
        lambda: t8.admin("GET", f"/api/admin/games/{game_id}").status_code == 404,
        what="the delete",
    )
    t8.game_ids.remove(game_id)
    for item in items:
        assert fetch(t8, item["url"]).status_code == 404
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM images WHERE game_id = %s", (game_id,))
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


async def test_legacy_image_keys_treated_as_missing(t8):
    course_id, game_id = t8.setup_game()
    other_course, other_game = t8.setup_game()
    mine = t8.upload_ok(game_id, png("red"))
    theirs = t8.upload_ok(other_game, png("blue"))

    # A missing id and a cross-game id, planted where the API would refuse them.
    q_missing = t8.question_ok(game_id, mc(prompt="missing"))
    plant_config(
        q_missing,
        {"prompt_image_id": 999999, "options": ["Paris", "Rome"], "option_images": [999999, None]},
    )
    q_cross = t8.question_ok(game_id, mc(prompt="cross"))
    plant_config(
        q_cross,
        {"prompt_image_id": theirs["id"], "options": ["Paris", "Rome"],
         "option_images": [theirs["id"], None]},
    )
    q_all_null = t8.question_ok(game_id, mc(prompt="all null"))
    plant_config(q_all_null, {"options": ["", ""], "option_images": [999999, 999998]})
    q_partial = t8.question_ok(game_id, mc(prompt="partial"))
    plant_config(q_partial, {"options": ["", ""], "option_images": [mine["id"], 999999]})

    library = t8.library(game_id)
    assert [u["question_id"] for u in library["images"][0]["used_by"]] == [q_partial]
    bundle = t8.export(game_id)
    assert len(bundle) == library["meters"]["export_bytes"]
    configs = {q["prompt"]: q["config"] for q in json.loads(bundle)["questions"]}
    assert configs["missing"] == {"options": ["Paris", "Rome"]}
    assert configs["cross"] == {"options": ["Paris", "Rome"]}
    assert configs["all null"] == {"options": ["", ""]}
    assert configs["partial"] == {"options": ["", "Missing image"], "option_images": ["img-1", None]}
    r = t8.import_bundle(course_id, bundle)
    assert r.status_code == 201, r.text

    # Play and report treat them as no image; the cross-game image never leaks.
    room_code, session_id = t8.create_room(course_id, game_id)
    try:
        host = await t8.socket(t8.admin_token, room_code, "HOST", "host")
        await host.emit("host_advance", {})
        payload = await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
        assert payload["images"] == {}
        await host.emit("host_advance", {})
        await host.wait_for("question_results", timeout=_SOCKET_TIMEOUT)
        await host.emit("host_advance", {})
        payload = await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
        assert payload["images"] == {}  # the cross-game id is unresolved
        links = t8.session_links(session_id).json()["images"]
        assert set(links) == {str(mine["id"])}
        # Each advance waits for its own event: the gateway handles events concurrently, so
        # an advance sent before the previous one took effect would race it.
        for _ in range(2):
            await host.emit("host_advance", {})
            await host.wait_for("question_results", timeout=_SOCKET_TIMEOUT)
            await host.emit("host_advance", {})
            await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
        await host.emit("host_advance", {})
        await host.wait_for("question_results", timeout=_SOCKET_TIMEOUT)
        await host.emit("host_advance", {})
        await host.wait_for("game_over", timeout=_SOCKET_TIMEOUT)
    finally:
        await t8.close_sockets()
    wait_until(lambda: t8.room_status(room_code) == "COMPLETED", what="completion")
    html = t8.admin("GET", f"/api/host/sessions/{session_id}/report").text
    assert len(data_uris(html)) == 1  # only the partial question's resolved image
    theirs_small = fetch(t8, theirs["small_url"]).content
    assert base64.b64encode(theirs_small).decode() not in html


# ---------------------------------------------------------------------------
# The lock tests (spec §2.6, §4.11). Each holds the game row lock from a test connection,
# starts requests, and releases only once PROCESSLIST shows every one of them waiting on it —
# so none can pass by running sequentially after the release.
# ---------------------------------------------------------------------------


def pending(client: SocketClient, event: str) -> int:
    return client._queues[event].qsize()


async def test_concurrent_uploads_respect_count(t8):
    _, game_id = t8.setup_game()
    for n in range(IMAGE_COUNT_LIMIT - 1):
        t8.upload_ok(game_id, png(size=(8, 8 + n)))
    harness = LockHarness()
    try:
        harness.lock(game_id)
        async with httpx.AsyncClient(base_url=t8.base_url, timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {t8.admin_token}"}

            async def upload(color: str):
                return await client.post(
                    f"/api/host/games/{game_id}/images",
                    headers=headers,
                    files={"file": ("x.png", png(color), "image/png")},
                    data={"description": f"Concurrent {color}"},
                )

            tasks = [asyncio.create_task(upload(c)) for c in ("red", "blue")]
            await harness.wait_for_waiters(game_id, 2)
            assert not any(task.done() for task in tasks)
            harness.release()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 20)
    finally:
        harness.close()
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    refused = next(r for r in responses if r.status_code == 409)
    assert "51 images" in refused.json()["message"]
    assert t8.library(game_id)["meters"]["image_count"] == IMAGE_COUNT_LIMIT


async def check_room_and_replace_serialized(t8: T8Env, first: str) -> str:
    course_id, game_id = t8.setup_game()
    image = t8.upload_ok(game_id, png("red"))
    original = sha256(fetch(t8, image["url"]).content)
    t8.question_ok(game_id, mc(options=["", "Rome"], option_images=[image["id"], None]))
    harness = LockHarness()
    try:
        harness.lock(game_id)
        async with httpx.AsyncClient(base_url=t8.base_url, timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {t8.admin_token}"}

            async def room():
                return await client.post(
                    "/api/game/rooms",
                    headers=headers,
                    json={"course_id": course_id, "game_id": game_id},
                )

            async def replace():
                return await client.put(
                    f"/api/host/games/{game_id}/images/{image['id']}",
                    headers=headers,
                    files={"file": ("new.png", png("blue", (80, 60)), "image/png")},
                )

            starters = {"room": room, "replace": replace}
            second = "replace" if first == "room" else "room"
            tasks = {first: asyncio.create_task(starters[first]())}
            await harness.wait_for_waiters(game_id, 1)
            tasks[second] = asyncio.create_task(starters[second]())
            await harness.wait_for_waiters(game_id, 2)
            assert not any(task.done() for task in tasks.values())
            harness.release()
            await asyncio.wait_for(asyncio.gather(*tasks.values()), 20)
    finally:
        harness.close()

    room_r, replace_r = tasks["room"].result(), tasks["replace"].result()
    assert room_r.status_code == 201, room_r.text
    room_code, session_id = room_r.json()["room_code"], room_r.json()["session_id"]
    t8.session_ids.append(session_id)
    stored = t8.image(game_id, image["id"])
    if replace_r.status_code == 409:
        # (a) The room first: the replace saw it live and changed nothing.
        assert f"room {room_code}" in replace_r.json()["message"]
        assert stored["version"] == 1
        assert sha256(fetch(t8, stored["url"]).content) == original
        expected, outcome = "/v1/", "room first"
    else:
        # (b) The replace first: the room uses the new version from the start.
        assert replace_r.status_code == 200, replace_r.text
        assert stored["version"] == 2
        expected, outcome = "/v2/", "replace first"
    links = t8.session_links(session_id).json()["images"]
    assert expected in links[str(image["id"])]["url"]
    host = await t8.socket(t8.admin_token, room_code, "HOST", "host")
    await host.emit("host_advance", {})
    payload = await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
    assert expected in payload["images"][str(image["id"])]["url"]
    return outcome


async def test_room_creation_and_replace_are_serialized(t8):
    try:
        outcomes = {
            first: await check_room_and_replace_serialized(t8, first)
            for first in ("room", "replace")
        }
    finally:
        await t8.close_sockets()
    # Whichever request took the lock first decided the outcome; both were consistent.
    assert set(outcomes.values()) <= {"room first", "replace first"}


async def test_new_room_after_replace_uses_new_version(t8):
    course_id, game_id = t8.setup_game()
    image = t8.upload_ok(game_id, png("red"))
    t8.question_ok(game_id, mc(options=["", "Rome"], option_images=[image["id"], None]))
    assert t8.replace(game_id, image["id"], png("blue")).status_code == 200
    room_code, session_id = t8.create_room(course_id, game_id)
    try:
        links = t8.session_links(session_id).json()["images"]
        assert "/v2/full?" in links[str(image["id"])]["url"]
        host = await t8.socket(t8.admin_token, room_code, "HOST", "host")
        await host.emit("host_advance", {})
        payload = await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
        assert "/v2/small?" in payload["images"][str(image["id"])]["url"]
    finally:
        await t8.close_sockets()


async def test_game_start_reads_fresh_questions(t8):
    course_id, game_id = t8.setup_game()
    t8.question_ok(game_id, tf(prompt="Q1"))
    room_code, _ = t8.create_room(course_id, game_id)
    harness = LockHarness()
    try:
        host = await t8.socket(t8.admin_token, room_code, "HOST", "host")
        harness.lock(game_id)
        harness.execute(
            "INSERT INTO questions (game_id, type, grading_type, prompt, config, answer_data, "
            "time_limit_seconds, points_value, order_index) "
            "VALUES (%s, 'true_false', 'ACCURACY', 'Q2', '{}', %s, 30, 1, 1)",
            (game_id, json.dumps({"answer_points": {"true": 1, "false": 0}})),
        )
        await host.emit("host_advance", {})
        await harness.wait_for_waiters(game_id, 1)
        harness.commit()  # the new question, and the release
        payload = await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
        assert payload["totalQuestions"] == 2
        assert payload["questionNumber"] == 1
    finally:
        harness.close()
        await t8.close_sockets()
    assert t8.room_status(room_code) == "IN_PROGRESS"


async def test_partial_update_reads_fresh_question(t8):
    _, game_id = t8.setup_game()
    qid = t8.question_ok(game_id, mc(options=["A", "B", "C", "D"], points=[1, 0, 0, 0]))
    harness = LockHarness()
    try:
        harness.lock(game_id)
        harness.execute(
            "UPDATE questions SET config = %s, answer_data = %s WHERE id = %s",
            (
                json.dumps({"options": ["A", "B", "C", "D", "E"]}),
                json.dumps({"answer_points": [1, 0, 0, 0, 0]}),
                qid,
            ),
        )
        async with httpx.AsyncClient(base_url=t8.base_url, timeout=30.0) as client:
            task = asyncio.create_task(
                client.put(
                    f"/api/host/games/{game_id}/questions/{qid}",
                    headers={"Authorization": f"Bearer {t8.admin_token}"},
                    json={"answer_data": {"answer_points": [1, 0, 0, 0]}},
                )
            )
            await harness.wait_for_waiters(game_id, 1)
            assert not task.done()
            harness.commit()
            r = await asyncio.wait_for(task, 20)
    finally:
        harness.close()
    # Merged with the fresh question (5 options), 4 points don't fit.
    assert r.status_code == 422, r.text
    stored = t8.question(game_id, qid)
    assert stored["config"]["options"] == ["A", "B", "C", "D", "E"]
    assert stored["answer_data"]["answer_points"] == [1, 0, 0, 0, 0]


async def test_concurrent_start_is_ignored(t8):
    course_id, game_id = t8.setup_game()
    t8.question_ok(game_id, tf(prompt="Q1"))
    t8.question_ok(game_id, tf(prompt="Q2"))
    room_code, _ = t8.create_room(course_id, game_id)
    harness = LockHarness()
    try:
        host = await t8.socket(t8.admin_token, room_code, "HOST", "host")
        harness.lock(game_id)
        await host.emit("host_advance", {})
        await host.emit("host_advance", {})
        # Both advances must be waiting in start_game: an advance that arrived after the first
        # had finished would take the QUESTION → RESULTS branch instead, and this test would
        # prove nothing about "already started".
        await harness.wait_for_waiters(game_id, 2)
        harness.release()
        payload = await host.wait_for("new_question", timeout=_SOCKET_TIMEOUT)
        assert payload["questionNumber"] == 1
        await asyncio.sleep(3)  # the absence checks are bounded by this window
        assert pending(host, "new_question") == 0
        assert pending(host, "error") == 0
        assert pending(host, "question_results") == 0
    finally:
        harness.close()
        await t8.close_sockets()
    assert t8.room_status(room_code) == "IN_PROGRESS"


async def test_question_order_stays_unique_under_concurrency(t8):
    _, game_id = t8.setup_game()
    existing = [t8.question_ok(game_id, tf(prompt=f"Q{n}")) for n in (1, 2)]
    harness = LockHarness()
    try:
        harness.lock(game_id)
        async with httpx.AsyncClient(base_url=t8.base_url, timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {t8.admin_token}"}
            creates = [
                asyncio.create_task(
                    client.post(
                        f"/api/host/games/{game_id}/questions",
                        headers=headers,
                        json=tf(prompt=f"New {n}"),
                    )
                )
                for n in (1, 2)
            ]
            reorder = asyncio.create_task(
                client.post(
                    f"/api/host/games/{game_id}/questions/reorder",
                    headers=headers,
                    json={"order": list(reversed(existing))},
                )
            )
            await harness.wait_for_waiters(game_id, 3)
            assert not any(t.done() for t in (*creates, reorder))
            harness.release()
            created = await asyncio.wait_for(asyncio.gather(*creates), 20)
            reordered = await asyncio.wait_for(reorder, 20)
    finally:
        harness.close()
    assert [r.status_code for r in created] == [201, 201], [r.text for r in created]
    new_ids = [r.json()["id"] for r in created]
    assert reordered.status_code in (204, 409), reordered.text

    def settled():
        questions = t8.questions(game_id)
        indexes = {q["id"]: q["order_index"] for q in questions}
        if reordered.status_code == 204 and indexes[existing[1]] > indexes[existing[0]]:
            return None  # the reorder commits after responding; wait for it
        return indexes

    indexes = wait_until(settled, timeout=10, what="the reorder")
    assert len(set(indexes.values())) == 4, indexes
    assert indexes[new_ids[0]] != indexes[new_ids[1]]
    if reordered.status_code == 204:
        # It ran before both creates, so they were appended after it.
        assert max(indexes[e] for e in existing) < min(indexes[n] for n in new_ids)


# ---------------------------------------------------------------------------
# Budgets and the export-size guarantee (spec §2.6)
# ---------------------------------------------------------------------------


def test_budget_sized_library_round_trips(t8):
    course_id, game_id = t8.setup_game()
    total, seed = 0, 10
    for size in [(1440, 1440)] * 6 + [(500, 500), (300, 300), (200, 200)]:
        data = noise_jpeg(size, quality=100, seed=seed)
        seed += 1
        if total + len(data) > LIBRARY_BUDGET:
            continue
        item = t8.upload_ok(game_id, data, description=f"Noise {seed}", filename=f"n{seed}.jpg")
        assert item["byte_size"] == len(data)  # conforming: stored unchanged
        total += len(data)
    meters = t8.library(game_id)["meters"]
    assert meters["library_bytes"] == total
    assert LIBRARY_BUDGET - MIB < total <= LIBRARY_BUDGET
    ids = [i["id"] for i in t8.library(game_id)["images"]]
    t8.question_ok(game_id, mc(prompt_image_id=ids[0], options=["", "B"], option_images=[ids[1], None]))
    t8.question_ok(game_id, ms(options=["", "", "C"], option_images=[ids[2], ids[3], None]))

    bundle = t8.export(game_id)
    assert len(bundle) == t8.meter(game_id)  # the meter is the exact export size
    assert len(bundle) <= EXPORT_LIMIT
    r = t8.import_bundle(course_id, bundle)
    assert r.status_code == 201, r.text
    assert image_hashes(t8, r.json()["game_id"]) == image_hashes(t8, game_id)


def padding_question(length: int) -> dict:
    """A question whose config carries a plain-ASCII padding string: each character is exactly
    one serialized byte, so the export size can be set to the byte."""
    return {
        "type": "fill_in_the_blank",
        "grading_type": "COMPLETENESS",
        "prompt": "Padding",
        "config": {"pad": "x" * length},
        "answer_data": {},
        "points_value": 1,
    }


def test_export_rule_at_the_boundary(t8):
    course_id, game_id = t8.setup_game()
    images = [
        t8.upload_ok(game_id, noise_jpeg((1400, 1400), 100, seed), description=f"Big {seed}")
        for seed in range(4)
    ]
    pad_q = t8.question_ok(game_id, padding_question(10))
    state = {"pad": 10, "title": f"T8 Game {t8.tag}"}

    def put_padding(length: int) -> httpx.Response:
        return t8.admin(
            "PUT",
            f"/api/host/games/{game_id}/questions/{pad_q}",
            json={"config": {"pad": "x" * length}},
        )

    def put_title(title: str) -> httpx.Response:
        return t8.admin("PUT", f"/api/host/games/{game_id}", json={"title": title})

    def set_headroom(headroom: int) -> None:
        """Resize the padding so the export is exactly the limit minus `headroom`. Question
        updates commit after responding, so the meter is polled, exactly."""
        length = state["pad"] + (EXPORT_LIMIT - headroom - t8.meter(game_id))
        r = put_padding(length)
        assert r.status_code == 200, r.text
        state["pad"] = length
        wait_until(
            lambda: t8.meter(game_id) == EXPORT_LIMIT - headroom,
            timeout=30,
            what=f"headroom {headroom}",
        )

    def refused(r: httpx.Response) -> None:
        assert r.status_code == 409, r.text
        assert f"over the export limit of {EXPORT_LIMIT:,} bytes" in r.json()["message"]
        assert t8.meter(game_id) == EXPORT_LIMIT  # nothing changed

    set_headroom(0)
    # At headroom 0, any growth of one byte or more crosses the limit.
    image = images[0]
    growing = [
        lambda: t8.create_question(game_id, tf(prompt="Q")),
        lambda: put_padding(state["pad"] + 1),
        lambda: put_title(state["title"] + "x"),
        lambda: t8.admin(
            "PATCH",
            f"/api/host/games/{game_id}/images/{image['id']}",
            json={"description": image["description"] + "x"},
        ),
        lambda: t8.upload(game_id, noise_jpeg((700, 700), 100, 99), description="1 MiB"),
        lambda: t8.replace(game_id, image["id"], noise_jpeg((1440, 1440), 100, 98)),
    ]
    for mutate in growing:
        refused(mutate())
    assert t8.library(game_id)["meters"]["image_count"] == 4
    assert t8.image(game_id, image["id"])["version"] == 1

    # A shrinking edit is accepted.
    state["title"] = state["title"][:-1]
    assert put_title(state["title"]).status_code == 200
    wait_until(lambda: t8.meter(game_id) == EXPORT_LIMIT - 1, what="the shorter title")

    # Exact fit: growth of exactly the headroom is accepted; one more byte is not.
    set_headroom(7)
    state["title"] += "abcdefg"
    assert put_title(state["title"]).status_code == 200
    wait_until(lambda: t8.meter(game_id) == EXPORT_LIMIT, what="the exact fit")
    refused(put_title(state["title"] + "h"))

    # A game exactly at the limit exports to exactly the limit, and re-imports.
    bundle = t8.export(game_id)
    assert len(bundle) == EXPORT_LIMIT == 41_943_040
    r = t8.import_bundle(course_id, bundle)
    assert r.status_code == 201, r.text
    assert t8.meter(r.json()["game_id"]) == EXPORT_LIMIT

    # A minified version 1 bundle within the import limit that grows past the export limit
    # when re-serialized (indent 2 puts every list element on its own line) → 409, no game.
    games_before = t8.game_count()
    question = padding_question(0)
    question["config"] = {"pad": [0] * 3_500_000}
    minified = json.dumps(
        {"format": "buzzer/game", "version": 1, "game": {"title": "Minified"},
         "questions": [question]},
        separators=(",", ":"),
    ).encode()
    assert len(minified) <= EXPORT_LIMIT
    r = t8.import_bundle(course_id, minified)
    assert r.status_code == 409, r.text
    assert "export limit" in r.json()["message"]
    assert t8.game_count() == games_before


# MySQL stores some numbers differently from how they were sent, and the stored form can be
# longer. Each pair is (as sent, as the export writes it after storage).
# - points_value is a single-precision FLOAT, read back to about 6 significant digits: a
#   subnormal value comes back with more digits than it was sent with.
FLOAT_SENT, FLOAT_STORED = "1e-40", "9.99995e-41"  # +6 bytes
# - a JSON integer outside 64 bits becomes a double. Both have 20 digits as sent, so the
#   change measures as zero growth before storage.
UINT64_MAX, UINT64_OVER = 18446744073709551615, 18446744073709551616
UINT64_OVER_STORED = "1.8446744073709552e+19"  # +2 bytes


def v1_bundle_as_exported(title: str, question: dict) -> bytes:
    """A version 1 bundle byte for byte as the exporter would write these values, so its
    length is exactly the size import measures before storage."""
    fields = ("type", "grading_type", "prompt", "config", "answer_data",
              "time_limit_seconds", "points_value")
    bundle = {
        "format": "buzzer/game",
        "version": 1,
        "game": {"title": title, "description": "", "max_players": 150},
        "questions": [{field: question[field] for field in fields}],
    }
    return json.dumps(bundle, indent=2, ensure_ascii=False).encode()


class StoredSizeGame:
    """A game held at an exact export headroom by a padding question, plus a small probe
    question whose numbers the tests change. Every refusal in these tests would have been
    accepted by a check on the values as sent, so each proves the stored form is measured;
    every exact fit lands on the limit to the byte."""

    def __init__(self, t8: T8Env):
        self.t8 = t8
        self.course_id, self.game_id = t8.setup_game()
        self.pad_q = t8.question_ok(self.game_id, padding_question(10))
        self.pad = 10
        # points_value 1 → exported "1.0"
        self.probe = t8.question_ok(self.game_id, tf(prompt="Probe"))

    def meter(self) -> int:
        return self.t8.meter(self.game_id)

    def set_headroom(self, headroom: int) -> None:
        length = self.pad + (EXPORT_LIMIT - headroom - self.meter())
        r = self.t8.admin(
            "PUT",
            f"/api/host/games/{self.game_id}/questions/{self.pad_q}",
            json={"config": {"pad": "x" * length}},
        )
        assert r.status_code == 200, r.text
        self.pad = length
        # Question updates commit after responding.
        wait_until(
            lambda: self.meter() == EXPORT_LIMIT - headroom,
            timeout=30,
            what=f"headroom {headroom}",
        )

    def put_probe(self, **fields) -> httpx.Response:
        """Update the probe. An accepted update commits only after its response, and
        set_headroom sizes the padding from a meter read, so this returns only once the probe
        reads back as the response stored it: the meter then includes the change. (Without
        this, a meter read could miss the change and size the padding that many bytes too
        long.)"""
        r = self.t8.admin(
            "PUT", f"/api/host/games/{self.game_id}/questions/{self.probe}", json=fields
        )
        if r.status_code == 200:
            wait_until(lambda: self.probe_question() == r.json(), what="the probe's update")
        return r

    def probe_question(self) -> dict:
        return self.t8.question(self.game_id, self.probe)

    def refused(self, r: httpx.Response, meter: int) -> None:
        assert r.status_code == 409, r.text
        assert f"over the export limit of {EXPORT_LIMIT:,} bytes" in r.json()["message"]
        assert self.meter() == meter  # rolled back: nothing changed


def test_question_update_measured_as_stored_float(t8):
    """"1.0" → "1e-40" grows 2 bytes as sent but 8 as stored."""
    g = StoredSizeGame(t8)
    g.set_headroom(7)
    g.refused(g.put_probe(points_value=float(FLOAT_SENT)), EXPORT_LIMIT - 7)
    assert g.probe_question()["points_value"] == 1.0

    g.set_headroom(8)
    r = g.put_probe(points_value=float(FLOAT_SENT))
    assert r.status_code == 200, r.text
    assert json.dumps(r.json()["points_value"]) == FLOAT_STORED  # the response is as stored
    wait_until(lambda: g.meter() == EXPORT_LIMIT, what="the exact fit")
    bundle = t8.export(g.game_id)
    assert len(bundle) == EXPORT_LIMIT
    assert f'"points_value": {FLOAT_STORED}\n'.encode() in bundle

    # Shrinking at the limit is still allowed.
    assert g.put_probe(points_value=1.0).status_code == 200
    wait_until(lambda: g.meter() == EXPORT_LIMIT - 8, what="the shrink")


def test_question_update_measured_as_stored_json(t8):
    """A same-length integer past 64 bits measures as no growth as sent, and grows 2 bytes
    as stored."""
    g = StoredSizeGame(t8)
    g.set_headroom(1_000)
    assert g.put_probe(config={"n": UINT64_MAX}).status_code == 200
    g.set_headroom(1)
    g.refused(g.put_probe(config={"n": UINT64_OVER}), EXPORT_LIMIT - 1)
    assert g.probe_question()["config"] == {"n": UINT64_MAX}

    g.set_headroom(2)
    assert g.put_probe(config={"n": UINT64_OVER}).status_code == 200
    wait_until(lambda: g.meter() == EXPORT_LIMIT, what="the exact fit")
    bundle = t8.export(g.game_id)
    assert len(bundle) == EXPORT_LIMIT
    assert f'"n": {UINT64_OVER_STORED}\n'.encode() in bundle


def test_question_create_measured_as_stored(t8):
    """The new question's stored growth is measured first; one byte short of it (6 bytes more
    than it grows as sent) is refused, and exactly it is accepted."""
    g = StoredSizeGame(t8)
    new_q = tf(prompt="New")
    new_q["points_value"] = float(FLOAT_SENT)
    g.set_headroom(10_000)
    r = t8.create_question(g.game_id, new_q)
    assert r.status_code == 201, r.text
    assert json.dumps(r.json()["points_value"]) == FLOAT_STORED
    growth = g.meter() - (EXPORT_LIMIT - 10_000)  # create commits before responding
    r = t8.admin("DELETE", f"/api/host/games/{g.game_id}/questions/{r.json()['id']}")
    assert r.status_code == 204, r.text
    wait_until(lambda: g.meter() == EXPORT_LIMIT - 10_000, what="the delete")

    count = len(t8.questions(g.game_id))
    g.set_headroom(growth - 1)
    g.refused(t8.create_question(g.game_id, new_q), EXPORT_LIMIT - (growth - 1))
    assert len(t8.questions(g.game_id)) == count

    g.set_headroom(growth)
    assert t8.create_question(g.game_id, new_q).status_code == 201
    assert g.meter() == EXPORT_LIMIT


def test_import_measured_as_stored(t8):
    """A bundle that fits as sent, by 3 bytes, but not as stored → 409 with no game; one that
    fits exactly as stored → 201, exporting to exactly the limit."""
    course_id = t8.create_course()
    question = padding_question(0)
    question.update(time_limit_seconds=30, points_value=float(FLOAT_SENT))
    base = len(v1_bundle_as_exported("Stored", question))

    question["config"] = {"pad": "x" * (EXPORT_LIMIT - 3 - base)}
    over = v1_bundle_as_exported("Stored", question)
    assert len(over) == EXPORT_LIMIT - 3
    games_before = t8.game_count()
    r = t8.import_bundle(course_id, over)
    assert r.status_code == 409, r.text
    assert f"over the export limit of {EXPORT_LIMIT:,} bytes" in r.json()["message"]
    assert t8.game_count() == games_before

    question["config"] = {"pad": "x" * (EXPORT_LIMIT - 6 - base)}
    fits = v1_bundle_as_exported("Stored", question)
    r = t8.import_bundle(course_id, fits)
    assert r.status_code == 201, r.text
    imported = r.json()["game_id"]
    assert t8.meter(imported) == EXPORT_LIMIT
    exported = t8.export(imported)
    assert len(exported) == EXPORT_LIMIT
    assert exported == fits.replace(
        f'"points_value": {FLOAT_SENT}\n'.encode(),
        f'"points_value": {FLOAT_STORED}\n'.encode(),
    )
