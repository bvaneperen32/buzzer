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

Three module-level dicts hold per-process state: `_sid_ctx` (sid → user/role/room/session),
`_timer_tasks` (session → question countdown), `_abandon_tasks` (session → host grace countdown).

**Who receives what** (whatever the `events.py` comments say):

- to `{room_code}` — `NEW_QUESTION`, `QUESTION_LOCKED`, `QUESTION_UNLOCKED`, `HOST_DISCONNECTED`,
  `GAME_ABANDONED`.
- to `{room_code}:host` only — `PLAYER_JOINED`, `PLAYER_LEFT`, `ANSWER_STATUS`,
  `ANSWER_PHASE_ENDED`, and the host's versions of `QUESTION_RESULTS` and `GAME_OVER`.
- to `user:{id}` — each player's own `QUESTION_RESULTS` and `GAME_OVER` (same event names as the
  host's, different payloads).
- to the sid — `SYNC_STATE`, `ANSWER_RECEIVED`, `ERROR`, and the late-join `NEW_QUESTION`.

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
`on_submit_answer` checks both T7 answer shapes exactly (after the phase, lock and idempotency
checks, before `record_answer`); a malformed one is an `error` with no score. Each player's
`QUESTION_RESULTS` for a T7 question adds `questionType`, `gradingType`, `pointsValue`,
`answered`, `yourAnswer` and `yourIsCorrect`, so the phone renders its result from that payload
alone; other types' results are unchanged.

`_answer_reveal` is *mostly* a derived view of `answer_data` — but for `multi_select` it forwards
the raw `answer_points` array, and for fill-in-the-blank the raw `acceptedAnswers`. Separately,
`_question_payload` sends `gradingType` and fill-in-the-blank's `editDistance` *during* the
question. The invariant that actually holds is "nothing answer-revealing before
`QUESTION_RESULTS`" — see [app/CLAUDE.md](../CLAUDE.md).

## Gotchas

### Multi-process correctness (the big one)

- **The three tracking dicts are per-process, and this breaks under more than one worker.** With
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
- **Double-submit protection is not atomic.** The `has_answered` check and the write
  (`record_answer`, which then calls `mark_answered`) are separate steps, and MySQL has no unique
  constraint to catch a duplicate — two near-simultaneous submits from one player can both be
  scored.
- `answer_time_ms` is taken from the client unvalidated (the player app measures it from page
  mount, not from `started_at`). It only feeds `avgAnswerTimeMs`; don't base scoring on it.
- Every rejection is an `ERROR` emit (`_emit_error`), and **both clients treat any `ERROR` as
  fatal** — they replace the game screen until the next reconnect. A routine race like "Question
  is locked" (a tap landing just after the auto-lock) therefore ejects the player. See
  [frontend/CLAUDE.md](../../../frontend/CLAUDE.md).

### Payload and numeric issues

- **`maxPossibleScore` in the host `GAME_OVER` is `int()`-truncated** (`gateway.py:959`) while
  `points_value` has been a float since migration `003`. A game of 0.5-point questions reports a
  max of 2 instead of 2.5.
- **`yourScore` comes from Redis, `yourRank` from MySQL.** The Redis running score is maintained
  by the non-atomic `update_player_score`, so a player's displayed score and their rank can
  disagree after a lost update. MySQL is the source of truth.
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

- **A second `HOST_ADVANCE` during the QUESTION → RESULTS transition re-runs results.** The
  handler sends the host its `QUESTION_RESULTS` first, then emits every player's own results in
  a loop, and records `question_phase = "RESULTS"` only after that loop. An advance that arrives
  in between (a script, or with enough players a very quick double click) still reads
  `QUESTION` and shows results again instead of moving on. Found by T8's 150-player load check,
  whose scripted host now waits for the phones' results first. Not fixed (not T8's scope).

### Host disconnect and reconnect

- **B2 (pre-existing; T7 spec §11; noted, not scheduled): none of the disconnect handling below
  runs for a started game.** The branch is gated on the *Redis* room status being `IN_PROGRESS`,
  and Redis stays `LOBBY`: `_on_host_advance_impl` reads `room_state` (status `LOBBY`) before
  calling `game_service.start_game`, which writes `IN_PROGRESS` to Redis, and then writes its own
  stale copy back (with the first question's index and phase) before broadcasting the question.
  MySQL says `IN_PROGRESS`; Redis says `LOBBY` for the rest of the game. Verified during T7: after
  the projector's browser closed mid-question, the phones received no `QUESTION_LOCKED` or
  `HOST_DISCONNECTED`, nothing was logged, and no abandon timer started. The timer keeps running
  and auto-locks at zero as usual.
  - **It is linked to B1** (both apps' `GameLayout` re-create their socket on every route change;
    see the host's [pages/CLAUDE.md](../../../frontend/host/src/pages/CLAUDE.md)). Fixing B2 alone
    would treat every host screen change as a disconnect: a lock, the "Host disconnected" banner
    (which stays up for the whole question) and an unlock at every question. And nothing orders
    the old socket's `disconnect` before the new socket's `join_room`, so a rejoin processed first
    would leave the question locked and the 5-minute abandon task running with the host
    connected. Fix B1 first, or both together.
- As designed, when the host's socket drops during an `IN_PROGRESS` game, `disconnect` emits
  `HOST_DISCONNECTED` (with `abandonAt`) to the room and starts the 5-minute abandon task. If a
  question is open and unlocked, it is also **auto-locked**: the timer is cancelled, the remaining
  time is saved in `timer_remaining_seconds`, and `question_locked_by_disconnect` is set.
- When the host rejoins, `_auto_unlock_for_host` reverses that — but only for a lock it caused
  (the flag), so a lock the host set by hand stays locked.
- In `LOBBY`, a host disconnect does nothing: no event, no abandon timer.
- **There is no "host reconnected" event.** Players learn the host is back only indirectly (a
  `QUESTION_UNLOCKED`, or the next `NEW_QUESTION`), so the player app's "host disconnected" banner
  stays up until the next question.

### Performance

- `GAME_OVER` runs `get_player_question_summary` once per player in a loop — N+1 queries plus N
  emits at the moment the game ends, with up to `max_players` (default 150) players.
- Timer resume after an unlock rewrites `started_at` to a *virtual* start so elapsed time excludes
  the paused interval (invariant stated at `gateway.py:1283`). Any change to lock/unlock has to
  preserve it or the countdown drifts on the second pause.
- `_question_timer_task`'s `finally` block only pops its own task, deliberately — a blind `pop()`
  would evict a replacement timer and fire `ANSWER_PHASE_ENDED` for the wrong question. The
  comment at `gateway.py:231` explains it; leave it alone.
