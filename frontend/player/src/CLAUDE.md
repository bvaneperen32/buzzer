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
  `-webkit-tap-highlight-color: transparent` to kill the mobile tap flash. T7 adds
  `.spot-height-cap` (`55vh`, then `55svh` where supported), the probe `SpotCanvas` measures for
  its height cap.
- `types/game.ts` — socket payload types: `SyncStatePayload`, `QuestionPayload`,
  `AnswerResultPayload`, `PlayerResultsPayload`, `PlayerGameOverPayload`, the
  `PlayerAnswerReveal` union, and `PlayerPhase`, plus `QuestionConfig` (T8's `prompt_image_id` /
  `option_images`) and `QuestionPayload.images` (small links for option images). Hand-maintained.
  T7 adds the `QuestionType` union, `GridCell`, `SortAnswer`, the T7 config fields, `cardOrder`,
  the two reveal variants, and the six optional T7 fields of `PlayerResultsPayload`.

**Subdirectories** — each has its own `CLAUDE.md`:

- [`pages/`](pages/CLAUDE.md) — join/identity flow and the five in-game screens, plus
  `GameLayout`.
- [`components/`](components/CLAUDE.md) — touch-tuned `Button`, `Card`, `Input`, `TimerBar`,
  `SafeImage`, and T7's answer surfaces, `SortBoard` and `SpotCanvas`.
- [`lib/`](lib/CLAUDE.md) — REST client, `cn`, `isTokenExpired`, `questionImages` (T8), and T7's
  `spotGrid`, `formatPoints`, `canvasColors` and `t7Results`.

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

**The player reacts; it never decides.** Two outbound socket events — `join_room`, sent by
`GameLayout` on every connect and reconnect, and `submit_answer` — and one outbound REST flow
(joining). Everything else is server-driven, including navigation.

**Server→client payloads are camelCase — except `sync_state.currentQuestion`, a raw snake_case
Redis record. Client→server payloads are snake_case** (`room_code` / `role`; `question_id`,
`answer_data`, `answer_time_ms`). REST is snake_case throughout.

## Gotchas

### Correctness is computed client-side, and disagrees with the server

`ResultsPage` and `GameOverPage` both decide correctness as `points > 0` for the original four
types, while the server uses `points >= pointsValue`. (T7's two types don't: their screens read
the server's `yourIsCorrect`, or the answer and reveal, from the payload.) Any partial-credit
answer shows the player a green "Correct!" that the database records as incorrect. This is the most user-visible defect in the app — and the fix for
`ResultsPage` is local: the server's authoritative `isCorrect` arrives on `answer_received` and
is already stored in `GameContext` as `answerResult`; `ResultsPage` just doesn't read it. (It's
absent after a refresh, and the game-over summary has no per-question `isCorrect` at all, so
`GameOverPage` needs a server change.)

### Types understate the protocol

`SyncStatePayload` declares only `status`, `playerCount`, and `questionLocked`. The server also
sends `yourScore`, `currentQuestion`, and `hasAnswered` — all three are dropped. Dropping
`hasAnswered` is mostly harmless, because the server only re-sends the question (the late-join
`new_question`) to players who *haven't* answered. The real reconnect gaps — stuck loading
screens after a refresh, and no way back to final results — are in
[pages/CLAUDE.md](pages/CLAUDE.md).

`QuestionPayload.prompt` is typed and received but never rendered during a live question — a
deliberate Jackbox-style choice (see [pages/CLAUDE.md](pages/CLAUDE.md)), not a bug.

### Errors eject the player

Any socket `error` event makes `GameLayout` replace the whole screen with the message and a "Back
to Join" link, cleared only on reconnect. The gateway sends `error` for routine rejections — most
often "Question is locked" when a tap lands just after the server's auto-lock (the phone's timer
isn't synced to the server, so this happens in real games). The player is effectively kicked out.

### Divergence from the sibling apps

This app shares no code with `host` or `admin`, and the copies have already drifted in ways that
matter:

- `TimerBar` here has no `initialSeconds` prop; the host's does.
- `api.ts` here has only `get` / `post`; the host's and admin's have every verb, uploads, and
  downloads. All three now share the same error-message helper (server `message`, then `detail`).
- `isTokenExpired` exists only here. The host app never checks expiry locally; it asks the server
  (`/auth/me`) when entering its management shell.
- `Button` / `Card` / `Input` are the same API with touch-scaled sizing.

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
