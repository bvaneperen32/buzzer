"""
T4 access-control integration tests (docs/plans/t4-ui-restructuring.md §5.11),
followed by the coverage tests B1a–B7d (docs/plans/t4-test-coverage.md §5).

Covers the host endpoints (/api/host/*), course-specific games and the game
rule, grant side effects, session downloads and deletes, the socket HOST join,
guest refusal, and /api/auth/me. Runs against the compose dev stack, where
APP_ENV=development disables login rate limiting:

    cd tests/integration && pytest test_t4_access.py -v

Each test creates its own users, courses, games, and rooms through the API and
removes them afterwards (see the `t4` fixture). Courses have no delete endpoint,
so, as in conftest's game_setup, they're left behind as inert records.

Commit timing: the backend's get_db dependency commits *after* the response has
been sent, so for endpoints that rely on it (grants, revokes, PUT, DELETE,
reorder, roster PATCH, room creation) a write can land a moment after its 2xx.
Tests therefore poll until a write is visible (wait_until) before building on it
or asserting on it, instead of reading it back once.
"""
from __future__ import annotations

import json
import pathlib
import time
import uuid

import httpx
import pytest

from .conftest import create_guest_tokens, create_questions
from .engine.orchestrator import run_game
from .engine.socket_client import TestSocketClient as SocketClient
from .scenarios.all_question_types import SINGLE_PLAYER

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_PASSWORD = "t4-integration-pass"
_SOCKET_TIMEOUT = 10.0

GUESTS_NOT_ALLOWED = "This game isn't open to guests — sign in with your school account"
HOST_ACCESS_LOST = "You no longer have HOST access to this course"
NOT_SESSION_HOST = "Not authorised as host for this session"
NO_GAME_ACCESS = "You do not have access to this game"
ACCOUNT_REQUIRED = "Authenticated account required"

# Request bodies shared by the coverage tests. Never mutate these; build variants
# with {**_TF_QUESTION, "prompt": ...} and {**_BASE_BUNDLE, "questions": [...]}.
_TF_QUESTION = {
    "type": "true_false",
    "grading_type": "COMPLETENESS",
    "prompt": "True?",
    "points_value": 1,
}
_BASE_BUNDLE = {
    "format": "buzzer/game",
    "version": 1,
    "game": {"title": "Import Check"},
    "questions": [],
}
# Below MySQL's signed INT maximum, and never a real id in a dev database.
_NO_SUCH_ID = 2_000_000_000


