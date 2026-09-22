# frontend

## Purpose

Three independent React + TypeScript + Vite applications, one per audience. Together they are the
entire client surface of Buzzer; there is no server-rendered UI.

## Contents

| App | Audience | Dev port | Served at | Transport |
|---|---|---|---|---|
| [`host/`](host/CLAUDE.md) | Presenter, projected | 5173 | `/host/` | socket-first |
| [`player/`](player/CLAUDE.md) | Participants, phones | 5174 | `/player/` | socket-first |
| [`admin/`](admin/CLAUDE.md) | Staff authoring & admin | 5175 | `/admin/` | REST-only |

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

**Admin is upstream of the other two.** Games and questions authored there are what a host runs;
rosters imported there are what gates a player's join.

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

## Cross-app conventions

- **Naming follows the transport, not the app.** REST payloads are `snake_case` (matching the
  backend's Pydantic schemas); socket server→client payloads are `camelCase`. The single
  client→server socket message (`submit_answer`) is `snake_case`. Admin, being REST-only, is
  `snake_case` throughout.
- **Auth is a bearer token in `localStorage`**, attached by each app's `lib/api.ts` and by the
  socket `auth` callback. Only the player app checks expiry (`isTokenExpired`).
- **One socket, owned by `GameLayout`.** In host and player, a single `GameLayout` component opens
  the connection, maps every event to state, and exposes it through a React context. Child pages
  are views. No Redux, Zustand, or query library anywhere.
- **The server owns the game.** Clients emit and react; they never decide phase transitions. Even
  routing is event-driven.
- Styling is Tailwind with hardcoded slate/indigo colours, dark-only, no theme layer.

## Gotchas

### Duplication is the defining structural fact

`Button`, `Card`, `Input`, `cn`, `api.ts`, and `TimerBar` exist in **three separate copies**, and
they have already drifted:

- Admin's `Button` / `Card` / `Input` / `utils.ts` are **byte-identical** to host's.
- Player's are the same API deliberately **retuned for touch** (larger padding, `active:scale-95`,
  16px inputs to stop iOS zoom).
- `TimerBar` differs in its actual **prop interface** — host's takes `initialSeconds`, player's
  doesn't.
- `api.ts` differs in **which verbs exist** — player has `get`/`post`, host adds `delete`, admin
  adds `put`/`patch`/`postForm`/`download`.

Treat a fix in one as needing review in the other two. Check whether a difference is deliberate
(player's touch sizing) before "resyncing" anything.

### The one bug all three share

**Every `lib/api.ts` reads `err.detail` from error responses, but the backend sends
`{"error": code, "message": ...}`.** No `detail` key exists on `BuzzerError` responses, so every
401/403/404/409 surfaces as a bare `HTTP <status>` and the server's real message is discarded.
Only 422 carries `detail`, and there it's an array that stringifies into noise. Admin repeats the
same expression three times in one file.

The impact is worst where the server's messages are most specific: guest join collisions in
player ("This email is already associated with an account"), and question validation and delete
conflicts in admin. Reading `message` first would fix all three. *Noted, not scheduled.*

### Cross-stack logic that must move together

- **Levenshtein distance** — three implementations: `rapidfuzz` in `game_service`, a hand-rolled
  one in `report_service`, and a third in host's `ResultsPage`. All three decide whether a
  fill-in-the-blank answer counts.
- **Histogram bucketing** — host's `GameOverPage` and the backend's `report_service` mirror each
  other (the backend carries a comment saying so) and already disagree on rounding.
- **Roster CSV parsing** — admin's `RosterPage` wizard reimplements `roster_service`, and they
  title-case differently, so the same student's stored name depends on the import path used.
- **`points_value` is derived by admin's question editor**, and host and player both read
  correctness off that relationship. It's an undocumented contract spanning all three apps and
  the backend.

### Shared shortcuts

- **No app has tests, lint config, or a CI hook.** `tsc -b` during build is the only automated
  check in the entire frontend.
- **Types are hand-written and drift.** Nothing generates them from the backend's OpenAPI schema;
  host's `types/game.ts` declares fields the server never sends, player's omits fields it does
  send, and admin has no `types/` at all (each page redeclares its own).
- **Route guards check only that a token string exists** — not validity, not expiry, not role.
  Admin's `RequireAdmin` doesn't verify the user is an admin.
- `StrictMode` double-invokes effects in dev, so sockets connect twice and `load()` runs twice.
  Expected.
- Gitignored build artifacts (`vite.config.js`, `tailwind.config.js`, `*.d.ts`, `*.tsbuildinfo`)
  sit beside their `.ts` sources in all three apps — always edit the `.ts`.
