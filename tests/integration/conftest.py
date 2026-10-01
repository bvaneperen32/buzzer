"""
Session-level fixtures for the Buzzer integration test suite.

Stack lifecycle
---------------
The `docker_stack` fixture detects whether the stack is already running
(by calling /api/health).  If it is, nothing is started or stopped — the
dev stack is used as-is.  If it is not, `docker compose up -d --build` is
run from the repo root and `docker compose down` is called on teardown.

--fast flag
-----------
Passing --fast to pytest sets time_limit_seconds=2 on every question created
by the `game_setup` fixture.  This keeps timer-expiry edge-case tests quick.
Without --fast those scenarios are skipped automatically.

Test isolation
--------------
Every test gets a freshly-created game (course + game + questions) and a new
room, so tests are independent and can run in any order.

Test data
---------
Every record a test creates through httpx — a course, game, room session, guest
or local user, whichever helper made it — is recorded by exact id in an
ownership ledger (tests/ledger.py) and deleted when the test ends, whether it
passed or failed; records a module- or session-scoped fixture creates, when the
session ends. A killed run leaves its ledgers for `python tests/cleanup.py`.
Nothing the run didn't create is deleted (see "Test data" below).
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import time
import logging
import pathlib
import uuid
import warnings
from typing import Generator

import httpx
import pytest

from ..ledger import Ledger, default_db_connect, ledger_dir, teardown, token_subject

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Repository root (two levels up from this file)
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# pytest options
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--base-url",
        default="http://localhost:8000",
        help="Backend base URL (default: http://localhost:8000)",
    )
    parser.addoption(
        "--fast",
        action="store_true",
        default=False,
        help="Set time_limit_seconds=2 on all questions (enables timer-expiry tests)",
    )
    parser.addoption(
        "--nginx-url",
        default=None,
        help="nginx base URL, e.g. http://localhost:8080 (enables test_t8_nginx_limits.py)",
    )
    parser.addoption(
        "--image-load",
        action="store_true",
        default=False,
        help="Run the opt-in T8 image load and burst checks (test_t8_image_load.py)",
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def read_repo_env() -> dict[str, str]:
    """KEY=VALUE pairs from the repo's .env (empty when it doesn't exist)."""
    env_path = _REPO_ROOT / ".env"
    env_vars: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env_vars[k.strip()] = v.strip()
    return env_vars


def wait_until(check, *, timeout: float = 5.0, what: str = "the write"):
    """Poll check() until it returns something truthy, and return that. Fails the test if
    the state never shows up. (Same helper as test_t4_access.py's: get_db commits many
    writes after responding, so a read right after a 2xx can still see the old state.)"""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out after {timeout}s waiting for {what}")
        time.sleep(0.1)


def db_settings() -> dict:
    """The stack's MySQL on the published port, as the backend's account (BUZZER_TEST_DB_*
    override each field). Used by the T8/T7 lock tests and the browser checks' cleanup."""
    env = read_repo_env()
    return {
        "host": os.environ.get("BUZZER_TEST_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("BUZZER_TEST_DB_PORT", "3306")),
        "database": os.environ.get("BUZZER_TEST_DB_NAME", "buzzer"),
        "user": os.environ.get("BUZZER_TEST_DB_USER", "app_user"),
        "password": os.environ.get(
            "BUZZER_TEST_DB_PASSWORD", env.get("MYSQL_PASSWORD", "")
        ),
    }


def redis_cli(*args: str, timeout: float = 20.0) -> str:
    """Run a redis-cli command against the stack's Redis, which publishes no port.

    Uses BUZZER_TEST_REDIS_CLI (default `docker compose exec -T redis redis-cli`) from the repo
    root. Raises RuntimeError if the command can't run, so callers can skip with the reason.
    """
    base = shlex.split(
        os.environ.get("BUZZER_TEST_REDIS_CLI", "docker compose exec -T redis redis-cli")
    )
    try:
        result = subprocess.run(
            [*base, *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"redis-cli could not run: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"redis-cli failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Simple config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def base_url(request) -> str:
    return request.config.getoption("--base-url").rstrip("/")


@pytest.fixture(scope="session")
def fast_mode(request) -> bool:
    return request.config.getoption("--fast")


# ---------------------------------------------------------------------------
# Docker stack lifecycle
# ---------------------------------------------------------------------------

def _is_healthy(url: str) -> bool:
    try:
        r = httpx.get(f"{url}/api/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def _wait_healthy(url: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_healthy(url):
            return
        time.sleep(2)
    raise RuntimeError(
        f"Stack at {url} did not become healthy within {timeout}s. "
        "Check `docker compose logs backend`."
    )


@pytest.fixture(scope="session")
def docker_stack(base_url: str):
    """
    Ensure the Docker stack is running for the test session.
    If it was already running before the session, it is left running afterwards.
    """
    already_running = _is_healthy(base_url)

    if not already_running:
        logger.info("Stack not detected — running docker compose up --build")
        subprocess.run(
            ["docker", "compose", "up", "-d", "--build"],
            check=True,
            cwd=_REPO_ROOT,
        )
        logger.info("Waiting for stack to become healthy…")
        _wait_healthy(base_url)
        logger.info("Stack is healthy")

    yield

    if not already_running:
        logger.info("Tearing down stack started by this session")
        subprocess.run(
            ["docker", "compose", "down"],
            check=True,
            cwd=_REPO_ROOT,
        )


# ---------------------------------------------------------------------------
# Admin token (session-scoped — one login for the whole session)
# ---------------------------------------------------------------------------

def _admin_credentials() -> tuple[str, str]:
    env_vars = read_repo_env()
    username = env_vars.get("ADMIN_USERNAME") or os.environ.get("ADMIN_USERNAME", "admin")
    password = env_vars.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "changeme123")
    return username, password


@pytest.fixture(scope="session")
def admin_token(docker_stack, base_url: str) -> str:
    """
    Log in as the admin user and return an access token.
    Credentials are read from the .env file (ADMIN_USERNAME / ADMIN_PASSWORD).
    """
    username, password = _admin_credentials()

    r = httpx.post(
        f"{base_url}/api/auth/login",
        json={"username": username, "password": password},
        timeout=10.0,
    )
    assert r.status_code == 200, (
        f"Admin login failed ({r.status_code}): {r.text}\n"
        f"Set ADMIN_USERNAME / ADMIN_PASSWORD in .env or environment."
    )
    token = r.json().get("access_token")
    assert token, "Login response missing access_token"
    logger.info("Admin login OK")
    return token


# ---------------------------------------------------------------------------
# Test data: every record a run creates is owned, and deleted after its test
# ---------------------------------------------------------------------------
#
# Creations are recorded from the responses themselves (httpx.Client.send and
# AsyncClient.send are wrapped for the session), so every helper — this file's,
# T4Env, T8Env, a test's own requests — is covered without being changed. Only
# a successful POST to a creating route is recorded, by the id it returned.
#
# Nothing the run didn't create is deleted. Pre-existing records never enter a
# ledger. Guests need more: /api/auth/guest with an existing guest's email hands
# back *that* guest, with the same response as a creation. So a guest is the
# run's only when its email came from mint_guest_email() (128 random bits,
# claimed in the ledger before the request); any other guest is recorded as
# uncertain, reported, and kept (tests/ledger.py).

class _Ownership:
    run: Ledger | None = None  # records made outside a test (module/session fixtures)
    test: Ledger | None = None  # records made by the running test and its fixtures


_OWNED = _Ownership()


def mint_guest_email(label: str = "guest") -> str:
    """The email for a guest the running test (or fixture) is about to create, claimed in its
    ledger first, so the suite can prove the guest is its own and delete it afterwards. Every
    guest a test creates should use one; a guest made with any other email is kept."""
    ledger = _OWNED.test or _OWNED.run
    if ledger is None:  # outside a pytest run: nothing can own it
        local = "".join(ch for ch in label.lower() if ch.isalnum()) or "guest"
        return f"{local}.{uuid.uuid4().hex}@example.com"
    return ledger.mint_guest_email(label)


def _record_creation(response: httpx.Response) -> None:
    ledger = _OWNED.test or _OWNED.run
    request = response.request
    if ledger is None or request.method != "POST" or response.status_code not in (200, 201):
        return
    path = request.url.path
    try:
        body = response.json()
    except ValueError:
        return
    if path == "/api/admin/courses":
        ledger.record("courses", id=body["id"], name=body["name"], semester=body["semester"])
    elif path in ("/api/admin/games", "/api/host/games"):
        ledger.record("games", id=body["id"], title=body["title"])
    elif path in ("/api/admin/games/import", "/api/host/games/import"):
        ledger.record("games", id=body["game_id"])  # identified by its creation time
    elif path == "/api/game/rooms":
        ledger.record("sessions", id=body["session_id"], room_code=body["room_code"])
    elif path == "/api/auth/guest":
        # Owned only if the email was minted by this run (the test's or the run's ledger):
        # otherwise the response may be an existing guest handed back.
        email = json.loads(request.content)["email"]
        owned = any(led is not None and led.is_minted(email) for led in (_OWNED.test, _OWNED.run))
        ledger.record("guests", id=token_subject(body["access_token"]), email=email, owned=owned)
    elif path == "/api/admin/users":
        ledger.record("users", id=body["id"], username=body["username"])


def _clean(ledger: Ledger, base_url: str) -> None:
    """Delete what the ledger owns. Anything kept stays in its file (for tests/cleanup.py)
    and is reported as a warning, not a test failure."""
    if ledger.is_empty():
        return
    username, password = _admin_credentials()
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        r = http.post("/api/auth/login", json={"username": username, "password": password})
        if r.status_code != 200:
            warnings.warn(f"Test data kept in {ledger.path}: admin login failed ({r.status_code})")
            return
        problems = teardown(ledger, http, r.json()["access_token"], db_connect=default_db_connect)
    if problems:
        warnings.warn(f"Test data kept or skipped ({ledger.path.name}): " + "; ".join(problems))


@pytest.fixture(scope="session", autouse=True)
def _own_created_records(base_url: str):
    run = Ledger.begin(ledger_dir(), "integration", base_url=base_url)
    _OWNED.run = run
    real_send, real_async_send = httpx.Client.send, httpx.AsyncClient.send

    def send(self, request, **kwargs):
        response = real_send(self, request, **kwargs)
        if not kwargs.get("stream"):
            _record_creation(response)
        return response

    async def async_send(self, request, **kwargs):
        response = await real_async_send(self, request, **kwargs)
        if not kwargs.get("stream"):
            _record_creation(response)
        return response

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx.Client, "send", send)
        patch.setattr(httpx.AsyncClient, "send", async_send)
        yield run
    _OWNED.run = None
    _clean(run, base_url)


@pytest.fixture(autouse=True)
def _own_test_records(_own_created_records: Ledger, base_url: str):
    """Set up before, and torn down after, every other fixture of the test."""
    run_id = _own_created_records.data["run_id"][:12]
    ledger = Ledger.begin(
        ledger_dir(), "integration", base_url=base_url, name=f"integration-{run_id}-test"
    )
    _OWNED.test = ledger
    yield ledger
    _OWNED.test = None
    _clean(ledger, base_url)


# ---------------------------------------------------------------------------
# Per-test game setup / teardown
# ---------------------------------------------------------------------------

def _admin_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def game_setup(docker_stack, base_url: str, admin_token: str, fast_mode: bool):
    """
    Creates a Course, Game, and Questions for one test scenario.

    Yields a dict:
        {
            "course_id": int,
            "game_id":   int,
            "question_ids": list[int],   # in question order
        }

    The run's ownership ledger deletes the course and game (with their rooms)
    after the test.
    """
    import uuid as _uuid
    tag = _uuid.uuid4().hex[:8]

    headers = _admin_headers(admin_token)

    # --- Course ---
    r = httpx.post(
        f"{base_url}/api/admin/courses",
        json={"name": f"Test Course {tag}", "semester": "Test"},
        headers=headers,
        timeout=10.0,
    )
    assert r.status_code == 201, f"Create course failed: {r.text}"
    course_id: int = r.json()["id"]

    # --- Game ---
    r = httpx.post(
        f"{base_url}/api/admin/games",
        json={
            "title": f"Test Game {tag}",
            "description": "Integration test game",
            "course_id": course_id,
        },
        headers=headers,
        timeout=10.0,
    )
    assert r.status_code == 201, f"Create game failed: {r.text}"
    game_id: int = r.json()["id"]

    yield {
        "course_id": course_id,
        "game_id": game_id,
        "fast_mode": fast_mode,
    }


def create_questions(
    base_url: str,
    admin_token: str,
    game_id: int,
    scenario,
    fast_mode: bool,
) -> list[int]:
    """
    POST each QuestionSpec to the admin API and return the assigned question IDs.
    When fast_mode is True, time_limit_seconds is overridden to 2.
    """
    headers = _admin_headers(admin_token)
    ids = []
    for q in scenario.questions:
        body = {
            "type": q.type,
            "grading_type": q.grading_type,
            "prompt": q.prompt,
            "config": q.config,
            "answer_data": q.answer_data,
            "points_value": q.points_value,
            "time_limit_seconds": 2 if fast_mode else q.time_limit_seconds,
        }
        r = httpx.post(
            f"{base_url}/api/admin/games/{game_id}/questions",
            json=body,
            headers=headers,
            timeout=10.0,
        )
        assert r.status_code == 201, f"Create question failed: {r.text}"
        ids.append(r.json()["id"])
    return ids


def create_room(base_url: str, admin_token: str, course_id: int, game_id: int) -> str:
    """Create a game room and return the 6-char room code."""
    r = httpx.post(
        f"{base_url}/api/game/rooms",
        json={"course_id": course_id, "game_id": game_id},
        headers=_admin_headers(admin_token),
        timeout=10.0,
    )
    assert r.status_code == 201, f"Create room failed: {r.text}"
    return r.json()["room_code"]


def create_guest_tokens(
    base_url: str, room_code: str, n: int
) -> list[str]:
    """Register n guest players for a room and return their access tokens. Their emails are
    minted, so the suite deletes the guests after the test."""
    import uuid as _uuid
    tokens = []
    for i in range(n):
        uid = _uuid.uuid4().hex[:6]
        r = httpx.post(
            f"{base_url}/api/auth/guest",
            json={
                "display_name": f"TestPlayer{i}_{uid}",
                "email": mint_guest_email(f"testplayer{i}"),
                "room_code": room_code,
            },
            timeout=10.0,
        )
        assert r.status_code == 200, f"Guest token {i} failed: {r.text}"
        tokens.append(r.json()["access_token"])
    return tokens