def wait_until(check, *, timeout: float = 5.0, what: str = "the write"):
    """Poll check() until it returns something truthy, and return that. Fails the
    test if the state never shows up."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out after {timeout}s waiting for {what}")
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Per-test environment: API helpers plus cleanup of everything a test creates
# ---------------------------------------------------------------------------


class T4Env:
    def __init__(self, base_url: str, admin_token: str, fast_mode: bool):
        self.base_url = base_url
        self.admin_token = admin_token
        self.fast_mode = fast_mode
        self.tag = uuid.uuid4().hex[:8]
        self.http = httpx.Client(base_url=base_url, timeout=15.0)
        self.session_ids: list[str] = []
        self.game_ids: list[int] = []
        self.user_ids: list[str] = []

    # -- requests ------------------------------------------------------------

    def req(
        self, method: str, path: str, token: str | None = None, **kwargs
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.http.request(method, path, headers=headers, **kwargs)

    def admin(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self.req(method, path, self.admin_token, **kwargs)

    # -- users, courses, grants ----------------------------------------------

    def create_user(self, name: str) -> tuple[str, str]:
        """A local USER account, logged in. Returns (user_id, token)."""
        username = f"t4-{name}-{self.tag}"
        r = self.admin(
            "POST",
            "/api/admin/users",
            json={
                "username": username,
                "display_name": f"T4 {name}",
                "password": _PASSWORD,
            },
        )
        assert r.status_code == 201, f"Create user failed: {r.text}"
        user_id = r.json()["id"]
        self.user_ids.append(user_id)
        r = self.req(
            "POST", "/api/auth/login", json={"username": username, "password": _PASSWORD}
        )
        assert r.status_code == 200, f"Login failed: {r.text}"
        return user_id, r.json()["access_token"]

    def create_course(self, label: str) -> int:
        r = self.admin(
            "POST",
            "/api/admin/courses",
            json={"name": f"T4 {label} {self.tag}", "semester": "T4 Test"},
        )
        assert r.status_code == 201, f"Create course failed: {r.text}"
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
        assert r.status_code == 204, f"Course grant failed: {r.text}"
        wait_until(
            lambda: {"course_id": course_id, "role": role}
            in self.user_detail(user_id)["course_access"],
            what=f"{role} on course {course_id}",
        )

    def revoke_course(self, user_id: str, course_id: int) -> None:
        r = self.admin("DELETE", f"/api/admin/users/{user_id}/course-access/{course_id}")
        assert r.status_code == 204, f"Course revoke failed: {r.text}"
        wait_until(
            lambda: all(
                ca["course_id"] != course_id
                for ca in self.user_detail(user_id)["course_access"]
            ),
            what=f"revoke of course {course_id}",
        )

    def grant_game(self, user_id: str, game_id: int) -> None:
        r = self.admin(
            "POST", f"/api/admin/users/{user_id}/game-access", json={"game_id": game_id}
        )
        assert r.status_code == 204, f"Game grant failed: {r.text}"
        wait_until(
            lambda: game_id in self.user_detail(user_id)["game_access"],
            what=f"grant on game {game_id}",
        )

    def make_host(
        self, name: str, course_id: int, game_id: int | None = None
    ) -> tuple[str, str]:
        """A USER with HOST on course_id, and a grant on game_id if one is given."""
        user_id, token = self.create_user(name)
        self.grant_course(user_id, course_id, "HOST")
        if game_id is not None:
            self.grant_game(user_id, game_id)
        return user_id, token

    # -- games, questions, rooms ---------------------------------------------

    def create_game(
        self, course_id: int, token: str | None = None, *, title: str | None = None
    ) -> int:
        """Through /api/host/games when a host token is given, else the admin API."""
        path = "/api/host/games" if token else "/api/admin/games"
        r = self.req(
            "POST",
            path,
            token or self.admin_token,
            json={"title": title or f"T4 Game {self.tag}", "course_id": course_id},
        )
        assert r.status_code == 201, f"Create game failed: {r.text}"
        game_id = r.json()["id"]
        self.game_ids.append(game_id)
        return game_id

    def upload_game(
        self, token: str | None, raw: bytes, course_id: int | None, *, router: str = "host"
    ) -> httpx.Response:
        """POST raw as a bundle to /api/{router}/games/import. A created game is
        queued for cleanup; the response is returned unasserted."""
        r = self.req(
            "POST",
            f"/api/{router}/games/import",
            token,
            files={"file": ("game.json", raw, "application/json")},
            data={"course_id": str(course_id)} if course_id is not None else None,
        )
        if r.status_code == 201:
            self.game_ids.append(r.json()["game_id"])
        return r

    def questions(self, game_id: int, token: str | None = None) -> list[dict]:
        r = self.req(
            "GET", f"/api/host/games/{game_id}/questions", token or self.admin_token
        )
        assert r.status_code == 200, r.text
        return r.json()

    def question(self, game_id: int, question_id: int) -> dict | None:
        return next(
            (q for q in self.questions(game_id) if q["id"] == question_id), None
        )

    def create_room(
        self, token: str, course_id: int, game_id: int, **extra
    ) -> tuple[str, str]:
        """Returns (room_code, session_id) once the room is visible in MySQL."""
        r = self.req(
            "POST",
            "/api/game/rooms",
            token,
            json={"course_id": course_id, "game_id": game_id, **extra},
        )
        assert r.status_code == 201, f"Create room failed: {r.text}"
        room_code, session_id = r.json()["room_code"], r.json()["session_id"]
        self.session_ids.append(session_id)
        wait_until(
            lambda: self.admin("GET", f"/api/game/rooms/{room_code}").status_code
            == 200,
            what=f"room {room_code}",
        )
        return room_code, session_id

    async def play_to_completion(
        self, host_token: str, course_id: int, game_id: int
    ) -> tuple[str, list[int]]:
        """Run SINGLE_PLAYER to the end with host_token's user as the session host.
        Returns (session_id, question_ids)."""
        question_ids = create_questions(
            self.base_url, self.admin_token, game_id, SINGLE_PLAYER, self.fast_mode
        )
        room_code, session_id = self.create_room(host_token, course_id, game_id)
        player_tokens = create_guest_tokens(
            self.base_url, room_code, len(SINGLE_PLAYER.player_scripts)
        )
        payloads = await run_game(
            base_url=self.base_url,
            host_token=host_token,
            player_tokens=player_tokens,
            room_code=room_code,
            scenario=SINGLE_PLAYER,
        )
        assert len(payloads) == len(SINGLE_PLAYER.player_scripts)
        wait_until(
            lambda: self.admin("GET", f"/api/game/rooms/{room_code}").json()["status"]
            == "COMPLETED",
            what=f"room {room_code} to complete",
        )
        return session_id, question_ids

    # -- cleanup -------------------------------------------------------------

    def cleanup(self) -> None:
        # Admin token throughout: a test may have revoked its host's HOST.
        # 1. Every room, played or not. This also clears the room's Redis keys, so
        #    leftover rooms can't count toward the concurrent-room cap.
        for session_id in self.session_ids:
            self.admin("DELETE", f"/api/game/sessions/{session_id}")
        # 2. Games.
        for game_id in self.game_ids:
            self.admin("DELETE", f"/api/admin/games/{game_id}")
        for game_id in self.game_ids:
            wait_until(
                lambda: self.admin("GET", f"/api/admin/games/{game_id}").status_code
                == 404,
                what=f"delete of game {game_id}",
            )
        # 3. Users last: sessions reference their host user.
        for user_id in self.user_ids:
            self.admin("DELETE", f"/api/admin/users/{user_id}")
        self.http.close()


@pytest.fixture()
def t4(docker_stack, base_url: str, admin_token: str, fast_mode: bool):
    env = T4Env(base_url, admin_token, fast_mode)
    yield env
    env.cleanup()


# ---------------------------------------------------------------------------
# Tests (numbered as in §5.11)
# ---------------------------------------------------------------------------


def test_me_reports_the_current_role(t4):
    """1 — /api/auth/me returns the freshly loaded user; 401 without a token."""
    r = t4.admin("GET", "/api/auth/me")
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "ADMIN"

    course_id = t4.create_course("C")
    host_id, host = t4.make_host("me", course_id)
    r = t4.req("GET", "/api/auth/me", host)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["role"] == "USER"
    assert me["id"] == host_id
    assert me["username"] == f"t4-me-{t4.tag}"
    assert me["display_name"] == "T4 me"

    assert t4.req("GET", "/api/auth/me").status_code == 401


def test_host_creates_a_game_in_a_course_they_host(t4):
    """2 — create through /api/host/games; a course is required; my-courses lists
    HOST courses only (all courses for an admin)."""
    course_id = t4.create_course("C")
    player_course = t4.create_course("P")
    other_course = t4.create_course("X")
    host_id, host = t4.make_host("creator", course_id)
    t4.grant_course(host_id, player_course, "PLAYER")

    game_id = t4.create_game(course_id, host)
    r = t4.req("GET", f"/api/host/games/{game_id}", host)
    assert r.status_code == 200, r.text
    assert r.json()["course_id"] == course_id

    # The creator was granted the game automatically.
    mine = t4.req("GET", "/api/game/my-games", host).json()
    assert [g["id"] for g in mine] == [game_id]
    assert mine[0]["course_id"] == course_id
    assert mine[0]["course_name"] == f"T4 C {t4.tag}"
    assert mine[0]["course_semester"] == "T4 Test"

    r = t4.req(
        "POST", "/api/host/games", host, json={"title": "No", "course_id": other_course}
    )
    assert r.status_code == 403, r.text
    assert r.json()["message"] == "You do not have HOST access to this course"

    r = t4.admin("POST", "/api/admin/games", json={"title": "No course"})
    assert r.status_code == 422, r.text

    r = t4.req(
        "POST",
        "/api/host/games",
        host,
        json={"title": "Extra", "course_id": course_id, "surprise": True},
    )
    assert r.status_code == 422, r.text

    host_courses = {c["id"] for c in t4.req("GET", "/api/game/my-courses", host).json()}
    assert course_id in host_courses
    assert player_course not in host_courses
    admin_courses = {c["id"] for c in t4.admin("GET", "/api/game/my-courses").json()}
    assert {course_id, player_course} <= admin_courses


def test_game_rule_needs_a_grant_as_well_as_host(t4):
    """3 — HOST on the game's course isn't enough without a game grant."""
    course_id = t4.create_course("C")
    game_id = t4.create_game(course_id)
    cohost_id, cohost = t4.make_host("cohost", course_id)

    r = t4.req("GET", f"/api/host/games/{game_id}", cohost)
    assert r.status_code == 403, r.text
    assert r.json()["message"] == "You do not have access to this game"
    r = t4.req(
        "POST", "/api/game/rooms", cohost, json={"course_id": course_id, "game_id": game_id}
    )
    assert r.status_code == 403, r.text

    t4.grant_game(cohost_id, game_id)
    r = t4.req("GET", f"/api/host/games/{game_id}", cohost)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == game_id
    t4.create_room(cohost, course_id, game_id)


def test_game_grant_requires_host_on_the_games_course(t4):
    """4 — the admin grant endpoint refuses a grant that could never take effect."""
    course_id = t4.create_course("C")
    game_id = t4.create_game(course_id)
    user_id, _ = t4.create_user("nohost")

    r = t4.admin(
        "POST", f"/api/admin/users/{user_id}/game-access", json={"game_id": game_id}
    )
    assert r.status_code == 409, r.text
    assert (
        r.json()["message"]
        == "User must be a HOST of this game's course before being granted the game"
    )

    # A PLAYER grant on the course isn't a HOST grant.
    t4.grant_course(user_id, course_id, "PLAYER")
    r = t4.admin(
        "POST", f"/api/admin/users/{user_id}/game-access", json={"game_id": game_id}
    )
    assert r.status_code == 409, r.text
    assert t4.user_detail(user_id)["game_access"] == []


def test_room_course_must_match_the_games_course(t4):
    """5 — a game runs only in its own course, for admins too."""
    c1, c2 = t4.create_course("C1"), t4.create_course("C2")
    game_id = t4.create_game(c1)
    host_id, host = t4.make_host("roomer", c1, game_id)
    t4.grant_course(host_id, c2, "HOST")

    for token in (host, t4.admin_token):
        r = t4.req(
            "POST", "/api/game/rooms", token, json={"course_id": c2, "game_id": game_id}
        )
        assert r.status_code == 409, r.text
        assert r.json()["message"] == "This game belongs to a different course"
    assert t4.req("GET", "/api/game/my-active-sessions", host).json() == []


def test_losing_host_removes_only_that_courses_game_grants(t4):
    """6 — a HOST revoke and a HOST->PLAYER downgrade both drop the user's grants
    for that course's games, and nothing else."""
    c1, c2 = t4.create_course("C1"), t4.create_course("C2")
    g1, g2 = t4.create_game(c1), t4.create_game(c2)
    user_id, _ = t4.create_user("twohats")
    for course_id, game_id in ((c1, g1), (c2, g2)):
        t4.grant_course(user_id, course_id, "HOST")
        t4.grant_game(user_id, game_id)

    # The revoke and its grant cleanup commit together, so once the revoke is
    # visible the grants are final.
    t4.revoke_course(user_id, c1)
    game_access = t4.user_detail(user_id)["game_access"]
    assert g1 not in game_access
    assert g2 in game_access

    # Same again through a downgrade to PLAYER.
    t4.grant_course(user_id, c1, "HOST")
    t4.grant_game(user_id, g1)
    t4.grant_course(user_id, c1, "PLAYER")
    game_access = t4.user_detail(user_id)["game_access"]
    assert g1 not in game_access
    assert g2 in game_access


