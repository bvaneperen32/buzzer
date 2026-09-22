# backend/app/routers

## Purpose

The HTTP API. Four `APIRouter`s, all mounted under `/api` by [main.py](../main.py). These handle
authentication, authoring/administration, host-side room management, and health — everything
*except* live gameplay, which goes over [websocket/](../websocket/CLAUDE.md).

## Contents

- `health.py` — `GET /api/health`. Pings MySQL and Redis, reports `healthy` / `degraded`, and
  returns active room and player counts. Unauthenticated.
- `auth.py` — `/api/auth/*`: `login` (username+password, plus a development-only `netid`
  fallback), `oauth2-callback` (consumes Traefik/oauth2-proxy's `X-Auth-Request-*` headers),
  `exchange-temp`, `guest`, refresh, and logout. Owns the refresh-token cookie helpers.
- `game.py` — `/api/game/*`: the host's pre- and post-game surface. Resource discovery
  (`my-courses`, `my-games`, `my-active-sessions`), room create/info/ping, session delete, guest
  listing and merging, and CSV export.
- `admin.py` — `/api/admin/*`, ~960 lines, every endpoint behind `require_admin`. CRUD for
  courses, rosters, games, questions, and users; access grants; game JSON import/export; session
  listing; and score/report downloads.

## How it fits in

The thin edge of the backend. A handler should authenticate via a `common.dependencies` guard,
validate through a `schemas` model, delegate to a `services` function, and return. Business logic
that shows up here — scoring rules, lifecycle transitions — belongs in a service instead.

Three guard levels are in use: `require_admin` (all of `admin.py`), `require_user` (host actions
in `game.py`; excludes guests), and `get_current_user` (any authenticated user, including
guests). Two endpoints have no guard at all: `GET /api/health` and
`GET /api/game/rooms/{room_code}/ping`.

## Gotchas

### Destructive behavior

- **`DELETE /api/admin/games/{id}` destroys play history.** Because `game_sessions.game_id` is
  NOT NULL, the handler explicitly deletes every `SessionScore`, then every `GameSession`, then
  the game. Deleting a game silently erases the results of every session ever played from it.
  Same shape in `DELETE /api/admin/users/{id}`, which deletes that user's scores first.
- **`POST /api/game/sessions/{session_id}/merge-guest` ignores its own `session_id`.** It selects
  `SessionScore` by `user_id` alone with no session filter, so merging a guest from one session
  moves that guest's scores across *every* session they ever played, then deletes the guest user.
  The admin twin at `POST /api/admin/users/merge-guest` is global by design; this one reads as
  though it is scoped, and isn't.

### Validation and typing

- **`merge_guest_for_session` takes `body: dict`**, bypassing Pydantic entirely — no schema, no
  `extra="forbid"`, no OpenAPI documentation of its shape. It hand-rolls its own presence checks
  and raises `ConflictError` (409) for what is really a validation failure (422). It's the one
  un-schema'd request body in the package.
- `PUT /api/admin/games/{gid}/questions/{qid}` does not re-run structural validation — see
  [schemas/CLAUDE.md](../schemas/CLAUDE.md). `POST /api/admin/games/import` *does* (it constructs
  a real `QuestionCreate` per question), so the import path is stricter than the edit path.
- **Question prompts are bleach-sanitized on write** (`_PROMPT_TAGS = ["b", "i", "br", "u"]`) in
  create, update, and import. This is load-bearing: `report_service` renders prompts as raw HTML.
  Any new write path for questions must bleach too.

### Health endpoint

- **`activePlayers` is always 0.** `health.py:39` does `SCARD` on `f"{key}:players"` where `key`
  is `room:{code}`, producing `room:{code}:players` — but the real key is
  `session:{session_id}:players`. The key it queries never exists.
- `activeRooms` counts every `room:*` key regardless of status, so COMPLETED rooms still lingering
  within their 90-minute TTL inflate it. It also uses `KEYS`, like the other room scans.
- The endpoint is unauthenticated and reports infrastructure status and occupancy.

### Auth

- `POST /api/auth/login` accepts a bare `netid` with no password, guarded by an
  `is_development` check inside the handler. That guard is the only thing between the dev
  fallback and impersonation of any netid — `APP_ENV` must never be `development` in production.
- `oauth2-callback` trusts `X-Auth-Request-User` / `X-Auth-Request-Email` headers. That's correct
  *only* behind an oauth2 proxy that strips client-supplied copies; if the backend port is ever
  reachable directly, those headers are a login-as-anyone.
- The redirect target is allowlisted (`_ALLOWED_REDIRECT_PATHS`) — keep it that way rather than
  accepting an arbitrary `redirect_to`.
- Interpolating `auth_data` into inline `<script>` in `_redirect_html` is safe only because the
  content is a server-built JSON string; don't extend it with user-controlled values.
- **Rate limits exist on only three endpoints**, all in `auth.py`: login (5/15min), exchange-temp
  (10/min), guest (10/15min). Nothing on admin, game, or health — and the limiter is per-process
  and in-memory (see [common/CLAUDE.md](../common/CLAUDE.md)).
- `GET /api/game/rooms/{room_code}/ping` is public and unthrottled, which makes it a room-code
  probe. The 31-character, 6-position code space is large enough that this isn't a practical
  enumeration risk, but the endpoint confirms existence and status to anyone.

### Consistency and performance

- **Commit discipline is mixed.** Some handlers call `await db.commit()` explicitly
  (`create_course`, `import_game`, `delete_session`); others rely on `get_db`'s commit-on-exit
  (`update_course`, `update_question`, `delete_user`, `delete_game`). Both work — know which
  you're in before adding code after a write. `create_course` does `flush` → `refresh` → `commit`,
  which is one step more than it needs.
- Many modules import `ForbiddenError`, `ConflictError`, and SQLAlchemy helpers *inside* function
  bodies rather than at the top. It's inconsistent with the top-level imports in the same files;
  there's no import cycle forcing it here.
- N+1 queries in several list endpoints: `my_active_sessions` fetches the game and course per
  session, and `get_room` loads every `Question` row just to `len()` them instead of issuing a
  `COUNT`.
