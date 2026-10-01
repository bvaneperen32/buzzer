"""
Unit tests for guest ownership in the test-data ledger (tests/ledger.py) and the integration
suite's tracker (tests/integration/conftest.py; docs/plans/fix-live-session-test-cleanup.md
§3.12).

A successful POST /api/auth/guest doesn't prove the run created the account: for an email that
already has a guest, the server hands back that guest. So only a guest whose email the run minted
is the run's; creation time never establishes ownership. The admin API is an in-memory fake
(httpx.MockTransport), so every timestamp here is exact.

Run with:
    cd tests/unit && PYTHONPATH=../../backend python -m pytest test_ledger.py -v
"""

from __future__ import annotations

import base64
import json
import re

import httpx
import pytest

from ..integration import conftest as suite
from ..ledger import Ledger, teardown

STARTED = (
    "2026-09-30T12:00:00.400000+00:00"  # the ledger's start; MySQL keeps whole seconds
)


class FakeAdmin:
    """The admin API's user routes, over an in-memory table."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.listing_fails = False  # GET /api/admin/users/guests answers 500

    def guest(self, user_id: str, email: str, created_at: str) -> None:
        self.users[user_id] = {
            "id": user_id,
            "role": "GUEST",
            "email": email,
            "username": None,
            "created_at": created_at,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/admin/users/guests" and request.method == "GET":
            if self.listing_fails:
                return httpx.Response(500, json={"detail": "listing failed"})
            return httpx.Response(
                200, json=[u for u in self.users.values() if u["role"] == "GUEST"]
            )
        match = re.fullmatch(r"/api/admin/users/([^/]+)", path)
        if match and match[1] in self.users:
            if request.method == "GET":
                return httpx.Response(200, json=self.users[match[1]])
            if request.method == "DELETE":
                self.deleted.append(match[1])
                del self.users[match[1]]
                return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture()
def api():
    fake = FakeAdmin()
    fake.http = httpx.Client(
        base_url="http://stack", transport=httpx.MockTransport(fake.handle)
    )
    return fake


@pytest.fixture()
def ledger(tmp_path) -> Ledger:
    led = Ledger.begin(tmp_path, "a_run", base_url="http://stack")
    led.data["started"] = STARTED
    return led


def clean(ledger: Ledger, api: FakeAdmin) -> list[str]:
    return teardown(ledger, api.http, "admin-token", db_connect=None)


# ---------------------------------------------------------------------------
# A reused guest is never the run's, however recent
# ---------------------------------------------------------------------------


def test_only_a_guest_the_run_minted_is_deleted(ledger, api):
    """Ownership comes from the minted email, never from creation time. In one cleanup:
    - the run's own guest (its email minted, 128 random bits) is deleted, even one created in
      the ledger's first second;
    - a reused guest is kept and reported "ownership uncertain", whether someone else created
      it in that same second (12:00:00 against the ledger's 12:00:00.4) or during the run;
    - so is a record from before ownership was tracked (no `owned`).
    Deciding by creation time instead deletes both reused guests and the old record."""
    email = ledger.mint_guest_email("Ada")
    assert re.fullmatch(r"ada\.[0-9a-f]{32}@example\.com", email)
    for user_id, address, created, owned in (
        ("g-owned", email, "2026-09-30T12:00:00", True),
        ("g-same-second", "someone@example.com", "2026-09-30T12:00:00", False),
        ("g-actor", "actor@example.com", "2026-09-30T12:05:00", False),
        ("g-before-tracking", "old@example.com", "2026-09-30T12:02:00", None),
    ):
        api.guest(user_id, address, created)
        fields = {"id": user_id, "email": address}
        if owned is not None:
            fields["owned"] = owned
        ledger.record("guests", **fields)

    problems = clean(ledger, api)

    assert api.deleted == ["g-owned"]
    for kept in ("g-same-second", "g-actor", "g-before-tracking"):
        assert kept in api.users
        assert any(kept in p and "ownership uncertain" in p for p in problems), problems
    # Reported once, then forgotten; the account stays.
    assert ledger.records("guests") == [] and not ledger.path.exists()


def test_an_owned_guest_older_than_its_ledger_is_kept(ledger, api):
    """Creation time is still a secondary check: an 'owned' guest older than the run is
    inconsistent, so it is kept and reported."""
    email = ledger.mint_guest_email("Cy")
    api.guest("g-odd", email, "2026-09-30T11:59:59")
    ledger.record("guests", id="g-odd", email=email, owned=True)

    problems = clean(ledger, api)

    assert api.deleted == [] and any(
        "g-odd" in p and "not this run's" in p for p in problems
    )


def test_an_owned_guest_that_no_longer_matches_its_record_is_kept(ledger, api):
    """A record whose account no longer has the recorded email is kept, and stays in the
    ledger (still owned, for later), whichever side differs. The lookup by minted email, which
    is only for a guest with no record at all, doesn't go around such a record."""
    moved = ledger.mint_guest_email("Dee")
    api.guest(
        "g-moved", "different@example.com", "2026-09-30T12:01:00"
    )  # account changed
    ledger.record("guests", id="g-moved", email=moved, owned=True)
    minted = ledger.mint_guest_email("Hana")
    api.guest("g-recorded", minted, "2026-09-30T12:01:00")  # the record differs
    ledger.record(
        "guests", id="g-recorded", email="someone.else@example.com", owned=True
    )

    problems = clean(ledger, api)

    assert api.deleted == []
    for guest in ("g-moved", "g-recorded"):
        assert any(p.startswith(f"guest {guest}: kept") for p in problems), problems
    assert sorted(r["id"] for r in ledger.records("guests")) == [
        "g-moved",
        "g-recorded",
    ]


def test_an_uncertain_guest_already_gone_is_not_reported(ledger, api):
    """E.g. a guest minted by another ledger, which that ledger has already deleted."""
    ledger.record("guests", id="g-gone", email="elsewhere@example.com", owned=False)

    assert clean(ledger, api) == []
    assert not ledger.path.exists()


# ---------------------------------------------------------------------------
# Interruptions between minting and recording
# ---------------------------------------------------------------------------


def test_a_failed_lookup_keeps_the_mint_for_the_next_cleanup(ledger, api):
    """A run killed after creating a guest but before recording it leaves only the minted email,
    on disk before the request. The guest listing then fails during cleanup: the unresolved
    mint stays in the ledger (the only proof that guest is the run's), so the ledger's file
    stays too, while the recorded guest is still deleted. The next cleanup, with the listing
    back, finds the unrecorded guest by its minted email and deletes it; no other account,
    even a look-alike, is touched."""
    lost = ledger.mint_guest_email("Ivy")  # killed before this guest was recorded
    api.guest("g-unrecorded", lost, "2026-09-30T12:03:00")
    kept = ledger.mint_guest_email("Jo")
    api.guest("g-recorded", kept, "2026-09-30T12:02:00")
    ledger.record("guests", id="g-recorded", email=kept, owned=True)
    api.guest("g-bystander", "bystander@example.com", "2026-09-30T12:03:00")
    api.guest("g-lookalike", "ivy.0123456789abcdef@example.com", "2026-09-30T12:03:00")
    api.listing_fails = True

    problems = clean(Ledger.load(ledger.path), api)

    assert problems == [
        f"minted guest emails ['{lost}']: couldn't look them up (500); "
        "kept in the ledger for tests/cleanup.py"
    ]
    assert api.deleted == ["g-recorded"]
    # The file is still there, as tests/cleanup.py would find it.
    left = Ledger.load(ledger.path)
    assert left.data["minted"] == [lost] and left.records("guests") == []

    api.listing_fails = False
    assert clean(left, api) == []

    assert api.deleted == ["g-recorded", "g-unrecorded"]
    assert set(api.users) == {"g-bystander", "g-lookalike"}
    assert not left.path.exists()


def test_a_minted_email_never_used_leaves_nothing(ledger, api):
    ledger.mint_guest_email("Fay")  # the request never went out, or failed
    api.guest("g-bystander", "bystander@example.com", "2026-09-30T12:03:00")

    assert clean(ledger, api) == []

    assert api.deleted == [] and not ledger.path.exists()


def test_a_dry_run_deletes_nothing_and_keeps_the_mints(ledger, api):
    email = ledger.mint_guest_email("Gus")
    api.guest("g-owned", email, "2026-09-30T12:01:00")
    ledger.record("guests", id="g-owned", email=email, owned=True)

    teardown(ledger, api.http, "admin-token", db_connect=None, dry_run=True)

    assert api.deleted == [] and ledger.is_minted(email)


# ---------------------------------------------------------------------------
# The integration suite's tracker
# ---------------------------------------------------------------------------


def token_for(user_id: str) -> str:
    body = (
        base64.urlsafe_b64encode(json.dumps({"sub": user_id}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{body}.signature"


def guest_response(email: str, user_id: str) -> httpx.Response:
    request = httpx.Request(
        "POST",
        "http://stack/api/auth/guest",
        json={"display_name": "X", "email": email, "room_code": "ABCDEF"},
    )
    return httpx.Response(
        200, json={"access_token": token_for(user_id)}, request=request
    )


@pytest.fixture()
def tracking(tmp_path, monkeypatch):
    run = Ledger.begin(tmp_path, "integration", base_url="http://stack")
    test = Ledger.begin(
        tmp_path, "integration", base_url="http://stack", name="per-test"
    )
    monkeypatch.setattr(suite._OWNED, "run", run)
    monkeypatch.setattr(suite._OWNED, "test", test)
    return run, test


def test_the_tracker_owns_only_guests_with_minted_emails(tracking):
    run, test = tracking
    minted = suite.mint_guest_email("Hal")  # in the running test's ledger
    minted_by_fixture = run.mint_guest_email("Ivy")  # e.g. by a module fixture

    suite._record_creation(guest_response(minted, "g-1"))
    suite._record_creation(
        guest_response(minted_by_fixture.upper(), "g-2")
    )  # server lowercases
    suite._record_creation(
        guest_response("existing@example.com", "g-3")
    )  # a reused guest

    assert [(r["id"], r["owned"]) for r in test.records("guests")] == [
        ("g-1", True),
        ("g-2", True),
        ("g-3", False),
    ]