def test_moving_a_game_keeps_only_grants_of_new_course_hosts(t4):
    """7 — hosts can't change a game's course; an admin move removes the grants of
    users who don't HOST the new course and keeps the rest."""
    c, c2 = t4.create_course("C"), t4.create_course("C2")
    game_id = t4.create_game(c)
    a_id, a = t4.make_host("hosta", c, game_id)
    b_id, b = t4.make_host("hostb", c, game_id)
    t4.grant_course(b_id, c2, "HOST")

    r = t4.req("PUT", f"/api/host/games/{game_id}", a, json={"course_id": c2})
    assert r.status_code == 422, r.text
    assert t4.admin("GET", f"/api/admin/games/{game_id}").json()["course_id"] == c

    r = t4.admin("PUT", f"/api/admin/games/{game_id}", json={"course_id": c2})
    assert r.status_code == 200, r.text
    assert r.json()["course_id"] == c2
    wait_until(
        lambda: game_id not in t4.user_detail(a_id)["game_access"],
        what="host A's grant to be removed",
    )
    assert t4.req("GET", f"/api/host/games/{game_id}", a).status_code == 403

    assert game_id in t4.user_detail(b_id)["game_access"]
    r = t4.req("GET", f"/api/host/games/{game_id}", b)
    assert r.status_code == 200, r.text
    assert r.json()["course_id"] == c2


def test_host_question_crud_and_merged_validation(t4):
    """8 — create, update, reorder, and delete through /api/host; an update that
    would leave the question invalid is a 422 on both routers and changes nothing."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("editor", course_id)
    game_id = t4.create_game(course_id, host)
    base = f"/api/host/games/{game_id}/questions"
    mc = {
        "type": "multiple_choice",
        "grading_type": "ACCURACY",
        "prompt": "First?",
        "config": {"options": ["a", "b"]},
        "answer_data": {"answer_points": [1, 0]},
        "points_value": 1,
    }

    r = t4.req("POST", base, host, json=mc)
    assert r.status_code == 201, r.text
    q1 = r.json()["id"]
    r = t4.req("POST", base, host, json={**mc, "prompt": "Second?"})
    assert r.status_code == 201, r.text
    q2 = r.json()["id"]
    assert [q["id"] for q in t4.questions(game_id, host)] == [q1, q2]

    r = t4.req(
        "PUT", f"{base}/{q1}", host, json={"prompt": "First, edited?", "time_limit_seconds": 45}
    )
    assert r.status_code == 200, r.text
    assert r.json()["prompt"] == "First, edited?"
    wait_until(
        lambda: t4.question(game_id, q1)["time_limit_seconds"] == 45,
        what="question update",
    )

    r = t4.req("POST", f"{base}/reorder", host, json={"order": [q2, q1]})
    assert r.status_code == 204, r.text
    wait_until(
        lambda: [q["id"] for q in t4.questions(game_id, host)] == [q2, q1],
        what="reorder",
    )

    # One option is structurally invalid for multiple_choice (POST would refuse it).
    invalid = {"config": {"options": ["only one"]}, "answer_data": {"answer_points": [1]}}
    before = t4.question(game_id, q1)
    for path, token in (
        (f"{base}/{q1}", host),
        (f"/api/admin/games/{game_id}/questions/{q1}", t4.admin_token),
    ):
        r = t4.req("PUT", path, token, json=invalid)
        assert r.status_code == 422, f"{path}: {r.text}"
        assert r.json()["error"] == "UNPROCESSABLE"
        assert "at least 2 items" in r.json()["message"]
    assert t4.question(game_id, q1) == before

    r = t4.req("DELETE", f"{base}/{q2}", host)
    assert r.status_code == 204, r.text
    wait_until(
        lambda: [q["id"] for q in t4.questions(game_id, host)] == [q1],
        what="question delete",
    )


def test_prompts_are_sanitized_on_every_write_path(t4):
    """9 — a <script> tag is stripped and <b> kept, on create and on update,
    through both the host and the admin endpoints."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("sanitizer", course_id)
    game_id = t4.create_game(course_id, host)
    dirty = "<script>x</script><b>y</b>"
    plain = {
        "type": "true_false",
        "grading_type": "COMPLETENESS",
        "prompt": "plain",
        "points_value": 1,
    }

    for router, token in (("host", host), ("admin", t4.admin_token)):
        base = f"/api/{router}/games/{game_id}/questions"

        r = t4.req("POST", base, token, json={**plain, "prompt": dirty})
        assert r.status_code == 201, r.text
        stored = t4.question(game_id, r.json()["id"])["prompt"]
        assert "<script>" not in stored and "<b>y</b>" in stored

        r = t4.req("POST", base, token, json=plain)
        assert r.status_code == 201, r.text
        qid = r.json()["id"]
        r = t4.req("PUT", f"{base}/{qid}", token, json={"prompt": dirty})
        assert r.status_code == 200, r.text

        def updated_prompt(qid: int = qid) -> str | None:
            prompt = t4.question(game_id, qid)["prompt"]
            return None if prompt == "plain" else prompt

        stored = wait_until(updated_prompt, what=f"prompt update via {router}")
        assert "<script>" not in stored and "<b>y</b>" in stored


