# frontend/host/src/pages

## Purpose

Every screen the host sees: the live game screens, the component that owns the game connection,
and the management pages (games, questions, rosters, session downloads) that moved here from the
admin app. This is where the host app's state and behavior live — the rest of `src/` is plumbing
and presentation.

## Contents

**Sign-in and shell**

- `LoginPage.tsx` — two sign-in paths: a link to UW NetID SSO, and a username/password form for
  admin and local accounts. Also handles the OAuth2 *return*: it reads the temp token from the URL
  fragment (`#oauth2_data=`), POSTs it to `/api/auth/exchange-temp`, stores the access token, and
  scrubs the fragment from history.
- `HostShell.tsx` — the layout route for every non-game page. On mount it loads
  `GET /auth/me`: spinner while loading; a 401 removes the token and goes to `/login`; any other
  failure (network, a 5xx while the backend reloads) shows the error with Retry and does *not*
  sign out; a `GUEST` token gets a full-screen "signed in as a guest" notice with Sign out.
  Otherwise it provides `MeContext` and renders the sidebar ("Buzzer Host": Play, Games, Rosters,
  Sessions, Sign out) around an `<Outlet />`. Admin-style dense sizing, not projector scale.

**Management pages** (inside the shell; REST-only, admin-style `load()` / mutate / reload)

- `HomePage.tsx` — **Play**, still at `/home` (see Gotchas). Resumable `LOBBY`/`IN_PROGRESS`
  sessions with Rejoin and two-step Delete, then Create Game Room. Course first: the quiz select
  stays disabled until a course is chosen and lists only that course's games (`my-games` filtered
  by `course_id`). Each select auto-selects when it has exactly one option. There's an "Allow
  guests" checkbox (default on, sent as `allow_guests`), and empty states for no courses and for
  a course with no games (the latter links to Games).
- `GamesPage.tsx` — `my-games` grouped by `course_id` under "Name (Semester)" headings, in the
  server's order, with "Unassigned" last (only admins are ever sent those). Create and import
  (JSON bundle) both take a course; the course select is pre-selected when the user hosts exactly
  one. Inline edit of title, description, and max players. Admins also get a per-row course
  select (move, after a native `confirm()`) and a Delete behind a two-step inline confirm, both
  on `/api/admin/*`. Hosts get no delete.
- `QuestionEditorPage.tsx` — per-type question authoring, reorder, delete, and JSON export, over
  `/api/host/games/{id}/…`. A port of the admin editor with the same form logic. T8: the Images
  panel sits above the question list (`useImageLibrary`); the form has an image picker for the
  prompt (every type) and one per multiple-choice and multi-select option row; the list shows a
  prompt thumbnail and each choice option as letter, thumbnail, caption. After any question
  change the library reloads, because uses, question numbers and the export meter follow it.
  T7 adds the Sort It editor (2–4 boxes, 4–10 cards, a correct-box select per card, the
  COMPLETENESS "Include an answer key" toggle) and the Spot It editor (the canvas image picker,
  grid inputs with the image's limits and default, the author canvas, and the confirmation
  checkbox), a "Needs review" badge with the server's reasons, and T7's round-trip rules: every
  frozen field is sent back exactly as loaded unless edited (unedited labels untrimmed,
  `points_value` as received, `accepted_cells` in stored order, optional keys present or absent as
  loaded, the loaded stamp while the box stays ticked, a fresh stamp after any tick; choosing the
  image that is already selected changes nothing, while a newer library version of it unticks the
  box). Its text checks only approximate the server's (`trim` vs `isspace`, `toLowerCase` vs
  `casefold`); the server's 422 shows in the form. A save adopts the response (review status included) instead of
  reloading, because updates commit after responding; a Replace reloads the questions.
- `RostersPage.tsx` — `my-courses` as a list of links to each course's roster.
- `RosterPage.tsx` — roster list plus the **CSV column-mapping wizard**: client-side RFC-4180
  parser, header auto-detection, per-column transforms (strip `@domain`, reverse
  `"Last, First"`), a live 3-row preview, then `POST /host/courses/{id}/roster/import`. Inline row
  editing via `PATCH`. The heading reads "Roster — Name (Semester)", from `my-courses`.
- `SessionsPage.tsx` — `GET /host/sessions`. Hosts see "Completed sessions you hosted"; admins
  see "Sessions" plus the status filter and two-step Delete. Report and export controls (Canvas
  gradebook vs raw CSV, SIS domain, roster-only, per-question) appear only on COMPLETED rows, for
  every role, matching the server's download rule.

