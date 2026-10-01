# frontend

## Purpose

Three independent React + TypeScript + Vite applications, one per audience. Together they are the
entire client surface of Buzzer; there is no server-rendered UI.

## Contents

| App | Audience | Dev port | Served at | Transport |
|---|---|---|---|---|
| [`host/`](host/CLAUDE.md) | Presenter: projected game + authoring, rosters, results | 5173 | `/host/` | socket (game) + REST (management) |
| [`player/`](player/CLAUDE.md) | Participants, phones | 5174 | `/player/` | socket-first |
| [`admin/`](admin/CLAUDE.md) | Admins: users, courses, access grants | 5175 | `/admin/` | REST-only |

Each is a complete, self-contained Vite project with its own `package.json`, `node_modules`,
Tailwind config, and `tsconfig`. Each has the same internal shape:
`src/{pages,components,lib}` plus `App.tsx` and `main.tsx`, and every one of those directories
has its own `CLAUDE.md`.

There is **no shared package, no workspace, and no root `src/`.** The root `package.json` exists
only to run the three projects together via `concurrently`. The one shared folder is
[`shared/`](shared/CLAUDE.md): the colour theme (tokens, base CSS, Tailwind preset, theme logic),
which each app imports by relative path. It is not a package and has no build step.

## Architecture

```
          ┌── host/   (:5173)  socket + REST ──┐
browser ──┼── player/ (:5174)  socket + REST ──┼──→ nginx ──→ FastAPI backend
          └── admin/  (:5175)  REST only     ──┘             /api  +  /socket.io
```

**Dev vs production serving are different mechanisms.** In development each app's own Vite server
proxies `/api` (and `/socket.io`, for host and player) to `localhost:8000`. In production the
three are built to `dist/` and mounted read-only into nginx, which serves each under its path
prefix with an SPA fallback and proxies `/api/` and `/socket.io/` to the backend. That's why
every app sets `base` to its prefix in production and `/` in dev, and passes
`import.meta.env.BASE_URL` as the router `basename`.

**Host and player are two halves of one screen.** The host renders the prompt, the QR, and the
answer distribution; the player renders only controls. Neither is complete alone, and a change to
the game's shape usually touches both.

**The host app is also where content is made.** Games and questions authored in its management
pages are what it runs, and the rosters imported there gate a player's join. Hosts do this for the
courses they HOST; admins use the same pages.

**Admin is upstream of both, through access.** The HOST/PLAYER course grants and game grants set
there decide what a host can see and run. Admin's "Host tools" sidebar links (and its per-course
roster buttons) open the host app in a new tab rather than duplicating its pages.

## Running them

```bash
npm install                # root — only installs concurrently
npm run install:all        # installs all three apps' dependencies
npm run dev                # all three, colour-prefixed (host/player/admin)
npm run dev:host           # or one at a time
npm run build              # builds all three to their dist/ folders
```

All three dev servers proxy to `localhost:8000`, so the backend must be running. For the
production path, `npm run build` must be run before nginx has anything to serve — the compose
file mounts `dist/` directories that do not exist until then.

The backend's `CORS_ORIGINS` must list every dev port that opens a socket (`:5173` host, `:5174`
player): socket.io checks the browser's `Origin`, and the Vite proxy passes it through unchanged.
The `config.py` default omits `:5174`, so the player socket works in dev only if `.env` adds it.

## Cross-app conventions

