# backend/app

## Purpose

The Buzzer backend: a FastAPI + python-socketio application serving a real-time party quiz
platform. A Host drives game state from a large screen; Players answer from phones. HTTP handles
everything around a game (auth, authoring, room setup, exports); WebSocket handles the game
itself.

## Contents

**Application-level modules**

- `main.py` — assembly. Builds the FastAPI app, registers the rate limiter, CORS, and exception
  handlers, mounts the six routers under `/api`, and runs `bootstrap_admin()` in the lifespan
  hook. The final `asgi_app` wraps FastAPI *inside* `socketio.ASGIApp` rather than mounting
  socket.io as a sub-app — Starlette's `Mount` strips the path prefix and breaks socket.io's own
  path matching. Don't "clean this up" into a `Mount`.
- `config.py` — `Settings` (pydantic-settings, from env / `.env`) and the `settings` singleton.
  `is_development` and `is_stress_test_mode` gate a surprising amount of behavior; see below.
  `CORS_ORIGINS` is also socket.io's Origin allowlist.
- `database.py` — async SQLAlchemy engine, `AsyncSessionLocal`, the declarative `Base`, and the
  `get_db` dependency (commits on clean exit, rolls back on exception). Under FastAPI 0.141 that
  commit runs **after the response has been sent**, so a client that writes and immediately
  reads can see the old state — see [routers/CLAUDE.md](routers/CLAUDE.md). Kept deliberately;
  *noted, not scheduled.*
- `redis_client.py` — a lazily-created module-level `Redis` singleton with `decode_responses=True`
  (so all Redis reads are `str`, never `bytes`).

**Subpackages** — each has its own `CLAUDE.md` with file-level detail and gotchas:

- [`routers/`](routers/CLAUDE.md) — the HTTP API: `health`, `auth`, `game`, `admin`, `host`, and
  `images` (signed image links, T8).
- [`websocket/`](websocket/CLAUDE.md) — the socket.io gateway, event constants, socket auth.
- [`services/`](services/CLAUDE.md) — all business logic: auth, access rules, game/question
  authoring, game rules, scoring, Redis state, roster import, exports, HTML reports, and (T8) the
  game row lock, image references, game bundles, and image processing, libraries and links.
- [`schemas/`](schemas/CLAUDE.md) — Pydantic request/response contracts.
- [`models/`](models/CLAUDE.md) — SQLAlchemy ORM models.
- [`common/`](common/CLAUDE.md) — auth dependencies, error hierarchy, logging, rate limiter.
- `migrations/` — Alembic env and five revisions (`001` initial schema, `002` answer_data on
  scores, `003` integer → float points, `004` course-specific games: nullable `games.course_id`
  and `game_sessions.allow_guests`, `005` the `images` table: one image library per game, T8). No
  context file; run `alembic upgrade head` from `backend/`. `env.py` reads `DATABASE_URL` from
  the process environment, not from `settings`. Round-trip a new migration on a disposable
  database, never by downgrading the dev one (`docs/plans/t8-image-support.md` §4.12 has the
  commands).

## Architecture

Two entry points, one logic layer:

```
HTTP      routers/  ─┐
                     ├─→ services/ ─→ models/ (MySQL)  +  Redis
WebSocket websocket/ ─┘
              ↑
           common/ (guards, errors)   schemas/ (HTTP contracts only)
```

Both entry points are deliberately thin: authenticate, validate, delegate, return. Game rules
live in `services/` so they can be tested without a request or socket context — services receive
`db` and `redis` as arguments rather than resolving them.

**Two stores, different roles.** MySQL is authoritative and durable: every answer is written to
`session_scores` the moment it's scored, and leaderboards are aggregated from it. Redis holds
live session state (room metadata, player set, current question, answered sets, answer
distributions) and is treated as a fast cache that may vanish. When they disagree, MySQL wins —
but MySQL has no constraint against duplicate score rows (see "Data model and numbers"), and a
vanished `room:{code}` key ends a live game (see "Redis lifecycle").

**The one import rule that bites:** `common/dependencies.py` imports from
`services/auth_service.py`, so `common` is not a leaf. Services must never import
`common.dependencies`.

**T8's module dependencies** (each arrow means "imports"; nothing may import against them):

```
image_refs              → (nothing)            schemas.admin also imports it, for the keys
game_lock               → models, common.exceptions
bundle_service          → image_refs, common.exceptions
image_service           → image_refs, bundle_service, game_lock, auth_service, state_service,
                          models, schemas.image, common.exceptions
game_management_service → image_refs, bundle_service, image_service, game_lock, game_service
game_service            → game_lock        (T7 will add image_service)
report_service          → image_service, image_refs
websocket/gateway       → image_service
```

