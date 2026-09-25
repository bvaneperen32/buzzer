# backend

## Purpose

The Buzzer server: a single FastAPI + python-socketio process backed by MySQL and Redis. It
serves the REST API all three frontends use, runs the real-time game over WebSocket, and owns
every game rule. The clients display state; this decides it.

## Contents

- [`app/`](app/CLAUDE.md) — the application. Routers, WebSocket gateway, services, schemas,
  models, and shared infrastructure, each with its own `CLAUDE.md`.
- `requirements.txt` — pinned loosely by lower bound. Note `bcrypt<4.0`, which is a **deliberate
  pin**: passlib 1.7.x misdetects bcrypt 4.x as broken. Don't bump it without upgrading passlib.
- `Dockerfile` — `python:3.12-slim` plus the build deps `asyncmy` and `cryptography` need. It
  has **no `CMD`**; the run command comes from `docker-compose.yml`.
- `alembic.ini` — `script_location = app/migrations`. `sqlalchemy.url` is intentionally blank;
  `app/migrations/env.py` fills it from the **process environment** (`os.environ["DATABASE_URL"]`),
  *not* from `settings` — so `.env` is not read and the `config.py` default doesn't apply. Compose
  exports it via `env_file`; on the Windows venv, run `scripts\load_env.ps1` first or alembic gets
  an empty URL.
- `scripts/generate_keys.py` — generates the RS256 keypair for `JWT_PRIVATE_KEY` /
  `JWT_PUBLIC_KEY`. Run it once and put the base64 values in `.env`.
- `logs/` — not a directory here: compose bind-mounts the **repo-root** `./logs` over
  `/app/logs`, so logs land in `<repo>/logs`, not `backend/logs`.

## How it runs

`docker-compose.yml` (at the repo root) builds this directory and runs:

```
uvicorn app.main:asgi_app --reload --host 0.0.0.0 --port 8000
```

`asgi_app` is socket.io wrapping FastAPI, not FastAPI itself — see
[app/CLAUDE.md](app/CLAUDE.md) for why that nesting is required.

The container mounts `./backend` at `/app`, so `/app` *is* this directory. MySQL and Redis are
gated by healthchecks; the app additionally retries in-process until the schema exists, so a
fresh stack needs:

```bash
docker compose up
docker compose run --rm backend alembic upgrade head    # required on a fresh DB
```

Until that runs, startup logs `admin_bootstrap_waiting_for_schema` every 5 seconds and serves
nothing. That's deliberate — crashing wouldn't self-heal under `--reload`.

## Working here

```bash
docker compose exec backend alembic upgrade head
docker compose exec backend alembic revision --autogenerate -m "description"
docker compose logs -f backend
ruff check . && ruff format .
python scripts/generate_keys.py
```

**Tests do not live here.** They're at the repo root in `tests/unit/` and `tests/integration/`,
each with its own `pytest.ini` (`asyncio_mode = auto`), and integration has its own
`requirements.txt`. The Dockerfile's build context is `./backend`, so **the test suites are not
in the image** — `docker compose exec backend pytest` finds nothing. Run them from the host with
the venv active:

```bash
cd tests/unit && pytest -v          # mocked DB/Redis, no stack needed
cd tests/integration && pytest -v   # needs the compose stack up
```

## Architecture

```
HTTP      app/routers/  ─┐
                         ├─→ app/services/ ─→ app/models/ (MySQL)  +  Redis
WebSocket app/websocket/ ─┘
```

Entry points are thin — authenticate, validate, delegate. All game logic is in `services/`, which
takes `db` and `redis` as arguments so it's testable without a request context.

**MySQL is authoritative; Redis is live state.** Every answer is written to `session_scores` as
it's scored, and leaderboards are aggregated from MySQL. Redis holds room metadata, the player
set, the current question, and answer distributions. When they disagree, MySQL wins — with one
hole: `session_scores` has no unique constraint on (session, user, question), so the only guard
against double-scoring is a non-atomic Redis check (see [app/CLAUDE.md](app/CLAUDE.md)).

## Gotchas

### This process cannot currently be scaled horizontally

Three independent mechanisms assume a single worker:

