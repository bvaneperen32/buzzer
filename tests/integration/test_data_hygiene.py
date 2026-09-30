"""
Test-data hygiene (tests/ledger.py, tests/cleanup.py, conftest.py "Test data";
docs/plans/fix-live-session-test-cleanup.md §3.6, §3.11, §3.12), against the live stack.

The ledger itself:
- Teardown deletes exactly the records a ledger owns — sessions (and their Redis room), games,
  guest accounts, courses — and leaves look-alike records beside them untouched.
- Anything whose identity no longer matches the record, or that something else still uses, is
  kept and reported.

The browser checks:
- A script that fails still tears down.
- A run killed outright leaves a ledger that `tests/cleanup.py` tears down, except while that
  run's process still lives.
- A kept ledger (the phone checklist's) is never touched.
- `--report` is read-only: no record and no ledger file changes, even with an abandoned ledger
  that plain `cleanup.py` would tear down.

The integration suite (run as a pytest session of its own, `hygiene_probe.py`):
- Records made through the existing helpers are deleted after the test passes or fails.
- A module fixture's records outlive each test and go at the session's end.
- A killed session is cleaned by `tests/cleanup.py`.
- A guest the run only obtained a token for (an email it didn't mint) is kept, whether it
  was created before the run or by another actor during it; the run's own guests are deleted.

Needs the MySQL connection the lock tests use (BUZZER_TEST_DB_*), because courses are deleted
there. Everything these tests create themselves is deleted by the suite's own ownership tracking
after each test.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import uuid

import httpx
import pytest

from ..ledger import Ledger, teardown, token_subject
from .conftest import db_settings, mint_guest_email, redis_cli

TESTS = pathlib.Path(__file__).resolve().parents[1]
BROWSER = TESTS / "browser"
INTEGRATION = TESTS / "integration"
SEMESTER = "T7 checks"  # the browser checks' courses; bc_common.SEMESTER


def db():
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
            autocommit=True,
        )
    except pymysql.MySQLError as exc:
        pytest.skip(f"Can't reach MySQL ({exc}); set BUZZER_TEST_DB_*")


class Stack:
    """Creates records through the API; each is also recorded in `ledger` when one is given.
    (The suite's own tracking deletes all of them after the test either way.)"""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token
        self.http = httpx.Client(base_url=base_url, timeout=15.0)
        self.tag = uuid.uuid4().hex[:6]

    def ok(self, method: str, path: str, status: int, **kwargs) -> dict:
        r = self.http.request(
            method, path, headers={"Authorization": f"Bearer {self.token}"}, **kwargs
        )
        assert r.status_code == status, f"{method} {path}: {r.status_code} {r.text}"
        return r.json() if r.content else {}

    def status(self, path: str) -> int:
        return self.http.get(
            path, headers={"Authorization": f"Bearer {self.token}"}
        ).status_code

    def course(self, ledger: Ledger | None, name: str) -> int:
        c = self.ok(
            "POST", "/api/admin/courses", 201, json={"name": name, "semester": SEMESTER}
        )
        if ledger:
            ledger.record("courses", id=c["id"], name=c["name"], semester=c["semester"])
        return c["id"]

    def game(self, ledger: Ledger | None, course_id: int, title: str) -> int:
        g = self.ok(
            "POST",
            "/api/admin/games",
            201,
            json={"title": title, "course_id": course_id},
        )
        if ledger:
            ledger.record("games", id=g["id"], title=g["title"])
        self.ok(
            "POST",
            f"/api/admin/games/{g['id']}/questions",
            201,
            json={
                "type": "true_false",
                "grading_type": "COMPLETENESS",
                "prompt": "Q",
                "points_value": 1,
            },
        )
        return g["id"]

    def room(self, ledger: Ledger | None, course_id: int, game_id: int) -> str:
        d = self.ok(
            "POST",
            "/api/game/rooms",
            201,
            json={"course_id": course_id, "game_id": game_id},
        )
        if ledger:
            ledger.record("sessions", id=d["session_id"], room_code=d["room_code"])
        return d["room_code"]

    def guest(self, ledger: Ledger | None, code: str, name: str) -> str:
        # Minted by the ledger under test, or else by the suite's (so the suite deletes it).
        email = ledger.mint_guest_email(name) if ledger else mint_guest_email(name)
        r = self.http.post(
            "/api/auth/guest",
            json={"display_name": name, "email": email, "room_code": code},
        )
        assert r.status_code == 200, r.text
        user_id = token_subject(r.json()["access_token"])
        if ledger:
            ledger.record("guests", id=user_id, email=email, owned=True)
        return user_id

    def world(self, ledger: Ledger | None, label: str) -> dict:
        """A course in the checks' semester with a game, a room and a guest — the shape of
        what a browser check creates. Every run of this uses the same game title."""
        course_id = self.course(ledger, f"BC Cleanup {label} {self.tag}")
        game_id = self.game(ledger, course_id, f"Cleanup check {self.tag}")
        code = self.room(ledger, course_id, game_id)
        guest_id = self.guest(ledger, code, label)
        return {"course": course_id, "game": game_id, "code": code, "guest": guest_id}

    def course_exists(self, course_id: int) -> bool:
        return self.status(f"/api/admin/courses/{course_id}") == 200

    def exists(self, w: dict) -> dict:
        return {
            "course": self.course_exists(w["course"]),
            "game": self.status(f"/api/admin/games/{w['game']}") == 200,
            "room": self.status(f"/api/game/rooms/{w['code']}/ping") == 200,
            "guest": self.status(f"/api/admin/users/{w['guest']}") == 200,
        }


ALL = {"course": True, "game": True, "room": True, "guest": True}
NONE = {"course": False, "game": False, "room": False, "guest": False}


@pytest.fixture()
def stack(docker_stack, base_url: str, admin_token: str):
    db().close()  # skip early, with the reason, if MySQL is unreachable
    s = Stack(base_url, admin_token)
    yield s
    s.http.close()


def tool_env(base_url: str, ledgers: pathlib.Path, **extra: str) -> dict:
    """The environment of a script or probe run: this stack, and a ledger directory of its
    own, so nothing it does can reach the suite's or anyone else's ledgers."""
    return {
        **os.environ,
        "BUZZER_BROWSER_BASE_URL": base_url,
        "BUZZER_BROWSER_OUTPUT": str(ledgers.parent / "output"),
        "BUZZER_TEST_LEDGERS": str(ledgers),
        **extra,
    }


def run_script(
    code: str, base_url: str, ledgers: pathlib.Path
) -> subprocess.CompletedProcess:
    """Run Python in tests/browser, as a browser script would run."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=BROWSER,
        env=tool_env(base_url, ledgers),
        capture_output=True,
        text=True,
        timeout=120,
    )


def cleanup_cli(
    base_url: str, ledgers: pathlib.Path, *args: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TESTS / "cleanup.py"), "--base-url", base_url, *args],
        env=tool_env(base_url, ledgers),
        capture_output=True,
        text=True,
        timeout=120,
    )


def dead_pid() -> int:
    """The pid of a process that has already exited."""
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(done.stdout)


def snapshot(directory: pathlib.Path) -> dict[str, bytes]:
    return (
        {p.name: p.read_bytes() for p in sorted(directory.glob("*"))}
        if directory.exists()
        else {}
    )


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_teardown_deletes_exactly_the_owned_records(stack, base_url, tmp_path):
    owned_ledger = Ledger.begin(tmp_path / "run", "a_check", base_url=base_url)
    owned = stack.world(owned_ledger, "Owned")
    unrelated = stack.world(None, "Unrelated")  # same shape, same game title
    assert stack.exists(owned) == ALL and stack.exists(unrelated) == ALL
    assert json.loads(redis_cli("GET", f"room:{owned['code']}"))["status"] == "LOBBY"

    assert teardown(owned_ledger, stack.http, stack.token) == []

    assert stack.exists(owned) == NONE
    assert redis_cli("EXISTS", f"room:{owned['code']}") == "0"
    assert stack.exists(unrelated) == ALL
    assert not owned_ledger.path.exists()  # an emptied ledger removes its file


def test_teardown_keeps_what_no_longer_matches_its_record(stack, base_url, tmp_path):
    """A record is deleted only while it still is what was recorded, and a course only while
    nothing outside the ledger uses it."""
    ledger = Ledger.begin(tmp_path / "run", "a_check", base_url=base_url)
    w = stack.world(ledger, "Guarded")
    other_game = stack.game(None, w["course"], f"Moved in {stack.tag}")
    stack.ok(
        "PUT",
        f"/api/admin/games/{w['game']}",
        200,
        json={"title": "Renamed by someone"},
    )
    # The guest record now names a different email than the account has.
    ledger.data["records"]["guests"][0]["email"] = "someone.else@example.com"
    ledger.save()

    problems = teardown(ledger, stack.http, stack.token)

    assert any(p.startswith(f"game {w['game']}: kept") for p in problems), problems
    assert any(p.startswith(f"guest {w['guest']}: kept") for p in problems), problems
    assert any(
        p.startswith(f"course {w['course']}: kept, still used") for p in problems
    ), problems
    assert stack.exists(w) == {
        "course": True,
        "game": True,
        "room": False,
        "guest": True,
    }
    assert stack.status(f"/api/admin/games/{other_game}") == 200
    left = Ledger.load(ledger.path)  # what was kept stays owned, for a later cleanup
    assert [r["id"] for r in left.records("games")] == [w["game"]]
    assert [r["id"] for r in left.records("guests")] == [w["guest"]]
    assert [r["id"] for r in left.records("courses")] == [w["course"]]
    assert left.records("sessions") == []

    # Put things back as recorded; the next teardown finishes the job.
    stack.ok(
        "PUT",
        f"/api/admin/games/{w['game']}",
        200,
        json={"title": f"Cleanup check {stack.tag}"},
    )
    left.data["records"]["guests"][0]["email"] = stack.ok(
        "GET", f"/api/admin/users/{w['guest']}", 200
    )["email"]
    left.save()
    stack.ok("DELETE", f"/api/admin/games/{other_game}", 204)
    assert teardown(left, stack.http, stack.token) == []
    assert stack.exists(w) == NONE


# ---------------------------------------------------------------------------
# The browser checks' scripts, and tests/cleanup.py
# ---------------------------------------------------------------------------


def test_a_failing_script_still_tears_down(stack, base_url, tmp_path):
    ledgers = tmp_path / "ledgers"
    result = run_script(
        """
        import json
        from bc_common import Api
        api = Api()
        try:
            course_id, game_id = api.setup_game("Failing")
            api.question(game_id, {"type": "true_false", "grading_type": "COMPLETENESS",
                                   "prompt": "Q", "points_value": 1})
            code, _ = api.room(course_id, game_id)
            api.guest(code, "Fay")
            print(json.dumps(api.ledger.data["records"]))
            raise RuntimeError("a check blew up")
        finally:
            api.cleanup()
        """,
        base_url,
        ledgers,
    )
    assert result.returncode != 0 and "a check blew up" in result.stderr
    records = json.loads(result.stdout.splitlines()[0])
    [course], [game], [session], [guest] = (
        records[k] for k in ("courses", "games", "sessions", "guests")
    )
    assert not stack.course_exists(course["id"])
    assert stack.status(f"/api/admin/games/{game['id']}") == 404
    assert stack.status(f"/api/game/rooms/{session['room_code']}/ping") == 404
    assert stack.status(f"/api/admin/users/{guest['id']}") == 404
    assert snapshot(ledgers) == {}


def killed_script(base_url: str, ledgers: pathlib.Path) -> dict:
    """A browser script killed after creating its records. Returns them (from its ledger)."""
    result = run_script(
        """
        import os
        from bc_common import Api
        api = Api()
        course_id, game_id = api.setup_game("Killed")
        api.question(game_id, {"type": "true_false", "grading_type": "COMPLETENESS",
                               "prompt": "Q", "points_value": 1})
        code, _ = api.room(course_id, game_id)
        api.guest(code, "Kit")
        os._exit(9)  # killed: no finally, no teardown
        """,
        base_url,
        ledgers,
    )
    assert result.returncode == 9, result.stderr
    [path] = list(ledgers.glob("*.json"))
    left = Ledger.load(path)
    return {
        "path": path,
        "course": left.records("courses")[0]["id"],
        "game": left.records("games")[0]["id"],
        "code": left.records("sessions")[0]["room_code"],
        "guest": left.records("guests")[0]["id"],
    }


def test_an_interrupted_run_is_cleaned_up_by_cleanup(stack, base_url, tmp_path):
    ledgers = tmp_path / "ledgers"
    unrelated = stack.world(None, "Bystander")
    killed = killed_script(base_url, ledgers)
    assert (
        stack.exists(killed) == ALL
    )  # the killed run left everything behind, and its ledger

    dry = cleanup_cli(base_url, ledgers, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert stack.exists(killed) == ALL and killed["path"].exists()

    done = cleanup_cli(base_url, ledgers)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stack.exists(killed) == NONE
    assert not killed["path"].exists()
    assert stack.exists(unrelated) == ALL


def test_report_is_read_only(stack, base_url, tmp_path):
    """--report changes nothing, even with an abandoned ledger right there that plain
    cleanup.py would tear down (its process is gone, it isn't kept). It can't be combined with
    a cleanup (--dry-run) either."""
    ledgers = tmp_path / "ledgers"
    abandoned = Ledger.begin(ledgers, "a_check", base_url=base_url)
    owned = stack.world(abandoned, "Abandoned")
    abandoned.data["pid"] = dead_pid()
    abandoned.save()
    unrelated = stack.world(None, "Unowned")
    before = snapshot(ledgers)

    combined = cleanup_cli(base_url, ledgers, "--report", "--dry-run")
    assert combined.returncode == 2 and "not allowed with" in combined.stderr
    result = cleanup_cli(base_url, ledgers, "--report")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "reported, never deleted" in result.stdout
    assert f"semester '{SEMESTER}'" in result.stdout
    assert "cleanup " not in result.stdout  # no teardown ran
    assert snapshot(ledgers) == before  # the ledger file, byte for byte
    assert stack.exists(owned) == ALL
    assert stack.exists(unrelated) == ALL

    # The same ledger is eligible for a real cleanup, which does delete it.
    done = cleanup_cli(base_url, ledgers)
    assert done.returncode == 0, done.stdout + done.stderr
    assert stack.exists(owned) == NONE and snapshot(ledgers) == {}
    assert stack.exists(unrelated) == ALL


def test_cleanup_skips_live_runs_and_kept_ledgers(stack, base_url, tmp_path):
    ledgers = tmp_path / "ledgers"
    live_run = Ledger.begin(ledgers, "a_check", base_url=base_url)
    running = stack.world(live_run, "Running")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        live_run.data["pid"] = (
            sleeper.pid
        )  # as if another, still running process wrote it
        live_run.save()
        kept = Ledger.begin(
            ledgers,
            "phone_checklist_setup",
            base_url=base_url,
            name="phone-checklist",
            keep=True,
        )
        checklist = stack.world(kept, "Kept")

        result = cleanup_cli(base_url, ledgers)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "still running" in result.stdout
        assert "removed only by its own cleanup" in result.stdout
        assert stack.exists(running) == ALL and stack.exists(checklist) == ALL

        forced = cleanup_cli(base_url, ledgers, "--force")
        assert forced.returncode == 0, forced.stdout + forced.stderr
        assert stack.exists(running) == NONE
        assert stack.exists(checklist) == ALL  # --force never reaches a kept ledger
    finally:
        sleeper.kill()
    assert teardown(Ledger.load(kept.path), stack.http, stack.token) == []
    assert stack.exists(checklist) == NONE


# ---------------------------------------------------------------------------
# The integration suite's own tracking (hygiene_probe.py as a session of its own)
# ---------------------------------------------------------------------------


def run_probe(
    base_url: str, ledgers: pathlib.Path, out: pathlib.Path, *args: str, **extra: str
):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "hygiene_probe.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "--base-url",
            base_url,
            *args,
        ],
        cwd=INTEGRATION,
        env=tool_env(base_url, ledgers, BUZZER_PROBE_OUT=str(out), **extra),
        capture_output=True,
        text=True,
        timeout=300,
    )


def probe_records(out: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in out.read_text().splitlines()]


def gone(stack: Stack, record: dict) -> bool:
    if record["kind"] == "course":
        return not stack.course_exists(record["id"])
    if record["kind"] == "game":
        return stack.status(f"/api/admin/games/{record['id']}") == 404
    if record["kind"] == "room":
        return stack.status(f"/api/game/rooms/{record['code']}/ping") == 404
    user_id = (
        token_subject(record["token"]) if record["kind"] == "guest" else record["id"]
    )
    return stack.status(f"/api/admin/users/{user_id}") == 404


def test_suite_records_are_deleted_after_passing_and_failing_tests(
    stack, base_url, tmp_path
):
    """The probe's own records go after it passes and fails; the guests it only reused are
    kept, however recently they were created: one this test made before the probe's run, and one
    another actor made during it (unseen by the run) — neither email was minted by the probe."""
    ledgers, out = tmp_path / "ledgers", tmp_path / "probe.jsonl"
    unrelated = stack.world(None, "SuiteBystander")
    code = stack.room(None, unrelated["course"], unrelated["game"])
    existing = stack.guest(None, code, "Earlier")  # this test's, minted by the suite
    email = stack.ok("GET", f"/api/admin/users/{existing}", 200)["email"]
    actor_guests: list[str] = []

    try:
        result = run_probe(base_url, ledgers, out, BUZZER_PROBE_REUSE_EMAIL=email)
        records = probe_records(out)
        actor_guests = [
            token_subject(r["token"]) for r in records if r["kind"] == "actor_guest"
        ]

        assert result.returncode == 1, (
            result.stdout + result.stderr
        )  # the one failing test
        assert (
            "1 failed" in result.stdout
            and "the probe fails on purpose" in result.stdout
        )
        owned = [
            r
            for r in records
            if r["kind"] in ("course", "game", "guest", "room", "user")
        ]
        kinds = sorted({r["kind"] for r in owned})
        assert kinds == ["course", "game", "guest", "room", "user"], kinds
        assert [
            r for r in owned if not gone(stack, r)
        ] == []  # run-owned guests included
        assert snapshot(ledgers) == {}  # every ledger emptied and removed

        # Reused, not created by the probe: both kept, and each reported as uncertain.
        kept = [
            token_subject(r["token"]) for r in records if r["kind"] == "reused_guest"
        ]
        assert (
            sorted(kept) == sorted([existing, *actor_guests]) and len(actor_guests) == 1
        )
        assert [u for u in kept if stack.status(f"/api/admin/users/{u}") != 200] == []
        assert result.stdout.count("ownership uncertain") == 2, result.stdout
        assert stack.exists(unrelated) == ALL
    finally:
        # The other actor's guest belongs to no ledger (that is the point): remove it by its id.
        for user_id in actor_guests:
            stack.ok("DELETE", f"/api/admin/users/{user_id}", 204)


def test_a_killed_suite_run_is_cleaned_up_by_cleanup(stack, base_url, tmp_path):
    ledgers, out = tmp_path / "ledgers", tmp_path / "probe.jsonl"
    result = run_probe(base_url, ledgers, out, "-k", "killed", BUZZER_PROBE_KILL="1")
    assert result.returncode == 3, result.stdout + result.stderr
    [course] = probe_records(out)
    assert not gone(stack, course) and list(ledgers.glob("*.json"))

    done = cleanup_cli(base_url, ledgers)
    assert done.returncode == 0, done.stdout + done.stderr
    assert gone(stack, course)
    assert snapshot(ledgers) == {}
