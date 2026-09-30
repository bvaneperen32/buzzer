"""Ownership ledger for the records test tooling creates: the browser checks and the integration
suite (docs/plans/fix-live-session-test-cleanup.md §3.6, §3.11, §3.12).

A ledger is a JSON file in the ledger directory (`BUZZER_TEST_LEDGERS`, default `tests/.ledgers/`,
git-ignored). Every record a run creates goes into it the moment its id comes back: sessions,
games, guest accounts, local user accounts, courses. The file is written atomically each time,
so an interrupted run leaves a complete ledger behind (`tests/cleanup.py` then deletes it).

`teardown()` deletes exactly those records and nothing else. Before each delete it checks that
the record still is what was recorded (a game's title, a guest's email, a course's name and
semester, …) and, as a secondary check, that it was created no earlier than the ledger started.
A record leaves the ledger only once it is gone, or once it is shown not to be the run's; an
empty ledger's file is removed.

**Guests need positive ownership.** A successful `POST /api/auth/guest` doesn't prove the run
created the account: for an email that already has a guest, the endpoint hands back *that*
guest (same response, no flag). So a run creates a guest only with an email it minted first
(`mint_guest_email`: 128 random bits, written to the ledger before the request). No account can
have had that email before, so the account the request returns is the run's own. A guest record
is `owned` only then. Any other guest (an email the run didn't mint: a reused guest, whoever
created it and whenever) is reported and kept, never deleted; creation time never establishes
ownership.

- Sessions, games, guests and users are deleted through the admin API.
- The API has no course delete, and none is added for tests. A course is deleted by its exact
  id, name and semester over the test MySQL connection (`BUZZER_TEST_DB_*`, see
  `integration.conftest.db_settings`), and only once no game or session uses it. The database
  refuses otherwise anyway: those foreign keys are `NO ACTION`. Its roster and access rows go
  with it (`ON DELETE CASCADE`).

A kept ledger (the phone checklist's) is never torn down by `tests/cleanup.py`, only by its
owner's explicit cleanup step.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import socket
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
KINDS = ("sessions", "games", "guests", "users", "courses")  # also the teardown order


def ledger_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("BUZZER_TEST_LEDGERS") or REPO_ROOT / "tests" / ".ledgers")


def token_subject(token: str) -> str:
    """The user id inside an access token, read locally (no request, so no window in which the
    account exists unrecorded). Not verified: the server issued it a moment ago."""
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["sub"]


def admin_credentials() -> tuple[str, str]:
    """BUZZER_ADMIN_USERNAME / BUZZER_ADMIN_PASSWORD, else ADMIN_USERNAME / ADMIN_PASSWORD from
    the environment, else from the repo's .env (as the integration suite reads them)."""
    from .integration.conftest import read_repo_env

    env = read_repo_env()
    username = (os.environ.get("BUZZER_ADMIN_USERNAME") or os.environ.get("ADMIN_USERNAME")
                or env.get("ADMIN_USERNAME"))
    password = (os.environ.get("BUZZER_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")
                or env.get("ADMIN_PASSWORD"))
    if not username or not password:
        sys.exit("Set BUZZER_ADMIN_USERNAME and BUZZER_ADMIN_PASSWORD (or ADMIN_* in .env).")
    return username, password