`image_service` and `bundle_service` never import `game_service` or `game_management_service`,
which keeps T7's future `game_service → image_service` import cycle-free.

## Cross-cutting invariants

- **Nothing that reveals the answer reaches a client before `QUESTION_RESULTS`.** That is the
  invariant the code actually keeps — the stronger "`answer_data` never reaches a client" is not
  true. During a question, the gateway's `_question_payload` sends `gradingType` and, for
  fill-in-the-blank, `editDistance` (read from `answer_data`). At reveal time, `_answer_reveal`
  and both game-over summary builders send derived facts for most types but *forward raw fields*
  for two: `multi_select` gets the whole `answer_points` array, fill-in-the-blank gets
  `acceptedAnswers`. `schemas/game.py`'s `QuestionPublic` is **not** a safeguard — nothing uses
  it. New payloads should derive what they need, and nothing answer-revealing may go out in
  `NEW_QUESTION` or `SYNC_STATE`.
- **Question prompts are bleach-sanitized on write**, in one place:
  `services/game_management_service.sanitize_prompt`, which every create, update, and import path
  (admin and host) goes through. This is load-bearing because `report_service` renders prompts as
  raw HTML. A new write path that skips that service is a stored-XSS hole in the report.
- **Every permission goes through the existing role and grant system**: `users.role`,
  `user_course_access` HOST/PLAYER rows, `user_game_access` grants, the `common.dependencies`
  guards, and the `game_service` assertions, which check `ADMIN` first. HOST is a live per-course
  grant checked on every request; `game_sessions.host_user_id` is a historical record that grants
  nothing on its own. The frontends' role checks (`GET /api/auth/me`) are display only. See
  [services/CLAUDE.md](services/CLAUDE.md).
- **Adding a question type touches about two dozen sites across the backend and all three
  frontend apps** — the full list is in the next section. The existing `multi_select` type missed
  the four `report_service` sites and renders as an empty card there; treat it as the worked
  example of what incomplete looks like.
- **The host drives every state transition.** Players only submit answers; phase changes come from
  `HOST_ADVANCE`. Don't add player-triggered advancement.
- **The game row lock serializes writes to one game** (T8, `services/game_lock.py`). Every write
  that can change a game's export (question create/update; title, description, max players;
  image upload, replace, description edit), every image delete, question reorder, `create_room`
  and `start_game` first call `lock_game` (`SELECT … FOR UPDATE` on the `games` row). After it,
  every read that feeds a check, merge, version increment or start is a **fresh read** —
  `fresh(stmt)`, a locking read with `populate_existing`. A plain read would return the pre-lock
  REPEATABLE READ snapshot, or an object SQLAlchemy's identity map never refreshes
  (`expire_on_commit=False`). Take every locking read **before** assigning to any ORM object:
  `populate_existing` overwrites unflushed changes. Only the game row, the image row being
  changed and the session being started are locked exclusively; everything else is `FOR SHARE`,
  so answer inserts (which share-lock question rows) never wait on an edit. Lock timeouts and
  deadlocks become a 409.
- **Image references are ids inside `config`** (`prompt_image_id`, `option_images`, `image_id`),
  defined once in `services/image_refs.py`. Every referenced id must be in the **same game's**
  library: `image_service.assert_in_library` on every write, under the lock. Every read resolves
  them scoped to the question's own game, so a missing id and another game's id both count as
  "no image" — and so does a legacy value planted before T8. Nothing that reads references may
  raise on a malformed one.
- **Every game's export stays within 40 MiB, so it re-imports** (the export rule). Covered writes
  are refused (409) if they'd push the exact export size, measured by `bundle_service` with the
  exporter's own serializer, past the limit, unless the size didn't grow. A new write path for
  questions or game metadata must run the same check (`image_service.check_export_change`), and
  must measure questions **as stored**, read back after the write
  (`game_management_service._as_stored`): MySQL's `FLOAT` and JSON columns can store a number
  longer than it was sent. Only strings and integers (metadata, image fields) are exact before
  they're written.
- **No image changes version while a session of its game is live** (LOBBY or IN_PROGRESS *and*
  its Redis room exists). Replace and delete are refused, and `create_room` takes the game lock
  and commits before responding. Links carry the version and are cached `immutable`, so this is
  what keeps every link a session received valid for that session.
