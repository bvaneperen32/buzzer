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

- `GameLayout.tsx` — the hub: opens the socket, maps events to state, provides `GameContext`, and
  renders a fixed "Host disconnected" banner when applicable.
- `LobbyPage.tsx` — room code, spinner, player count. Message differs for `LOBBY` vs
  `IN_PROGRESS` (late joiner waiting for the next question).
- `QuestionPage.tsx` — the answer UI, with a separate branch per question type: colour-coded
  lettered tiles for `multiple_choice`, two large buttons for `true_false`, a text field for
  `fill_in_the_blank`, and a toggle list with an explicit Submit for `multi_select`.
- `FeedbackPage.tsx` — static "Answer locked in" screen with a spinner.
- `ResultsPage.tsx` — per-question outcome: correct/incorrect, what you answered, points, running
  total, and rank.
- `GameOverPage.tsx` — final rank and score, plus a per-question recap with your answer and the
  correct one.

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

### The phone deliberately doesn't show the question

**`QuestionPage` never renders `currentQuestion.prompt`.** All four branches render only the
question number, timer, and answer controls. This is the Jackbox model — the prompt is on the
projector, the phone is a controller — and `GameOverPage` *does* render prompts in the recap, so
the omission is a deliberate live-screen decision rather than a missing line. The field is sent
and typed, so it looks like an oversight. Don't "fix" it without checking the intent.

### "Correct" is derived locally, and gets it wrong

- `ResultsPage` computes `isCorrect = yourPoints > 0`, and `GameOverPage` uses
  `pointsAwarded > 0`. The server's own `is_correct` uses `points >= pointsValue`. **Any
  partial-credit answer therefore displays a green "Correct!" to the player while the server
  recorded it as incorrect** — reachable today with `multi_select` and with per-option
  `answer_points` on multiple choice.
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
- **`sync_state`'s `hasAnswered` and `currentQuestion` are ignored**, and for `IN_PROGRESS` it
  doesn't navigate — it relies on the server's late-join `new_question`, which is sent only to
  players who **haven't** answered. After a page refresh:
  - mid-question, not yet answered — recovers (a fresh question with the remaining time);
  - mid-question, already answered — usually on `/feedback`, which is static and fine; if the
    `answer_received` never arrived, on `/question` showing "Loading question…" until the next
    question;
  - during results — "Loading results…" until the next question (`sync_state` has no results);
  - after game over — the server rejects `join_room` for a `COMPLETED` game, so the player gets
    the error screen and can't see their final results again.
- The "Host disconnected" banner is cleared only by `new_question`. The server has no "host
  reconnected" event, so once the host is back the banner stays up until the next question.
- Any socket `error` replaces the whole screen (see [src/CLAUDE.md](../CLAUDE.md)); a tap just
  after the auto-lock gets "Question is locked" and ejects the player.
- `QuestionPage`'s `startTimeRef` is initialised at mount, so `answer_time_ms` measures from when
  the page rendered, not from when the server started the question. Network latency is included,
  and after a reconnect the elapsed time restarts from zero.
- `submitted` is local component state, reset by the remount between questions. It is the only
  thing preventing a double submit client-side.
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
