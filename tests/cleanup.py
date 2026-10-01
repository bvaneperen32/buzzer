"""Clean up after interrupted test runs: the browser checks and the integration suite
(tests/browser/README.md, "Test data"; docs/plans/fix-live-session-test-cleanup.md §3.6, §3.11).

    python tests/cleanup.py             # tear down every ledger an interrupted run left behind
    python tests/cleanup.py --dry-run   # show what that would delete; delete nothing
    python tests/cleanup.py --report    # only list test-like records no ledger owns; read-only

Every run tears its own records down when it ends, even after a failure or Ctrl-C. A run that
was killed outright leaves its ledger in the ledger directory (`BUZZER_TEST_LEDGERS`, default
`tests/.ledgers/`), and this deletes exactly the records in it, through the API the run used.
It skips a ledger whose process is still running on this host (`--force` overrides), and never
touches a kept ledger: the phone-checklist game is removed only by
`tests/browser/phone_checklist_setup.py --cleanup`.

`--report` changes nothing — no records, no ledger files. It lists courses in the browser
checks' semester and `@example.com` guest accounts that no ledger owns: leftovers from before
the ledgers existed. Their ownership can't be established, so they are reported, never deleted.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tests.ledger import Ledger, admin_login, ledger_dir, teardown  # noqa: E402

REPORT_SEMESTER = "T7 checks"  # the browser checks' courses (tests/browser/bc_common.SEMESTER)


def ledgers(directory: pathlib.Path) -> list[Ledger]:
    return [Ledger.load(p) for p in sorted(directory.glob("*.json"))] if directory.exists() else []


def report(http: httpx.Client, token: str, owned: list[Ledger]) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    owned_courses = {r["id"] for led in owned for r in led.records("courses")}
    owned_guests = {r["id"] for led in owned for r in led.records("guests")}
    courses = [c for c in http.get("/api/admin/courses", headers=headers).json()
               if c["semester"] == REPORT_SEMESTER and c["id"] not in owned_courses]
    guests = [u for u in http.get("/api/admin/users/guests", headers=headers).json()
              if (u.get("email") or "").endswith("@example.com") and u["id"] not in owned_guests]
    for led in owned:
        print(f"ledger  {led.path.name} ({led.data['script']}{', kept' if led.keep else ''}: {led.summary()})")
    print("Not owned by any ledger (reported, never deleted):")
    ids = sorted(c["id"] for c in courses)
    print(f"  {len(courses)} course(s) in the semester '{REPORT_SEMESTER}'"
          + (f": ids {ids[0]}–{ids[-1]}" if ids else ""))
    print(f"  {len(guests)} guest account(s) with an @example.com email")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would be deleted; delete nothing")
    mode.add_argument("--report", action="store_true",
                      help="only list test-like records no ledger owns; changes nothing")
    parser.add_argument("--force", action="store_true", help="also clean ledgers whose process still runs")
    parser.add_argument("--ledgers", type=pathlib.Path, default=ledger_dir(), help="ledger directory")
    parser.add_argument("--base-url", default=os.environ.get("BUZZER_BROWSER_BASE_URL", "http://localhost:8080"),
                        help="API for --report, and for ledgers that don't record one")
    args = parser.parse_args(argv)

    found = ledgers(args.ledgers)
    if args.report:
        http = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60.0)
        report(http, admin_login(http), found)
        return 0

    sessions: dict[str, tuple[httpx.Client, str]] = {}

    def api(base_url: str) -> tuple[httpx.Client, str]:
        if base_url not in sessions:
            http = httpx.Client(base_url=base_url, timeout=60.0)
            sessions[base_url] = (http, admin_login(http))
        return sessions[base_url]

    problems: list[str] = []
    for led in found:
        label = f"{led.path.name} ({led.data['script']}, started {led.data['started']}: {led.summary()})"
        if led.keep:
            print(f"kept    {label} — removed only by its own cleanup step")
            continue
        if led.running_elsewhere() and not args.force:
            print(f"skipped {label} — its process ({led.data['pid']}) is still running")
            continue
        print(f"{'plan   ' if args.dry_run else 'cleanup'} {label}")
        http, token = api((led.data.get("base_url") or args.base_url).rstrip("/"))
        problems += teardown(led, http, token, dry_run=args.dry_run)
    if not found:
        print(f"No ledgers in {args.ledgers}.")
    for problem in problems:
        print(f"KEPT    {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