def admin_login(http: httpx.Client) -> str:
    username, password = admin_credentials()
    r = http.post("/api/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def default_db_connect():
    """The test MySQL connection (autocommit), used only to delete courses."""
    import pymysql

    from .integration.conftest import db_settings

    settings = db_settings()
    return pymysql.connect(
        host=settings["host"], port=settings["port"], user=settings["user"],
        password=settings["password"], database=settings["database"],
        connect_timeout=5, autocommit=True,
    )


def _utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class Ledger:
    def __init__(self, path: pathlib.Path, data: dict):
        self.path = path
        self.data = data
        for kind in KINDS:  # ledgers written before a kind existed
            self.data["records"].setdefault(kind, [])

    @classmethod
    def begin(cls, directory: pathlib.Path, script: str, *, base_url: str, name: str | None = None,
              keep: bool = False) -> Ledger:
        """A new ledger for this process (an existing one, for a fixed `name`: a kept ledger);
        otherwise the file is named after the script and a fresh run id. The file is written
        with the first record, so a run that creates nothing leaves none."""
        run_id = uuid.uuid4().hex
        path = directory / f"{name or f'{script}-{run_id[:12]}'}.json"
        if path.exists():
            return cls.load(path)
        return cls(path, {
            "run_id": run_id,
            "script": script,
            "keep": keep,
            "base_url": base_url,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "started": datetime.now(timezone.utc).isoformat(),
            "records": {kind: [] for kind in KINDS},
        })

    @classmethod
    def load(cls, path: pathlib.Path) -> Ledger:
        return cls(path, json.loads(path.read_text()))

    @property
    def keep(self) -> bool:
        return bool(self.data.get("keep"))

    @property
    def started(self) -> datetime:
        """When the ledger started, to the second (MySQL stores creation times to the second)."""
        return _utc(self.data["started"]).replace(microsecond=0)

    def records(self, kind: str) -> list[dict]:
        return list(self.data["records"][kind])

    def mint_guest_email(self, label: str = "guest") -> str:
        """A fresh email for a guest the run is about to create. It carries 128 random bits, so
        no account can already have it, and it is on disk before the request goes out: an
        interrupted run's teardown can still find the guest by it."""
        local = "".join(ch for ch in label.lower() if ch.isalnum()) or "guest"
        email = f"{local}.{uuid.uuid4().hex}@example.com"
        self.data.setdefault("minted", []).append(email)
        self.save()
        return email

    def is_minted(self, email: str) -> bool:
        return email.lower() in (e.lower() for e in self.data.get("minted", []))

    def record(self, kind: str, **fields) -> None:
        """Take ownership of a record the moment it exists. `fields` holds its `id` and what
        identifies it at teardown (a game's title, a guest's email, a course's name and
        semester). A guest also carries `owned`: True only when its email was minted by this
        run (see the module docstring); otherwise it is recorded only to be reported.
        `adopted=True` marks a record named explicitly by a person, which may be older than the
        ledger."""
        assert kind in KINDS and "id" in fields, (kind, fields)
        self.data["records"][kind].append(fields)
        self.save()

    def forget(self, kind: str, record_id) -> None:
        self.data["records"][kind] = [r for r in self.data["records"][kind] if r["id"] != record_id]
        self.save()

    def is_empty(self) -> bool:
        return not any(self.data["records"][kind] for kind in KINDS) and not self.data.get("minted")

    def running_elsewhere(self) -> bool:
        """Whether the process that wrote this ledger still runs on this host (then its own
        teardown is still to come, and cleaning up under it would break its checks)."""
        if self.data.get("host") != socket.gethostname() or self.data.get("pid") == os.getpid():
            return False
        try:
            os.kill(int(self.data["pid"]), 0)
        except (OSError, ValueError, KeyError):
            return False
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)

    def remove_file(self) -> None:
        self.path.unlink(missing_ok=True)

    def summary(self) -> str:
        return ", ".join(f"{len(self.data['records'][k])} {k}" for k in KINDS if self.data["records"][k]) or "nothing"


