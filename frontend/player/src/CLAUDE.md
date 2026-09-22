# frontend/player/src

## Purpose

The Player application: the phone client. Players join a room by code or QR, identify themselves
as a guest or a signed-in student, answer questions, and see their own result and rank. It is a
controller, not a display — it can submit answers but never advances the game.

## Contents

**Application-level files**

- `main.tsx` — mounts `<App />` into `#root` inside `<StrictMode>`.
- `App.tsx` — router: `/join`, `/login`, `/name/:code`, and the nested `/game/:code/*` routes
  (`lobby`, `question`, `feedback`, `results`, `gameover`). Unmatched paths go to `/join`.
  **There is no route guard** — `GameLayout` handles auth itself.
- `index.css` — Tailwind directives, the dark slate background, and
  `-webkit-tap-highlight-color: transparent` to kill the mobile tap flash.
- `types/game.ts` — socket payload types: `SyncStatePayload`, `QuestionPayload`,
  `AnswerResultPayload`, `PlayerResultsPayload`, `PlayerGameOverPayload`, the
  `PlayerAnswerReveal` union, and `PlayerPhase`. Hand-maintained.

**Subdirectories** — each has its own `CLAUDE.md`:

- [`pages/`](pages/CLAUDE.md) — join/identity flow and the five in-game screens, plus
  `GameLayout`.
- [`components/`](components/CLAUDE.md) — touch-tuned `Button`, `Card`, `Input`, `TimerBar`.
- [`lib/`](lib/CLAUDE.md) — REST client, `cn`, and `isTokenExpired`.

## Architecture

```
main.tsx → App.tsx (routes, no guard)
              └── pages/game/GameLayout.tsx ── socket.io ──→ backend gateway
                     │  owns ALL game state + its own auth check
                     └── Lobby / Question / Feedback / Results / GameOver
                                    ↑
                     components/ (dumb)      lib/api.ts (REST: join flow only)
```

**Five phases, one per screen.** `PlayerPhase` is `lobby | question | feedback | results |
gameover`, and each maps to a route. `feedback` is the gap between submitting and the host
revealing results — the player app has this extra state the host app doesn't.

**The player reacts; it never decides.** One outbound event (`submit_answer`) and one outbound
REST flow (joining). Everything else is server-driven, including navigation.

**Server→client payloads are camelCase; the one client→server payload is snake_case**
(`question_id`, `answer_data`, `answer_time_ms`). REST is snake_case throughout.

## Gotchas

### Correctness is computed client-side, and disagrees with the server

`ResultsPage` and `GameOverPage` both decide correctness as `points > 0`, while the server uses
`points >= pointsValue`. Any partial-credit answer shows the player a green "Correct!" that the
database records as incorrect. The server sends a real `isCorrect` on `answer_received`, but
`PlayerResultsPayload` has no field for it. This is the most user-visible defect in the app.

### Types understate the protocol

`SyncStatePayload` declares only `status`, `playerCount`, and `questionLocked`. The server also
sends `yourScore`, `currentQuestion`, and `hasAnswered` — all three are dropped. `hasAnswered` is
the consequential one: a player reconnecting mid-question after answering gets a fresh, enabled
answer UI.

`QuestionPayload.prompt` is typed and received but never rendered during a live question — a
deliberate Jackbox-style choice (see [pages/CLAUDE.md](pages/CLAUDE.md)), not a bug.

### Divergence from the sibling apps

This app shares no code with `host` or `admin`, and the copies have already drifted in ways that
matter:

- `TimerBar` here has no `initialSeconds` prop; the host's does.
- `api.ts` here has no `delete`; the host's does.
- `isTokenExpired` exists only here — the host app never checks expiry at all.
- `Button` / `Card` / `Input` are the same API with touch-scaled sizing.

The one bug they *do* share is `api.ts` reading `err.detail` when the backend sends
`{error, message}`, which discards every server error message.

### Mobile constraints

- `index.html` sets `maximum-scale=1.0, user-scalable=no`, which blocks pinch-zoom. It prevents
  accidental zoom on rapid tapping, and it is also an accessibility trade-off — users who need to
  magnify can't.
- `Input` sets an explicit `text-base` (16px) because iOS Safari force-zooms the viewport on focus
  for anything smaller. Don't shrink it.
- The timer uses wall-clock deltas, which matters here: phones background tabs and throttle
  intervals aggressively.
- Every layout is single-column `min-h-screen` centred; there is no desktop or landscape design.

### Development behavior

- `StrictMode` double-invokes effects in dev, so `GameLayout` connects, tears down, and
  reconnects on mount, emitting `join_room` twice. Expected, not a bug.
- `basename={import.meta.env.BASE_URL}` — a production build only routes correctly when served
  from `/player/`.