async def test_answered_question_cannot_be_deleted(t4):
    """10 — deleting a question that has scores is a readable 409 on both routers,
    and the question survives."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("played", course_id)
    game_id = t4.create_game(course_id, host)
    _, question_ids = await t4.play_to_completion(host, course_id, game_id)
    qid = question_ids[0]

    for path, token in (
        (f"/api/host/games/{game_id}/questions/{qid}", host),
        (f"/api/admin/games/{game_id}/questions/{qid}", t4.admin_token),
    ):
        r = t4.req("DELETE", path, token)
        assert r.status_code == 409, f"{path}: {r.text}"
        assert "has been answered in 1 session(s)" in r.json()["message"]
    assert t4.question(game_id, qid) is not None


async def test_session_downloads_follow_the_download_rule(t4):
    """11 — the session's host downloads a COMPLETED session; bystanders and a
    revoked host get 403; a LOBBY session is a 409 for everyone, admins included,
    on the host router and the legacy game-router export alike."""
    course_id = t4.create_course("C")
    host_id, host = t4.make_host("downloader", course_id)
    game_id = t4.create_game(course_id, host)
    done_id, _ = await t4.play_to_completion(host, course_id, game_id)
    _, lobby_id = t4.create_room(host, course_id, game_id)
    _, bystander = t4.make_host("bystander", course_id)

    report = f"/api/host/sessions/{done_id}/report"
    raw = f"/api/host/sessions/{done_id}/export?format=raw"
    canvas = f"/api/host/sessions/{done_id}/export?format=canvas"
    legacy = f"/api/game/sessions/{done_id}/export"

    r = t4.req("GET", report, host)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.lstrip().startswith("<!DOCTYPE html>")
    for path, first_column in ((raw, "Player"), (canvas, "Student"), (legacy, "Player")):
        r = t4.req("GET", path, host)
        assert r.status_code == 200, f"{path}: {r.text}"
        assert r.headers["content-type"].startswith("text/csv")
        assert r.text.splitlines()[0].split(",")[0] == first_column

    for path in (report, raw, legacy):
        r = t4.req("GET", path, bystander)
        assert r.status_code == 403, f"{path}: {r.text}"
        assert r.json()["message"] == "Only the session's host can download it"

    lobby_paths = (
        f"/api/host/sessions/{lobby_id}/report",
        f"/api/host/sessions/{lobby_id}/export",
        f"/api/game/sessions/{lobby_id}/export",
    )
    for token in (host, t4.admin_token):
        for path in lobby_paths:
            r = t4.req("GET", path, token)
            assert r.status_code == 409, f"{path}: {r.text}"
            assert (
                r.json()["message"]
                == "Downloads are available once the session has completed"
            )

    # The admin needs no HOST grant for a COMPLETED session.
    for path in (report, raw, canvas):
        assert t4.admin("GET", path).status_code == 200, path

    t4.revoke_course(host_id, course_id)
    for path in (report, raw, legacy):
        r = t4.req("GET", path, host)
        assert r.status_code == 403, f"{path}: {r.text}"


async def test_session_list_is_scoped_by_role(t4):
    """12 — a host lists only their COMPLETED sessions; a co-host of the same
    course sees none of them; an admin can filter by any status."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("lister", course_id)
    game_id = t4.create_game(course_id, host)
    done_id, _ = await t4.play_to_completion(host, course_id, game_id)
    _, lobby_id = t4.create_room(host, course_id, game_id)
    _, cohost = t4.make_host("colister", course_id)

    rows = t4.req("GET", "/api/host/sessions", host).json()
    assert [s["session_id"] for s in rows] == [done_id]
    assert rows[0]["status"] == "COMPLETED"
    assert rows[0]["course_id"] == course_id
    assert rows[0]["player_count"] == len(SINGLE_PLAYER.player_scripts)
    assert rows[0]["host_display_name"] == "T4 lister"
    # A host's ?status= is ignored rather than widening the list.
    rows = t4.req("GET", "/api/host/sessions?status=LOBBY", host).json()
    assert [s["session_id"] for s in rows] == [done_id]

    assert t4.req("GET", "/api/host/sessions", cohost).json() == []

    rows = t4.admin("GET", "/api/host/sessions?status=LOBBY").json()
    lobby = [s for s in rows if s["session_id"] == lobby_id]
    assert lobby, "admin's LOBBY filter should include the lobby session"
    assert all(s["status"] == "LOBBY" for s in rows)
    # A LOBBY session has no scores: the outer join must still count it, as 0.
    assert lobby[0]["player_count"] == 0
    assert lobby[0]["host_display_name"] == "T4 lister"


def test_host_import_requires_a_course_they_host(t4):
    """13 — import goes through create_game: the course is required, the creator is
    granted the game, and every question arrives."""
    course_id = t4.create_course("C")
    other_course = t4.create_course("X")
    _, host = t4.make_host("importer", course_id)
    raw = (_REPO_ROOT / "sample_games" / "mental_math.json").read_bytes()
    expected_questions = len(json.loads(raw)["questions"])

    def upload(path: str, token: str, form: dict | None = None) -> httpx.Response:
        return t4.req(
            "POST",
            path,
            token,
            files={"file": ("mental_math.json", raw, "application/json")},
            data=form or {},
        )

    r = upload("/api/host/games/import", host, {"course_id": str(course_id)})
    assert r.status_code == 201, r.text
    game_id = r.json()["game_id"]
    t4.game_ids.append(game_id)
    r = t4.req("GET", f"/api/host/games/{game_id}", host)
    assert r.status_code == 200, r.text
    assert r.json()["course_id"] == course_id
    assert len(t4.questions(game_id, host)) == expected_questions
    my_games = t4.req("GET", "/api/game/my-games", host).json()
    assert game_id in [g["id"] for g in my_games]

    assert upload("/api/host/games/import", host).status_code == 422
    r = upload("/api/host/games/import", host, {"course_id": str(other_course)})
    assert r.status_code == 403, r.text
    assert upload("/api/admin/games/import", t4.admin_token).status_code == 422


