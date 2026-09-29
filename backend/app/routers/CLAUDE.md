# backend/app/routers

## Purpose

The HTTP API. Six `APIRouter`s, all mounted under `/api` by [main.py](../main.py). These handle
authentication, administration, host-side management (games, questions, rosters, session
downloads), room setup, and health — everything *except* live gameplay, which goes over
[websocket/](../websocket/CLAUDE.md).

## Contents

- `health.py` — `GET /api/health`. Pings MySQL and Redis, reports `healthy` / `degraded`, and
  returns active room and player counts. Unauthenticated.
- `auth.py` — `/api/auth/*`: `login` (username+password, plus a development-only `netid`
  fallback), `oauth2-callback` (consumes Traefik/oauth2-proxy's `X-Auth-Request-*` headers),
  `exchange-temp`, `guest`, refresh, logout, and `me` (the freshly loaded current user, so the
  frontends can show role-appropriate UI — display only). Owns the refresh-token cookie helpers.
  `guest` refuses a room with `allow_guests` off (403, `game_service.GUESTS_NOT_ALLOWED`)
  *before* creating the guest user.
- `game.py` — `/api/game/*`: the host's pre- and post-game surface. Resource discovery, room
  create/info/ping, session delete, guest listing and merging, and CSV export. `my-courses` lists
  only courses the user HOSTs (every course for admins); `my-games` only games with a game grant
  on a course they HOST (every game, unassigned included, for admins); `my-active-sessions` only
  rooms the socket would let them rejoin as HOST. Room create takes `allow_guests`, and
  **commits before responding** (T8), so a room is visible to the image live guard before its
  code is returned and the game row lock it took is released promptly. Session delete and the
  raw CSV export go through `game_service`'s session-host check.
  `GET /sessions/{id}/images` (T8) is the projector's image link map: it needs **both**
  `user_can_host_session` and the *current* game rule (`user_can_use_game`), because the
  session check alone ignores the game grant. It doesn't depend on the Redis room, so a
  projector refreshed on the game-over screen still gets links.
- `admin.py` — `/api/admin/*`, ~760 lines, every endpoint behind `require_admin`. CRUD for
  courses, users, and access grants; guest merge; session listing; any-status score/report
  downloads. Its game, question, and roster endpoints are thin wrappers over
  `game_management_service` and `roster_service`, which `host.py` shares. It owns the grant side
  effects: revoking a HOST row, or downgrading one to PLAYER, calls
  `revoke_game_grants_for_course`; `PUT /games/{id}` with a new `course_id` revokes grants held by
  users who don't HOST the new course; `POST /users/{id}/game-access` returns 409 unless the game
  has a course and the user HOSTs it.
- `host.py` — `/api/host/*`: host management for the courses a user HOSTs. Game
  create/import/edit/export, question CRUD and reorder, roster list/import/patch, the session
  list with COMPLETED-session report and CSV downloads, and (T8) a game's image library:
  `GET/POST /games/{id}/images`, `PUT/PATCH/DELETE /games/{id}/images/{image_id}` (list,
  upload, replace, description, delete). An image of another game is a 404. Upload runs, in
  order: the game rule, `clean_description`, the advisory `precheck_library`, `process_upload`
  (processing slot, capped read, pipeline), the locked `image_service` write, then an explicit
  `commit` before the response. Every endpoint is `require_user` plus a `game_service` course,
  game, or download assertion; those let `ADMIN` through, so admins do host work here too. There
  is deliberately no game delete (405) — that stays admin-only.
- `images.py` — `GET /api/images/{id}/v{version}/{full|small}?t=…` (T8): serves image bytes
  through signed links. **No auth dependency** — the signed `t` is the whole check (an `<img>`
  can't send a bearer header). A bad, expired or mismatched token → 403; a missing image or a
  superseded version → 404; never 401. Success carries the stored content type (WebP for
  `small`), `nosniff`, `Content-Disposition: inline` with no filename, and
  `Cache-Control: private, max-age=<until expiry>, immutable`. Errors carry no caching headers,
  so a retry after a failure really re-requests. It **commits before returning**, so the pooled
  connection is released before the body goes out; left to `get_db`, every slow phone download
  would hold one of the connections answer handling needs.

## How it fits in

The thin edge of the backend. A handler should authenticate via a `common.dependencies` guard,
validate through a `schemas` model, delegate to a `services` function, and return. Business logic
that shows up here — scoring rules, lifecycle transitions — belongs in a service instead.

Three guard levels are in use: `require_admin` (all of `admin.py`), `require_user` (all of
`host.py` and the host actions in `game.py`; excludes guests), and `get_current_user` (any
authenticated user, including guests — e.g. `/auth/me`). Outside `auth.py` (whose login, OAuth2,
guest, refresh, and logout endpoints are necessarily open), two endpoints have no guard at all:
`GET /api/health` and `GET /api/game/rooms/{room_code}/ping`.

Per-resource access sits on top of the guard, in `game_service` assertions that check `ADMIN`
first (course rule, game rule, download rule, session delete — see
[services/CLAUDE.md](../services/CLAUDE.md)). **`admin.py` is never loosened for hosts**: host work
gets its own endpoints in `host.py`, and both call the same service functions.

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
- **The two merge-guest endpoints behave differently**, despite the host one's docstring saying it
  "delegates to the same logic". When no user has the target netid, the admin one **promotes the
  guest in place** (sets `netid`, `role = "USER"`) — so a typo'd netid silently creates a
  real-looking account — while the host one returns 404. The admin one also strips whitespace from
  the netid; the host one only lowercases. Either can leave duplicate score rows if the real user
  also played the same session (see [models/CLAUDE.md](../models/CLAUDE.md)).
- **Deleting an answered question is refused with a 409** on both routers ("This question has
  been answered in N session(s)… edit it instead, or export the game and import a copy.").
  `session_scores.question_id` is NOT NULL with no cascade, so this used to 500; the fix
  deliberately doesn't delete the scores, which would rewrite past results.

### Validation and typing

- **Both merge-guest handlers take `body: dict`** (`merge_guest_for_session` in `game.py` and
  `merge_guest` in `admin.py`), bypassing Pydantic entirely — no schema, no `extra="forbid"`, no
  OpenAPI documentation of their shape. They hand-roll presence checks and raise `ConflictError`
  (409) for what is really a validation failure (422). They're the only un-schema'd request
  bodies in the package.
- **Question `PUT` (both routers) validates the merged result**: the service overlays the sent
  non-null fields on the stored question and builds a `QuestionCreate` from it, so an edit can't
  leave a question in a state `POST` would reject. Failure is a 422 `UNPROCESSABLE` whose
  `message` joins each error's `msg` with `"; "`. An explicit `null` means "leave unchanged".
  Import failures raise the same `UnprocessableError`, so their body is `{"error", "message"}`,
  not the old `{"detail"}`.
- **Question prompts are bleach-sanitized on write** in exactly one place,
  `game_management_service.sanitize_prompt` (tags `b`, `i`, `br`, `u`), which create, update, and
  import all use. This is load-bearing: `report_service` renders prompts as raw HTML. Any new
  write path for questions must go through that service.

### Host router and access specifics

- **Route order in `host.py` matters**: `POST /games/import` is declared before the
  `/games/{game_id}` routes so "import" is never parsed as an id.
- **Only admins move a game between courses.** The host `PUT /games/{id}` takes `GameUpdate`
  (`extra="forbid"`), so a `course_id` there is a 422; the admin `PUT` takes `AdminGameUpdate`,
  which can set or change a course but never clear it.
- `POST /api/host/games/import` checks course access *before* reading the uploaded file. Both
  import endpoints read it with `image_service.read_capped` (40 MiB import limit), which bounds
  only the handler's own copy — FastAPI has already received the whole body, so nginx's
  per-route limit is the real gate (see [backend/CLAUDE.md](../../CLAUDE.md)).
- **Report images follow the game rule, not the download rule** (T8). The host report endpoint
  passes `include_images=user_can_use_game(...)`, so a host who has lost the game still gets the
  report's text but not its (current) images; the admin endpoint passes `True`.
- **Downloads through `host.py` and `game.py` require a COMPLETED session for every role**,
  admins included. A non-owner gets the 403 first, so the status isn't revealed to them. The admin
  router's own `/sessions/{id}/export` and `/report` stay any-status: they're the only way to fetch
  an ABANDONED session's partial results.
- The host CSV export defaults to `format=canvas`, as the admin one does. The legacy
  `GET /api/game/sessions/{id}/export` stays raw CSV; no frontend calls it.

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
  and in-memory, and behind nginx it probably keys every client on the proxy's address, giving
  the whole class one shared bucket (see [common/CLAUDE.md](../common/CLAUDE.md)).
- **`POST /api/auth/guest` identifies a guest by email alone.** An existing guest's email returns a
  token *for that guest* — no verification — and a non-guest email gets a 409 that confirms an
  account exists. See [services/CLAUDE.md](../services/CLAUDE.md).
- `GET /api/game/rooms/{room_code}/ping` is public and unthrottled, which makes it a room-code
  probe. The 31-character, 6-position code space is large enough that this isn't a practical
  enumeration risk, but the endpoint confirms existence and status to anyone.

### Consistency and performance

- **Commit discipline is mixed.** Some handlers call `await db.commit()` explicitly
  (`create_course`, the game create/import and question create handlers on both routers,
  roster import, `delete_session`); others rely on `get_db`'s commit-on-exit (`update_course`,
  game and question update/delete/reorder, `delete_user`, `delete_game`, the grant endpoints).
  Both work — know which you're in before adding code after a write. `create_course` does
  `flush` → `refresh` → `commit`, which is one step more than it needs.
- **Commit-on-exit lands after the response is sent.** FastAPI 0.141 runs a `yield`
  dependency's cleanup after responding, so for the second group above the client has its 2xx
  before `get_db` commits. A write followed immediately by a read can see the old state — which
  is exactly the frontends' "save, then reload" pattern (e.g. revoking HOST, then reloading the
  user, can briefly still list the removed game grants). `test_t4_access.py` polls for writes to
  become visible (`wait_until`). Kept deliberately. *Noted, not scheduled.*
- Many modules import `ForbiddenError`, `ConflictError`, and SQLAlchemy helpers *inside* function
  bodies rather than at the top. It's inconsistent with the top-level imports in the same files;
  there's no import cycle forcing it here.
- N+1 queries in several list endpoints: `my_active_sessions` runs `user_can_host_session` and
  fetches the game and course per session, and `get_room` loads every `Question` row just to
  `len()` them instead of issuing a `COUNT`. The worst is `admin.py`'s `list_sessions`: four
  queries per session (game, course, host, player count) over an unpaginated list of every
  session ever played. The host twin, `GET /api/host/sessions`, avoids that via
  `game_service.list_sessions_for` (joins plus one grouped count).
