# Browser checks (T7)

Playwright scripts that drive the **production builds through nginx**, with a real projector
page and phone-sized pages, for Sort It and Spot It (`docs/plans/t7-question-types.md` §4.6).
Setup goes through the REST API as the admin; every check then uses the real UI.

| Script | What it checks |
|---|---|
| `bc_editor.py` | The question editor: limits vs the server (edge-size images), default grids, too-dense drafts, authoring dimensions, tick-only reconfirmation, the Replace warning, re-selecting the current image (live lobby, prompt-only save), the live freeze and the Play gate, the real-editor round-trip matrix, answer keys, text rules, keyboard |
| `bc_sort.py` | Sort It on phones: drag, tap-tap and keyboard, lock and unlock, time-out, points display and rounding, game over, the projector ↔ report cross-check, one socket per page for the whole game (B1), and placements surviving a socket drop and reconnect and a host disconnect and return (B2) |
| `bc_spot.py` | Spot It on a projector and three phones: coordinates and aspect, taps, nudges, rotation, keyboard, reveal, heatmap, phone texts, game over, image isolation, image failure and Retry, auto-advance, redraws, one socket per page (B1), a selection surviving a socket drop and reconnect, an answer given while offline delivered after the phone re-joins, a host disconnect and return (the phones' banner cleared, a manual lock kept; B2), the projector's own lost connection pausing its controls, auto-advance never firing into a reconnect (checked on the wire), and late answers rejected without the error screen |
| `bc_answers.py` | The original question types (multiple choice, true/false, fill in the blank) on two phones: what the results say for an answer that counted (right, wrong, or worth 0 points) and for one that didn't (refused as late, dropped after a reconnect, never given): "No answer" from the server's `answered`, never "Answer recorded!", "Incorrect" or "You answered …"; a refusal for another question leaving the current answer alone; and Show Results while an answer is still being recorded past the server's wait (`results_delayed`): the projector's notice, Show Results still enabled, and the results, counting that answer, when it is pressed again. Holds a question's row in MySQL for that, so it needs MySQL on 3306 (`BUZZER_TEST_DB_*`) |
| `bc_common.py` | Shared helpers: configuration, the REST setup client, result recording, socket observation (and `SocketProxy`: dropping a page's connection, holding it offline, withholding chosen server events, recording every event the page sends, and sending one as the page), test images |
| `phone_checklist_setup.py` | Not a check: creates, and with `--cleanup` removes, the game for the physical-phone checklist (below) |

Two files outside this directory serve these scripts and the integration suite alike:
`tests/ledger.py` (the ownership ledger) and `tests/cleanup.py` (cleanup after interrupted runs);
see "Test data" below.

They are standalone scripts, not pytest tests: `pytest` doesn't collect them.

## Prerequisites

1. **The stack is up** with migrations applied (root README, "First-time setup"), with
   `APP_ENV=development` (the default). Outside development, the gateway allows 10 socket
   connections per IP address every 10 seconds, and every page in a run connects from the same
   address, so the checks' sockets would be refused.
2. **The frontend is built**: `npm run build` at the repo root. nginx serves the `dist/` folders,
   so rebuild after every frontend change — the checks never see the Vite dev servers.
3. **Python dependencies**, in the repo's virtual environment:

   ```bash
   pip install -r tests/browser/requirements.txt
   ```

   This includes the integration suite's requirements, because the scripts reuse its helpers
   (and PyMySQL, which the cleanup uses for courses).
4. **A browser.** By default the scripts use the installed Google Chrome (Playwright's `chrome`
   channel). Without Chrome, install Playwright's own Chromium and select it with an empty channel:

   ```bash
   python -m playwright install chromium
   export BUZZER_BROWSER_CHANNEL=
   ```

5. **An admin account** (below). Nothing in these files holds credentials.
6. **MySQL on the published port `3306`**, for deleting the courses a run created (the API has
   no course delete). It is the same connection the integration suite's lock tests use:
   `BUZZER_TEST_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD`, defaulting to the backend's
   account and `MYSQL_PASSWORD` from `.env`. Without it a run still works; its courses stay in
   its ledger for `tests/cleanup.py`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BUZZER_BROWSER_BASE_URL` | `http://localhost:8080` | The nginx address serving `/host/`, `/player/` and `/api` |
| `BUZZER_ADMIN_USERNAME`, `BUZZER_ADMIN_PASSWORD` | `ADMIN_USERNAME` / `ADMIN_PASSWORD` from the environment, else from the repo's `.env` | The admin used for setup; a run stops at once if neither is set |
| `BUZZER_BROWSER_CHANNEL` | `chrome` | Playwright channel; empty uses the bundled Chromium |
| `BUZZER_BROWSER_HEADED` | off | `1` shows the browser windows |
| `BUZZER_BROWSER_OUTPUT` | `tests/browser/output` | Where results and screenshots go (git-ignored) |

## Running

From anywhere, with the virtual environment active:

```bash
python tests/browser/bc_editor.py
python tests/browser/bc_sort.py
python tests/browser/bc_spot.py
python tests/browser/bc_answers.py
```

On a local stack each takes under a minute. They have been verified one at a time; running
them in parallel is untested. Each script creates its own courses, games, rooms and guest
accounts, with a random suffix in their names, and deletes all of them when it ends, even after a
failure or Ctrl-C (see "Test data" below).

## Reading the results

Each check prints one line, and the same line is appended to `<output>/results.jsonl`. Delete that
file to start a fresh record. `<output>/shots/` holds screenshots: some for visual review (the
phone shapes, heatmaps, results) and one for every scenario that stopped with an error.

| Status | Meaning |
|---|---|
| `PASS` / `FAIL` | A behaviour held / didn't. Any `FAIL` makes the script exit 1 |

A scenario that raises is recorded as `FAIL  <scenario> ran to completion`, and the script moves on
to the next one.

## Sockets, reconnects and the host disconnect (B1, B2)

Both were open when T7 recorded them (spec §11). Both are fixed now
(`docs/plans/fix-live-session-test-cleanup.md`), and the checks assert the fixed behaviour.

- **B1: one socket per page.** A screen change no longer re-creates the socket. The scripts
  answer as soon as a question shows, with no wait for a re-join after navigation. They count
  the sockets each page opened: one per page load.
- **Real reconnects.** A check drops a phone's connection through a Playwright WebSocket route
  (`SocketProxy`), and can keep it offline with the browser's offline mode. Then
  `SocketLog.rejoined` waits for the new socket's `sync_state`: an event, not a delay. While
  offline, the phone shows "Connection lost — reconnecting…" and keeps its question and
  selection. An answer given then says "Sending your answer…" (never "Answer locked in!") and is
  delivered, and scored, once the phone has re-joined.
- **B2: a host disconnect.** Closing the projector mid-question shows the phones "Host
  disconnected" and locks their answers, keeping placements and selections. Opening it again
  clears the banner and unlocks them. A lock the host set by hand stays, and the banner still
  clears.
- **The projector's own connection.** Held offline, the projector says it is reconnecting and
  disables Show Results and Lock until the server has it back in the room.
  - What the projector sends is checked on the wire: `SocketProxy` records every event a page
    sends (`sent()`).
  - `auto_advance_waits_for_the_rejoin` drops the projector inside auto-advance's 1.5 s pause,
    and again inside the results countdown. It keeps it offline past the timer, then lets it
    reconnect with its `sync_state` held back.
  - No `host_advance` goes out while offline, nor while connected but not yet seated. The advance
    is sent once, a full pause (or countdown) after the rejoin.
  - Without the gating, the timer's advance is buffered offline and flushed on reconnect before
    the rejoin, then sent a second time; these checks fail.
  - (A disabled button can't be clicked at all, so clicking one proves nothing about the
    gating.)
- **Late answers.** `SocketProxy.withhold()` holds back the server's `question_locked`, and
  then its `question_results`, from a phone, so the phone locks in after the question has
  closed: the race a real last-moment tap runs. The server's `answer_rejected` leaves the
  phone's screen up, says the answer didn't count, and never shows it as accepted. The game then
  plays on.
- **Results for answers that didn't count** (`bc_answers.py`). The results say "No answer",
  with no "You answered" line, for a refused answer, for one dropped after a reconnect, and for
  none: in accuracy and completeness modes alike. An accepted answer keeps its result, even one
  worth 0 points (still "Answer recorded!" or "Incorrect").

## Test data

**Ownership.** Every record a run creates is written, the moment it exists, to that run's
ownership ledger, `tests/.ledgers/<script>-<run>.json` (`BUZZER_TEST_LEDGERS` moves the
directory; `tests/ledger.py`): sessions, games (with their questions and images), guest
accounts, courses. The integration suite records what its tests create the same way (its
`conftest.py`, "Test data").

**When the run ends**, even after a failure or Ctrl-C, the script deletes exactly those records.

- Before each delete it checks the record is still what was recorded: a game's title, a guest's
  role and email, a course's name and semester. As a secondary check, the record must have been
  created no earlier than the run started.
- **A guest needs positive proof.** `/api/auth/guest` hands back an *existing* guest for an email
  that already has one, so a successful response doesn't show the run created the account.
  - The run creates guests only with emails it minted first (`mint_guest_email`: 128 random
    bits, written to the ledger before the request). Only those guests are deleted.
  - A guest with any other email is reported ("ownership uncertain") and kept, however recently
    it was created.
- A course is deleted only once nothing else uses it.
- Anything kept is printed, and stays in the ledger. An emptied ledger's file is removed.
- Records the run didn't create are never touched, even ones with the same kind of name.

**A run that was killed** (its process ended without the `finally`) leaves its ledger behind:

```bash
python tests/cleanup.py --dry-run   # what it would delete; deletes nothing
python tests/cleanup.py             # delete it
python tests/cleanup.py --report    # only list test-like records no ledger owns; changes nothing
```

- `tests/cleanup.py` handles both suites' ledgers, each through the API its run used.
- It skips a ledger whose process is still running on this machine (`--force` overrides), and
  never touches the phone checklist's kept ledger.
- `--report` is read-only: it changes no record and no ledger file, and can't be combined with a
  cleanup. It lists courses in the checks' semester (`T7 checks`) and `@example.com` guest
  accounts that no ledger owns. They include leftovers from before ledgers existed. Ownership of
  those can't be established, so they are only reported.

## The physical-phone checklist game

`phone_checklist_setup.py` creates the game for spec §4.7's checklist. It is "T7 phone checklist"
in the course "T7 phone checklist (Physical phone)", with seven questions:

- Sort It with 10 cards and 4 boxes;
- Spot It on a landscape image and on a portrait image;
- a Sort It worth 5.1 points;
- a COMPLETENESS question of each type;
- a 20-second Spot It.

The script prints which checklist items each question serves and its answer.

**Keeping and removing it.**

- The game is kept for re-runs, recorded in a kept ledger (`tests/.ledgers/phone-checklist.json`)
  that `tests/cleanup.py` never touches.
- `--replace` recreates it.
- `--cleanup` is the explicit cleanup step: it deletes the game, and its course if this script
  created it.
- Both delete only a game the ledger owns. For a checklist game created before ledgers existed,
  name it with `--game-id <id>`: it must be "T7 phone checklist" in the checklist course.
- A course the script didn't create, and guests who joined from phones, are reported, never
  deleted.

```bash
BUZZER_BROWSER_BASE_URL=http://<LAN address>:8080 python tests/browser/phone_checklist_setup.py
python tests/browser/phone_checklist_setup.py --cleanup [--game-id <id>]
```

For the phone to reach the stack, follow spec §4.7 (T8 §4.12):

- Open the host at `http://<LAN address>:8080/host/`, with the trailing slash. The QR code encodes
  the host page's own address.
- Add `http://<LAN address>:8080` to `CORS_ORIGINS` in `.env` and recreate the backend
  (`docker compose up -d --no-deps --force-recreate backend`). **Afterwards, restore it:** remove
  the entry, leaving the four `localhost` origins (`:8080`, `:8000`, `:5173`, `:5174`), and
  recreate the backend again. Check that a localhost page's socket still connects. `.env` is
  git-ignored; never commit it.

## What these checks don't cover

- **Real phones.** These are Chrome pages with phone viewports, touch and device pixel ratios
  emulated, not Safari on iOS or real touch hardware. The physical-phone checklist is spec §4.7;
  its results are recorded in §11.
- Other browser engines (WebKit, Firefox).