def _wait_gone(http: httpx.Client, headers: dict, path: str, timeout: float = 10.0) -> bool:
    """The admin API commits a delete just after responding; wait until the record reads as
    gone, so the course delete that follows sees it gone too."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if http.get(path, headers=headers).status_code == 404:
            return True
        time.sleep(0.1)
    return False


def teardown(ledger: Ledger, http: httpx.Client, token: str, *, dry_run: bool = False,
             db_connect: Callable | None = default_db_connect) -> list[str]:
    """Delete the ledger's records, exactly and only those. Returns the problems: records kept
    because they couldn't be deleted or no longer match what was recorded (those stay in the
    ledger for `tests/cleanup.py`), and records shown not to be the run's (forgotten)."""
    headers = {"Authorization": f"Bearer {token}"}
    problems: list[str] = []

    def predates(record: dict, created_at) -> bool:
        """Created before the ledger started: not this run's record (unless adopted)."""
        return not record.get("adopted") and _utc(created_at) < ledger.started

    def plan(kind: str, record: dict) -> None:
        print(f"  would delete {kind[:-1]} {record}")

    for record in ledger.records("sessions"):
        if dry_run:
            plan("sessions", record)
            continue
        r = http.delete(f"/api/game/sessions/{record['id']}", headers=headers)
        if r.status_code in (204, 404):  # 404: already gone (its game's delete takes it too)
            ledger.forget("sessions", record["id"])
        else:
            problems.append(f"session {record['id']}: delete returned {r.status_code} {r.text[:200]}")

    deleted_games = []
    for record in ledger.records("games"):
        r = http.get(f"/api/admin/games/{record['id']}", headers=headers)
        if r.status_code == 404:
            ledger.forget("games", record["id"])
            continue
        game = r.json() if r.status_code == 200 else {}
        if r.status_code != 200 or ("title" in record and game.get("title") != record["title"]):
            problems.append(f"game {record['id']}: kept, it isn't the recorded '{record.get('title')}' "
                            f"({r.status_code} {r.text[:200]})")
            continue
        if predates(record, game["created_at"]):
            problems.append(f"game {record['id']}: not this run's (created {game['created_at']}); forgotten")
            ledger.forget("games", record["id"])
            continue
        if dry_run:
            plan("games", record)
            continue
        r = http.delete(f"/api/admin/games/{record['id']}", headers=headers)
        if r.status_code in (204, 404):
            deleted_games.append(record["id"])
        else:
            problems.append(f"game {record['id']}: delete returned {r.status_code} {r.text[:200]}")
    for game_id in deleted_games:
        if _wait_gone(http, headers, f"/api/admin/games/{game_id}"):
            ledger.forget("games", game_id)
        else:
            problems.append(f"game {game_id}: still readable after its delete")

    # A minted email with no guest record at all: the run was interrupted between creating the
    # guest and recording it (or the request failed). The email alone proves ownership; look it
    # up. A guest that has a record, matching or not, is left to that record's checks below.
    recorded_emails = {r["email"].lower() for r in ledger.records("guests")}
    unrecorded = [e for e in ledger.data.get("minted", []) if e.lower() not in recorded_emails]
    unresolved: list[str] = []  # minted emails whose guest couldn't be looked up
    if unrecorded:
        r = http.get("/api/admin/users/guests", headers=headers)
        by_email = {(u.get("email") or "").lower(): u for u in r.json()} if r.status_code == 200 else None
        if by_email is None:
            unresolved = unrecorded
            problems.append(f"minted guest emails {unrecorded}: couldn't look them up ({r.status_code}); "
                            "kept in the ledger for tests/cleanup.py")
        else:
            recorded_ids = {r["id"] for r in ledger.records("guests")}
            for email in unrecorded:
                user = by_email.get(email.lower())
                if user and user["id"] not in recorded_ids:
                    ledger.record("guests", id=user["id"], email=email, owned=True)

    for record in ledger.records("guests"):
        if not record.get("owned"):
            # Not proven to be the run's: the email wasn't minted by it, so the server may have
            # handed back an existing guest's token. Kept, whatever its creation time; reported
            # while it exists.
            if http.get(f"/api/admin/users/{record['id']}", headers=headers).status_code != 404:
                problems.append(f"guest {record['id']} ({record['email']}): kept, ownership uncertain "
                                "(an email this run didn't mint; the account may be someone else's)")
            if not dry_run:
                ledger.forget("guests", record["id"])

    for kind, label, matches in (
        ("guests", "guest", lambda user, rec: user.get("role") == "GUEST"
                                               and (user.get("email") or "").lower() == rec["email"].lower()),
        ("users", "user", lambda user, rec: user.get("role") not in (None, "ADMIN", "GUEST")
                                             and user.get("username") == rec["username"]),
    ):
        for record in ledger.records(kind):
            if kind == "guests" and not record.get("owned"):
                continue  # reported above; never deleted
            r = http.get(f"/api/admin/users/{record['id']}", headers=headers)
            if r.status_code == 404:
                ledger.forget(kind, record["id"])
                continue
            user = r.json() if r.status_code == 200 else {}
            if not matches(user, record):
                problems.append(f"{label} {record['id']}: kept, it isn't the recorded {label} "
                                f"({r.status_code} role={user.get('role')!r} email={user.get('email')!r} "
                                f"username={user.get('username')!r})")
                continue
            if predates(record, user["created_at"]):
                # A secondary check: an owned record can't be older than its run
                problems.append(f"{label} {record['id']}: not this run's (created {user['created_at']}); forgotten")
                ledger.forget(kind, record["id"])
                continue
            if dry_run:
                plan(kind, record)
                continue
            r = http.delete(f"/api/admin/users/{record['id']}", headers=headers)
            if r.status_code in (204, 404):
                ledger.forget(kind, record["id"])
            else:
                problems.append(f"{label} {record['id']}: delete returned {r.status_code} {r.text[:200]}")

    if not dry_run and ledger.data.get("minted"):
        # A minted email is settled once its guest is recorded (and deleted, or kept with a
        # problem) or shown never to have been created. One that couldn't be looked up is the
        # only proof its guest is the run's, so it stays, and with it the ledger's file.
        ledger.data["minted"] = unresolved
        ledger.save()

    courses = ledger.records("courses")
    if courses:
        try:
            if db_connect is None:
                raise RuntimeError("no database connection given")
            conn = db_connect()
        except Exception as exc:  # noqa: BLE001 — report and keep, never guess
            conn = None
            problems.append(f"courses {[c['id'] for c in courses]}: kept, MySQL unreachable ({exc}); "
                            "set BUZZER_TEST_DB_* and run tests/cleanup.py")
        if conn is not None:
            with conn:
                for record in courses:
                    problem = _delete_course(conn, ledger, record, dry_run)
                    if problem:
                        problems.append(problem)

    if not dry_run:
        if ledger.is_empty():
            ledger.remove_file()
        else:
            ledger.save()
    return problems


