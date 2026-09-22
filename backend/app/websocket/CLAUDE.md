# backend/app/websocket

## Purpose

The python-socketio real-time layer. Everything that happens *during* a live game — joining,
advancing questions, submitting answers, locking, disconnect handling — flows through here.
HTTP routers only get you to the room; this drives play.

## Contents

- `events.py` — event-name constants, grouped by direction (client→server, broadcast,
  targeted, host-only). Import these as `E.NEW_QUESTION`; never hardcode event strings.
- `middleware.py` — `authenticate_socket(auth, db)`: validates the JWT from the socket.io `auth`
  dict and returns the `User`. Raises `ValueError`; the connect handler turns that into `False`.
- `gateway.py` — everything else (~1300 lines): the `sio` server, the room/phase state machine,
  timer and abandon background tasks, and the five event handlers.

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

## How it fits in

Sits beside `routers/` as a second entry point, mounted as a sub-app from
[main.py](../main.py). It authenticates through `services.auth_service` (via `middleware.py`)
and delegates all game logic to `services.game_service` / `services.state_service` — the gateway
is orchestration and I/O, and new scoring or lifecycle rules belong in a service, not here.

`_answer_reveal` is deliberately a *derived* view of `answer_data`. Raw `answer_data` must never
be emitted to any client.

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
- **`answer_data` is not validated except for `multi_select`.** The handler checks
  `selectedIndices` bounds and types, but `multiple_choice`, `true_false`, and
  `fill_in_the_blank` accept whatever dict the client sends. It's persisted to a JSON column, and
  fill-in-the-blank text additionally becomes a **Redis hash field key** with no length cap.
- **Rate limiting covers connections only, not events.** 10 connects per IP per 10s at
  `connect`; `SUBMIT_ANSWER`, `HOST_ADVANCE`, and `HOST_LOCK_QUESTION` have no throttle beyond
  the phase/idempotency checks.
- That connection limit is **per IP**, which is the wrong unit for the target setting — a lecture
  hall behind one campus NAT or one Wi-Fi gateway shares an IP, so a class joining at once can
  rate-limit itself. Dev is exempt; production isn't.
- Late joiners get `remaining = max(5, time_limit - elapsed)` — always at least 5 seconds, even
  if the question's time has fully expired.

### Payload and numeric issues

- **`maxPossibleScore` in the host `GAME_OVER` is `int()`-truncated** (`gateway.py:959`) while
  `points_value` has been a float since migration `003`. A game of 0.5-point questions reports a
  max of 2 instead of 2.5.
- **`yourScore` comes from Redis, `yourRank` from MySQL.** The Redis running score is maintained
  by the non-atomic `update_player_score`, so a player's displayed score and their rank can
  disagree after a lost update. MySQL is the source of truth.
- `SYNC_STATE` is asymmetric: the host gets a full question payload (via `_host_question_payload`),
  while the player gets the raw compact Redis record (`question_id`, `started_at`, …) with no
  prompt or options. Players depend on the follow-up `NEW_QUESTION` for content.
- `q_scores` is annotated `dict[str, int]` but holds floats.

### Auth and access

- `authenticate_socket` accepts `temp` tokens as well as `access` tokens, matching — and
  inheriting — the HTTP-side issue described in [common/CLAUDE.md](../common/CLAUDE.md).
- **Any `ADMIN` can join any session as HOST**, regardless of `host_user_id`, in both
  `JOIN_ROOM` and `REJOIN_ROOM`. Intentional as an override, but it means two hosts can drive one
  game and there is no "someone else is hosting" guard.
- `REJOIN_ROOM` infers role from `host_user_id` rather than taking it from the client — don't
  "simplify" it to trust a client-sent role.

### Performance

- `GAME_OVER` runs `get_player_question_summary` once per player in a loop — N+1 queries plus N
  emits at the moment the game ends, with up to `max_players` (default 150) players.
- Timer resume after an unlock rewrites `started_at` to a *virtual* start so elapsed time excludes
  the paused interval (invariant stated at `gateway.py:1283`). Any change to lock/unlock has to
  preserve it or the countdown drifts on the second pause.
- `_question_timer_task`'s `finally` block only pops its own task, deliberately — a blind `pop()`
  would evict a replacement timer and fire `ANSWER_PHASE_ENDED` for the wrong question. The
  comment at `gateway.py:231` explains it; leave it alone.