- **Don't `ORDER BY` in SQL while selecting question content or image blobs.** MySQL 8 carries
  the selected columns (`config`, blobs) in its 256 KB sort buffer and fails with error 1038 once
  one row is bigger. For questions that takes a ~300 KB question, well inside the 1 MiB request
  limit; one stored image of that size is enough. T8's reads sort by `(order_index, id)` in Python
  (`image_service.question_order`), and the export's image read by id. The pre-existing
  game-over summaries in `game_service` still sort in SQL.

## Adding a question type — the canonical checklist

This is the one list; the other context files point here. Nothing enforces it — the DB column is
a plain string, and every client has a silent fallback for an unknown type.

**Backend**

- `schemas/admin.py` — the type regex in `QuestionCreate` *and* `QuestionUpdate`, plus a branch
  in `QuestionCreate.validate_structure`. **Image references** (T8): its `_validate_image_refs`
  allows `prompt_image_id` on every type and `option_images` only on `multiple_choice` /
  `multi_select`, and refuses `image_id` everywhere. A canvas type (T7) must be allowed, and
  required, to carry `image_id` there. A type whose options can be images must be added to
  `_IMAGE_OPTION_TYPES` *and* rendered by both apps' option-image code.
- `services/game_service.py` — `calculate_score`, `record_answer`'s distribution key,
  `get_player_question_summary`'s reveal, and `get_host_question_summary` (twice: its reveal *and*
  its distribution rebuild).
- `websocket/gateway.py` — `_answer_reveal`; `_question_payload` if the client needs extra
  non-revealing fields (as fill-in-the-blank needs `editDistance`); and `on_submit_answer` if the
  answer shape needs validating (only `multi_select` has a check today).
- `services/report_service.py` — `_answer_reveal`, `_extract_answer_key`, the type-badge map (and
  its CSS class), and the chart dispatch in `build_session_report`.

**Frontend** (see [frontend/CLAUDE.md](../../frontend/CLAUDE.md))

- host — `types/game.ts` (the type unions and `AnswerReveal`), `QuestionPage`'s type label,
  `ResultsPage.buildBars` and the word-cloud branch, and `GameOverPage`'s `QuestionCard`.
- player — `types/game.ts`, a `QuestionPage` branch (the answer controls),
  `ResultsPage.describeAnswer`, and `GameOverPage`'s `describePlayerAnswer` /
  `describeCorrectAnswer`.
- host — also `QuestionEditorPage` (the authoring form, moved here from admin): form state,
  `questionToForm`, `formToPayload` (including the `points_value` derivation), and the list
  rendering.

## Gotchas

### Single-process assumptions (the theme to know before scaling)

Three separate mechanisms silently assume one worker process:

- **Gateway bookkeeping.** `_sid_ctx`, `_timer_tasks`, and `_abandon_tasks` are module-level
  dicts. Across processes this produces a real failure, not just inefficiency: a host who
  reconnects to a different process leaves the original process's abandon task running, which
  then abandons a game that has a live host. Detail in
  [websocket/CLAUDE.md](websocket/CLAUDE.md).
- **Rate limiting.** slowapi defaults to in-memory storage — counters are per-process and reset on
  restart.
- **JWT keys.** When `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY` are unset *or fail to decode*,
  `auth_service` generates an ephemeral keypair in memory. Each worker then signs with a different
  key, so tokens fail verification depending on which worker answers — presenting as random
  intermittent 401s.

Running more than one uvicorn worker requires addressing all three.

### Development / production divergence

`APP_ENV` and `STRESS_TEST_KEY` change behavior in ways that make local testing unrepresentative:

- Rate limiting is **fully disabled** in development (the key function returns a fresh UUID per
  request), and bypassed in production for any request carrying a matching `X-Stress-Key`.
- socket.io uses the in-memory client manager in development and the Redis pubsub manager in
  production, so multi-instance routing bugs cannot appear locally.
- `POST /api/auth/login` accepts a bare `netid` with no password when `is_development` — an
  impersonate-anyone path if `APP_ENV` is ever wrong in production.
- API docs are served only in development; SQLAlchemy `echo` follows the same flag.
- `STRESS_TEST_KEY` must be unset when not actively load-testing. It is both a rate-limit bypass
  (HTTP and WebSocket) and a verbose-logging switch.
- `CORS_ORIGINS` is also socket.io's Origin allowlist, and a list there does **not** implicitly
  allow same-origin. The Vite dev proxy forwards the browser's `Origin` unchanged, so every dev
  port that opens a socket (`:5173` host, `:5174` player) must be listed. The `config.py` default
  omits `:5174`; the player socket works in dev only if `.env` adds it, and there is no
  `.env.example` to say so.

### Data model and numbers

