# frontend/host/src/pages

## Purpose

Every screen the host sees, plus the component that owns the live game connection. This is where
the host app's state and behavior live — the rest of `src/` is plumbing and presentation.

## Contents

- `LoginPage.tsx` — two sign-in paths: a link to UW NetID SSO, and a username/password form for
  admin and local accounts. Also handles the OAuth2 *return*: it reads the temp token from the URL
  fragment (`#oauth2_data=`), POSTs it to `/api/auth/exchange-temp`, stores the access token, and
  scrubs the fragment from history.
- `HomePage.tsx` — post-login landing. Lists resumable `LOBBY`/`IN_PROGRESS` sessions with
  Rejoin and two-step Delete, and offers course + quiz selectors to create a new room. Owns
  logout.
- `game/GameLayout.tsx` — **the hub.** Opens the one socket.io connection, registers every event
  handler, holds all game state, and exposes it through `GameContext` / `useGame()`. Renders an
  `<Outlet />` plus a persistent QR + room-code panel. Also drives routing: incoming events
  navigate between the child routes.
- `game/LobbyPage.tsx` — pre-game screen: large QR code and room code, live player count, the
  auto-advance toggle, and Start Game.
- `game/QuestionPage.tsx` — live question: metadata line, `TimerBar`, prompt, answered counter,
  and the Lock / Show Results buttons. The prompt is hidden while the question is locked (see
  Gotchas).
- `game/ResultsPage.tsx` — per-question results. Bar chart for choice-style questions, word cloud
  for fill-in-the-blank, and the auto-advance countdown.
- `game/GameOverPage.tsx` — final screen: score histogram, summary stats, and a per-question
  breakdown card list.

## How it fits in

`GameLayout` is the only stateful piece; the four `game/*` pages are presentational views over
`useGame()` and emit exactly two actions (`emitAdvance`, `emitLockQuestion`). Adding host state
means adding it to the context, not to a child page.

The socket is the primary transport. REST is used only for setup and teardown — listing courses
and games, creating a room, fetching the game title, deleting a session.

**Routing is event-driven.** The server's events decide the route: `new_question` navigates to
`/question`, `question_results` to `/results`, `game_over` to `/gameover`, and `sync_state`
restores the correct route on reconnect. Phase state and URL are kept in lockstep deliberately —
but `sync_state` carries no results or game-over data, so recovery after a refresh is partial:

- mid-question — recovers fully (the question payload includes `startedAt`);
- during results — lands on `/results` with `questionResults` null: heading and Next button, no
  chart;
- after game over — `sync_state` reports `COMPLETED`, which isn't handled, and `GameOverPage`
  shows "Loading final results…" indefinitely;
- in every case `answeredCount` restarts at 0 and `autoAdvance` resets to off.

## Gotchas

### Rendering and duplication

