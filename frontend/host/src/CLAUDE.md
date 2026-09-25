# frontend/host/src

## Purpose

The Host application: the big-screen view of a live game. It shows the room code and QR for
joining, presents each question, visualises answers as they arrive, and gives the host the
controls that drive the game forward. It is the only client that can advance game state.

## Contents

**Application-level files**

- `main.tsx` — entry point. Mounts `<App />` into `#root` inside `<StrictMode>`.
- `App.tsx` — router. Defines `/login`, `/home`, and the nested `/game/:code/*` routes
  (`lobby`, `question`, `results`, `gameover`), wraps protected routes in `RequireAuth`, and
  sends anything unmatched to `/login`. Uses `basename={import.meta.env.BASE_URL}` because the
  app is served under the `/host` path.
- `index.css` — Tailwind directives plus the dark slate background (`#0f172a`) the whole app
  assumes.
- `types/game.ts` — every socket payload and shared type: `SyncStatePayload`, `QuestionPayload`,
  `HostResultsPayload`, `HostGameOverPayload`, the `AnswerReveal` discriminated union, and
  `HostPhase`. Hand-maintained to match the backend; nothing generates it.

**Subdirectories** — each has its own `CLAUDE.md`:

- [`pages/`](pages/CLAUDE.md) — all screens, plus `GameLayout`, which owns the socket and all
  game state.
- [`components/`](components/CLAUDE.md) — presentational primitives (`Button`, `Card`, `Input`)
  and `TimerBar`.
- [`lib/`](lib/CLAUDE.md) — the REST client and the `cn` class-name helper.

## Architecture

```
main.tsx → App.tsx (routes, RequireAuth)
              └── pages/game/GameLayout.tsx ── socket.io ──→ backend gateway
                     │  owns ALL game state, provides GameContext
                     └── Lobby / Question / Results / GameOver  (read useGame())
                                    ↑
                     components/ (dumb)      lib/api.ts (REST: setup + teardown only)
```

**One socket, one state owner.** `GameLayout` opens the single socket.io connection, maps every
server event onto state, and exposes it via `useGame()`. Child pages are views: they read context
and emit exactly two actions, `emitAdvance` and `emitLockQuestion`. There is no Redux/Zustand and
no server-state library — React context is the whole store.

**The server owns the game; the client reflects it.** The host UI never decides what happens next
— it emits `host_advance` and waits for the resulting event. Routing follows suit: `new_question`,
`question_results`, and `game_over` each navigate, and `sync_state` restores the route after a
reconnect — but only the route. A refresh mid-question recovers fully; a refresh during results
or after game over does not (see [pages/CLAUDE.md](pages/CLAUDE.md)).

**Two transports, two naming conventions.** Socket payloads are camelCase (except
`sync_state.players`, below); REST responses are snake_case (`room_code`, `game_title`,
`access_token`). Both are live.

## Gotchas

### Contract drift with the backend

`types/game.ts` is hand-written and already out of step in places — it is documentation, not a
guarantee:

- `AnswerStatusPayload` declares `displayName` and `answered`; the server sends neither.
- **`SyncStatePayload.players` is typed `PlayerInfo[]` (`userId`, `displayName`), but the server
  sends raw Redis records** — `user_id`, `display_name`, `is_guest`, `score`, `is_connected`. No
  screen renders `players` today, so nothing visibly breaks; the first lobby roster will show
  `undefined` names, and `player_joined` / `player_left` (which match on `userId`) will never
  dedupe or remove players that arrived via `sync_state`.
- `LeaderboardEntry` is unused, and `game_over` gives the host anonymous scores
  (`scores: number[]`), so no host screen shows named rankings. That is a UI choice, not a data
  boundary: `sync_state.players` already delivers every player's name and running score to the
  host.
- `AnswerReveal` includes `Record<string, never>` as a member, so an unknown question type
  type-checks as an empty object rather than failing. Convenient, but it hides new types.

### Logic duplicated across the stack

Three pieces of logic exist in both this app and the backend and must be changed together:

- **Levenshtein matching** — here in `ResultsPage`, and twice in the backend.
- **Histogram bucketing** — `GameOverPage.chooseWidth`/`buildBuckets` mirrors
  `report_service`, which carries a comment pointing back at this page. They already disagree on
  rounding.
- **`multi_select` per-option correctness** — derived client-side as `answerPoints[i] > 0`,
  because the server sends raw `answerPoints` rather than `correctIndices` for this type. It
  holds only while the admin question editor keeps deriving `points_value` as the sum of the
  positive options.

### Auth

- The access token is kept in `localStorage`, read by `lib/api.ts`, written by `LoginPage`,
  cleared by `HomePage.logout`, and checked by `App`'s `RequireAuth`. There is no auth module and
  no single owner.
- `RequireAuth` checks only that a token *exists*. Expiry is never checked, the refresh endpoint
  is never called, and 401s aren't handled — an expired session renders normally and then fails on
  use.
- The OAuth2 temp token arrives in a URL fragment and is exchanged by `LoginPage`, which then
  scrubs the fragment via `history.replaceState`.

### Development behavior

- **`StrictMode` double-invokes effects in development**, so `GameLayout`'s socket effect connects,
  tears down, and reconnects on mount, emitting `join_room` twice. The cleanup handles it, but
  duplicated joins in dev logs are expected, not a bug.
- `basename={import.meta.env.BASE_URL}` means routes only resolve correctly when the app is served
  from its configured base path. Opening a built bundle at `/` will not route.

### Errors

- Any socket `error` event makes `GameLayout` replace the entire game UI with an error screen,
  cleared only when the socket reconnects — including recoverable errors such as starting a game
  that has no questions.

### Styling

- The dark background is set on `body` in `index.css`, and every component assumes a dark slate
  palette with hardcoded Tailwind colors. There is no theme token layer and no light mode.
- This app targets a projector: type scales are large (`text-8xl` room codes, `text-4xl` prompts)
  and layouts assume a wide landscape viewport. Don't optimise these screens for mobile — the
  player app is the mobile client.