- **Points are floats** (migration `003`), but several places still assume integers: the host
  `GAME_OVER` truncates `maxPossibleScore` with `int()`, and a few dict annotations say `int`.
  Exact float equality is also used for `is_correct` on two question types.
- **Deletes destroy history.** `game_sessions.game_id` and `session_scores.user_id` are NOT NULL
  with no cascade, so `DELETE /api/admin/games/{id}` and `DELETE /api/admin/users/{id}` explicitly
  delete dependent score rows first. Removing a game erases every session ever played from it.
- **An answered question can't be deleted — by design, with a 409.** `Question.scores` has no
  cascade and `session_scores.question_id` is NOT NULL, so this used to 500. Now
  `game_management_service.delete_question` counts the sessions that answered it and refuses,
  pointing the author at editing or export-and-reimport. Deleting the scores instead would
  rewrite past results.
- **Games belong to one course** (`games.course_id`, migration `004`), and only admins can change
  it. Games that predate `004` are unassigned (NULL): invisible to hosts and unrunnable by anyone
  until an admin assigns a course, since a room's course must equal its game's.
- **Nothing in MySQL prevents double-scoring.** `session_scores` has no unique constraint on
  (session, user, question). The only guard is the gateway's Redis `has_answered` check, a
  separate read before `record_answer` writes — not atomic. A double-submit race, or losing the
  Redis `answered` set, can insert duplicate rows, and the "authoritative" MySQL leaderboard then
  counts them. Guest merge can produce the same duplicates.
- `Question.type` is an unconstrained `String(50)`; the allowlist exists only in Pydantic.

### Redis lifecycle

- Only `room:{code}` carries a TTL (90 min, refreshed on activity). The player, question,
  answered, and distribution keys have none and are removed only by an explicit
  `delete_room_state` — which `complete_game` never calls, so completed sessions leak keys.
- Room scans use `KEYS` in three places (room cap check, room cleanup, health). Fine at the
  50-room cap; the wrong primitive if that grows.
- **Redis loss is not survivable mid-game.** Losing a player's keys is fine — `JOIN_ROOM`, which
  every client sends on every (re)connect, re-authorises and re-adds a missing player. But if
  `room:{code}` is lost (TTL, eviction, flush), `HOST_ADVANCE` fails with "Session state not
  found" and every submit gets "No active question"; the game cannot continue. Nothing rebuilds
  it: `restore_from_mysql` is never called, and wouldn't write to Redis if it were.
  (`REJOIN_ROOM` has similar re-add logic, but no client sends it.)

### Security notes carried up from the leaves

- `temp` tokens (15 min, delivered to the browser in a **URL fragment** by the OAuth2 callback)
  authenticate every protected endpoint and every socket connection, not just `/exchange-temp`.
- `oauth2-callback` trusts `X-Auth-Request-User` / `X-Auth-Request-Email`. Correct only behind a
  proxy that strips client-supplied copies; if the backend port is directly reachable, those
  headers are a login-as-anyone.
- Any `ADMIN` can join any session as HOST, and WebSocket rate limiting is per-IP — the wrong unit
  for a lecture hall behind one NAT — with the IP taken from the first `X-Forwarded-For` entry,
  which the client controls, so the limit is also easy to bypass.
- HTTP rate limiting keys on `request.client.host`. Behind nginx, uvicorn (run without
  `--forwarded-allow-ips`) most likely reports the proxy's address, so every client shares one
  bucket — five logins per 15 minutes for a whole class. Unverified against the production
  deployment; check before relying on it.
- **Guest identity is an unverified email.** `POST /api/auth/guest` with an existing guest's email
  returns a token *for that guest*, so anyone who knows the address gets the account and its
  scores. And because `users.email` is UNIQUE, a student who once joined as a guest with their
  school email will likely 500 on their first SSO sign-in (`get_or_create_user_by_netid` inserts
  a second row with the same email).
- `POST /api/game/sessions/{id}/merge-guest` is not actually scoped to that session.
- `GET /api/health` is unauthenticated, and its `activePlayers` figure is always 0 (wrong key
  prefix).

## Working here

```bash
# from backend/ (alembic.ini lives there), with DATABASE_URL exported
alembic upgrade head                      # required before first run; bootstrap waits for it
ruff check . && ruff format .
docker compose exec backend <command>

# tests are NOT under backend/ — they live at the repo root (see ../CLAUDE.md)
cd tests/unit && pytest -v
```

Startup blocks (logging every 5s) until the schema exists rather than crashing — under
`uvicorn --reload` a startup crash leaves the container "Up" while nothing serves, which is much
harder to diagnose than a retry loop.
