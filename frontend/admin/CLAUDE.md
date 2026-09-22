# frontend/admin

## Purpose

The Admin console — one of the three frontend apps. It's the authoring and administration
surface: courses and rosters, user accounts and access grants, games and questions, guest
reconciliation, and session exports to Canvas. It never participates in a live game.

## Contents

- `index.html` — Vite entry document.
- `package.json` — React 18 + TypeScript + Vite. The **smallest dependency set** of the three
  apps: no `socket.io-client` (this app is REST-only) and no `qrcode.react`. Just
  `react-router-dom`, `lucide-react`, and `clsx` + `tailwind-merge`.
- `vite.config.ts` — React plugin; `base` is `/admin/` in production, `/` in dev; dev server on
  **port 5175**. Proxies **only `/api`** — no `/socket.io` entry, because nothing here opens a
  socket.
- `tailwind.config.ts`, `postcss.config.js` — styling config.
- `tsconfig.json`, `tsconfig.node.json` — `npm run build` runs `tsc -b` first, so type errors fail
  the build.
- [`src/`](src/CLAUDE.md) — the application. See that file for architecture and the
  `pages` / `components` / `lib` breakdown.

## How it fits in

One of three sibling apps under `frontend/` (`host` :5173, `player` :5174, `admin` :5175), built
independently and served under their own path prefixes. They share no code — `Button`, `Card`,
`Input`, `cn`, and `api.ts` are duplicated per app. This app's copies are byte-identical to the
host's except for `api.ts`, which adds `put`, `patch`, `postForm`, and `download`.

This app produces what the other two consume: the games and questions authored here are what a
host runs and a player answers, and the rosters imported here are what `authorise_player` checks
at join time. It is also the only app that talks to `/api/admin/*`, all of which requires an
`ADMIN` role.

## Running it

```bash
npm run dev          # this app alone, on :5175
npm run build        # tsc -b && vite build
npm run preview      # serve the production build locally
```

From the repo root, `npm run dev` runs all three apps concurrently and `npm run dev:admin` runs
just this one. The dev proxy expects the backend at `localhost:8000`.

**You need an admin account to see anything.** The first one comes from `ADMIN_USERNAME` /
`ADMIN_PASSWORD` in the backend's environment, created by `bootstrap_admin()` on startup — and
only if migrations have already run. `python scripts/seed_demo.py` is the quicker path to a
populated console.

## Gotchas

- **`base` differs between dev and production** (`/` vs `/admin/`), and `App.tsx` uses
  `import.meta.env.BASE_URL` as the router `basename`. A production build served anywhere but
  `/admin/` renders blank.
- **Compiled config artifacts sit beside the sources and are gitignored**: `vite.config.js`,
  `vite.config.d.ts`, `tailwind.config.js`, `tailwind.config.d.ts`, `*.tsbuildinfo`. Edit the
  `.ts`; an edit to the `.js` is overwritten by the next `tsc -b` and never committed.
- **This app has the weakest access guard and the most dangerous operations.** `RequireAdmin`
  checks only that a token string exists — not its validity or the user's role — while the pages
  behind it can delete a game along with every session and score ever recorded from it. The
  backend enforces the role properly; the UI just doesn't reflect it.
- No tests, no lint config, no CI hook — `tsc -b` at build time is the only automated check. That
  matters more here than in the sibling apps, because `QuestionEditorPage` encodes scoring
  invariants that nothing verifies.
- Desktop-only by design: fixed sidebar, wide content area, no responsive breakpoints.