def _delete_course(conn, ledger: Ledger, record: dict, dry_run: bool) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT name, semester, created_at FROM courses WHERE id = %s", (record["id"],))
        row = cur.fetchone()
        if row is None:
            ledger.forget("courses", record["id"])
            return None
        name, semester, created_at = row
        if (name, semester) != (record["name"], record["semester"]):
            return f"course {record['id']}: kept, it isn't the recorded '{record['name']}' ({name}, {semester})"
        if not record.get("adopted") and _utc(created_at) < ledger.started:
            ledger.forget("courses", record["id"])
            return f"course {record['id']}: not this run's (created {created_at}); forgotten"
        # By now a real teardown has deleted the ledger's games and sessions; a dry run hasn't,
        # so it leaves out the ones it would have deleted.
        own_games = {r["id"] for r in ledger.records("games")} if dry_run else set()
        own_sessions = {r["id"] for r in ledger.records("sessions")} if dry_run else set()
        cur.execute("SELECT id FROM games WHERE course_id = %s", (record["id"],))
        games = len({g for (g,) in cur.fetchall()} - own_games)
        cur.execute("SELECT id FROM game_sessions WHERE course_id = %s", (record["id"],))
        sessions = len({s for (s,) in cur.fetchall()} - own_sessions)
        if games or sessions:
            return (f"course {record['id']}: kept, still used by {games} game(s) and "
                    f"{sessions} session(s) the ledger doesn't own")
        if dry_run:
            print(f"  would delete course {record}")
            return None
        try:
            cur.execute(
                "DELETE FROM courses WHERE id = %s AND name = %s AND semester = %s",
                (record["id"], record["name"], record["semester"]),
            )
        except Exception as exc:  # noqa: BLE001 — e.g. a game moved into it since the check
            return f"course {record['id']}: kept, the delete failed ({exc})"
    ledger.forget("courses", record["id"])
    return None