def test_roster_management_is_course_scoped(t4):
    """14 — a HOST can list, import, and deactivate their course's roster; nobody
    else can; and no roster entry can be edited through another course's URL."""
    c, d = t4.create_course("C"), t4.create_course("D")
    _, host = t4.make_host("rosterer", c)
    _, outsider = t4.create_user("outsider")
    player_id, player = t4.create_user("playeronly")
    t4.grant_course(player_id, c, "PLAYER")
    base = f"/api/host/courses/{c}/roster"
    row = {
        "netid": f"t4s{t4.tag}",
        "full_name": "T4 Student",
        "email": f"t4s{t4.tag}@example.com",
    }

    r = t4.req("POST", f"{base}/import", host, json={"rows": [row]})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1
    entries = t4.req("GET", base, host).json()
    assert [e["netid"] for e in entries] == [row["netid"]]
    assert entries[0]["is_active"] is True
    entry_id = entries[0]["id"]

    r = t4.req("PATCH", f"{base}/{entry_id}", host, json={"is_active": False})
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False
    wait_until(
        lambda: t4.req("GET", base, host).json()[0]["is_active"] is False,
        what="roster deactivation",
    )

    for token in (outsider, player):
        assert t4.req("GET", base, token).status_code == 403
        r = t4.req("POST", f"{base}/import", token, json={"rows": [row]})
        assert r.status_code == 403, r.text
        r = t4.req("PATCH", f"{base}/{entry_id}", token, json={"is_active": True})
        assert r.status_code == 403, r.text

    d_row = {
        "netid": f"t4d{t4.tag}",
        "full_name": "D Student",
        "email": f"t4d{t4.tag}@example.com",
    }
    r = t4.admin("POST", f"/api/admin/courses/{d}/roster/import", json={"rows": [d_row]})
    assert r.status_code == 200, r.text
    d_entry = t4.admin("GET", f"/api/admin/courses/{d}/roster").json()[0]

    r = t4.req(
        "PATCH", f"/api/host/courses/{c}/roster/{d_entry['id']}", host, json={"is_active": False}
    )
    assert r.status_code == 404, r.text
    assert r.json()["message"] == f"Roster entry {d_entry['id']} not found in course {c}"
    r = t4.admin(
        "PATCH", f"/api/admin/courses/{c}/roster/{d_entry['id']}", json={"is_active": False}
    )
    assert r.status_code == 404, r.text
    assert t4.admin("GET", f"/api/admin/courses/{d}/roster").json()[0]["is_active"] is True


async def test_rooms_can_refuse_guests(t4):
    """15 — allow_guests=false refuses /auth/guest, and the socket backstop refuses
    a guest who already holds a token; a default room still takes guests."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("guestkeeper", course_id)
    game_id = t4.create_game(course_id, host)
    open_code, _ = t4.create_room(host, course_id, game_id)
    closed_code, _ = t4.create_room(host, course_id, game_id, allow_guests=False)

    r = t4.req(
        "POST",
        "/api/auth/guest",
        json={
            "display_name": "Refused",
            "email": f"t4g{t4.tag}@example.com",
            "room_code": closed_code,
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["message"] == GUESTS_NOT_ALLOWED

    [guest] = create_guest_tokens(t4.base_url, open_code, 1)

    client = SocketClient(t4.base_url, guest, "guest")
    try:
        await client.connect()
        await client.emit("join_room", {"room_code": closed_code, "role": "PLAYER"})
        error = await client.wait_for("error", _SOCKET_TIMEOUT)
        assert error["message"] == GUESTS_NOT_ALLOWED

        await client.emit("join_room", {"room_code": open_code, "role": "PLAYER"})
        sync = await client.wait_for("sync_state", _SOCKET_TIMEOUT)
        assert sync["role"] == "PLAYER"
    finally:
        await client.disconnect()


def test_hosts_cannot_delete_games(t4):
    """16 — there is no host game delete; only the admin endpoint deletes games."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("nodelete", course_id)
    game_id = t4.create_game(course_id, host)

    r = t4.req("DELETE", f"/api/host/games/{game_id}", host)
    assert r.status_code == 405, r.text
    assert t4.req("GET", f"/api/host/games/{game_id}", host).status_code == 200


def test_session_delete_needs_the_live_session_host(t4):
    """17 — deleting a session needs the session's host *with* a live HOST grant;
    host_user_id alone isn't enough."""
    course_id = t4.create_course("C")
    host_id, host = t4.make_host("deleter", course_id)
    game_id = t4.create_game(course_id, host)
    room_code, session_id = t4.create_room(host, course_id, game_id)
    _, cohost = t4.make_host("codeleter", course_id)

    r = t4.req("DELETE", f"/api/game/sessions/{session_id}", cohost)
    assert r.status_code == 403, r.text
    assert r.json()["message"] == "Only the session host can delete this session"

    t4.revoke_course(host_id, course_id)
    r = t4.req("DELETE", f"/api/game/sessions/{session_id}", host)
    assert r.status_code == 403, r.text
    assert t4.admin("GET", f"/api/game/rooms/{room_code}").status_code == 200

    assert t4.admin("DELETE", f"/api/game/sessions/{session_id}").status_code == 204
    assert t4.admin("GET", f"/api/game/rooms/{room_code}").status_code == 404


async def test_socket_host_join_needs_a_live_host_grant(t4):
    """18 — once HOST is revoked, the recorded host can't take the room over the
    socket (JOIN_ROOM or REJOIN_ROOM) and Play stops offering it; a non-host is
    refused as before; the admin override still works."""
    course_id = t4.create_course("C")
    host_id, host = t4.make_host("sockethost", course_id)
    game_id = t4.create_game(course_id, host)
    room_code, _ = t4.create_room(host, course_id, game_id)
    _, stranger = t4.create_user("stranger")

    def active_rooms() -> list[str]:
        r = t4.req("GET", "/api/game/my-active-sessions", host)
        assert r.status_code == 200, r.text
        return [s["room_code"] for s in r.json()]

    assert room_code in active_rooms()
    t4.revoke_course(host_id, course_id)

    host_client = SocketClient(t4.base_url, host, "revoked-host")
    try:
        await host_client.connect()
        await host_client.emit("join_room", {"room_code": room_code, "role": "HOST"})
        error = await host_client.wait_for("error", _SOCKET_TIMEOUT)
        assert error["message"] == HOST_ACCESS_LOST
        # REJOIN_ROOM infers the role; it must refuse rather than seat a player.
        await host_client.emit("rejoin_room", {"room_code": room_code})
        error = await host_client.wait_for("error", _SOCKET_TIMEOUT)
        assert error["message"] == HOST_ACCESS_LOST
    finally:
        await host_client.disconnect()

    assert room_code not in active_rooms()

    stranger_client = SocketClient(t4.base_url, stranger, "stranger")
    try:
        await stranger_client.connect()
        await stranger_client.emit("join_room", {"room_code": room_code, "role": "HOST"})
        error = await stranger_client.wait_for("error", _SOCKET_TIMEOUT)
        assert error["message"] == NOT_SESSION_HOST
    finally:
        await stranger_client.disconnect()

    admin_client = SocketClient(t4.base_url, t4.admin_token, "admin-host")
    try:
        await admin_client.connect()
        await admin_client.emit("join_room", {"room_code": room_code, "role": "HOST"})
        sync = await admin_client.wait_for("sync_state", _SOCKET_TIMEOUT)
        assert sync["role"] == "HOST"
        assert sync["status"] == "LOBBY"
    finally:
        await admin_client.disconnect()


