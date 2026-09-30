# frontend/host/src

## Purpose

The Host application, in two parts. The **game screens** are the big-screen view of a live game:
they show the room code and QR for joining, present each question, visualise answers as they
arrive, and give the host the controls that drive the game forward. This is the only client that
can advance game state. The **management pages**, inside a sidebar shell, are where a host
creates and edits games and questions, manages course rosters, and downloads completed sessions'
results, for the courses they HOST. Admins use them too.

## Contents

**Application-level files**

- `main.tsx` — entry point. Mounts `<App />` into `#root` inside `<StrictMode>`.
- `App.tsx` — router. `/login`; a pathless `RequireAuth` → `HostShell` layout route holding
  `/home` (Play), `/games`, `/games/:gameId`, `/rosters`, `/rosters/:courseId`, and `/sessions`;
  and the nested `/game/:code/*` routes (`lobby`, `question`, `results`, `gameover`) *outside*
  the shell. `/` and anything unmatched go to `/home` (`RequireAuth` sends signed-out users on
  to `/login`). Uses `basename={import.meta.env.BASE_URL}` because the app is served under the
  `/host` path.
- `index.css` — Tailwind directives plus the dark slate background (`#0f172a`) the whole app
  assumes.
- `types/game.ts` — every socket payload and shared type: `SyncStatePayload`, `QuestionPayload`,
  `HostResultsPayload`, `HostGameOverPayload`, the `AnswerReveal` discriminated union, and
  `HostPhase`, plus `QuestionConfig` (with T8's `prompt_image_id` / `option_images`) and the
  `images` map on `QuestionPayload`. Hand-maintained to match the backend; nothing generates it.
  T7 adds the `QuestionType` union, `GridCell`, the Sort It / Spot It config fields, `cardOrder`,
  and the two `AnswerReveal` variants.
- `types/images.ts` — T8's REST types (`ImageItem`, `LibraryResponse`, `LinkEntry`, …, mirroring
  the backend's `schemas/image.py`) and the input and import limits the UI checks before
  uploading.

**Subdirectories** — each has its own `CLAUDE.md`:

- [`pages/`](pages/CLAUDE.md) — all screens: `HostShell` and the management pages, plus
  `GameLayout`, which owns the socket and all game state, and its game screens.
- [`components/`](components/CLAUDE.md) — presentational primitives (`Button`, `Card`, `Input`,
  `SafeImage`), `TimerBar`, the editor's image components (`images/`), and T7's Sort It and
  Spot It displays (`question/`).
- [`lib/`](lib/CLAUDE.md) — the REST client (with `ApiError`, uploads, and downloads), the
  in-memory current-user context (`useMe`), the `cn` class-name helper, T8's image hooks and
  helpers (`useImageLibrary`, `useSessionImageLinks`, `questionImages`, `format`), and T7's
  `spotGrid`, `sortResults` and `canvasColors`.

## Architecture

```
main.tsx → App.tsx (routes, RequireAuth)
              ├── pages/HostShell.tsx ── GET /auth/me → MeContext, sidebar
              │      └── Play / Games / QuestionEditor / Rosters / Roster / Sessions
              │             (REST only: load → mutate → reload, via lib/api.ts)
              └── pages/game/GameLayout.tsx ── socket.io ──→ backend gateway
                     │  owns ALL game state, provides GameContext
                     └── Lobby / Question / Results / GameOver  (read useGame())
                                    ↑
                     components/ (dumb)      lib/api.ts (REST: game title only here)
```

**Two halves, deliberately separate.** The game screens stay outside the shell, so they're
full-screen on the projector and the socket opens only on game routes. The management pages are
REST-only and follow the admin app's page pattern.

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

Six pieces of logic exist in both this app and the backend and must be changed together:

- **Levenshtein matching** — here in `ResultsPage`, and twice in the backend.
- **Histogram bucketing** — `GameOverPage.chooseWidth`/`buildBuckets` mirrors
  `report_service`, which carries a comment pointing back at this page. They already disagree on
  rounding.
- **`multi_select` per-option correctness** — derived client-side as `answerPoints[i] > 0`,
  because the server sends raw `answerPoints` rather than `correctIndices` for this type. It
  holds only while this app's `QuestionEditorPage` keeps deriving `points_value` as the sum of the
  positive options.
- **Roster CSV parsing** — `RosterPage`'s wizard reimplements `roster_service`, and they
  title-case differently (see [pages/CLAUDE.md](pages/CLAUDE.md)).
- **Spot It's grid geometry** (T7) — `lib/spotGrid.ts` copies the backend's `spot_it.max_grid` /
  `default_grid` in the same integer arithmetic. The editor shows limits from it; the server's
  confirmation check is authoritative. The player app has no copy of the limits: its
  `lib/spotGrid.ts` repeats only this file's labels, readers and canvas layout.
- **Sort It's results percentages** (T7) — `lib/sortResults.ts` ports `sort_it.results_breakdown`
  (integer half-up percentages, ties to the lowest box). The report uses the Python original; a
  browser check compares the two on one session.

### Auth

- The access token is kept in `localStorage`, read by `lib/api.ts`, written by `LoginPage`,
  cleared by `HostShell` (Sign out, or a 401 from `/auth/me`), and checked by `App`'s
  `RequireAuth`. There is no auth module and no single owner.
- `RequireAuth` checks only that a token *exists*. For the management pages, `HostShell` then
  asks the server (`GET /auth/me`): a 401 signs out, and a `GUEST` token gets a notice instead of a
  page full of 403s (production's shared token makes that a real case). The game routes skip the
  shell, and the refresh endpoint is never called, so an expired token there still renders and
  then fails on use.
- The role from `/auth/me` (`useMe()`) is **display only** and held in memory. Every admin-only
  control calls an endpoint that enforces `ADMIN` server-side.
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
- The game screens target a projector: type scales are large (`text-8xl` room codes, `text-4xl`
  prompts) and layouts assume a wide landscape viewport. Don't optimise them for mobile — the
  player app is the mobile client. The shell and management pages use the admin app's dense,
  desktop sizing (224px sidebar), not projector scale.
