"""
A probe for the suite's test-data ownership (conftest.py, "Test data"). Not collected by a normal
run (its name doesn't start with `test_`): test_data_hygiene.py runs it as a pytest session of
its own, with BUZZER_TEST_LEDGERS pointing at a temporary directory, and checks afterwards that
everything it created is gone.

It creates records the way the existing tests do — the conftest helpers, the game_setup fixture,
plain httpx calls, a module-scoped fixture — writes each id to BUZZER_PROBE_OUT (JSON lines),
and then passes, fails, or kills its own process.
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid

import httpx
import pytest

from .conftest import create_guest_tokens, create_room

pytestmark = pytest.mark.skipif(
    not os.environ.get("BUZZER_PROBE_OUT"), reason="run only by test_data_hygiene.py"
)


def note(kind: str, **fields) -> None:
    with open(os.environ["BUZZER_PROBE_OUT"], "a") as out:
        out.write(json.dumps({"kind": kind, **fields}) + "\n")


def admin(base_url: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=15.0, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture(scope="module")
def module_game(docker_stack, base_url, admin_token):
    """Made outside any test: owned by the run, deleted when the session ends, so it must
    outlive every test of this module."""
    with admin(base_url, admin_token) as api:
        course = api.post("/api/admin/courses", json={"name": f"Probe module {uuid.uuid4().hex[:6]}",
                                                      "semester": "Probe"}).json()
        game = api.post("/api/admin/games", json={"title": f"Probe module {uuid.uuid4().hex[:6]}",
                                                  "course_id": course["id"]}).json()
    note("course", id=course["id"])
    note("game", id=game["id"])
    return game["id"]


def test_creates_then_passes(module_game, game_setup, base_url, admin_token):
    note("course", id=game_setup["course_id"])
    note("game", id=game_setup["game_id"])
    with admin(base_url, admin_token) as api:
        api.post(f"/api/admin/games/{game_setup['game_id']}/questions", json={
            "type": "true_false", "grading_type": "COMPLETENESS", "prompt": "Q", "points_value": 1})
        user = api.post("/api/admin/users", json={
            "username": f"probe-{uuid.uuid4().hex[:8]}", "display_name": "Probe", "password": "probe-pass-1"}).json()
    note("user", id=user["id"])
    code = create_room(base_url, admin_token, game_setup["course_id"], game_setup["game_id"])
    note("room", code=code)
    for token in create_guest_tokens(base_url, code, 2):
        note("guest", token=token)
    reused = os.environ.get("BUZZER_PROBE_REUSE_EMAIL")
    if reused:  # a guest created before this run: the server hands back that guest's token
        r = httpx.post(f"{base_url}/api/auth/guest",
                       json={"display_name": "Reused", "email": reused, "room_code": code})
        assert r.status_code == 200, r.text
        note("reused_guest", token=r.json()["access_token"])
    # Another actor creates a guest during this run, unseen by it (urllib, not the tracked
    # httpx), with an email the run didn't mint; the run then asks for the same email.
    actor_email = f"actor.{uuid.uuid4().hex}@example.com"
    body = json.dumps({"display_name": "Actor", "email": actor_email, "room_code": code}).encode()
    request = urllib.request.Request(f"{base_url}/api/auth/guest", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        note("actor_guest", token=json.loads(response.read())["access_token"])
    r = httpx.post(f"{base_url}/api/auth/guest",
                   json={"display_name": "Reused", "email": actor_email, "room_code": code})
    assert r.status_code == 200, r.text
    note("reused_guest", token=r.json()["access_token"])


def test_creates_then_fails(module_game, base_url, admin_token):
    with admin(base_url, admin_token) as api:
        course = api.post("/api/admin/courses", json={"name": f"Probe failing {uuid.uuid4().hex[:6]}",
                                                      "semester": "Probe"}).json()
    note("course", id=course["id"])
    assert False, "the probe fails on purpose"


def test_module_records_outlive_each_test(module_game, base_url, admin_token):
    with admin(base_url, admin_token) as api:
        assert api.get(f"/api/admin/games/{module_game}").status_code == 200


def test_killed(base_url, admin_token):
    if not os.environ.get("BUZZER_PROBE_KILL"):
        pytest.skip("only with BUZZER_PROBE_KILL")
    with admin(base_url, admin_token) as api:
        course = api.post("/api/admin/courses", json={"name": f"Probe killed {uuid.uuid4().hex[:6]}",
                                                      "semester": "Probe"}).json()
    note("course", id=course["id"])
    os._exit(3)  # no teardown of any kind