# ---------------------------------------------------------------------------
# Coverage tests (docs/plans/t4-test-coverage.md, numbered as in its §5)
# ---------------------------------------------------------------------------

# Every game-rule endpoint in host.py, as (method, path, httpx kwargs) for a game
# and one of its questions. Every body is valid: an invalid one is a 422 before
# the handler's access check runs, which would hide a missing check.
# Add a row whenever a game-rule endpoint is added to host.py.
_GAME_RULE_REQUESTS = {
    "get-game": lambda g, q: ("GET", f"/api/host/games/{g}", {}),
    "put-game": lambda g, q: (
        "PUT", f"/api/host/games/{g}", {"json": {"title": "Changed"}}
    ),
    "list-questions": lambda g, q: ("GET", f"/api/host/games/{g}/questions", {}),
    "create-question": lambda g, q: (
        "POST",
        f"/api/host/games/{g}/questions",
        {"json": {**_TF_QUESTION, "prompt": "Sweep"}},
    ),
    "update-question": lambda g, q: (
        "PUT", f"/api/host/games/{g}/questions/{q}", {"json": {"prompt": "Changed"}}
    ),
    "delete-question": lambda g, q: (
        "DELETE", f"/api/host/games/{g}/questions/{q}", {}
    ),
    "reorder-questions": lambda g, q: (
        "POST", f"/api/host/games/{g}/questions/reorder", {"json": {"order": [q]}}
    ),
    "export-game": lambda g, q: ("GET", f"/api/host/games/{g}/export", {}),
}