- **Gateway bookkeeping** — `_sid_ctx`, `_timer_tasks`, `_abandon_tasks` are per-process dicts. A
  host who reconnects to a different process leaves the original process's abandon task running,
  which then **abandons a game that has a live host**.
- **Rate limiting** — slowapi's default in-memory storage; counters are per-process.
- **JWT keys** — if `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY` are unset *or fail to base64-decode*, each
  worker silently generates its own ephemeral keypair, so tokens verify only on the worker that
  issued them. Presents as random intermittent 401s.

`gunicorn` is in `requirements.txt` and the compose command runs a single uvicorn worker, so the
gap only appears the moment someone adds workers. Fix all three first.

### Redis is configured to evict game state

`docker-compose.yml` runs Redis with `--maxmemory 512mb --maxmemory-policy allkeys-lru`. Under
memory pressure **any** key is evictable, including the live room state of a game in progress —
not just stale data. Two things make that more likely than it sounds: only `room:{code}` has a
TTL (player, question, answered, and distribution keys have none and are never cleaned up after a
normal `complete_game`), and fill-in-the-blank answer distributions use raw player text as hash
fields, so cardinality is unbounded. `appendonly yes` means Redis survives a restart, but
eviction is silent. MySQL remains correct either way.

### Dev and production diverge substantially

`APP_ENV` and `STRESS_TEST_KEY` change real behavior, and compose hardcodes
`APP_ENV=development`:

- Rate limiting is **fully disabled** in development, and bypassed in production for any request
  carrying a matching `X-Stress-Key`.
- socket.io uses the in-memory client manager in dev and Redis pubsub in production, so
  multi-instance routing bugs cannot reproduce locally.
- `POST /api/auth/login` accepts a bare `netid` with **no password** when `is_development`.
- API docs are exposed only in development; SQLAlchemy `echo` follows the same flag.

`STRESS_TEST_KEY` must be unset when not actively load-testing — it is both a rate-limit bypass
and a verbose-logging switch.

### Data model

- **Points are floats** since migration `003`, but integer assumptions remain (the host's
  `maxPossibleScore` is `int()`-truncated; some annotations still say `int`). Two question types
  compare `is_correct` by exact float equality.
- **Deletes destroy history.** `game_sessions.game_id` and `session_scores.user_id` are NOT NULL
  with no cascade, so the admin delete endpoints explicitly remove dependent rows first. Removing
  a game erases every session ever played from it. Question delete has no such step, so deleting
  any question that has ever been answered fails with a 500.
- **`Question.type` is an unconstrained `String(50)`** — the allowlist lives only in a Pydantic
  regex. Adding a question type touches about two dozen sites across the backend *and all three
  frontend apps*; the canonical checklist is in [app/CLAUDE.md](app/CLAUDE.md). The existing
  `multi_select` missed four of them in `report_service`.

### Security notes

- **`temp` tokens authenticate everything**, not just `/exchange-temp` — and they reach the
  browser in a URL fragment from the OAuth2 callback.
- **`oauth2-callback` trusts `X-Auth-Request-User` / `X-Auth-Request-Email`.** Safe only behind a
  proxy that strips client-supplied copies; if port 8000 is directly reachable, those headers are
  a login-as-anyone. Compose publishes 8000 to the host.
- WebSocket connection rate limiting is **per IP** — the wrong unit for a lecture hall behind one
  NAT — and it takes the IP from the first `X-Forwarded-For` entry, which the client controls.
- HTTP rate limiting probably keys every client on nginx's address (uvicorn isn't told to trust
  the proxy), so the whole class may share one bucket. Unverified against production.
- **Guest identity is an unverified email.** Anyone who knows a guest's address gets that guest's
  token and scores; and a student who once joined as a guest with their school email will likely
  500 on first SSO sign-in (`users.email` is UNIQUE).
- socket.io enforces `CORS_ORIGINS` against the browser's `Origin`. The `config.py` default omits
  the player's dev port (`:5174`), so the player socket only works in dev if `.env` adds it.
- `GET /api/health` is unauthenticated, and its `activePlayers` figure is always 0 (it queries a
  key prefix nothing writes).

Full detail for each of these is in [app/CLAUDE.md](app/CLAUDE.md) and the per-directory files
beneath it.