- **Naming follows the transport, not the app.** REST payloads are `snake_case` (matching the
  backend's Pydantic schemas); socket server→client payloads are `camelCase` — **except parts of
  `sync_state`**: the host's `players` array and the player's `currentQuestion` are raw Redis
  records in `snake_case` (see the backend's
  [websocket/CLAUDE.md](../backend/app/websocket/CLAUDE.md)). Every client→server socket message is
  `snake_case`: `join_room` (`room_code`, `role`), `submit_answer`, and the host's payload-less
  `host_advance` / `host_lock_question`. Admin, being REST-only, is `snake_case` throughout.
- **Auth is a bearer token in `localStorage`**, attached by each app's `lib/api.ts` and by the
  socket `auth` callback. Only the player app checks expiry (`isTokenExpired`). **All three apps
  use the same key, `token`, and in production they share one origin** (nginx serves `/host/`,
  `/player/`, and `/admin/` from `:8080`) — so one browser holds one token for all three. Signing
  in anywhere signs every app in that browser in as that user: the player's "Play Again" signs the
  host out, and a guest joining from the host's machine replaces the host's token with a guest's.
  The host app's shell and the admin console check `GET /api/auth/me` and show a notice (guest, or
  not an administrator) instead of a page full of 403s; the role is display only and never
  stored. Dev hides all of this, because each Vite port is a separate origin — which is also why
  admin's links into the host app arrive signed in only under nginx.
