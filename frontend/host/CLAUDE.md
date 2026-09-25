# frontend/host

## Purpose

The Host client — one of the three frontend apps. It runs on the presenter's machine and is
projected onto a large screen: room code and QR for joining, the current question, live answer
visualisations, and the controls that drive the game. It is the only client permitted to advance
game state.

## Contents

- `index.html` — Vite entry document. Loads `/src/main.tsx` into `#root`.
- `package.json` — React 18 + TypeScript + Vite. Runtime deps are deliberately few:
  `react-router-dom` (routing), `socket.io-client` (the live connection), `qrcode.react` (join
  QR), `lucide-react` (icons), and `clsx` + `tailwind-merge` (the `cn` helper). No state-management
  or data-fetching library.
- `vite.config.ts` — React plugin; `base` is `/host/` in production and `/` in dev; the dev server
  runs on **port 5173** and proxies `/api` and `/socket.io` (with `ws: true`) to
  `localhost:8000`.
- `tailwind.config.ts`, `postcss.config.js` — styling config.
- `tsconfig.json`, `tsconfig.node.json` — TypeScript project references; `npm run build` runs
  `tsc -b` before `vite build`, so type errors fail the build.
- [`src/`](src/CLAUDE.md) — the application itself. See that file for architecture and the
  `pages` / `components` / `lib` breakdown.

## How it fits in

One of three sibling apps under `frontend/` (`host`, `player`, `admin`), each independently built
and served under its own path prefix. They share no code — the `Button`, `Card`, `cn`, and
`api` helpers are duplicated per app rather than extracted into a shared package. Treat a fix in
one as needing review in the other two.

All three talk to the same FastAPI backend: REST under `/api`, socket.io under `/socket.io`. This
app is socket-first (the game runs over events) and uses REST only for setup and teardown.

## Running it

```bash
npm run dev          # this app alone, on :5173
npm run build        # tsc -b && vite build
npm run preview      # serve the production build locally
```

From the repo root, `npm run dev` starts all three apps concurrently and `npm run dev:host` starts
just this one. The dev proxy assumes the backend is reachable at `localhost:8000` — under
`docker compose` that's the mapped port, so the backend must be up or every request 502s.

## Gotchas

- **`base` differs between dev and production** (`/` vs `/host/`), and `App.tsx` passes
  `import.meta.env.BASE_URL` as the router `basename`. A production build only routes correctly
  when served from `/host/`; opening it at the domain root gives a blank screen with no error.
- **Compiled config artifacts sit beside the sources and are gitignored**: `vite.config.js`,
  `vite.config.d.ts`, `tailwind.config.js`, `tailwind.config.d.ts`, `*.tsbuildinfo`. They're
  output from `tsc -b`. Always edit the `.ts` file — an edit to the `.js` looks like it works and
  is then overwritten and never committed. The reverse trap is worse: **Vite and Tailwind both
  load the `.js` before the `.ts`**, so an edit to `vite.config.ts` or `tailwind.config.ts` has no
  effect under `npm run dev` until `npm run build` regenerates the `.js` (then restart the dev
  server).
- The dev proxy needs `ws: true` on `/socket.io` for the WebSocket upgrade. Without it, socket.io
  silently degrades to long-polling and the timing-sensitive parts of the game get much worse.
- There is no test setup, no linter config, and no CI hook in this app — `tsc -b` during build is
  the only automated check.
- **UI primitives are copy-pasted across the three apps.** Divergence is the default here; if you
  change `Button` or `api.ts`, check whether `player` and `admin` need the same change.
- This app assumes a wide landscape viewport and a dark background. `index.html` still declares
  the standard mobile viewport meta tag, but no screen here is designed for phone width.
