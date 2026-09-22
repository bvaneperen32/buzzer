# backend/app

## Purpose

The Buzzer backend: a FastAPI + python-socketio application serving a real-time party quiz
platform. A Host drives game state from a large screen; Players answer from phones. HTTP handles
everything around a game (auth, authoring, room setup, exports); WebSocket handles the game
itself.

## Contents

**Application-level modules**

- `main.py` — assembly. Builds the FastAPI app, registers the rate limiter, CORS, and exception
  handlers, mounts the four routers under `/api`, and runs `bootstrap_admin()` in the lifespan
  hook. The final `asgi_app` wraps FastAPI *inside* `socketio.ASGIApp` rather than mounting
  socket.io as a sub-app — Starlette's `Mount` strips the path prefix and breaks socket.io's own
  path matching. Don't "clean this up" into a `Mount`.
- `config.py` — `Settings` (pydantic-settings, from env / `.env`) and the `settings` singleton.
  `is_development` and `is_stress_test_mode` gate a surprising amount of behavior; see below.
- `database.py` — async SQLAlchemy engine, `AsyncSessionLocal`, the declarative `Base`, and the
  `get_db` dependency (commits on clean exit, rolls back on exception).
- `redis_client.py` — a lazily-created module-level `Redis` singleton with `decode_responses=True`
  (so all Redis reads are `str`, never `bytes`).

**Subpackages** — each has its own `CLAUDE.md` with file-level detail and gotchas:

- [`routers/`](routers/CLAUDE.md) — the HTTP API: `health`, `auth`, `game`, `admin`.
- [`websocket/`](websocket/CLAUDE.md) — the socket.io gateway, event constants, socket auth.
- [`services/`](services/CLAUDE.md) — all business logic: auth, game rules, scoring, Redis state,
  roster import, exports, HTML reports.
- [`schemas/`](schemas/CLAUDE.md) — Pydantic request/response contracts.
- [`models/`](models/CLAUDE.md) — SQLAlchemy ORM models.
- [`common/`](common/CLAUDE.md) — auth dependencies, error hierarchy, logging, rate limiter.
- `migrations/` — Alembic env and three revisions (`001` initial schema, `002` answer_data on
  scores, `003` integer → float points). No context file; run with `alembic upgrade head`.

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
distributions) and is treated as a fast cache that may vanish. When they disagree, MySQL wins.

**The one import rule that bites:** `common/dependencies.py` imports from
`services/auth_service.py`, so `common` is not a leaf. Services must never import
`common.dependencies`.

## Cross-cutting invariants

- **`answer_data` never reaches a client.** Three layers enforce it independently: `QuestionPublic`
  omits it, the gateway's `_answer_reveal` emits only derived facts, and the game-over summaries
  build their own reveal dicts. Any new payload must derive, never forward.
- **Question prompts are bleach-sanitized on write**, in all three admin write paths. This is
  load-bearing because `report_service` renders prompts as raw HTML. A new write path that skips
  bleach is a stored-XSS hole in the report.
- **Adding a question type touches six-plus sites**: the regex in `schemas/admin.py` (×2),
  `calculate_score`, `record_answer`'s distribution keys, both game-over summary builders, the
  gateway's `_answer_reveal`, and three functions in `report_service`. The existing `multi_select`
  type missed the `report_service` ones and renders as an empty card there — treat it as the
  worked example of what incomplete looks like.
- **The host drives every state transition.** Players only submit answers; phase changes come from
  `HOST_ADVANCE`. Don't add player-triggered advancement.

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

### Data model and numbers

- **Points are floats** (migration `003`), but several places still assume integers: the host
  `GAME_OVER` truncates `maxPossibleScore` with `int()`, and a few dict annotations say `int`.
  Exact float equality is also used for `is_correct` on two question types.
- **Deletes destroy history.** `game_sessions.game_id` and `session_scores.user_id` are NOT NULL
  with no cascade, so `DELETE /api/admin/games/{id}` and `DELETE /api/admin/users/{id}` explicitly
  delete dependent score rows first. Removing a game erases every session ever played from it.
- `Question.type` is an unconstrained `String(50)`; the allowlist exists only in Pydantic.

### Redis lifecycle

- Only `room:{code}` carries a TTL (90 min, refreshed on activity). The player, question,
  answered, and distribution keys have none and are removed only by an explicit
  `delete_room_state` — which `complete_game` never calls, so completed sessions leak keys.
- Room scans use `KEYS` in three places (room cap check, room cleanup, health). Fine at the
  50-room cap; the wrong primitive if that grows.
- Redis loss is survivable by design — `rejoin_room` re-authorises and re-adds a missing player —
  but `restore_from_mysql` does not actually repopulate Redis despite its name.

### Security notes carried up from the leaves

- `temp` tokens (15 min, delivered to the browser in a **URL fragment** by the OAuth2 callback)
  authenticate every protected endpoint and every socket connection, not just `/exchange-temp`.
- `oauth2-callback` trusts `X-Auth-Request-User` / `X-Auth-Request-Email`. Correct only behind a
  proxy that strips client-supplied copies; if the backend port is directly reachable, those
  headers are a login-as-anyone.
- Any `ADMIN` can join any session as HOST, and WebSocket rate limiting is per-IP — the wrong unit
  for a lecture hall behind one NAT.
- `POST /api/game/sessions/{id}/merge-guest` is not actually scoped to that session.
- `GET /api/health` is unauthenticated, and its `activePlayers` figure is always 0 (wrong key
  prefix).

## Working here

```bash
alembic upgrade head                      # required before first run; bootstrap waits for it
pytest                                    # or: pytest --cov=app
ruff check . && ruff format .
docker compose exec backend <command>
```

Startup blocks (logging every 5s) until the schema exists rather than crashing — under
`uvicorn --reload` a startup crash leaves the container "Up" while nothing serves, which is much
harder to diagnose than a retry loop.
