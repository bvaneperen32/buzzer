# backend/app/websocket

## Purpose

The python-socketio real-time layer. Everything that happens *during* a live game — joining,
advancing questions, submitting answers, locking, disconnect handling — flows through here.
HTTP routers only get you to the room; this drives play.

## Contents

- `events.py` — event-name constants. Import these as `E.NEW_QUESTION`; never hardcode event
  strings. **Its grouping comments are wrong** ("broadcast to all in room" covers events that go
  to the host or to one player) — see "Who receives what" below for the real routing.
- `middleware.py` — `authenticate_socket(auth, db)`: validates the JWT from the socket.io `auth`
  dict and returns the `User`. Raises `ValueError`; the connect handler turns that into `False`.
- `gateway.py` — everything else (~1300 lines): the `sio` server, the room/phase state machine,
  timer and abandon background tasks, and the five event handlers. It also defines a
  `socket_app` (line 61) that nothing uses — the real ASGI wrapping happens in `main.py`.

## Room and state model

- Socket.io room `{room_code}` — everyone in the game.
- Socket.io room `{room_code}:host` — host-only events (`ANSWER_STATUS`, `QUESTION_RESULTS`
  bar-chart view, host `GAME_OVER`).
- Socket.io room `user:{user_id}` — per-player personalised payloads, so one player's score and
  rank never reach the whole room.

The **host drives every transition** via `HOST_ADVANCE`. The phase machine lives in
`room_state["question_phase"]` in Redis:
`None` (lobby) → `QUESTION` → `RESULTS` → `QUESTION` … → `COMPLETED`.

Six module-level dicts and a set hold per-process state:
- `_sid_ctx` (sid → user/role/room/session);
- `_timer_tasks` (session → question countdown);
- `_abandon_tasks` (session → host grace countdown);
- `_host_presence_locks` (session → the `asyncio.Lock` that serializes a host's disconnect
  handling with a host joining; below);
- `_answer_turns` ((session, question, user) → the lock that runs one player's attempts at a
  question one at a time, removed when unused);
- `_answers_in_flight` (session → the answers being handled now, which showing results waits
  for);
- `_results_building` (the sessions whose Show Results is being handled).

The last three are under "An answer counts once" and "Showing results" below.

**Who receives what** (whatever the `events.py` comments say):

- to `{room_code}` — `NEW_QUESTION`, `QUESTION_LOCKED`, `QUESTION_UNLOCKED`, `HOST_DISCONNECTED`,
  `HOST_RECONNECTED`, `GAME_ABANDONED`.
- to `{room_code}:host` only — `PLAYER_JOINED`, `PLAYER_LEFT`, `ANSWER_STATUS`,
  `ANSWER_PHASE_ENDED`, `RESULTS_DELAYED`, and the host's versions of `QUESTION_RESULTS` and
  `GAME_OVER`.
- to `user:{id}` — each player's own `QUESTION_RESULTS` and `GAME_OVER` (same event names as the
  host's, different payloads).
- to the sid — `SYNC_STATE`, `ANSWER_RECEIVED`, `ANSWER_REJECTED`, `ERROR`, and the late-join
  `NEW_QUESTION`.

**Clients reconnect with `JOIN_ROOM`, not `REJOIN_ROOM`.** Host, player, `scripts/`, and the
integration tests all send `JOIN_ROOM` on every connect *and* reconnect; nothing sends
`REJOIN_ROOM`. Reason about reconnection through `on_join_room`.

## How it fits in

Sits beside `routers/` as a second entry point. It is **not** mounted as a sub-app:
[main.py](../main.py) wraps the whole FastAPI app *inside* `socketio.ASGIApp(sio,
other_asgi_app=app)`, so `/socket.io/*` is handled here and everything else falls through to
FastAPI. It authenticates through `services.auth_service` (via `middleware.py`)
and delegates all game logic to `services.game_service` / `services.state_service` — the gateway
is orchestration and I/O, and new scoring or lifecycle rules belong in a service, not here.

For a narrative walkthrough of one answer travelling end to end, see
[docs/realtime.md](../../../docs/realtime.md).