- **Question prompts render as plain text, so the backend's sanitized formatting never appears.**
  The API deliberately permits `<b>`, `<i>`, `<u>`, and `<br>` in prompts (bleach's allowlist),
  but every render site here interpolates `{prompt}` as a JSX string, which React escapes. An
  author who bolds a word sees the literal `<b>` tags on the projector. Either the allowlist or
  the rendering is wrong; they currently disagree.
- **`QuestionPage` hides the prompt whenever the question is locked** (`{!questionLocked &&
  <h2>…}`). The server auto-locks 0.5s after the timer expires, and also when the host
  disconnects, so the projector loses the question text at exactly the moment the room is
  discussing it. It may be deliberate, but nothing in the code says so.
- **The QR code and room code render twice on the lobby.** `GameLayout` draws a fixed
  bottom-right join panel on *every* child route, and `LobbyPage` draws its own large QR and code
  in the page body. On the lobby screen both are visible at once.
- **`ResultsPage` ships a third copy of Levenshtein distance.** The backend has two already
  (`rapidfuzz` in `game_service`, a hand-rolled one in `report_service`); this is the third
  implementation of the same fuzzy-match rule that decides whether a word-cloud entry is green.
  All three must agree or the host sees a word marked correct that wasn't scored correct.
- **`GameOverPage`'s histogram bucketing is a port of the backend's.** `chooseWidth` /
  `buildBuckets` mirror `report_service._choose_bin_width` / `_build_histogram` line for line —
  the backend even carries a comment saying it mirrors this page. Change one and the exported HTML
  report stops matching the screen.
- The two views disagree on rounding already: `GameOverPage` shows `Math.round(avg)` (whole
  points) while the backend report rounds to one decimal.
- `GameOverPage`'s per-question cards show accepted answers for fill-in-the-blank but **no
  response distribution** — the word cloud exists only on `ResultsPage`. FITB data silently
  disappears from the final breakdown.

### Correctness semantics

- **"Correct" is expressed two different ways across question types.** For `multiple_choice` the
  server sends `correctIndices` and the client trusts it; for `multi_select` the server sends the
  raw `answerPoints` array and each client decides per option with `answerPoints[i] > 0`. That
  asymmetry is deliberate — `multi_select`'s `points_value` is the *sum* of the positive options
  (see [admin's QuestionEditorPage](../../../admin/src/pages/CLAUDE.md)), so a
  `points >= points_value` rule can't express "this single option is correct". The cost is that
  the `> 0` rule is duplicated in `ResultsPage.buildBars` and `GameOverPage.isCorrectIndex`, and
  it only holds while the editor keeps deriving `points_value` that way.
- The word cloud renders raw player-typed text at large size on a projector. Players control that
  string entirely — the same exposure the backend's HTML report has.

### State and lifecycle

- **`GameLayout`'s socket effect depends on `[code, navigate]`.** It tears down and reopens the
  connection if either identity changes; keep `navigate` stable (it is, in React Router v6) or the
  game reconnects on every render.
- The `answer_phase_ended` handler filters on `currentQuestionIdRef` to drop stale events. This
  guards against the backend's orphaned-timer race — don't remove it.
- Auto-advance is driven from two different pages with different timings: `QuestionPage` waits
  1500ms after the answer phase ends, `ResultsPage` counts down 10s. The toggle that enables it
  is only reachable on `LobbyPage`, so it cannot be changed once the game starts — and since it's
  plain component state, any refresh turns it off for the rest of the game.
- `players` in `GameLayout` is filled from `sync_state` in the wrong shape (snake_case — see
  [src/CLAUDE.md](../CLAUDE.md)) and isn't rendered anywhere yet. Separately, `player_left`
  removes a player from the list on *disconnect*, while the server's `playerCount` still includes
  them (they stay in the Redis set), so a roster built from `players` and the count will disagree.
- Any `error` event replaces the whole game UI (`GameLayout` returns an error screen) until the
  socket reconnects — including recoverable errors such as starting a game with no questions.
- `RequireAuth` (in `App.tsx`) only checks that a token string exists. An expired token passes the
  guard, renders the page, and then fails on the first request.
- `LoginPage`'s OAuth2 effect runs once with an eslint-disabled empty dep array and reads
  `window.location.hash` directly rather than through the router.
- `GameLayout` fetches the game title with `.catch(() => {})` — a failure leaves the title blank
  with no error shown, which is intentional but means a broken room fetch is invisible here.

### Types

- `types/game.ts` overstates two payloads. `AnswerStatusPayload` declares `displayName` and
  `answered`, but the server's `ANSWER_STATUS` emits only `userId`, `answeredCount`, and
  `totalPlayers` — both extra fields are always `undefined`. `LeaderboardEntry` is unused
  entirely; `game_over` gives the host an anonymous `scores: number[]`, so no host screen shows a
  named leaderboard — though `sync_state.players` does carry names and scores.
- `SyncStatePayload.players` is typed camelCase (`userId`, `displayName`), but the server sends
  snake_case Redis records (see [src/CLAUDE.md](../CLAUDE.md)).
- `QuestionPayload.startedAt` is present only in `sync_state` reconnect payloads, never in
  `new_question`. `QuestionPage` handles both cases when computing remaining time.
