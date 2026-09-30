# frontend/player/src/pages

## Purpose

Every screen a player sees on their phone: joining a room, identifying themselves, answering
questions, and reviewing results. Plus `GameLayout`, which owns the socket and all game state.

## Contents

**Entry flow**

- `JoinPage.tsx` — room-code entry. Uppercases input, requires 6 characters, and verifies the
  room with `GET /game/rooms/{code}/ping` before advancing. **Auto-submits** when loaded with
  `?code=` — that's the QR-scan path from the host screen.
- `NamePage.tsx` — identity. Three modes: `guest` (display name + email → `/auth/guest`), `netid`
  (UW SSO, stashing the room code in `sessionStorage` first), and `local`
  (username/password). Pre-fills name and email from `localStorage`, and collapses to a single
  "Join Game" button when a valid token is already present. Under that button, **"Use a different
  account"** removes the token and shows the sign-in modes again (Guest selected, as on a fresh
  visit).
- `LoginPage.tsx` — OAuth2 return handler only, not a form. Reads the temp token from the URL
  fragment, exchanges it, recovers `joinRoomCode` from `sessionStorage`, and redirects to
  `/name/{code}`.

**In-game** (all under `game/`, children of `GameLayout`)

- `GameLayout.tsx` — the hub: opens the socket, maps events to state, provides `GameContext`
  (including `pendingAnswerId`, the answer on its way), and renders a fixed banner when the host
  has disconnected or the phone's own connection is lost.
- `LobbyPage.tsx` — room code, spinner, player count. Message differs for `LOBBY` vs
  `IN_PROGRESS` (late joiner waiting for the next question).
- `QuestionPage.tsx` — the answer UI, with a separate branch per question type: colour-coded
  lettered tiles for `multiple_choice`, two large buttons for `true_false`, a text field for
  `fill_in_the_blank`, and a toggle list with an explicit Submit for `multi_select`. T8: when any
  multiple-choice or multi-select option has an image in `currentQuestion.images`, the options
  render as 2-column tiles (`OptionTiles`): letter badge, the small image (`SafeImage`), caption if
  any. Tapping works exactly as before — one tap submits multiple choice; multi-select toggles,
  then Submit. An option whose id isn't in `images` is a text-only tile. Each image tile's
  accessible name is "Option B: {caption}. Image: {description}" (or without the caption).
  T7: Sort It renders `SortBoard` and Spot It `SpotCanvas`, each keyed by `questionId`, so a
  lock, an unlock or a socket reconnect's late-join `new_question` never clears the player's
  placements or selection — they only toggle `disabled`. Nothing is ever submitted
  automatically; a board never submitted shows "No answer" at results.
