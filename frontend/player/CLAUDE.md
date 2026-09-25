# frontend/player

## Purpose

The Player client — one of the three frontend apps. It runs on each participant's phone: join by
room code or QR, identify as a guest or signed-in student, answer questions, see your own score
and rank. It can submit answers but never advances the game.

## Contents

- `index.html` — Vite entry document. Note the viewport tag: `maximum-scale=1.0,
  user-scalable=no`, which the host and admin apps don't set. Title is plain "Buzzer" (it's the
  public-facing app).
- `package.json` — React 18 + TypeScript + Vite. Same dependency set as the host app **minus
  `qrcode.react`** — players scan QR codes, they don't render them.
- `vite.config.ts` — React plugin; `base` is `/player/` in production, `/` in dev; dev server on
  **port 5174**, proxying `/api` and `/socket.io` (with `ws: true`) to `localhost:8000`.
- `tailwind.config.ts`, `postcss.config.js` — styling config.
- `tsconfig.json`, `tsconfig.node.json` — `npm run build` runs `tsc -b` first, so type errors fail
  the build.
- [`src/`](src/CLAUDE.md) — the application. See that file for architecture and the
  `pages` / `components` / `lib` breakdown.

## How it fits in

One of three sibling apps under `frontend/` (`host` on :5173, `player` on :5174, `admin` on
:5175), each built independently and served under its own path prefix. They share no code —
`Button`, `Card`, `Input`, `TimerBar`, `cn`, and `api.ts` are duplicated per app and have already
diverged. A fix in one needs review in the other two.

This app pairs directly with the host: the host renders the QR that points here
(`/player/join?code=XXXXXX`), displays the prompt this app deliberately omits, and drives every
phase change this app reacts to.

## Running it

```bash
npm run dev          # this app alone, on :5174
npm run build        # tsc -b && vite build
npm run preview      # serve the production build locally
```

From the repo root, `npm run dev` runs all three apps concurrently and `npm run dev:player` runs
just this one. The dev proxy expects the backend at `localhost:8000`, and the backend's
`CORS_ORIGINS` must include `http://localhost:5174` — socket.io rejects the connection otherwise,
and the `config.py` default doesn't include it (your `.env` has to).

**Testing on a real phone** needs more than `npm run dev`: the Vite server binds localhost by
default, and the QR code encodes `window.location.origin` from the *host* app's browser, so a
phone on the same network can't resolve it without both dev servers listening on the LAN address.
`scripts/simulate_players.py` is the easier path for multi-player behavior.

## Gotchas

- **`base` differs between dev and production** (`/` vs `/player/`), and `App.tsx` uses
  `import.meta.env.BASE_URL` as the router `basename`. A production build served anywhere but
  `/player/` renders a blank screen.
- **Compiled config artifacts sit beside the sources and are gitignored**: `vite.config.js`,
  `vite.config.d.ts`, `tailwind.config.js`, `tailwind.config.d.ts`, `*.tsbuildinfo` — all output
  from `tsc -b`. Edit the `.ts` file; an edit to the `.js` appears to work, then gets overwritten
  and is never committed. The reverse trap is worse: **Vite and Tailwind both load the `.js`
  before the `.ts`**, so an edit to `vite.config.ts` or `tailwind.config.ts` has no effect under
  `npm run dev` until `npm run build` regenerates the `.js` (then restart the dev server).
- The dev proxy needs `ws: true` on `/socket.io`. Without it socket.io silently falls back to
  long-polling, which degrades answer-timing accuracy — the thing this app is most sensitive to.
- `user-scalable=no` blocks pinch-zoom. Deliberate (prevents accidental zoom during fast tapping),
  but an accessibility trade-off worth knowing before anyone files it as a bug.
- No tests, no lint config, no CI hook — `tsc -b` at build time is the only automated check.
- This is the only app real end users touch, and the only one that accepts unauthenticated
  entry (guest join). Its error messages and failure states deserve more care than the host or
  admin equivalents.
