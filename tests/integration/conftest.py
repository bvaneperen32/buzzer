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
room, so tests are independent and can run in any order.  Cleanup deletes the
game record; MySQL CASCADE removes questions and sessions automatically.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
import logging
import pathlib
from typing import Generator

import httpx
import pytest

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

@pytest.fixture(scope="session")
def admin_token(docker_stack, base_url: str) -> str:
    """
    Log in as the admin user and return an access token.
    Credentials are read from the .env file (ADMIN_USERNAME / ADMIN_PASSWORD).
    """
    env_path = _REPO_ROOT / ".env"
    env_vars: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env_vars[k.strip()] = v.strip()

    username = env_vars.get("ADMIN_USERNAME") or os.environ.get("ADMIN_USERNAME", "admin")
    password = env_vars.get("ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "changeme123")

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

    Deletes the Game on teardown (MySQL CASCADE removes everything else).
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

    # --- Teardown (CASCADE removes questions, sessions, scores) ---
    httpx.delete(
        f"{base_url}/api/admin/games/{game_id}",
        headers=headers,
        timeout=10.0,
    )
    # Courses have no cascade and no DELETE endpoint; leave them as inert records.


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
    """Register n guest players for a room and return their access tokens."""
    import uuid as _uuid
    tokens = []
    for i in range(n):
        uid = _uuid.uuid4().hex[:6]
        r = httpx.post(
            f"{base_url}/api/auth/guest",
            json={
                "display_name": f"TestPlayer{i}_{uid}",
                "email": f"testplayer{i}.{uid}@example.com",
                "room_code": room_code,
            },
            timeout=10.0,
        )
        assert r.status_code == 200, f"Guest token {i} failed: {r.text}"
        tokens.append(r.json()["access_token"])
    return tokens