**Game screens** (outside the shell, full-screen)
- `game/GameLayout.tsx` — **the hub.** Opens the one socket.io connection, registers every event
  handler, holds all game state, and exposes it through `GameContext` / `useGame()`. Renders an
  `<Outlet />` plus a persistent QR + room-code panel. Also drives routing: incoming events
  navigate between the child routes.
- `game/LobbyPage.tsx` — pre-game screen: large QR code and room code, live player count, the
  auto-advance toggle, and Start Game. The typed-join hint shows `<address>/player`, without the
  trailing slash, and nginx returns 404 for that path. The QR code's link works. *Noted, not
  scheduled*; see [frontend/CLAUDE.md](../../../CLAUDE.md).
- `game/QuestionPage.tsx` — live question: metadata line, `TimerBar`, prompt, answered counter,
  and the Lock / Show Results buttons. The prompt is hidden while the question is locked (see
  Gotchas). T8: the prompt image below the prompt (about 55% of the viewport; it stays up when
  the question locks), and for multiple choice / multi-select with any image option a lettered
  grid of full images with captions. Text-only questions still show no options here — phones
  have them. T7: Sort It shows its boxes and cards (in the shared `cardOrder`); Spot It its image
  under a labelled grid (`SpotItCanvas`), which stays up when the question locks.
- `game/ResultsPage.tsx` — per-question results. Bar chart for choice-style questions, word cloud
  for fill-in-the-blank, and the auto-advance countdown. T8: a prompt thumbnail, and bar labels
  of letter + thumbnail + caption once any option has an image. T7: Sort It's per-card table in
  `cardOrder`, and Spot It's heatmap with its legend — shown only on entering RESULTS.
- `game/GameOverPage.tsx` — final screen: score histogram, summary stats, and a per-question
  breakdown card list. T8: each card shows the prompt thumbnail and option thumbnails. T7: Sort
  It's per-card rows in the author's order, and a small Spot It heatmap (small link).

## How it fits in

Two halves with different shapes:

- **Game screens** (`/game/:code/*`, outside `HostShell`, so they stay full-screen and the socket
  opens only there). `GameLayout` is the only stateful piece; the four `game/*` pages are
  presentational views over `useGame()` and emit exactly two actions (`emitAdvance`,
  `emitLockQuestion`). Adding host state means adding it to the context, not to a child page. The
  socket is the transport; REST fetches the game title and session id, and (T8) the session's
  image links: `GameLayout` holds `useSessionImageLinks(sessionId)` and exposes `imageLinks`,
  `refreshImageLinks` and `imageLinksFailed`. It refreshes them after a socket reconnect and once
  per question that uses an id the map doesn't have. An id still missing after that is no image;
  if the map itself couldn't be fetched (a lost game grant), images show as unavailable
  instead.
- **Management pages** (inside `HostShell`). REST-only and built like the admin app's pages:
  each owns its own `useState`, runs `load()` on mount, and after a mutation calls `await load()`
  again. `useMe()` gives the role, for display only. They call `/api/host/*` and
  `/api/game/*` (`my-courses`, `my-games`, rooms, session delete), plus `/api/admin/games/{id}`
  for the two admin-only game actions.

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

### Management pages

- **Play is at `/home`, not `/play`,** because `GameLayout` and `GameOverPage` already navigate
  to `/home` (game abandoned, the error screen, "New Game"). Keep the path.
- **`QuestionEditorPage.formToPayload` derives `points_value`, and that's load-bearing.** It
  doesn't ask the author for a points value on ACCURACY questions — it computes one:

  | Type | `points_value` |
  |---|---|
  | `multiple_choice` | max of the option points |
  | `true_false` | max(true, false) |
  | `fill_in_the_blank` | max of the accepted-answer points |
  | `multi_select` | **sum** of the positive option points |
  | `sort_into_boxes`, `find_in_image` (T7) | the value the author typed, in both modes |
  | any COMPLETENESS | the value the author typed |

  This is what makes the backend's reveal rule (`answer_points[i] >= points_value` ⇒ correct)
  work for the first three: only the top-scoring option(s) hit the max. For `multi_select` the
  "correct" set can't be expressed that way, which is why the server sends the raw `answerPoints`
  array instead of `correctIndices` and lets clients mark each option with `points > 0`.
  Hand-built JSON that sets `points_value` independently will break both the reveal display and
  `is_correct`.
- **Option rows with images** (T8): when any option row of a multiple-choice or multi-select
  question has an image, `formToPayload` first drops rows with neither text nor image, and derives
  `options`, `option_images`, `answer_points` and `points_value` from that one filtered list, so a
  leftover blank row can't block saving. With no image, both types behave exactly as before
  (including the flaw below). `option_images` is sent only when some option has an image, and
  `prompt_image_id` only when set, so text-only payloads are unchanged.
