# frontend/admin

## Purpose

The Admin console — one of the three frontend apps. It's the administration surface: user
accounts and their course and game access grants, courses, and guest reconciliation. Host work
(games and questions, rosters, session exports to Canvas) moved to the host app, which admins
reach from this app's "Host tools" links. It never participates in a live game.

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
`Input`, `cn`, and `api.ts` are duplicated per app. This app's `Button`, `Card`, `Input`, and
`cn` are byte-identical to the host's, and its `api.ts` matches the host's in behavior.

This app decides who can do what in the other two: the HOST/PLAYER course grants and game grants
set here are what the host app's pages and `authorise_player` check. It is the main client of
`/api/admin/*`, all of which requires an `ADMIN` role. (The host app also calls two admin
endpoints — move and delete a game — and shows those controls only to admins.)

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
  `.ts`; an edit to the `.js` is overwritten by the next `tsc -b` and never committed. The reverse
  trap is worse: **Vite and Tailwind both load the `.js` before the `.ts`**, so an edit to
  `vite.config.ts` or `tailwind.config.ts` has no effect under `npm run dev` until
  `npm run build` regenerates the `.js` (then restart the dev server).
- **`RequireAdmin` checks the role with the server** (`GET /api/auth/me`) and shows non-admins a
  notice linking to the host app. That's display only — the backend's `require_admin` on every
  endpoint is the real guard. See [src/CLAUDE.md](src/CLAUDE.md).
- **Cross-app links differ between dev and production.** `lib/hostApp.ts` points at `/host` in a
  production build (same origin, same token) and at `http://localhost:5173` in dev (separate
  origin and token, so you sign in again). `VITE_HOST_APP_URL` overrides both. Verify end-to-end
  link behavior under nginx (`:8080`).
- No tests, no lint config, no CI hook — `tsc -b` at build time is the only automated check.
- Desktop-only by design: fixed sidebar, wide content area, no responsive breakpoints.