- `FeedbackPage.tsx` — static "Answer locked in" screen with a spinner.
- `ResultsPage.tsx` — per-question outcome: correct/incorrect, what you answered, points, running
  total, and rank. T7 questions render from the `question_results` payload alone
  (`lib/t7Results.ts`: "6 of 8 correct, 1 left out", "You picked B3 — Correct", "No answer",
  points as "687.5 of 1,000 points"), so they're right after a reload between answering and
  results.
  - Every type reads **"No answer"** from the payload's `answered: false`: an answer refused as
    late, dropped after a reconnect, or never given. It then shows no "You answered" line.
  - Neither what the phone sent (`lastAnswerData`) nor zero points can tell: a wrong answer, or
    a completeness question worth 0, still counted.
  - The container carries `data-results` (the question's type) for the browser checks.
- `GameOverPage.tsx` — final rank and score, plus a per-question recap with your answer and the
  correct one. T7 questions get one line each ("Sort It — 6 of 8 correct, 1 left out · 687.5 /
  1,000 pts", "Spot It — You picked B3 — Correct · Correct cells: B3, C3 · 1,000 / 1,000 pts").

## How it fits in

`GameLayout` is the only stateful component; every `game/*` page is a view over `useGame()`.
Pages emit exactly one thing — `submit_answer`, via `emitAnswer` — and `GameLayout` sends
`join_room` on every connect and reconnect; everything else reacts to server events. Routing
is event-driven: `new_question` → `/question`, `answer_received` → `/feedback`,
`question_results` → `/results`, `game_over` → `/gameover`.

There is no `RequireAuth` wrapper here (unlike the host app). `GameLayout` does its own token
check with `isTokenExpired` and redirects to `/name/{code}`, which preserves the room the player
was trying to join.

## Gotchas

### One socket per game; an answer waits until the socket has re-joined

*B1, fixed with B2 (`docs/plans/fix-live-session-test-cleanup.md`).*

- **Screen changes keep the socket.** `GameLayout`'s socket effect depends only on `code`.
  Handlers navigate through `navigateRef`, because React Router 6.30 rebuilds `navigate` on every
  pathname change.
  - With `navigate` in the dependencies, every screen change re-created the socket. An answer
    sent before the new socket had re-joined was dropped by the server, while the phone said
    "Answer submitted".
  - Cleanup removes every listener before disconnecting.
- **Readiness** (`readyRef`) is true only between `sync_state` and the next `connect` or
  `disconnect`. The server sends `sync_state` once it has seated the socket in the room.
  - Before that, an answer would be lost: socket.io flushes what it buffered while disconnected
    *before* the `connect` handler's `join_room`.
  - The server handles one socket's events concurrently, so even an answer sent right after
    `join_room` can overtake it.
- **The pending answer** (`pendingRef`, exposed as `pendingAnswerId`). `emitAnswer` stores the
  answer and sends it only while the socket is ready; otherwise the next `sync_state` sends it.
  That `sync_state` also settles it:
  - the server already has it (`hasAnswered`; only the reply was lost) → treated as accepted;
  - the question is locked, or has moved on → dropped, and the page shows the lock;
  - otherwise → sent.
- **It clears on `answer_received`** (or its `alreadyAnswered` echo, which then also counts as
  accepted), on `answer_rejected`, on results, on a different question, and on `error`.
- **What the phone sent is forgotten when it didn't count** (`forgetAnswer`). `lastAnswerData`
  (the results' "You answered …") is cleared when the server refuses the answer
  (`answer_rejected`), when a pending answer is dropped on the rejoin, or on `error`.
  - Only for that question (`lastAnswerQuestionRef`), and never for an answer the server
    accepted: a refusal of a stale answer to an earlier question leaves the current question's
    accepted answer alone.
  - The results' "No answer" doesn't rely on this; it comes from the server's `answered`.
- **A late answer is rejected, not an error.** The server sends `answer_rejected` (`"locked"`
  or `"closed"`) when the answer arrives after the question locked or moved on.
  - The phone keeps its screen and marks the question closed (`questionLocked`), with the note
    "Too late — answers had already closed, so that answer didn't count."
    (`rejectedAnswerId`).
  - The note clears on an unlock (the player may answer again) or the next question.
  - Only a genuine failure, such as a malformed answer or a refused join, still gets the error
    screen.
- **Only the server's acknowledgement shows "Answer locked in!".** While an answer is pending,
  the question page says "Sending your answer…". `QuestionPage`'s `submitted` is
  `pendingAnswerId === questionId`, no longer local state.
- **A repeated question is ignored once answered.** After a reconnect the server re-sends the
  open question (late-join `new_question`) to a player it has no answer from. That repeat can
  arrive after the acknowledgement of the answer just re-sent, so it is ignored for a question
  already accepted.
  - A repeat for an unanswered question keeps the pending answer and `lastAnswerData`.
  - T7's boards keep their state because they are keyed by `questionId`.
- **A lost connection shows "Connection lost — reconnecting…"** as a banner, and the page
  underneath stays mounted.
  - A failed retry used to hit the fatal error screen. That unmounted the page, lost a board's
    placements or selection, and offered "Back to Join".
  - The banner shows only while socket.io will retry (`sock.active`), and stays until the
    server confirms the rejoin (`sync_state`), not just until the socket connects. A first
    connection that fails, or one the server refuses, still gets the error screen.

### The phone deliberately doesn't show the question

**`QuestionPage` never renders `currentQuestion.prompt`.** All four branches render only the
question number, timer, and answer controls. This is the Jackbox model — the prompt is on the
projector, the phone is a controller — and `GameOverPage` *does* render prompts in the recap, so
the omission is a deliberate live-screen decision rather than a missing line. The field is sent
and typed, so it looks like an oversight. Don't "fix" it without checking the intent.

### Images on the phone (T8)

- **The phone shows option images only, never the prompt image** — the projector shows that,
  as it shows the prompt. Links arrive in `new_question` (`images`), and a failed image is retried
  once with the same link, then shows "Image unavailable" with its description; the option stays
  answerable by its letter.
- Results and the game-over recap stay text: `A — Paris`, or the letter alone when the option has
  no caption (`choiceLabel`).

### "Correct" is derived locally, and gets it wrong

- For an answer the server has (`answered`), `ResultsPage` computes `isCorrect = yourPoints > 0`,
  and `GameOverPage` uses `pointsAwarded > 0`. The server's own `is_correct` uses
  `points >= pointsValue`. **Any partial-credit answer therefore displays a green "Correct!" to
  the player while the server recorded it as incorrect** — reachable today with `multi_select`
  and with per-option `answer_points` on multiple choice. (Whether there *is* an answer is the
  server's `answered` now; only right-versus-wrong is still derived.)
- The server's true `isCorrect` (from `answer_received`) is already in context as
  `answerResult.isCorrect`; `ResultsPage` just doesn't use it. The game-over summary has no
  per-question `isCorrect`, so making `GameOverPage` authoritative needs a server change.

### `multi_select` gaps

- `ResultsPage.describeAnswer` has no `multi_select` branch and returns `null`, so those players
  never see what they picked on the per-question results screen. `GameOverPage.describePlayerAnswer`
  *does* handle it — the two "describe my answer" helpers are near-duplicates that disagree.
- `describeCorrectAnswer` marks a `multi_select` option correct when `answerPoints[i] > 0`. That
  one is *intended* — the server sends raw `answerPoints` for this type precisely so clients can
  decide per option, because `points_value` is the sum of the positive options. The host app does
  the same. It's duplicated logic, not a wrong rule.

### State and reconnection

- **`player_left` is never handled.** `playerCount` updates on `player_joined` and `sync_state`
  only, so the count drifts upward as players leave and only corrects on reconnect. The host app
  handles this event; this one doesn't.
- **`sync_state` doesn't restore the screen.** Its `hasAnswered` and `currentQuestion` only
  settle a pending answer (above), and for `IN_PROGRESS` it doesn't navigate — it relies on the
  server's late-join `new_question`, which is sent only to players who **haven't** answered. A
  pending answer lives in memory, so a page refresh loses it (the phone then shows the question
  again). After a page refresh:
  - mid-question, not yet answered — recovers (a fresh question with the remaining time);
  - mid-question, already answered — usually on `/feedback`, which is static and fine; if the
    `answer_received` never arrived, on `/question` showing "Loading question…" until the next
    question;
  - during results — "Loading results…" until the next question (`sync_state` has no results);
  - after game over — the server rejects `join_room` for a `COMPLETED` game, so the player gets
    the error screen and can't see their final results again.
- A host disconnect mid-question locks the question (`question_locked`) and shows the "Host
  disconnected" banner. The host's return unlocks it, unless the host had locked it by hand. See
  the backend's [websocket/CLAUDE.md](../../../../backend/app/websocket/CLAUDE.md).
  - The banner clears on `host_reconnected`, even when a manual lock keeps the question closed.
  - A phone that reconnects meanwhile takes the host's absence from `sync_state`
    (`hostDisconnected`).
  - `new_question` also clears it.
  - The phone's own "reconnecting" banner takes precedence while it's shown.
- Any socket `error` replaces the whole screen (see [src/CLAUDE.md](../CLAUDE.md)). Late taps
  are `answer_rejected` instead (above).
- `QuestionPage`'s `startTimeRef` is initialised at mount, so `answer_time_ms` measures from when
  the page rendered, not from when the server started the question. Network latency is included,
  and after a reconnect the elapsed time restarts from zero.
- `QuestionPage`'s `submitted` comes from `GameLayout` (`pendingAnswerId`), and `emitAnswer`
  ignores a second answer to a question whose answer is already pending. Those two are the only
  client-side guard against a double submit.
- `GameLayout` deliberately **disconnects the socket on `game_over`** and uses `phaseRef` to
  suppress the error banner and reconnect attempts afterwards — otherwise the teardown surfaces
  as a spurious "Connection error" on the final screen.

### Auth flow

- `NamePage`'s `isAuthenticated` is state, initialised from the token once at mount, so "Use a
  different account" can re-render the page after removing the token. It still doesn't notice a
  token changed by another tab.
- **A still-valid guest token skips `/auth/guest`** and goes straight to the socket. For a room
  with guests turned off, `authorise_player` then refuses it with a fatal `error`, and "Back to
  Join" returns to the same collapsed page. "Use a different account" is the way out to NetID
  sign-in. It's a plain link rather than special handling of that one error message, which would
  be brittle.
- The NetID path depends on `sessionStorage.joinRoomCode` surviving the OAuth2 round trip. If it
  doesn't, `LoginPage` falls back to `/join` and the player re-enters the code.
- `GameOverPage`'s "Play Again" clears the token outright, signing out NetID and local users as
  well as guests.
- `JoinPage` shows one generic "Room not found" for every failure, including a room that exists
  but has already ended — the server distinguishes these (404 vs 409) but the message doesn't.

### Submission shape

`emitAnswer` sends **snake_case** keys (`question_id`, `answer_data`, `answer_time_ms`), matching
what the gateway reads (as does `join_room`: `room_code`, `role`). Every server→client payload is
camelCase except `sync_state.currentQuestion`, which is a raw snake_case Redis record. That
asymmetry is real — follow it rather than normalising.