- One flaw in that derivation: for `multi_select` and `fill_in_the_blank`, `formToPayload` drops
  rows with blank text from the saved options/answers but computes `points_value` over *all*
  rows. A blank row that still carries points inflates `points_value` above anything an answer
  can score, so `is_correct` can never be true for that question.
- The server now validates every question edit structurally (a 422 with a readable message), so
  the editor is no longer the only thing enforcing shape on the edit path.
- **Deleting an answered question is refused** (409, with the server's explanation shown in the
  page's error line). Deletes, edits, and reorders of the original four types are *not* blocked
  while a session of the game is live — a mid-game edit can change what later questions show or
  how they score. T7's two types are frozen then (the server's 409 shows in the form).
- **"Save, then reload" can briefly show stale data.** The backend commits some writes *after*
  sending the response (see [routers/CLAUDE.md](../../../../backend/app/routers/CLAUDE.md)), so
  the `await load()` that follows can read the old state — e.g. an admin's game delete or move,
  a game edit, or a question edit. *Noted, not scheduled.*
- **Roster import duplicates backend parsing, differently.** `RosterPage.mapRow` reimplements what
  `roster_service.process_roster_csv` does server-side:
  - **Title-casing.** The client does `full_name.replace(/\b\w/g, c => c.toUpperCase())`, which
    uppercases each word's first letter but leaves the rest alone — `"MCDONALD"` stays
    `"MCDONALD"`. Python's `.title()` on the server path yields `"Mcdonald"`. The same student
    imported through the wizard versus a direct CSV upload gets a different stored name, and that
    name is what players see on screen.
  - The wizard stores the mapped `full_name` as-is, while the server's own CSV path keeps only
    the *given* name. Two import paths, two meanings for one column.
  - There's also a third CSV parser in play: this file's `parseCSV` is hand-rolled, while the
    server uses Python's `csv` module.
- **An admin's game Delete erases every session and score ever recorded for that game.** It sits
  behind a two-step inline confirm that says so; hosts never see it. Moving a game uses a native
  `confirm()`, because it silently removes other hosts' access.
- `SessionsPage` sends `per_question` for the `raw` format too, where the backend ignores it
  (`build_session_csv` takes no such argument and always emits per-question columns). Harmless,
  but the UI implies a choice that doesn't exist.
- ABANDONED sessions have no download controls here, for anyone. The admin API's any-status
  endpoints are the only way to their partial results.
- `moveQuestion` sends the full reorder array and updates local state only on success, so a
  failed reorder leaves the list unchanged. Each arrow click is a full round trip.
- A `Button` inside a `<form>` submits it (no default `type`). The create and edit forms rely on
  that; every Cancel passes `type="button"` explicitly.

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
- **Image layouts keep clear of the fixed QR / room-code panel.** On `QuestionPage` the image area
  is `22rem` narrower than the viewport (the panel's width, twice, since it's centred) and sized to
  fit one screen (a 28vh prompt image plus 20vh option images when both are shown);
  `ResultsPage` keeps a bottom band (`pb-48`). Text-only questions keep their old layout.
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
  (see `QuestionEditorPage` under "Management pages" above), so a
  `points >= points_value` rule can't express "this single option is correct". The cost is that
  the `> 0` rule is duplicated in `ResultsPage.buildBars` and `GameOverPage.isCorrectIndex`, and
  it only holds while the editor keeps deriving `points_value` that way.
- The word cloud renders raw player-typed text at large size on a projector. Players control that
  string entirely — the same exposure the backend's HTML report has.

### State and lifecycle

- **`GameLayout`'s socket effect depends on `[code, navigate]`, and `navigate` is *not* stable
  here** (B1, found during T7's browser checks; T7 spec §11). With a plain `BrowserRouter`, React
  Router 6.30's `useNavigate` is rebuilt whenever the pathname changes, so every route change (lobby →
  question → results → …) tears the socket down and opens a new one, which re-sends `join_room`.
  It mostly works because join re-syncs, but it's churn, and in the player app it loses answers
  (see [player pages/CLAUDE.md](../../../player/src/pages/CLAUDE.md)). The fix is to keep
  `navigate` out of the effect's dependencies (read it through a ref). *Noted, not scheduled.*
  It's linked to B2 (the backend never runs its host-disconnect handling for a started game; see
  [websocket/CLAUDE.md](../../../../backend/app/websocket/CLAUDE.md)): B2 currently hides this
  churn from the players, so fix B1 first or both together.
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
- `RequireAuth` (in `App.tsx`) only checks that a token string exists. Inside the shell,
  `HostShell`'s `/auth/me` then catches an expired token (401 → `/login`). **Game routes don't
  pass through the shell**, so there an expired token still renders the page and fails at the
  socket.
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