- **One socket, owned by `GameLayout`.** In host and player, a single `GameLayout` component opens
  the connection, maps every event to state, and exposes it through a React context. Child pages
  are views. No Redux, Zustand, or query library anywhere. The socket lives for the whole game:
  its effect depends only on the room code, and handlers navigate through a ref, because React
  Router rebuilds `navigate` on every screen change (B1; each app's `pages/CLAUDE.md`).
- **The server owns the game.** Clients emit and react; they never decide phase transitions. Even
  routing is event-driven.
- **Any socket `error` event is treated as fatal.** Both `GameLayout`s swap the whole game screen
  for the message and a "Back to…" link, and clear it only on reconnect. The gateway keeps
  `error` for malformed requests and authorization/session failures.
  - **Recoverable outcomes use their own events.** A late answer (a tap just after the question
    locked or moved on) is `answer_rejected`: the phone keeps its screen and says the answer
    didn't count. Results held back while an answer is still being recorded are
    `results_delayed`: the projector says so, and Show Results can be pressed again.
  - **A lost connection is not an error.** Once a socket has connected, a disconnect or failed
    retry shows a "Connection lost — reconnecting…" banner over the mounted screen, until the
    server confirms the rejoin (`sync_state`). Until then the projector's controls are
    disabled.
- **Images arrive as server-issued links, never built by a client** (T8). A link is a
  root-relative `/api/images/{id}/v{version}/{full|small}?t=…`, signed and valid for 2–3 hours,
  loaded by a plain `<img>` with no bearer token. The host gets them from REST (the editor's
  library list; the projector's session link map, `GET /api/game/sessions/{id}/images`); phones
  get small links for option images inside `new_question` (`images`). An image id a screen
  can't resolve means "no image" — show the letter and caption, never a broken icon. Question
  `config` carries the ids (`prompt_image_id`, `option_images`) in snake_case on both transports.
- **Styling is Tailwind over shared colour tokens, in light and dark** (T9). Every colour lives in
  `shared/theme/tokens.css`: Radix Colors primitives, with role-named semantic tokens on top. The
  shared preset replaces Tailwind's palette, so classes read `bg-surface`, `text-primary`,
  `border-input`, and `bg-slate-800` or `text-white` no longer exist. `<html data-theme>` selects
  the theme. An inline script in each `index.html` sets it before first paint, and each app's
  fixed top-right `ThemeToggle` changes it and stores the choice under the app's own key
  (`buzzer-theme-host` / `-player` / `-admin`). Focus comes from one global `:focus-visible`
  outline in `base.css`, so no component carries `focus:ring` or `outline-none` classes. See
  [shared/CLAUDE.md](shared/CLAUDE.md). T7's Spot It canvases can't use classes: they read the
  tokens at draw time (`lib/canvasColors.ts`) and redraw when `observeTheme` reports a change.

## Gotchas

### Duplication is the defining structural fact

`Button`, `Card`, `Input`, `cn`, `api.ts`, and `TimerBar` exist in **three separate copies**, and
they have already drifted. T8 adds two more copied pieces, in host and player only: `SafeImage`
(host: refreshes links and offers Retry; player: one same-link retry, no button — deliberate) and
`lib/questionImages.ts` (reading image ids out of `config`, the option labels). T7 adds
`lib/spotGrid.ts` and `lib/canvasColors.ts` to both, and neither pair is a full copy. The host's
`spotGrid.ts` holds the grid limits (`maxGrid` / `gridFits` / `defaultGrid`, copied from the
backend's `spot_it`, the authority) plus labels, readers, canvas layout, heat steps and the
editor's texts; the player's repeats only the labels, readers and canvas layout, with no limits.
Each `canvasColors.ts` maps only its own app's canvas colours to tokens; the small `token()`
reader in each is a deliberate copy. How the older copies differ:

- Admin's `Button` / `Card` / `Input` / `utils.ts` are **byte-identical** to host's.
- Player's are the same API deliberately **retuned for touch** (larger padding, `active:scale-95`,
  16px inputs to stop iOS zoom).
- `TimerBar` differs in its actual **prop interface** — host's takes `initialSeconds`, player's
  doesn't.
- `api.ts` differs in **which verbs exist** — player has `get`/`post`; host and admin both have
  every verb plus `postForm`/`download`, and throw an `ApiError` carrying the HTTP status. All
  three share one error-message helper (below).
- `ThemeToggle` (T9) has three copies that differ only in their storage key, which must match
  that app's `index.html` script, and in player's larger 44px touch target. The logic behind them
  is shared (`shared/theme/theme.ts`).

Treat a fix in one as needing review in the other two. Check whether a difference is deliberate
(player's touch sizing) before "resyncing" anything.

### Error messages: one rule, three copies

The backend sends three error shapes: `{"error", "message"}` for app errors (including the 422
`UNPROCESSABLE`), `{"detail": "..."}` from a plain HTTPException, and `{"detail": [{msg, …}]}`
for request validation. Every `lib/api.ts` now applies the same rule: use `message` if it's a
string, else a string `detail`, else join the array's `msg`s with `"; "`, else `HTTP <status>`.
(They used to read only `detail`, which discarded every server message.) It's still three copies
of the rule — change one, change all three.

The one path it doesn't cover is the hand-rolled `fetch` for `/api/auth/exchange-temp` in the host
and player `LoginPage`s, which still reads only `detail`. *Noted, not scheduled.*

### Cross-stack logic that must move together

- **Question types** — adding one touches the host (game screens *and* the question editor) and
  the player, each switching on type in several files. The one checklist is in the backend's
  [app/CLAUDE.md](../backend/app/CLAUDE.md).
- **Option labels** — every screen keeps its own format (host results `A  Paris`, host game over
  `A.` then the caption, phone `A — Paris`, report `A Paris` with an EN SPACE), and since T8 all
  of them show the letter alone when the caption is empty. Change a format in one place and the
  others stay as they are.
- **Levenshtein distance** — three implementations: `rapidfuzz` in `game_service`, a hand-rolled
  one in `report_service`, and a third in host's `ResultsPage`. `rapidfuzz` decides the score; the
  other two decide what is *shown* as correct (the report's and the host's word clouds), so a
  mismatch makes the display disagree with the score.
- **Histogram bucketing** — host's `GameOverPage` and the backend's `report_service` mirror each
  other (the backend carries a comment saying so) and already disagree on rounding.
- **Roster CSV parsing** — host's `RosterPage` wizard reimplements `roster_service`, and they
  title-case differently, so the same student's stored name depends on the import path used.
- **`points_value` is derived by host's question editor**, and host's game screens and the player
  both read correctness off that relationship. It's an undocumented contract spanning the
  frontend apps and the backend. (T7's two types are the exception: the author types their
  points value in both grading modes.)
- **Spot It's grid limits** (T7) — two implementations in the same integer arithmetic: the
  backend's `spot_it.max_grid` / `default_grid` (the authority; the server's confirmation check
  decides) and the host's `lib/spotGrid.ts`, which gives the editor its limits and default grid. A
  browser check compares the editor's limits with the server's on edge-size images. The player
  has no limits: its `lib/spotGrid.ts` copies only the host's labels, readers and canvas layout,
  so a change to those belongs in both apps.
- **Sort It's results percentages** (T7) — `sort_it.results_breakdown` (the report) and the host's
  `lib/sortResults.ts` (ResultsPage and GameOverPage): integer half-up percentages, ties to the
  lowest box, "—" with no answers. A browser check compares the projector with the report.
- **T7's points display** — the phone's `lib/formatPoints.ts` (`Intl.NumberFormat`, the viewer's
  locale) and the report's `format_points` (`Decimal(repr(x))`, half up) agree because both round
  the value's shortest decimal representation (0.15 → "0.2", 2.55 → "2.6"). Switching the report
  to `Decimal(x)` would break that; unit tests pin it.

### The shared theme

Two hard rules, both verified while designing T9 (spec §2.3):

- **No file in `shared/` may import a package** (not `react`, not `tailwindcss` types). There is no
  `node_modules` above it, so the import can't resolve. That is why `preset.d.ts` is hand-written
  and why the React toggle lives in each app.
- **CI does not protect the shared set-up.** CI's `tsc --noEmit` passes shared-file set-ups that
  fail `tsc -b`, so a wrong variant breaks only `npm run build`. Run `npm run build` in all three
  apps before merging a change to `shared/` or to an app's `tailwind.config.ts` or `tsconfig`.

Also: a class the preset doesn't define (a leftover `bg-slate-800`, a typo) builds fine and
renders with no colour. And a page with correct page colours but colourless buttons, bars and
status text is a stale compiled `tailwind.config.js` or a stale tab: restart the dev server, then
hard-refresh. `python scripts/check_theme.py` checks contrast, token names and leftovers.

### Shared shortcuts

- **No app has tests or lint config.** CI (`.gitlab-ci.yml`) runs `tsc --noEmit` in each app;
  `tsc -b` during `npm run build` is stricter, and `python scripts/check_theme.py` checks colours.
- **Types are hand-written and drift.** Nothing generates them from the backend's OpenAPI schema;
  host's `types/game.ts` declares fields the server never sends, player's omits fields it does
  send, and admin has no `types/` at all (each page redeclares its own).
- **Route guards mostly check only that a token string exists.** The exceptions are host's
  `HostShell` and admin's `RequireAdmin`, which ask `/api/auth/me` on entry (401 → sign-in page;
  guest or non-admin → a notice). Host's game routes and the whole player app still don't check
  validity or role up front; the player checks expiry locally.
- **"Save, then reload" can show stale data for a moment.** The backend commits many writes just
  *after* responding (see the backend's [routers/CLAUDE.md](../backend/app/routers/CLAUDE.md)),
  so the `await load()` that host and admin pages run after a mutation can read the old state.
  *Noted, not scheduled.*
- `StrictMode` double-invokes effects in dev, so sockets connect twice and `load()` runs twice.
  Expected.
- **Typing an app's address can fail under nginx** (`nginx/nginx.dev.conf`, found during T8's
  phone check). These are two separate follow-ups, *noted, not scheduled*; detail is in
  `docs/plans/t8-image-support.md` §5.
  - `/player`, `/host` and `/admin` without the trailing slash return 404, yet the host lobby tells
    players to type `<address>/player`.
  - The bare `http://<address>:8080/` redirects to port 80 (`http://<address>/player/`), because
    nginx builds the redirect from its own container port.
  - The QR code works, because it links to `/player/join?code=…`.
- Gitignored build artifacts (`vite.config.js`, `tailwind.config.js`, `*.d.ts`, `*.tsbuildinfo`)
  sit beside their `.ts` sources in all three apps, emitted by `tsc -b`. Always edit the `.ts` —
  but **Vite 5 loads `vite.config.js` before `vite.config.ts`, and Tailwind 3 loads
  `tailwind.config.js` before its `.ts`**. A `.ts` edit therefore has no effect under
  `npm run dev` until `npm run build` regenerates the `.js`; restart the dev server afterwards.
