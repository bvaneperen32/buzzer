# Browser checks (T7)

Playwright scripts that drive the **production builds through nginx**, with a real projector
page and phone-sized pages, for Sort It and Spot It (`docs/plans/t7-question-types.md` §4.6).
Setup goes through the REST API as the admin; every check then uses the real UI.

| Script | What it checks |
|---|---|
| `bc_editor.py` | The question editor: limits vs the server (edge-size images), default grids, too-dense drafts, authoring dimensions, tick-only reconfirmation, the Replace warning, re-selecting the current image (live lobby, prompt-only save), the live freeze and the Play gate, the real-editor round-trip matrix, answer keys, text rules, keyboard |
| `bc_sort.py` | Sort It on phones: drag, tap-tap and keyboard, lock and unlock, time-out, points display and rounding, game over, the projector ↔ report cross-check, and placements surviving a socket drop and reconnect |
| `bc_spot.py` | Spot It on a projector and three phones: coordinates and aspect, taps, nudges, rotation, keyboard, reveal, heatmap, phone texts, game over, image isolation, image failure and Retry, auto-advance, redraws, a selection surviving a socket drop and reconnect, and the two known bugs (B1, B2) |
| `bc_common.py` | Shared helpers: configuration, the REST setup client, result recording, socket observation, test images |
| `phone_checklist_setup.py` | Not a check: creates the game for the physical-phone checklist (below) |

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

   This includes the integration suite's requirements, because the scripts reuse its helpers.
4. **A browser.** By default the scripts use the installed Google Chrome (Playwright's `chrome`
   channel). Without Chrome, install Playwright's own Chromium and select it with an empty channel:

   ```bash
   python -m playwright install chromium
   export BUZZER_BROWSER_CHANNEL=
   ```

5. **An admin account** (below). Nothing in these files holds credentials.

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
```

On a local stack each takes well under a minute (about 20 seconds each on the development
machine). They have been verified one at a time; running them in parallel is untested. Each script creates its own courses, games and rooms, with a random suffix in their names. At the
end it deletes the sessions and games it created, even after a failure. The courses (`BC … (T7
checks)`) and the guest accounts stay, because the API has no course delete.

## Reading the results

Each check prints one line, and the same line is appended to `<output>/results.jsonl`. Delete that
file to start a fresh record. `<output>/shots/` holds screenshots: some for visual review (the
phone shapes, heatmaps, results) and one for every scenario that stopped with an error.

| Status | Meaning |
|---|---|
| `PASS` / `FAIL` | A T7 behaviour held / didn't. Any `FAIL` makes the script exit 1 |
| `XFAIL` | A known **pre-existing** bug (below) reproduced, as expected. It doesn't fail the run |
| `XPASS` | A known bug did *not* reproduce: it may have been fixed. Revisit its entry in the spec's §11 and the synchronization described below |

A scenario that raises is recorded as `FAIL  <scenario> ran to completion`, and the script moves on
to the next one.

## Known pre-existing bugs (spec §11; not T7's to fix)

- **B1 — every screen change re-creates the socket** (host and player `GameLayout`). An answer sent
  in the few tens of milliseconds before the new socket has re-joined its room is dropped
  silently. The scripts don't hide this behind a fixed delay. After each navigation, they wait for
  the event that ends the window: the new socket's `sync_state` (`SocketLog.rejoined`, used by
  `Host.click`, `Phone.ready` and `bc_sort.ready`). `bc_spot.py` reports B1 itself as `XFAIL`, by
  counting the sockets each page opened. Once B1 is fixed, those waits return after a 3-second
  grace period and the B1 checks turn `XPASS`: remove the waits then.
- **B2 — the host-disconnect handling never runs for a started game** (Redis keeps the room's
  status `LOBBY`). `bc_spot.py`'s host-disconnect scenario closes the projector mid-question and
  reports `XFAIL` while the phone shows no "Host disconnected" banner and keeps its Lock in button
  enabled. Until B2 is fixed, the host-disconnect behaviour of T7's lock rules can't be verified,
  in a browser or on a phone.

## The physical-phone checklist game

`phone_checklist_setup.py` creates the game for spec §4.7's checklist. It is "T7 phone checklist"
in the course "T7 phone checklist (Physical phone)", with seven questions:

- Sort It with 10 cards and 4 boxes;
- Spot It on a landscape image and on a portrait image;
- a Sort It worth 5.1 points;
- a COMPLETENESS question of each type;
- a 20-second Spot It.

The script prints which checklist items each question serves and its answer. The game is kept for
re-runs; `--replace` recreates it.

```bash
BUZZER_BROWSER_BASE_URL=http://<LAN address>:8080 python tests/browser/phone_checklist_setup.py
```

For the phone to reach the stack, follow spec §4.7 (T8 §4.12):

- Open the host at `http://<LAN address>:8080/host/`, with the trailing slash. The QR code encodes
  the host page's own address.
- Add `http://<LAN address>:8080` to `CORS_ORIGINS` in `.env` and recreate the backend
  (`docker compose up -d --no-deps --force-recreate backend`). Remove the entry and recreate the
  backend again afterwards.

## What these checks don't cover

- **Real phones.** These are Chrome pages with phone viewports, touch and device pixel ratios
  emulated, not Safari on iOS or real touch hardware. The physical-phone checklist is spec §4.7;
  its results are recorded in §11.
- Other browser engines (WebKit, Firefox).
