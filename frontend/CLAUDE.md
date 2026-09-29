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
only to run the three projects together via `concurrently`.

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
  are views. No Redux, Zustand, or query library anywhere.
- **The server owns the game.** Clients emit and react; they never decide phase transitions. Even
  routing is event-driven.
- **Any socket `error` event is treated as fatal.** Both `GameLayout`s swap the whole game screen
  for the message and a "Back to…" link, and clear it only on reconnect. The gateway uses `error`
  for routine rejections too, so a player whose tap lands just after the question auto-locks
  ("Question is locked") is ejected from the game. Separate recoverable errors before building on
  this.
- **Images arrive as server-issued links, never built by a client** (T8). A link is a
  root-relative `/api/images/{id}/v{version}/{full|small}?t=…`, signed and valid for 2–3 hours,
  loaded by a plain `<img>` with no bearer token. The host gets them from REST (the editor's
  library list; the projector's session link map, `GET /api/game/sessions/{id}/images`); phones
  get small links for option images inside `new_question` (`images`). An image id a screen
  can't resolve means "no image" — show the letter and caption, never a broken icon. Question
  `config` carries the ids (`prompt_image_id`, `option_images`) in snake_case on both transports.
- Styling is Tailwind with hardcoded slate/indigo colours, dark-only, no theme layer.

## Gotchas

### Duplication is the defining structural fact

`Button`, `Card`, `Input`, `cn`, `api.ts`, and `TimerBar` exist in **three separate copies**, and
they have already drifted. T8 adds two more copied pieces, in host and player only: `SafeImage`
(host: refreshes links and offers Retry; player: one same-link retry, no button — deliberate) and
`lib/questionImages.ts` (reading image ids out of `config`, the option labels):

- Admin's `Button` / `Card` / `Input` / `utils.ts` are **byte-identical** to host's.
- Player's are the same API deliberately **retuned for touch** (larger padding, `active:scale-95`,
  16px inputs to stop iOS zoom).
- `TimerBar` differs in its actual **prop interface** — host's takes `initialSeconds`, player's
  doesn't.
- `api.ts` differs in **which verbs exist** — player has `get`/`post`; host and admin both have
  every verb plus `postForm`/`download`, and throw an `ApiError` carrying the HTTP status. All
  three share one error-message helper (below).

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
  frontend apps and the backend.

### Shared shortcuts

- **No app has tests, lint config, or a CI hook.** `tsc -b` during build is the only automated
  check in the entire frontend.
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