**T8 images.** `_question_payload` is `async` and adds `images`: small links for the question's
option images only, keyed by image id, one query scoped to the question's own game (`{}` when
there are none). It builds `new_question`, the late-join push and the host's `sync_state`
question alike. Prompt images never go to phones; the projector gets its links over REST
(`GET /api/game/sessions/{id}/images`), not the socket. When `game_service.start_game` returns
`None`, a concurrent Start (a double click) already started the game while this advance waited
on the game lock; the handler returns without emitting anything. If it raises (T7's backstop: a
Spot It question needing review), the host gets an `error` and the session stays LOBBY.

**T7.** `_question_payload` adds `cardOrder` to Sort It questions (a permutation seeded by the
question id, shared by the projector and every phone; never the author's order). Spot It's
`config.image_id` is only an id — `images` stays `{}` for it, so phones never receive the image.
`on_submit_answer` checks both T7 answer shapes exactly (after the idempotency, phase and lock
checks, before `record_answer`); a malformed one is an `error` with no score. Each player's
`QUESTION_RESULTS` for a T7 question adds `questionType`, `gradingType`, `pointsValue`,
`yourAnswer` and `yourIsCorrect`, so the phone renders its result from that payload alone.
`answered` (whether the server has the player's answer, from its MySQL rows) goes to every
type's results: the phone's "No answer" comes from it.

`_answer_reveal` is *mostly* a derived view of `answer_data` — but for `multi_select` it forwards
the raw `answer_points` array, and for fill-in-the-blank the raw `acceptedAnswers`. Separately,
`_question_payload` sends `gradingType` and fill-in-the-blank's `editDistance` *during* the
question. The invariant that actually holds is "nothing answer-revealing before
`QUESTION_RESULTS`" — see [app/CLAUDE.md](../CLAUDE.md).

## Gotchas

### Multi-process correctness (the big one)

- **The tracking dicts are per-process, and this breaks under more than one worker.** With
  `AsyncRedisManager`, socket.io delivers events to the right clients across processes, but the
  bookkeeping doesn't move. If a host disconnects on process A and reconnects on process B,
  B's `_abandon_tasks.pop(session.id)` finds nothing, so **A's abandon task still fires and
  abandons a game that has a live host**. The same split makes a question timer on A
  un-cancellable from B. The module docstring notes the design; this is its consequence.
- **Dev and prod use different socket.io client managers** — in-memory in development, Redis
  pubsub in production (the comment at `gateway.py:46` explains why). Multi-instance routing bugs
  are therefore invisible locally by construction.

### Answer-phase logic

- **A disconnected player still counts toward `all_players_answered`.** Disconnect only sets
  `is_connected = False`; the player stays in the Redis `players` set, and `get_player_count` is a
  `SCARD` of that set. Once anyone drops mid-question without answering, the "everyone answered"
  early-finish can never fire and the host must wait out the timer.
- **`answer_data` is not validated except for `multi_select` and T7's two types.** The handler
  checks `selectedIndices` bounds and types, and the exact Sort It / Spot It shapes (T7), but
  `multiple_choice`, `true_false`, and `fill_in_the_blank` accept whatever dict the client
  sends. It's persisted to a JSON column, and fill-in-the-blank text additionally becomes a
  **Redis hash field key** with no length cap.
- **Rate limiting covers connections only, not events.** 10 connects per IP per 10s at
  `connect`; `SUBMIT_ANSWER`, `HOST_ADVANCE`, and `HOST_LOCK_QUESTION` have no throttle beyond
  the phase/idempotency checks.
- That connection limit is **per IP**, which is the wrong unit for the target setting — a lecture
  hall behind one campus NAT or one Wi-Fi gateway shares an IP, so a class joining at once can
  rate-limit itself. Dev is exempt; production isn't.
- The IP comes from `_client_ip`, which trusts the **first** `X-Forwarded-For` value. nginx
  appends to that header rather than replacing it, so a client can send its own value and choose
  its IP — the limit is bypassable. The counters (`ws_rate:{ip}`, 10s TTL) live in Redis, so unlike
  HTTP rate limiting they are shared across processes.
- Late joiners get `remaining = max(5, time_limit - elapsed)` — always at least 5 seconds, even
  if the question's time has fully expired. That push happens only in `JOIN_ROOM`, and only for a
  player who **hasn't answered** yet; one who has gets `SYNC_STATE` alone, with no question
  content.
- **An answer counts once** (`docs/plans/fix-live-session-test-cleanup.md` §3.13). A phone
  whose connection drops before the reply sends its answer again after re-joining, possibly
  while the first attempt is still in the server, or after it failed there. Before this, both
  could pass the `has_answered` check and be recorded (reproduced: two rows, the distribution
  counted twice). Now:
  - **One player's attempts at a question run one at a time** (`_answer_turn`). A retry waits
    for the attempt before it (logged `submit_answer_waits_for_earlier_attempt`) and then sees
    its outcome.
  - **An answer already counted is acknowledged again** (`alreadyAnswered: true`), checked
    first, before the phase and lock: a retry of an answer that counted is never told it came
    too late.
  - **`record_answer` commits the row, then counts it in Redis** (score, answered mark,
    distribution, together: `state.apply_answer`), and only then is `ANSWER_RECEIVED` sent. So
    "Answer locked in!" means stored and counted, and a failure leaves nothing, or a committed
    row that isn't counted yet.
  - **A retry finds that row** (`game_service.recorded_answer`) and counts it
    (`finish_recorded_answer`, logged `answer_counted_on_retry`) rather than recording a second
    one.
  - MySQL still has no unique constraint behind this, and the turns are per-process (above).
  - **An attempt that fails inside the server** (a database or Redis error) gets no reply and is
    logged `submit_answer_failed`. It leaves nothing, or a committed row not yet counted in
    Redis. The phone keeps "Sending your answer…" and sends it again after its next reconnect.
    If it doesn't, Show Results counts the stored row (below): a stored answer is never shown
    as "No answer".
- **Showing results** (`_show_results`, the QUESTION → RESULTS advance) publishes only results
  that nothing can change afterwards.
  - **It closes the question first** (`_close_answers`, before anything is read). It stores the
    lock, with the time left (`timer_remaining_seconds`) if this closed it, so a later answer
    is `ANSWER_REJECTED` `"locked"`. It then ends the handler's transaction, whose snapshot
    predates the answers still being committed.
  - **It waits for the answers being handled** (`_answers_in_flight`, logged
    `question_results_wait_for_answers`, at most `_ANSWER_DRAIN_SECONDS`, 10 s).
  - **If one is still being recorded when the wait ends, nothing is published.** The room gets
    `QUESTION_LOCKED` (if this closed it), and the host `RESULTS_DELAYED` (`questionId`,
    `answersPending`; logged `question_results_delayed`). Not an `ERROR`: the host app shows a
    notice, and Show Results can be pressed again once the answer finishes (or fails). An
    Unlock reopens the question instead, with its time left.
  - **It counts stored answers Redis hasn't** (`_count_stored_answers`, logged
    `answer_counted_before_results`) before reading totals and the distribution from Redis. The
    results' points, `answered` and ranks come from MySQL, so the two then agree. Each is
    counted in that player's turn, so a retry arriving at the same moment can't count it too.
  - **One at a time per session** (`_results_building`). A second Show Results while one is
    being handled is ignored, since that one's outcome is on its way, and so is a lock toggle,
    which would reopen the question under it. This also ended an older issue: a quick second
    advance used to read `QUESTION` again and re-run the results.
  - So the results count exactly the answers the server acknowledged, and each phone hears its
    acknowledgement before its results. Before this (reproduced), an answer acknowledged while
    the results were being built was missing from them.
- `answer_time_ms` is taken from the client unvalidated (the player app measures it from page
  mount, not from `started_at`). It only feeds `avgAnswerTimeMs`; don't base scoring on it.
- A `SUBMIT_ANSWER` from a socket that isn't seated yet (no `_sid_ctx` entry) is dropped with a
  `submit_answer_before_join` warning. The player app never sends one: it waits for
  `SYNC_STATE`, which the server sends once the socket is seated, and holds an answer given in
  the meantime (see the player's [pages/CLAUDE.md](../../../frontend/player/src/pages/CLAUDE.md)).
- **A late answer is `ANSWER_REJECTED`, not `ERROR`** (`_reject_answer`: `questionId`, `reason`,
  `message`). It applies when the answer arrives after its question locked (`"locked"`), or after
  the question moved on (`"closed"`: results shown, or a different question open). Those are
  normal races in a live game, such as a tap just after the auto-lock, and the phone keeps its
  screen.
- **Every other rejection is an `ERROR` emit** (`_emit_error`): a malformed request, a missing
  room or a session no longer in progress, and join refusals. **Both clients treat any `ERROR`
  as fatal** and replace the game screen until the next reconnect. See
  [frontend/CLAUDE.md](../../../frontend/CLAUDE.md).

### Payload and numeric issues

- **`maxPossibleScore` in the host `GAME_OVER` is `int()`-truncated** (`gateway.py:959`) while
  `points_value` has been a float since migration `003`. A game of 0.5-point questions reports a
  max of 2 instead of 2.5.
- **`yourScore` comes from Redis, `yourRank` from MySQL.** The Redis running score changes only
  in `state.apply_answer`, a watched transaction that a concurrent write to the player's record
  (the connected flag, itself watched) can't overwrite. MySQL is still the source of truth: a
  lost `room:`/player key, or a guest merge, can make the two disagree.
- `SYNC_STATE` is asymmetric, and parts of it break the camelCase convention every other
  server→client payload follows. The host gets a full question payload (via
  `_host_question_payload`), but its `players` array is the raw Redis player records — `user_id`,
  `display_name`, `is_guest`, `score`, `is_connected`, all snake_case, including every player's
  named running score. The player gets `currentQuestion` as the raw compact Redis record
  (`question_id`, `started_at`, …, snake_case) with no prompt or options, and depends on the
  follow-up `NEW_QUESTION` for content — which is sent only if they haven't answered (above).
- `SYNC_STATE` carries no results or game-over data. A client that reconnects during `RESULTS` or
  after `GAME_OVER` cannot recover what it missed, and for players `JOIN_ROOM` on a `COMPLETED`
  session is rejected outright ("This game is not accepting players").
- `q_scores` is annotated `dict[str, int]` but holds floats.

### Auth and access

- `authenticate_socket` accepts `temp` tokens as well as `access` tokens, matching — and
  inheriting — the HTTP-side issue described in [common/CLAUDE.md](../common/CLAUDE.md).
- **Any `ADMIN` can join any session as HOST**, regardless of `host_user_id`, in both
  `JOIN_ROOM` and `REJOIN_ROOM`. Intentional as an override, but it means two hosts can drive one
  game and there is no "someone else is hosting" guard.
- **Being seated as `HOST` requires `game_service.user_can_host_session`**: `ADMIN`, or the
  session's recorded host (`host_user_id`) who *still* holds HOST on the session's course.
  `JOIN_ROOM` — the path clients actually use — takes `role` from the client and applies that
  check before granting `HOST`. A recorded host who has lost HOST gets `ERROR` "You no longer have
  HOST access to this course"; anyone else asking for HOST keeps "Not authorised as host for this
  session". Don't loosen it back to a bare `host_user_id` test.
- **`REJOIN_ROOM` is not used by any client** (see "Room and state model"), but it infers the role
  with the same predicate so it can't become the bypass. A recorded host who fails it gets the
  same "no longer have HOST access" error and is refused, not seated as a player.
- **The HOST check runs only at join.** A host whose HOST is revoked mid-game keeps driving it on
  their current socket; their next reconnect is refused, and the existing 5-minute abandon timer
  then ends the game. Forcing an immediate disconnect would need gateway bookkeeping that doesn't
  exist.
- `authorise_player` (the player path) refuses a `GUEST` when the session has `allow_guests` off —
  the backstop for a guest whose still-valid token skips `/auth/guest`.

- **A second `HOST_ADVANCE` during the QUESTION → RESULTS transition is ignored**
  (`_results_building`, above).
  - The handler sends the host its `QUESTION_RESULTS` first, then every player's own results in
    a loop, and records `question_phase = "RESULTS"` only after that loop.
  - An advance arriving in between (a script, or a very quick double click with enough players)
    used to read `QUESTION` and show results again.
  - Found by T8's 150-player load check, whose scripted host waits for the phones' results
    first. A genuine next Next still has to come after the results.

### Host disconnect and reconnect

*B2, fixed with B1 (`docs/plans/fix-live-session-test-cleanup.md`).* The handling below is gated
on the *Redis* room status being `IN_PROGRESS`.

- Before the fix it never ran: Redis stayed `LOBBY` for the whole game, because the start wrote
  its pre-start copy of the room back.
- `_on_host_advance_impl` now re-reads the room after `game_service.start_game`.
- It also relies on B1's fix: a projector that re-created its socket on every screen change
  would look like a host disconnect at every question.

- **A host socket closing during an `IN_PROGRESS` game** (`_host_left`):
  - if a question is open and unlocked, **auto-locks** it: the timer is cancelled, the remaining
    time is saved in `timer_remaining_seconds`, and `question_locked_by_disconnect` is set;
  - starts the 5-minute abandon task, and emits `QUESTION_LOCKED` (if it locked) and
    `HOST_DISCONNECTED` (with `abandonAt`) to the room.
  - **The state comes before the broadcasts**: the lock is stored and the abandon task
    registered before either emit. A player joining during them reads `questionLocked` and
    `hostDisconnected` (the task's presence) as the room is being told. With the task registered
    last, such a player got `hostDisconnected: false`, and could clear the banner the broadcast
    had just raised.
- **When the host rejoins** (`_seat_host`, used by `JOIN_ROOM` and `REJOIN_ROOM`), the abandon
  task is cancelled, and `_auto_unlock_for_host` reverses the lock.
  - It unlocks only a lock the disconnect caused (the flag), so a lock the host set by hand stays
    locked.
  - If the time was already up when the host dropped (remaining 0), the question stays locked,
    as the timer would have left it.
- **Ordering.** Both paths run under the session's `_host_presence_locks` entry, and the rejoin
  re-reads the room inside it. So a disconnect being handled finishes before a rejoin looks at
  the room, and the rejoin undoes it.
- **Another host socket still seated.** A close is ignored (logged
  `host_socket_closed_host_still_seated`) while another host socket of the session is in
  `_sid_ctx`. That covers a reconnect whose new socket joined before the old one's close
  arrived, and a second (admin) host.
  - The cost: if the old socket's death hasn't been noticed yet (the server's ping timeout, about
    45 s with python-socketio's defaults), a real close of the *new* socket is handled only when
    the old one times out.
- **The abandon task** removes only its own `_abandon_tasks` entry (as the timer task does), and
  never abandons a session whose host is seated.
- In `LOBBY`, a host disconnect does nothing: no event, no abandon timer.
- **`HOST_RECONNECTED`** goes to the room when a host is seated while the abandon task was
  running, i.e. after `HOST_DISCONNECTED` had been sent.
  - It is sent whether or not the question unlocks: a lock the host set by hand stays.
  - A player's `SYNC_STATE` carries `hostDisconnected` (the abandon task is running), so a phone
    that reconnects meanwhile knows too.
- A host action (`HOST_ADVANCE` / `HOST_LOCK_QUESTION`) that reaches the server before the host's
  socket is seated is ignored (`_sid_ctx` has no entry for it). The host app never sends one: its
  controls are disabled until its `SYNC_STATE` (see the host's
  [pages/CLAUDE.md](../../../frontend/host/src/pages/CLAUDE.md)).

### Performance

- `GAME_OVER` runs `get_player_question_summary` once per player in a loop — N+1 queries plus N
  emits at the moment the game ends, with up to `max_players` (default 150) players.
- Timer resume after an unlock rewrites `started_at` to a *virtual* start so elapsed time excludes
  the paused interval (invariant stated at `gateway.py:1283`). Any change to lock/unlock has to
  preserve it or the countdown drifts on the second pause.
- `_question_timer_task`'s `finally` block only pops its own task, deliberately — a blind `pop()`
  would evict a replacement timer and fire `ANSWER_PHASE_ENDED` for the wrong question. The
  comment at `gateway.py:231` explains it; leave it alone.