@pytest.mark.parametrize("case", list(_GAME_RULE_REQUESTS))
def test_every_host_game_endpoint_refuses_without_access(t4, case):
    """B1a — each game-rule endpoint refuses a co-host without a game grant (the
    game rule), a guest (require_user), and a request with no token; nothing is
    written."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("owner", course_id)
    game_id = t4.create_game(course_id, host)
    r = t4.req(
        "POST",
        f"/api/host/games/{game_id}/questions",
        host,
        json={**_TF_QUESTION, "prompt": "Original"},
    )
    assert r.status_code == 201, r.text
    question_id = r.json()["id"]
    _, cohost = t4.make_host("cohost", course_id)
    room_code, _ = t4.create_room(host, course_id, game_id)
    [guest] = create_guest_tokens(t4.base_url, room_code, 1)
    method, path, kwargs = _GAME_RULE_REQUESTS[case](game_id, question_id)

    r = t4.req(method, path, cohost, **kwargs)
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "FORBIDDEN"
    assert r.json()["message"] == NO_GAME_ACCESS

    r = t4.req(method, path, guest, **kwargs)
    assert r.status_code == 403, r.text
    assert r.json()["message"] == ACCOUNT_REQUIRED

    r = t4.req(method, path, **kwargs)
    assert r.status_code == 401, r.text
    assert r.json()["error"] == "UNAUTHORIZED"

    # Refused requests write nothing, so there's no commit to wait for.
    r = t4.admin("GET", f"/api/admin/games/{game_id}")
    assert r.status_code == 200, r.text
    assert r.json()["title"] == f"T4 Game {t4.tag}"
    questions = t4.questions(game_id)
    assert [q["id"] for q in questions] == [question_id]
    assert questions[0]["prompt"] == "Original"


# Every host.py endpoint _GAME_RULE_REQUESTS doesn't cover (the course rule, the
# download rule, and the session list), as a function that sends the request with
# a given token (None for no token). Every body is valid, as above.
# Add a row whenever an endpoint is added to host.py.
_OTHER_HOST_REQUESTS = {
    "create-game": lambda t4, token, c, sid: t4.req(
        "POST", "/api/host/games", token, json={"title": "Sweep", "course_id": c}
    ),
    "import-game": lambda t4, token, c, sid: t4.upload_game(
        token, json.dumps(_BASE_BUNDLE).encode(), c
    ),
    "list-roster": lambda t4, token, c, sid: t4.req(
        "GET", f"/api/host/courses/{c}/roster", token
    ),
    "import-roster": lambda t4, token, c, sid: t4.req(
        "POST",
        f"/api/host/courses/{c}/roster/import",
        token,
        json={
            "rows": [
                {
                    "netid": f"t4s{t4.tag}",
                    "full_name": "T4 Student",
                    "email": f"t4s{t4.tag}@example.com",
                }
            ]
        },
    ),
    "patch-roster": lambda t4, token, c, sid: t4.req(
        "PATCH",
        f"/api/host/courses/{c}/roster/{_NO_SUCH_ID}",
        token,
        json={"is_active": False},
    ),
    "list-sessions": lambda t4, token, c, sid: t4.req("GET", "/api/host/sessions", token),
    "session-report": lambda t4, token, c, sid: t4.req(
        "GET", f"/api/host/sessions/{sid}/report", token
    ),
    "session-export": lambda t4, token, c, sid: t4.req(
        "GET", f"/api/host/sessions/{sid}/export", token
    ),
}


@pytest.mark.parametrize("case", list(_OTHER_HOST_REQUESTS))
def test_every_other_host_endpoint_requires_an_account(t4, case):
    """B1c — each remaining host endpoint refuses a guest with require_user's
    message (not the course or download rule's, or an empty session list) and a
    request with no token; nothing is written."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("sweeper", course_id)
    game_id = t4.create_game(course_id, host)
    room_code, session_id = t4.create_room(host, course_id, game_id)
    [guest] = create_guest_tokens(t4.base_url, room_code, 1)
    send = _OTHER_HOST_REQUESTS[case]

    r = send(t4, guest, course_id, session_id)
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "FORBIDDEN"
    assert r.json()["message"] == ACCOUNT_REQUIRED

    r = send(t4, None, course_id, session_id)
    assert r.status_code == 401, r.text
    assert r.json()["error"] == "UNAUTHORIZED"

    r = t4.admin("GET", "/api/game/my-games")
    assert r.status_code == 200, r.text
    assert [g["id"] for g in r.json() if g["course_id"] == course_id] == [game_id]
    r = t4.admin("GET", f"/api/admin/courses/{course_id}/roster")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_game_rule_hides_whether_a_game_exists(t4):
    """B1b — a non-admin gets the same 403 for a game that doesn't exist as for
    one they can't use; an admin gets the 404."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("prober", course_id)

    r = t4.req("GET", f"/api/host/games/{_NO_SUCH_ID}", host)
    assert r.status_code == 403, r.text
    assert r.json()["message"] == NO_GAME_ACCESS

    r = t4.admin("GET", f"/api/host/games/{_NO_SUCH_ID}")
    assert r.status_code == 404, r.text
    assert r.json()["message"] == f"Game {_NO_SUCH_ID} not found"


def test_questions_are_scoped_to_the_game_in_the_url(t4):
    """B2 — a host can't edit or delete another game's question through the URL of
    a game they can use."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("a", course_id)
    _, other = t4.make_host("b", course_id)
    game_a = t4.create_game(course_id, host)
    game_b = t4.create_game(course_id, other)
    r = t4.req(
        "POST",
        f"/api/host/games/{game_a}/questions",
        host,
        json={**_TF_QUESTION, "prompt": "A question"},
    )
    assert r.status_code == 201, r.text
    r = t4.req(
        "POST",
        f"/api/host/games/{game_b}/questions",
        other,
        json={**_TF_QUESTION, "prompt": "B question"},
    )
    assert r.status_code == 201, r.text
    q_b = r.json()["id"]

    # Premise: the host can't reach game B directly.
    r = t4.req("GET", f"/api/host/games/{game_b}/questions", host)
    assert r.status_code == 403, r.text

    hijack = f"/api/host/games/{game_a}/questions/{q_b}"
    for method, kwargs in (("PUT", {"json": {"prompt": "Hijacked"}}), ("DELETE", {})):
        r = t4.req(method, hijack, host, **kwargs)
        assert r.status_code == 404, f"{method}: {r.text}"
        assert r.json()["message"] == f"Question {q_b} not found in game {game_a}"

    questions = t4.questions(game_b, other)
    assert [q["id"] for q in questions] == [q_b]
    assert questions[0]["prompt"] == "B question"


@pytest.mark.parametrize(
    ("raw", "prefix"),
    [
        pytest.param(b"{nope", "Invalid JSON", id="not-json"),
        pytest.param(
            json.dumps({**_BASE_BUNDLE, "format": "other"}).encode(),
            "Unrecognised file format",
            id="wrong-format",
        ),
        pytest.param(
            json.dumps({**_BASE_BUNDLE, "version": 2}).encode(),
            "Unsupported version",
            id="wrong-version",
        ),
        pytest.param(
            json.dumps({**_BASE_BUNDLE, "game": {"title": ""}}).encode(),
            "Invalid game metadata",
            id="bad-game",
        ),
        pytest.param(
            json.dumps({**_BASE_BUNDLE, "questions": [{"type": "nope"}]}).encode(),
            "Question 1 invalid",
            id="bad-question",
        ),
    ],
)
def test_import_rejections_use_the_unprocessable_format(t4, raw, prefix):
    """B3a — a rejected import is a 422 UNPROCESSABLE with a readable message, and
    creates no game. Prefixes only: the §9.1 fix may improve the wording."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("importer", course_id)

    def my_game_ids() -> list[int]:
        r = t4.req("GET", "/api/game/my-games", host)
        assert r.status_code == 200, r.text
        return [g["id"] for g in r.json()]

    before = my_game_ids()
    r = t4.upload_game(host, raw, course_id)
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "UNPROCESSABLE"
    assert r.json()["message"].startswith(prefix), r.json()["message"]
    assert my_game_ids() == before


def test_imported_prompts_are_sanitized(t4):
    """B3b — import runs prompts through the same sanitizer as create and update."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("sanitizer", course_id)
    dirty = {**_TF_QUESTION, "prompt": "<script>x</script><b>y</b>"}
    raw = json.dumps({**_BASE_BUNDLE, "questions": [dirty]}).encode()

    r = t4.upload_game(host, raw, course_id)
    assert r.status_code == 201, r.text
    [question] = t4.questions(r.json()["game_id"], host)
    assert "<script>" not in question["prompt"] and "<b>y</b>" in question["prompt"]


def test_admin_import_puts_the_game_in_the_given_course(t4):
    """B3c — an admin import lands in the course sent with it, and the admin gets
    no game grant."""
    course_id = t4.create_course("C")
    questions = [{**_TF_QUESTION, "prompt": "One"}, {**_TF_QUESTION, "prompt": "Two"}]
    raw = json.dumps({**_BASE_BUNDLE, "questions": questions}).encode()

    r = t4.upload_game(t4.admin_token, raw, course_id, router="admin")
    assert r.status_code == 201, r.text
    game_id = r.json()["game_id"]
    r = t4.admin("GET", f"/api/admin/games/{game_id}")
    assert r.status_code == 200, r.text
    assert r.json()["course_id"] == course_id
    assert len(t4.questions(game_id)) == 2

    r = t4.admin("GET", "/api/auth/me")
    assert r.status_code == 200, r.text
    assert game_id not in t4.user_detail(r.json()["id"])["game_access"]


def test_host_edits_game_details(t4):
    """B4a — a host with the game edits its title, description, and max players
    through /api/host; the course stays put."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("editor", course_id)
    game_id = t4.create_game(course_id, host)
    edits = {"title": "Renamed", "description": "Edited", "max_players": 42}

    r = t4.req("PUT", f"/api/host/games/{game_id}", host, json=edits)
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in edits} == edits

    def edited_game() -> dict | None:
        r = t4.req("GET", f"/api/host/games/{game_id}", host)
        assert r.status_code == 200, r.text
        game = r.json()
        return game if {k: game[k] for k in edits} == edits else None

    game = wait_until(edited_game, what="game edit")
    assert game["course_id"] == course_id


def test_export_is_portable_and_round_trips(t4):
    """B4b — the export is a buzzer/game bundle with no ids or course, and importing
    it into another course reproduces the questions in order."""
    course_id, other_course = t4.create_course("C"), t4.create_course("C2")
    host_id, host = t4.make_host("exporter", course_id)
    t4.grant_course(host_id, other_course, "HOST")
    game_id = t4.create_game(course_id, host, title="Export Check")
    mc = {
        "type": "multiple_choice",
        "grading_type": "ACCURACY",
        "prompt": "First?",
        "config": {"options": ["a", "b"]},
        "answer_data": {"answer_points": [1, 0]},
        "points_value": 1,
    }
    for body in (mc, {**_TF_QUESTION, "prompt": "Second?"}):
        r = t4.req("POST", f"/api/host/games/{game_id}/questions", host, json=body)
        assert r.status_code == 201, r.text
    fields = {
        "type",
        "grading_type",
        "prompt",
        "config",
        "answer_data",
        "time_limit_seconds",
        "points_value",
    }

    export = t4.req("GET", f"/api/host/games/{game_id}/export", host)
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith("application/json")
    assert "attachment" in export.headers["content-disposition"]
    assert 'filename="Export Check.json"' in export.headers["content-disposition"]

    bundle = export.json()
    assert set(bundle) == {"format", "version", "game", "questions"}
    assert bundle["format"] == "buzzer/game"
    assert bundle["version"] == 1
    assert set(bundle["game"]) == {"title", "description", "max_players"}
    assert bundle["game"]["title"] == "Export Check"
    assert [q["prompt"] for q in bundle["questions"]] == ["First?", "Second?"]
    for q in bundle["questions"]:
        assert set(q) == fields

    r = t4.upload_game(host, export.content, other_course)
    assert r.status_code == 201, r.text
    copy_id = r.json()["game_id"]
    r = t4.req("GET", f"/api/host/games/{copy_id}", host)
    assert r.status_code == 200, r.text
    assert r.json()["course_id"] == other_course

    def portable(game: int) -> list[dict]:
        return [{f: q[f] for f in fields} for q in t4.questions(game, host)]

    assert portable(copy_id) == portable(game_id)


def test_explicit_nulls_leave_question_fields_unchanged(t4):
    """B5 — an explicit null in a question PUT means "leave unchanged" for every
    field, order_index included."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("nuller", course_id)
    game_id = t4.create_game(course_id, host)
    mc = {
        "type": "multiple_choice",
        "grading_type": "ACCURACY",
        "prompt": "First?",
        "config": {"options": ["a", "b"]},
        "answer_data": {"answer_points": [1, 0]},
        "points_value": 1,
    }
    r = t4.req("POST", f"/api/host/games/{game_id}/questions", host, json=mc)
    assert r.status_code == 201, r.text
    question_id = r.json()["id"]
    before = t4.question(game_id, question_id)

    r = t4.req(
        "PUT",
        f"/api/host/games/{game_id}/questions/{question_id}",
        host,
        json={
            "type": None,
            "grading_type": None,
            "config": None,
            "answer_data": None,
            "time_limit_seconds": None,
            "points_value": None,
            "order_index": None,
            "prompt": "Canary",
        },
    )
    assert r.status_code == 200, r.text

    # The PUT commits after responding: compare against the row once the canary
    # prompt shows the whole edit has landed.
    def canary_landed() -> dict | None:
        question = t4.question(game_id, question_id)
        return question if question["prompt"] == "Canary" else None

    after = wait_until(canary_landed, what="the canary prompt")
    assert {**after, "prompt": before["prompt"]} == before


def test_admin_course_field_cannot_be_cleared_or_dangle(t4):
    """B6 — an admin PUT with course_id null leaves the course alone, and one
    naming a course that doesn't exist is a 404 that changes nothing."""
    course_id = t4.create_course("C")
    game_id = t4.create_game(course_id)

    def stored_game() -> dict:
        r = t4.admin("GET", f"/api/admin/games/{game_id}")
        assert r.status_code == 200, r.text
        return r.json()

    r = t4.admin(
        "PUT", f"/api/admin/games/{game_id}", json={"course_id": None, "title": "Canary"}
    )
    assert r.status_code == 200, r.text
    # A cleared course would commit in the same transaction as the canary title.
    def canary_landed() -> dict | None:
        game = stored_game()
        return game if game["title"] == "Canary" else None

    game = wait_until(canary_landed, what="the canary title")
    assert game["course_id"] == course_id

    r = t4.admin("PUT", f"/api/admin/games/{game_id}", json={"course_id": _NO_SUCH_ID})
    assert r.status_code == 404, r.text
    assert r.json()["message"] == f"Course {_NO_SUCH_ID} not found"
    assert stored_game()["course_id"] == course_id


def test_me_reports_a_guest(t4):
    """B7a — /api/auth/me answers a guest token too (get_current_user, not
    require_user)."""
    course_id = t4.create_course("C")
    _, host = t4.make_host("guesthost", course_id)
    game_id = t4.create_game(course_id, host)
    room_code, _ = t4.create_room(host, course_id, game_id)

    r = t4.req(
        "POST",
        "/api/auth/guest",
        json={
            "display_name": "T4 Guest",
            "email": f"t4me{t4.tag}@example.com",
            "room_code": room_code,
        },
    )
    assert r.status_code == 200, r.text
    r = t4.req("GET", "/api/auth/me", r.json()["access_token"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "GUEST"
    assert r.json()["display_name"] == "T4 Guest"


def test_missing_session_is_404_for_download_and_delete(t4):
    """B7b — a session that doesn't exist is a 404 for hosts and admins alike: the
    existence check runs before the ownership check."""
    session_id = str(uuid.uuid4())
    course_id = t4.create_course("C")
    _, host = t4.make_host("nobody", course_id)

    for token in (host, t4.admin_token):
        for method, path in (
            ("GET", f"/api/host/sessions/{session_id}/report"),
            ("GET", f"/api/host/sessions/{session_id}/export"),
            ("GET", f"/api/game/sessions/{session_id}/export"),
            ("DELETE", f"/api/game/sessions/{session_id}"),
        ):
            r = t4.req(method, path, token)
            assert r.status_code == 404, f"{method} {path}: {r.text}"
            assert r.json()["error"] == "NOT_FOUND"
            assert r.json()["message"] == f"Session {session_id} not found"


def test_admin_my_games_carries_course_fields_in_course_order(t4):
    """B7c — an admin's my-games is sorted by course, then title, and each item
    carries its course's id, name, and semester."""
    course_a, course_b = t4.create_course("A"), t4.create_course("B")
    # Created in the opposite of the expected order, so insertion order can't pass.
    g1 = t4.create_game(course_b, title="Alpha")
    g2 = t4.create_game(course_a, title="Zulu")
    g3 = t4.create_game(course_a, title="Mike")

    r = t4.admin("GET", "/api/game/my-games")
    assert r.status_code == 200, r.text
    ours = [g for g in r.json() if g["id"] in (g1, g2, g3)]
    assert [g["id"] for g in ours] == [g3, g2, g1]
    for game, (course_id, label) in zip(
        ours, ((course_a, "A"), (course_a, "A"), (course_b, "B"))
    ):
        assert game["course_id"] == course_id
        assert game["course_name"] == f"T4 {label} {t4.tag}"
        assert game["course_semester"] == "T4 Test"


def test_admin_session_list_keeps_sessions_whose_host_was_deleted(t4):
    """B7d — deleting a host leaves their sessions in the admin list, with no host
    name (the host join is an outer join)."""
    course_id = t4.create_course("C")
    host_id, host = t4.make_host("leaver", course_id)
    game_id = t4.create_game(course_id, host)
    _, session_id = t4.create_room(host, course_id, game_id)

    r = t4.admin("DELETE", f"/api/admin/users/{host_id}")
    assert r.status_code == 204, r.text
    # A 204 alone proves nothing: the delete commits after the response.
    wait_until(
        lambda: t4.admin("GET", f"/api/admin/users/{host_id}").status_code == 404,
        what="delete of the host to commit",
    )

    def orphaned_row() -> dict | None:
        r = t4.admin("GET", "/api/host/sessions?status=LOBBY")
        assert r.status_code == 200, r.text
        return next(
            (
                s
                for s in r.json()
                if s["session_id"] == session_id and s["host_display_name"] is None
            ),
            None,
        )

    row = wait_until(orphaned_row, what="the orphaned session in the admin list")
    assert row["course_id"] == course_id
    assert row["game_id"] == game_id
