# T4 — UI Restructuring (Host and Admin Interfaces)

Status: design agreed and goldfish-tested; revised on 2026-09-23 and 2026-09-24 (see Section 12). Not yet
implemented. Source requirement: `instructions.md` → T4.
This document is the implementation contract. If implementation reveals a flaw, update this
file first, then the code.

---

## 1. Problem

The admin console currently owns work that belongs to hosts: authoring games and questions,
managing course rosters, and downloading session results. Hosts can only log in, pick a course
and a game, and run it. T4 requires:

- **Host app gains:** session summary download (HTML) and per-player scores download (CSV) for
  completed sessions; roster view/update for courses they HOST; game creation and question
  add/edit/delete/reorder; and **course-specific games** — a game belongs to one course at
  creation and is accessible only to that course's hosts and players.
- **Admin app becomes admin-first:** user accounts, course creation, and granting course/game
  access come first; host and player actions remain available to admins but are secondary.
- Everything must go through the existing role/permission system — no bypassed access checks.

---

## 2. Background the implementer needs

Read `backend/CLAUDE.md` and `frontend/CLAUDE.md` (and the per-directory `CLAUDE.md` files they
link) for architecture. The facts this design depends on:

- **Roles.** `users.role` is `ADMIN`, `USER`, or `GUEST`. There is no "host account": a host is a
  `USER` with a row in `user_course_access` (`role = 'HOST'`) for a course. The primary key of
  `user_course_access` is `(user_id, course_id)` — one role per user per course.
  `user_game_access` is a separate yes/no grant per `(user_id, game_id)`.
- **Guards.** `common/dependencies.py`: `get_current_user` (any valid token; reloads the user
  from the DB every request — the token's `role` claim is never trusted), `require_user` (not a
  guest), `require_admin` (role `ADMIN`). Per-resource checks are plain functions in
  `services/game_service.py` (`assert_host_can_use_course`, `assert_host_can_use_game`) that
  let `ADMIN` through first.
- **Every handler in `routers/admin.py` declares `require_admin` itself**; the router has no
  router-level guard.
- **Game/question write logic lives inside `routers/admin.py` handlers**, including bleach
  sanitization of question prompts (`_PROMPT_TAGS = ["b","i","br","u"]`). This sanitization is
  load-bearing: `services/report_service.py` renders prompts as raw HTML.
- **`games` has no course today.** A course and a game are paired only when a room is created
  (`game_sessions.course_id`).
- **The frontend never knows the current user's role.** No `/me` endpoint exists; route guards
  only check that a `token` exists in `localStorage`.
- **The three frontend apps share no code.** In production they share one origin (nginx serves
  `/host/`, `/player/`, `/admin/` on :8080) and one `localStorage` key `token`; in dev each
  runs on its own port (host 5173, player 5174, admin 5175) with its own token.
- **Every app's `lib/api.ts` reads `err.detail`**, but `BuzzerError` responses are
  `{"error": code, "message": ...}` — so every server error message is currently discarded and
  shown as `HTTP <status>`.

---

## 3. Technical plan (summary)

**Permission principle (normative).** Every change in this spec works through the existing role
and permission system: `users.role`, `user_course_access` HOST/PLAYER grants,
`user_game_access` grants, the `common/dependencies.py` guards, and the `game_service`
assertion functions (which check `ADMIN` first). There is no new role, no new grant table, no
endpoint that skips a guard, and no permission decided by the frontend. Section 4 shows how each
change complies.

1. **Games get a course.** New nullable `games.course_id`; existing games stay unassigned.
   Every game-create path requires a course. Only admins can change a game's course.
2. **One access rule for a game** (running *and* editing): `ADMIN`, or HOST on the game's
   course **and** a game grant. The creator of a game is granted automatically. Game grants
   never outlive what makes them effective: losing HOST on a course removes that user's grants
   for the course's games, and moving a game to another course removes grants held by users who
   don't HOST the new course.
3. **Host endpoints are new; admin endpoints are not loosened.** A new `routers/host.py`
   (`/api/host/*`) serves host management; `routers/admin.py` stays admin-only. Host endpoints
   admit admins (existing pattern), so **admins do host work through the host app**.
4. **No duplicated logic.** Game/question logic moves out of `admin.py` into a new service
   that both routers call. Bleach, the course requirement, and the new validation live there
   once.
5. **Host app gets a sidebar shell** (Play, Games, Rosters, Sessions). Admin's Games, Question
   Editor, Roster, and Sessions pages move into the host app. Game screens stay full-screen.
6. **Admin app is reorganized:** Users (landing), Courses, Guests, then a secondary "Host
   tools" section of links into the host app.
7. **`GET /api/auth/me`** lets both apps show role-appropriate UI (display only — the backend
   still enforces everything).
8. **Guests** can be switched off per room ("Allow guests", default on).
9. **Session downloads** through the host and game routers require the session to be
   COMPLETED — for everyone, admins included. Non-admins must also have hosted it and still HOST
   its course (admins bypass only those two). The admin router's download endpoints stay
   any-status behind `require_admin` (Section 9).
10. **Acting on a past session as its host** — downloading it, deleting it, or rejoining it over
    the socket as HOST — requires having hosted it **and** currently holding HOST on its course.
    One predicate, `user_can_host_session`, decides all three.

---

## 4. Access rules (normative)

Terms: a **HOST grant** is a `user_course_access` row with `role = 'HOST'` (a `PLAYER` row is
not one); a **game grant** is a `user_game_access` row.

Every rule below is enforced by a `game_service` assertion (or, for listings, a query) that
checks `ADMIN` first, per the Section 3 principle. How each change stays inside the existing
system:

| Change | How it stays inside the system |
|---|---|
| `/api/host/*` | `require_user`, plus the course, game, or download assertion on every endpoint |
| `admin.py` | `require_admin` on every handler, unchanged; nothing is loosened |
| Stricter game rule | Uses only the existing HOST and game-grant tables |
| Creator auto-grant | Writes an ordinary game grant, only after the course rule passes; admins can see and revoke it in `UserDetailPage`. It is **the only grant a non-admin can cause** |
| Grant side effects (losing HOST, moving a game) | Only delete grant rows; never add any |
| `allow_guests` | A per-room restriction: it can deny a join, never grant one. Checked in the existing join path (`/auth/guest` and `authorise_player`) |
| Session delete, downloads, socket HOST join | Assertions in `game_service` sharing one predicate (`user_can_host_session`), following the existing pattern |
| `/me` and frontend role checks | Display only. Every admin-only UI action (moving a game, deleting a game, the session status filter) calls an endpoint that enforces `ADMIN` server-side |

| Rule | Allowed when | Denied response |
|---|---|---|
| **Course rule** (manage a course's roster; create a game in it; import into it) | `ADMIN` (course must exist → else 404), or HOST grant on the course | 403 `You do not have HOST access to this course`. Implemented by `assert_host_can_use_course`: verify it both 404s a missing course for admins and checks `role == 'HOST'` (not merely that an access row exists); make it do both if it doesn't. Message unchanged |
| **Game rule** (view/edit a game and its questions; export it; run it) | `ADMIN` (game must exist → else 404), or: `game.course_id` is not null **and** HOST grant on `game.course_id` **and** a game grant on the game | 403 `You do not have access to this game` — also for non-admins when the game doesn't exist (don't reveal existence; matches today) |
| **Room creation** | Course rule on `course_id` **and** game rule on `game_id` **and** `game.course_id == course_id` (applies to admins too) | 409 `This game belongs to a different course` when ids mismatch (unassigned games can therefore never be run until assigned) |
| **Session-host check** (`user_can_host_session`; used by the three rules below) | `ADMIN`, or `session.host_user_id == user.id` **and** HOST grant on `session.course_id` | — (a predicate; each rule below words its own denial) |
| **Download rule** (session HTML report and CSV, on the host and game routers) | `session.status == 'COMPLETED'` **for every role**, and additionally the session-host check. `ADMIN` bypasses only the session-host check. 404 if the session doesn't exist | Non-admin: 403 `Only the session's host can download it` if the session-host check fails (checked first, so status isn't revealed to non-owners); then, for everyone, 409 `Downloads are available once the session has completed`. (The admin router's own download endpoints stay any-status; see Section 9) |
| **Delete a session** (`DELETE /api/game/sessions/{id}`) | Session-host check, any status | 403 with the message the endpoint raises today; 404 if the session doesn't exist (as today) |
| **Socket HOST join** (`JOIN_ROOM` with `role = HOST`, and `REJOIN_ROOM`'s role inference) | Session-host check (`ADMIN` keeps its existing override) | the gateway's existing `ERROR` emit (both clients treat it as fatal, which is the intended outcome). The session's recorded host (`host_user_id == user.id`) who fails the check gets `You no longer have HOST access to this course`; any other user asking for HOST on `JOIN_ROOM` keeps today's `Not authorised as host for this session` |
| **Create game** | Course rule on the chosen course | as course rule |
| **Change a game's course** | `ADMIN` only (via the admin endpoint) | host endpoint rejects the field (422, `extra="forbid"`) |
| **Delete a game** | `ADMIN` only (existing admin endpoint, unchanged). Admins reach it from an admin-only button on the host app's Games page | no host endpoint exists (405) |
| **Edit a roster entry** (`PATCH .../roster/{roster_id}`, host and admin) | Course rule on the path's course **and** the entry's `course_id` equals the path's course | 404 `Roster entry {rid} not found in course {cid}` — for admins too; no cross-course edits |
| **Delete a question** | Game rule, and the question has **no** `session_scores` rows | 409 `This question has been answered in N session(s) and can't be deleted — edit it instead, or export the game and import a copy.` |
| **Grant a game** (admin endpoint) | Target user holds a HOST grant on `game.course_id` | 409 `Assign this game to a course before granting access` (unassigned) / 409 `User must be a HOST of this game's course before being granted the game` |
| **Guest join** | `game_sessions.allow_guests` is true for the room | 403 `This game isn't open to guests — sign in with your school account` |

Side effects:

- **Game creation** by a non-admin inserts a game grant for the creator. Admins get no grant
  (they bypass the rule).
- **Losing HOST** on course C — via `DELETE /api/admin/users/{id}/course-access/{C}` when the
  row was HOST, **or** via `POST /api/admin/users/{id}/course-access` changing an existing HOST
  row to PLAYER (today's code overwrites the role in place) — deletes that user's game grants
  for every game whose `course_id == C`.
- **Changing a game's course** (admin `PUT` with a `course_id` different from the current one,
  including assigning a course to a previously unassigned game) deletes the game grants on that
  game held by users who don't hold HOST on the *new* course. Grants of users who HOST the new
  course stay and remain effective.

Principle recorded for reviewers: **HOST is a persistent per-course grant checked live on every
request. Losing it removes course-scoped access immediately. `game_sessions.host_user_id` is a
historical record, not a permission — on its own it grants nothing; `user_can_host_session`
always pairs it with a live HOST grant. Game grants never outlive what makes them effective: the
side effects above delete them, so an unrelated HOST grant later can't silently revive one.**

---

## 5. Detailed implementation

### 5.1 Database — migration `004`

New file `backend/app/migrations/versions/004_course_specific_games.py`, `revision = "004"`,
`down_revision = "003"` (follow the header/format of `003_float_points.py`).

Upgrade:
- `games.course_id` — `INTEGER`, **nullable**, FK → `courses.id` (constraint name
  `fk_games_course_id`, no `ondelete`; courses have no delete endpoint), plus index
  `ix_games_course_id`. No backfill: existing games stay NULL ("unassigned").
- `game_sessions.allow_guests` — `BOOLEAN`, NOT NULL, server default true.

Downgrade: drop the FK constraint, the index, and both columns.

Models:
- `models/game.py` `Game`: add `course_id: Mapped[int | None]` (FK `courses.id`, nullable,
  indexed). No relationship is required; code in this spec loads courses explicitly.
- `models/session.py` `GameSession`: add `allow_guests: Mapped[bool]` (not null, Python default
  `True`, server default true).

### 5.2 Error type

`common/exceptions.py`: add `UnprocessableError(BuzzerError)` → HTTP 422, code
`UNPROCESSABLE`. It produces the standard `{"error", "message"}` body (a readable string),
unlike FastAPI's request-validation 422 which carries a `detail` array. The service raises it
for bundle-parsing and question-structure failures.

### 5.3 Schemas

`schemas/admin.py`:
- `GameCreate` — **unchanged.** It validates the `game` section of v1 import bundles and must not
  gain `course_id` (it's `extra="forbid"`; adding a required field would break every existing
  game file, and course ids are database-specific, so they don't belong in portable files).
- New `GameCreateRequest(GameCreate)` — adds `course_id: int` (must be > 0). Request body for
  `POST /api/admin/games` and `POST /api/host/games`.
- `GameUpdate` — fields unchanged (title/description/max_players). Request body for the **host**
  update. It **must** be `extra="forbid"` — add it if absent; test 7 depends on a `course_id`
  field being a 422.
- New `AdminGameUpdate(GameUpdate)` — adds `course_id: int | None` (> 0 when present). Request
  body for the **admin** update. A course can be set or changed, never cleared.
- `GameResponse` — add `course_id: int | None`.
- `AdminSessionItem` — unchanged; also used as the response item of `GET /api/host/sessions`.

`schemas/game.py`:
- `RoomCreateRequest` — add `allow_guests: bool = True` (optional so existing callers, including
  the integration test helper, keep working under `extra="forbid"`).
- `MyGameItem` — add `course_id: int | None`, `course_name: str | None`, and
  `course_semester: str | None`. It must also carry every field the host `GamesPage` renders or
  pre-fills — at least `title` and `description` (inline edit pre-fills it). Compare against the
  fields admin's `GamesPage` reads from `GameResponse` and add whichever are missing.
- The `my-courses` item schema — must include `id`, `name`, and `semester`; add `semester` if
  absent. Courses are name + semester, so same-name courses in different semesters are expected,
  and every course label in both apps shows "Name (Semester)".

`schemas/auth.py`:
- New `MeResponse` — `id: str`, `username: str | None`, `netid: str | None`,
  `display_name: str | None`, `role: str`.

### 5.4 New service — `services/game_management_service.py`

Owns every game/question write. Services may import `models`, `schemas`, and
`common.exceptions`, but **never `common.dependencies`** (import cycle — see
`backend/app/CLAUDE.md`). Function names below are normative.

- `PROMPT_TAGS = ["b", "i", "br", "u"]` and `sanitize_prompt(text)` — bleach with those tags, no
  attributes, `strip=True`. **After this change this is the only place question prompts are
  sanitized**; remove `_PROMPT_TAGS` and the bleach calls from `routers/admin.py`.
- `SUPPORTED_IMPORT_VERSION = 1` (moved from `admin.py`).
- `create_game(db, *, meta: GameCreate, course_id, creator: User) -> Game` — 404 if the course
  doesn't exist; insert; flush + refresh; if `creator.role != "ADMIN"`, insert a game grant for
  the creator.
- `update_game(db, game, changes: GameUpdate, *, course_id=None) -> Game` — apply the non-None
  values of `GameUpdate`'s own three fields only (title/description/max_players). Don't iterate
  the passed model's fields: the admin passes an `AdminGameUpdate`, and its `course_id` must go
  through the check below rather than a plain `setattr`. If `course_id` is given (admin path
  only) and differs from the current value: 404 if that course doesn't exist, else set it and
  call `game_service.revoke_game_grants_without_host(db, game.id, course_id)`.
- `list_questions(db, game_id) -> list[Question]` — ordered by `order_index`.
- `create_question(db, game_id, body: QuestionCreate) -> Question` — sanitize prompt; auto
  `order_index` = max + 1 when absent (today's behavior).
- `update_question(db, game_id, question_id, body: QuestionUpdate) -> Question` — 404 `Question
  {qid} not found in game {gid}`. Build the merged question: current stored values overlaid with
  only the fields the client actually sent with a non-null value
  (`exclude_unset=True, exclude_none=True`). An explicit `null` means "leave unchanged", as it
  does today; overlaying it would write NULL into NOT NULL columns such as `order_index`.
  **Validate the merged result by constructing a `QuestionCreate` from it**; on a Pydantic
  validation error raise `UnprocessableError` whose message joins each error's `msg` with
  `"; "`. Then sanitize the prompt and apply. This closes the gap where `PUT` skipped the structural checks `POST` applies
  (T7 explicitly warns about it). A question already stored in an invalid state will refuse edits
  until they make it valid — acceptable, and the message says why.
- `delete_question(db, game_id, question_id)` — 404 as above; count distinct `session_id` in
  `session_scores` for this question; if > 0 raise `ConflictError` with the Section 4 message;
  else delete. (Today this path 500s because `session_scores.question_id` is NOT NULL.)
- `reorder_questions(db, game_id, order)` — today's logic (409 if `order` isn't exactly the
  game's question ids).
- `export_game(db, game) -> (filename, bytes)` — today's bundle (format `buzzer/game`, version
  1, `game` = title/description/max_players, `questions` without ids/order). **No course in the
  bundle.**
- `import_game(db, raw: bytes, *, course_id, creator: User) -> Game` — parse JSON, check
  `format` and `version`, validate `game` with `GameCreate` and each question with
  `QuestionCreate` (all failures → `UnprocessableError` with the same messages as today), then
  `create_game(...)` and insert questions in file order with sanitized prompts.

Commit discipline: keep each endpoint's current behavior — create and import handlers commit
explicitly after the service returns (as `admin.py` does today); update, delete, and reorder rely
on `get_db`'s commit-on-exit.

### 5.5 Changes to existing services

`services/game_service.py`:
- `assert_host_can_use_game(db, user, game_id) -> Game` — implement the **game rule** (Section 4)
  and return the `Game`. Currently returns nothing and checks only the game grant.
- `assert_host_can_use_course` — make sure it implements the **course rule** exactly (Section 4:
  404 for an admin on a missing course; `role == 'HOST'` for everyone else).
- New `user_can_host_session(db, user, session) -> bool` — the **session-host check**: `True`
  for `ADMIN`; otherwise `session.host_user_id == user.id` **and** a HOST grant on
  `session.course_id`. The three assertions below and `my-active-sessions` all call it, so the
  rules can't drift apart.
- New `assert_can_download_session(db, user, session_id) -> GameSession` — implement the
  **download rule**; 404 if the session doesn't exist.
- New `assert_can_delete_session(db, user, session_id) -> GameSession` — implement the **delete
  a session** rule; 404 if the session doesn't exist.
- New `revoke_game_grants_for_course(db, user_id, course_id) -> int` — delete the user's game
  grants for games with `course_id == course_id`; return the count.
- New `revoke_game_grants_without_host(db, game_id, course_id) -> int` — delete the game grants
  on `game_id` held by users with no HOST grant on `course_id`; return the count.
- New `list_sessions_for(db, user, status: str | None) -> list[dict]` — returns
  `AdminSessionItem`-shaped dicts, newest first. **Admin:** all sessions, optional `status`
  filter. **Non-admin:** only sessions where `host_user_id == user.id`, `status == 'COMPLETED'`,
  and the user currently holds HOST on `course_id`; the `status` argument is ignored. Build it
  with joins (game, course, host) and one grouped count of distinct `session_scores.user_id` per
  session — don't copy the four-queries-per-row pattern in `admin.py` `list_sessions`. The host
  join **must be LEFT OUTER** (`host_user_id` is nullable, and the admin list must not lose
  host-less sessions), and so must the score count (a LOBBY session has no scores → count 0).
  Outer-join any other nullable reference too.
- `create_room(..., allow_guests: bool = True)`: run the course rule, then
  `game = assert_host_can_use_game(...)`, then `if game.course_id != course_id: ConflictError`
  (Section 4 message), then the existing room-cap check. Store `allow_guests` on the new
  `GameSession`. Remove the dead `await db.get(Game, game_id)` line.
- `authorise_player`: for `GUEST`, if `session.allow_guests` is false raise `ForbiddenError`
  (Section 4 message); otherwise unchanged. This is the backstop; the primary check is in the
  guest endpoint.

`services/roster_service.py`:
- New `list_roster(db, course_id)` and `patch_roster_entry(db, course_id, roster_id, patch:
  RosterEntryPatch)` — move the inline logic out of `admin.py`'s `list_roster` and
  `patch_roster_entry` handlers verbatim, so the admin and host endpoints share it, **with one
  required addition**: `patch_roster_entry` raises 404 `Roster entry {rid} not found in course
  {cid}` when the entry's `course_id` differs from the `course_id` argument (add the check if the
  moved code lacks it). For admins this never mattered; once hosts call it, it's the only thing
  stopping a host of course A from editing course B's roster via A's URL.

### 5.6 Router changes

**`routers/auth.py`**
- New `GET /api/auth/me` — `Depends(get_current_user)`, returns `MeResponse` from the freshly
  loaded user.
- `POST /api/auth/guest` — after the existing room-status check, load the `GameSession` by the
  room state's `session_id`; if it exists and `allow_guests` is false raise `ForbiddenError`
  (Section 4 message) **before** creating the guest user.

**`routers/admin.py`** — every handler keeps `require_admin`; no access is loosened.
- `POST /games` — body `GameCreateRequest` → `create_game(creator=admin)`; commit.
- `GET /games`, `GET /games/{id}` — `GameResponse` now includes `course_id`.
- `PUT /games/{id}` — body `AdminGameUpdate` → `update_game(..., course_id=body.course_id)`
  (which revokes grants on a course change — Section 4 side effects).
- `DELETE /games/{id}` — unchanged.
- Question list/create/update/delete/reorder and `GET /games/{id}/export` — thin wrappers over
  the service. Each first calls `assert_host_can_use_game(db, admin, game_id)`, which 404s a
  missing game for admins, so the service never receives a nonexistent game id (otherwise
  `create_question` would fail on the foreign key with a 500).
- `POST /games/import` — multipart: `file` **and a required form field `course_id`** →
  `import_game(course_id=..., creator=admin)`; commit. Missing `course_id` → 422.
- `GET /courses/{id}/roster` and `PATCH /courses/{id}/roster/{rid}` — call the new
  `roster_service` functions (so the admin PATCH gains the same-course check too).
- `POST /users/{id}/course-access` — if an existing row is HOST and the new role is PLAYER, call
  `revoke_game_grants_for_course` before updating the role.
- `DELETE /users/{id}/course-access/{course_id}` — if the row being deleted is HOST, call
  `revoke_game_grants_for_course` first.
- `POST /users/{id}/game-access` — after loading the game: 409 if `game.course_id` is null; 409
  if the target user has no HOST grant on `game.course_id` (messages in Section 4). Existing
  "already granted → no-op" behavior stays.
- Session list/export/report endpoints — unchanged, still any status. The admin UI stops calling
  them. They're deliberately kept as the only path to download an ABANDONED session's partial
  results (see Section 9).

**`routers/game.py`**
- `GET /my-courses` — normative contract (the host app's Play, Games, Rosters, create, and
  import all depend on it): **admin:** every course; **non-admin:** only courses where the user
  holds a HOST grant (PLAYER rows excluded). Items carry `id`, `name`, `semester`. Verify today's
  behavior and change it to match if it differs; test 2 asserts it.
- `GET /my-games` — **admin:** all games, including unassigned, with the course fields from a
  left outer join on courses. **Non-admin:** games where the user has a game grant **and** a HOST
  grant on `game.course_id` (unassigned games never match). Order by course name, semester, then
  title. Returns `MyGameItem` (Section 5.3).
- `GET /my-active-sessions` — for non-admins, return only sessions that pass
  `user_can_host_session`, so Play never offers a Rejoin the socket would refuse. Admins
  unchanged.
- `DELETE /sessions/{id}` — replace the inline host-or-admin check with
  `assert_can_delete_session`. Everything after the check (MySQL and Redis cleanup) is unchanged.
- `POST /rooms` — pass `body.allow_guests` to `create_room`.
- `GET /sessions/{id}/export` — replace the inline `host_user_id` check with
  `assert_can_download_session`. No frontend calls this endpoint; it's tightened so it can't
  serve as a looser path around the download rule. Output format stays raw CSV.
- Room info/ping and the guest endpoints — unchanged.

**`websocket/gateway.py`** — the only gateway change:
- In `on_join_room`, the check that grants the `HOST` role calls
  `game_service.user_can_host_session` in place of the bare `host_user_id`-or-`ADMIN` test. On
  failure, `_emit_error` with `You no longer have HOST access to this course` if the user is the
  session's recorded host (`host_user_id == user.id`), otherwise with today's `Not authorised as
  host for this session`. `REJOIN_ROOM` (no client sends it, but it must not become the bypass)
  infers HOST with the same predicate; if the recorded host fails it, emit the same `You no
  longer have HOST access to this course` error and stop, rather than seating them as a player.
  Everyone else follows today's player path.
- The check runs only at join. A host whose HOST is revoked mid-game keeps their current socket;
  their next reconnect is refused, and the existing 5-minute abandon timer then ends the game
  (Section 9). Nothing else in the gateway changes.

**New `routers/host.py`** — `APIRouter(prefix="/host", tags=["host"])`, mounted in `main.py`
under `/api` next to the others. Every endpoint depends on `require_user` (guests excluded), then
applies the resource check shown. Route order matters: declare `POST /games/import` before any
`/games/{game_id}` route.

| Method | Path | Check | Request | Response / behavior |
|---|---|---|---|---|
| POST | `/games` | course rule on `body.course_id` | `GameCreateRequest` | 201 `GameResponse`; creator auto-granted; commit |
| POST | `/games/import` | course rule on form `course_id` | multipart `file` + form `course_id` (required) | 201 `{"game_id": int}`; commit. Check access before parsing the file |
| GET | `/games/{game_id}` | game rule | — | `GameResponse` |
| PUT | `/games/{game_id}` | game rule | `GameUpdate` (a `course_id` field → 422) | `GameResponse` |
| GET | `/games/{game_id}/questions` | game rule | — | `list[QuestionResponse]` |
| POST | `/games/{game_id}/questions` | game rule | `QuestionCreate` | 201 `QuestionResponse` |
| PUT | `/games/{game_id}/questions/{question_id}` | game rule | `QuestionUpdate` | `QuestionResponse`; 422 if merged result is invalid |
| DELETE | `/games/{game_id}/questions/{question_id}` | game rule | — | 204; 409 if answered |
| POST | `/games/{game_id}/questions/reorder` | game rule | `QuestionReorder` | 204 |
| GET | `/games/{game_id}/export` | game rule | — | JSON attachment (same as admin) |
| GET | `/courses/{course_id}/roster` | course rule | — | `list[RosterEntryResponse]` |
| POST | `/courses/{course_id}/roster/import` | course rule | `RosterImportPayload` | `RosterUploadResult`; commit (same as admin's) |
| PATCH | `/courses/{course_id}/roster/{roster_id}` | course rule | `RosterEntryPatch` | `RosterEntryResponse` |
| GET | `/sessions` | scoped by role (see `list_sessions_for`) | `?status=` (admins only; ignored otherwise) | `list[AdminSessionItem]` |
| GET | `/sessions/{session_id}/report` | download rule | — | HTML attachment via `report_service.build_session_report` |
| GET | `/sessions/{session_id}/export` | download rule | `format` (`canvas` default \| `raw`), `title`, `sis_domain`, `roster_only`, `per_question` — same meaning as admin's | CSV attachment via `export_service` |

Deliberately absent: `DELETE /games/{game_id}` (hosts can't delete games); course and game
listing (use `GET /api/game/my-courses` and `GET /api/game/my-games`); session delete (the
existing `DELETE /api/game/sessions/{id}`, now under the session-host check). The
server-side Canvas CSV roster upload (`POST /api/admin/courses/{id}/roster`, multipart) gets no
host twin — the ported wizard uses the JSON `/roster/import` path.

**Admins changing a game's course or deleting a game from the host app** call the admin-only
`PUT /api/admin/games/{id}` with `{course_id}` and `DELETE /api/admin/games/{id}`. Those are
admin actions on admin endpoints by an admin user, which is consistent with the "don't loosen
admin endpoints" rule.

### 5.7 Host app (`frontend/host`)

**`src/lib/api.ts`**
- Add `put`, `patch`, `postForm`, and `download`, matching the admin app's implementations
  (`postForm` must *not* set `Content-Type`, so the browser adds the multipart boundary;
  `download` fetches with the bearer header, reads the filename from `Content-Disposition`, and
  saves via a temporary `<a download>`).
- Replace the error extraction (every throw site in the file) with one helper: use `message` if
  it's a string; else `detail` if it's a string; else if `detail` is an array, join each item's
  `msg` with `"; "`; else `HTTP <status>`. Throw an `ApiError` (extends `Error`) that also
  carries the numeric `status`, so callers can tell a 401 from other failures.

**Current-user context** — new `src/lib/me.tsx` (a small React context; `lib/` already holds the
non-page plumbing): type `Me` (`id`, `username`, `netid`, `display_name`, `role`), a
`MeContext`, and a `useMe()` hook. Hold it **in memory only** — never write the role to
`localStorage` (a stored copy goes stale the same way the token claim does).

**New `src/pages/HostShell.tsx`** — the layout route for every non-game page:
- On mount, `GET /auth/me`. While loading show a small spinner. Then:
  - **401** (expired or invalid token) → remove `token` from `localStorage` and navigate to
    `/login`.
  - **Any other failure** (network, 5xx — e.g. the backend restarting under `--reload`) → show
    the error with a Retry button; don't sign the user out.
  - **`role === 'GUEST'`** → a full-screen notice "You're signed in as a guest. Sign in with a
    host account to use Buzzer Host." with a Sign out button (removes `token`, goes to
    `/login`). This is the production shared-token case: a guest joining from the host's machine
    replaces the host's token.
  - Otherwise provide `MeContext` and render the shell.
- Render a fixed left sidebar modeled on admin's `AdminLayout` (same width, dense admin-style
  sizing, not projector-scale), headed **Buzzer Host**: **Play** (`/home`), **Games**
  (`/games`), **Rosters** (`/rosters`), **Sessions** (`/sessions`), and **Sign out** at the
  bottom (removes `token`, goes to `/login`), with the lucide icons `PlayCircle`, `Gamepad2`,
  `Users`, `History`, and `LogOut` respectively. Content in an `<Outlet />`.

**`src/App.tsx` routes**
```
/login                       LoginPage (public)
RequireAuth → HostShell      (layout route, no path)
    /home                    HomePage  — labelled "Play" in the nav
    /games                   GamesPage
    /games/:gameId           QuestionEditorPage
    /rosters                 RostersPage
    /rosters/:courseId       RosterPage
    /sessions                SessionsPage
/game/:code  (RequireAuth → GameLayout, children unchanged)
/  and  *                    → Navigate to /home   (RequireAuth sends signed-out users to /login)
```
The Play page **keeps the path `/home`**, because `GameLayout` and `GameOverPage` already
navigate to `/home` (game abandoned, error screen, "New Game"). Those files don't change. Game
screens stay outside the shell so they remain full-screen, and `GameLayout` still opens its
socket only on game routes.

**`src/pages/HomePage.tsx` → Play**
- Renders inside the shell: drop the full-screen centering wrapper and the Sign Out button (now in
  the sidebar).
- **Course first:** the quiz select stays disabled until a course is chosen. Its options are
  `my-games` filtered to `course_id === selected course` (unassigned games never match). Reset
  the game when the course changes. Auto-select whenever a select has exactly one option: the
  course select on load, the game select after each course change. Course options read
  "Name (Semester)".
- **"Allow guests"** checkbox, default checked; send `allow_guests` in `POST /game/rooms`.
- Empty states: no courses → "You don't host any courses yet — ask an administrator for HOST
  access." A course with no games → "No games in this course yet — create or import one on the
  Games page.", with "Games page" linking to `/games`.
- The Active Sessions card (Rejoin / two-step Delete) is unchanged, except that its course line
  reads "Name (Semester)" (§5.3).

**New `src/pages/GamesPage.tsx`** — ported from admin's `GamesPage`, adapted:
- Loads `GET /game/my-games` and `GET /game/my-courses`. Games are grouped **by `course_id`**
  (not by name — same-name courses in different semesters must stay separate), under headings
  "Name (Semester)", in the server's order (course name, then semester). An **"Unassigned"**
  group is rendered last for games with `course_id === null`; only admins ever receive these
  (the server filters them), so no role check is needed to *show* the group. A hosted course
  with no games gets no heading (the create form's course select still lists it).
- Create form: title, description, max players (kept from admin's form, which this page
  replaces), and a course select (from `my-courses`) → `POST /host/games` → navigate to
  `/games/{id}`.
- Import: an **Import JSON** button opens a small card with a course select and a **Choose
  file** button; picking a file sends `postForm('/host/games/import')` with FormData fields
  `file` and `course_id` → navigate to the new game's editor.
- The course selects in the create form and the import card are pre-selected when the user
  hosts exactly one course.
- Inline edit of title, description, and max players → `PUT /host/games/{id}`.
- **Admins only** (`useMe().role === 'ADMIN'`): a course select on each game row, including
  Unassigned → `PUT /admin/games/{id}` with `{course_id}`. Confirm first with native `confirm()`:
  "Moving this game changes which hosts can use it — hosts who don't host the new course lose
  their access to it. Past sessions keep their original course."
- **Admins only:** a Delete button on each game row → `DELETE /admin/games/{id}`, behind a
  two-step inline confirm (the `SessionsPage` pattern, not `confirm()`) that says "This erases
  every session and score ever recorded for this game." Then reload.
- **Hosts get no delete button.**

**New `src/pages/QuestionEditorPage.tsx`** — a port of admin's `QuestionEditorPage`, with the
same form logic. **Keep `formToPayload`'s `points_value` derivation exactly as is**: host and
player correctness display depend on it (see `frontend/admin/src/pages/CLAUDE.md`). Changes:
- `/admin/games/...` → `/host/games/...`; the "back" button → `/games`.
- Write `…`, `·`, and `±` as literal characters in JSX text and attributes (the admin copy
  renders the escapes verbatim at the lines listed in admin's `pages/CLAUDE.md`).
- The delete action surfaces the server's 409 message (readable thanks to the `api.ts` fix).

**New `src/pages/RostersPage.tsx`** — lists `GET /game/my-courses` (courses the user HOSTs, or
all courses for an admin), each linking to `/rosters/:courseId`.

**New `src/pages/RosterPage.tsx`** — a port of admin's `RosterPage` (CSV column-mapping wizard,
roster list, inline row edit including `is_active`). `/admin/courses/{id}/roster...` →
`/host/courses/{id}/roster...` (list, `/import`, `PATCH /{rosterId}`); back → `/rosters`. The
heading reads "Roster — Name (Semester)", with the course taken from `GET /game/my-courses`.

**New `src/pages/SessionsPage.tsx`** — a port of admin's `SessionsPage`:
- List from `GET /host/sessions`. Hosts see a heading "Completed sessions you hosted"; admins
  see "Sessions". Admins also get the status filter (passed as `?status=`) and the two-step
  Delete (calls the existing `DELETE /game/sessions/{id}`). Hide both from non-admins.
- Download controls (export panel and HTML report) appear **only on COMPLETED rows**, for every
  role, matching the server rule. Admins filtering to other statuses see those rows without
  download controls.
- Export options panel (Canvas vs raw, assignment title, SIS domain, roster-only, per-question)
  → `download('/host/sessions/{id}/export?...')`; HTML report →
  `download('/host/sessions/{id}/report')`.
- Each row's course reads "Name (Semester)" (§5.3), not admin's "Name · Semester".

REST types stay page-local (the admin convention); `course_id` is typed `number | null`
everywhere.

**Unchanged:** `LoginPage`, `GameLayout`, and every `game/*` page.

### 5.8 Admin app (`frontend/admin`)

**New `src/lib/hostApp.ts`** — `HOST_APP_URL` = `import.meta.env.VITE_HOST_APP_URL` if set, else
`/host` when `import.meta.env.PROD`, else `http://localhost:5173`; and `hostAppUrl(path)` that
joins it with a path. (The admin `tsconfig` already includes `vite/client` types.) No `.env`
file is required; the defaults cover dev and production.

**`src/lib/api.ts`** — the same error-extraction helper and `ApiError` (with `status`) as the
host (it's used in three places here: `apiFetch`, `downloadFetch`, `postForm`).

**`src/App.tsx`**
- `RequireAdmin`: no token → `/login`. Otherwise `GET /auth/me`: loading → spinner; 401 →
  remove the token, go to `/login`; any other failure → the error with a Retry button (same as
  `HostShell`); `role !== 'ADMIN'` → a full-screen notice "This console is
  for administrators." with a link to the host app (`hostAppUrl('/home')`, same tab) and a Sign
  out button; otherwise `<Outlet />`.
- `AdminLayout` sidebar, in order: **Users** (`/users`), **Courses** (`/courses`), **Guests**
  (`/guests`); then a divider and a muted **"Host tools"** label with plain anchors (not
  `NavLink`, and visually secondary with an external-link icon; they open in a new tab with
  `target="_blank" rel="noopener noreferrer"`, so the admin console stays open): Play → `hostAppUrl('/home')`,
  Games → `hostAppUrl('/games')`, Rosters → `hostAppUrl('/rosters')`, Sessions →
  `hostAppUrl('/sessions')`. Logout stays at the bottom.
- Routes: remove `/courses/:courseId/roster`, `/games`, `/games/:gameId/questions`, and
  `/sessions`. The catch-all `*` → `/users`.

**`src/pages/LoginPage.tsx`** — after login, navigate to `/users`. (The catch-all and the
post-login redirect must agree.)

**`src/pages/CoursesPage.tsx`** — the per-course roster button becomes an anchor to
`hostAppUrl('/rosters/{id}')` that opens in a new tab (`target="_blank" rel="noopener
noreferrer"`), like the Host tools links.

**`src/pages/UserDetailPage.tsx`** — game-access panel: build the set of course ids where this
user's `course_access` role is `HOST`; offer only games from `GET /admin/games` whose `course_id`
is in that set and not already granted; label each option "Title (Course name, Semester)" using
the courses the page already loads. If the user hosts no courses, show "Grant HOST on a course
first — games can only be granted within courses the user hosts." in place of **Add Games**; if
they host courses but no ungranted game remains, hide **Add Games** (as today). After a HOST
revoke, the page's normal reload shows the removed game grants (removed server-side). Revoking a
**HOST** row warns first with native `confirm()`: "This also removes the user's access to this
course's games." The page has no control for downgrading HOST to PLAYER, and this change adds
none; a downgrade through the API still triggers the server-side revoke (test 6). Its existing
course labels (the course-access list and the add-course picker) change from "Name — Semester"
to "Name (Semester)" (§5.3).

**Delete** `src/pages/GamesPage.tsx`, `QuestionEditorPage.tsx`, `RosterPage.tsx`, and
`SessionsPage.tsx` from the admin app. Their functionality now lives in the host app.

### 5.9 Player app (`frontend/player`)

- `src/lib/api.ts` — the same error-extraction helper. It makes the guest refusal readable on
  `NamePage` instead of `HTTP 403`.
- `src/pages/NamePage.tsx` — when a valid token is already stored, the page collapses to a
  single "Join Game" button (see `frontend/player/src/pages/CLAUDE.md`). Add a "Use a different
  account" link beneath it that removes `token` from `localStorage` and shows the three
  sign-in modes. No role check.

  **Why:** a guest who played in the last 2 hours still holds a valid guest token. They skip
  `/auth/guest` and go straight to the socket, where `authorise_player` refuses them for an
  `allow_guests = false` room with a fatal `error`. "Back to Join" returns them to the same
  collapsed page. Without this link they can't reach NetID sign-in until the token expires.

### 5.10 Scripts and existing tests

- `scripts/seed_demo.py` — `create_game` must send `course_id` (the script already creates a
  course before the game; pass its id through).
- `tests/integration/conftest.py` — the `game_setup` fixture adds `"course_id": course_id` to the
  `POST /api/admin/games` body. This is a required-field update, not a weakening; the fixture's
  assertions and teardown are unchanged. `create_room` and `create_guest_tokens` stay as they are
  (`allow_guests` defaults to true).
- **Audit every caller of a changed contract before step 2.** Search `scripts/` and `tests/` for
  `POST /api/admin/games`, `/games/import`, `/game-access`, `/course-access`, and
  `DELETE /api/game/sessions`:
  - Every game create or import must send a `course_id`.
  - Every game-access grant must come **after** a HOST grant on the game's course (otherwise
    it's the new 409).
  - Every room paired with a game must use the game's own course.
  - Session deletes must use a token that passes the session-host check. The admin token is
    always safe.

  This covers `seed_demo.py`, `simulate_players.py`, `smoke_test_websocket.py`, `conftest.py`,
  and `test_game_flow.py`. Adjust inputs only; never weaken an assertion (T5).

### 5.11 New integration tests (T5) — `tests/integration/test_t4_access.py`

These run against the compose dev stack, where `APP_ENV=development` disables login rate
limiting.

Helpers:
- **Create a local user** via `POST /api/admin/users` with a unique username, log in, and return
  `(user_id, token)`. Grant or revoke access with the admin endpoints.
- **Produce a COMPLETED session:** use the existing `create_questions` helper with the
  `SINGLE_PLAYER` scenario from `scenarios/all_question_types.py`, create the room with the
  **host user's** token (the socket HOST join requires `host_user_id`), register guests with
  `create_guest_tokens`, and drive it with `engine.orchestrator.run_game(host_token=<host
  token>, ...)`, as `test_game_flow.py` does.

Cleanup, in this order, all with the **admin** token (a test may have revoked the host's HOST):
1. **Delete every room a test creates, played or not**, with `DELETE /api/game/sessions/{id}`.
   That also wipes its Redis keys. Rooms otherwise live 90 minutes and may count toward the
   50-room cap, which would make later tests fail spuriously (T5's note about deletion paths
   cleaning up both datastores).
2. Delete created games (`DELETE /api/admin/games/{id}`).
3. Delete created users (`DELETE /api/admin/users/{id}`) last, because sessions reference their
   host user.

Tests (each asserts behavior, not only status codes):

1. **`/me`** — returns `ADMIN` for the admin token and `USER` for a host; 401 without a token.
2. **Create game** — a host with HOST on C creates via `/api/host/games` → 201, `course_id == C`,
   and it appears in the host's `my-games`. Creating in a course the host doesn't HOST → 403.
   `POST /api/admin/games` without `course_id` → 422. `/api/host/games` with an unknown field →
   422. `my-courses` for the host lists C but not a course where they hold only PLAYER; for the
   admin it lists both.
3. **Game rule** — a co-host (HOST on C, no game grant) gets 403 on `GET /api/host/games/{id}` and
   on `POST /api/game/rooms`; after an admin grants the game, both succeed.
4. **Grant validation** — granting a game to a user without HOST on its course → 409.
5. **Room course match** — `POST /api/game/rooms` with a `course_id` other than the game's → 409,
   including for the admin.
6. **Losing HOST** — the user holds HOST on C1 and C2 and a grant on a game in each. Revoke HOST
   on C1 → the user's `game_access` (from `GET /api/admin/users/{id}`) no longer lists the C1
   game **and still lists the C2 game**. Repeat with a downgrade to PLAYER via
   `POST .../course-access` → same result.
7. **Course change** — `PUT /api/host/games/{id}` with `course_id` → 422. Setup: host A holds
   HOST on C only, host B on both C and C2, and both hold a grant on the game. Admin
   `PUT /api/admin/games/{id}` `{course_id: C2}` → 200 with `course_id == C2`. A's grant is
   gone from `GET /api/admin/users/{id}` and A gets 403 on the game; B keeps the grant and
   access.
8. **Question CRUD via host** — create, update, reorder, delete (unanswered → 204) all succeed
   and are reflected in the question list. A `PUT` that makes the question invalid (e.g. a
   `multiple_choice` with one option) → 422 on **both** `/api/host/...` and `/api/admin/...`, and
   the stored question is unchanged.
9. **Sanitization survived the move** — a prompt `<script>x</script><b>y</b>` is stored without
   the `<script>` tag and keeps `<b>` on create **and** update, via both the host and admin
   endpoints.
10. **Answered question** — after a completed session, `DELETE` of one of its questions → 409 on
    both host and admin endpoints (body `message` mentions it's been answered), and the question
    still exists.
11. **Downloads** — for the host's own COMPLETED session: report → 200 `text/html`; export
    `format=raw` and `format=canvas` → 200 `text/csv`. Another user with HOST on the same course
    who didn't host it → 403. The original host after HOST is revoked → 403. A host's LOBBY
    session → 409. The admin gets 200 for the COMPLETED session (no HOST grant needed) and
    **409 for the LOBBY session** on `/api/host/...`. The legacy
    `GET /api/game/sessions/{id}/export` follows the same rule (host + LOBBY → 409; admin + LOBBY
    → 409).
12. **Session list** — a host sees their COMPLETED session and not their LOBBY one; a co-host
    with HOST on the **same** course sees neither; the admin with `?status=LOBBY` sees the lobby
    one.
13. **Import** — `POST /api/host/games/import` with `sample_games/mental_math.json` and
    `course_id` → 201; the new game's question count equals the file's, its `course_id` is the
    one sent, and it appears in the host's `my-games` (proving the import went through
    `create_game`, auto-grant included). Without `course_id` → 422. Into a course the host
    doesn't HOST → 403. The admin import without `course_id` → 422.
14. **Roster** — a host with HOST on C can list, import rows, and PATCH `is_active=false` (the
    entry is then inactive in the list); a user without HOST on C, and a user holding only
    PLAYER on C, → 403 on all three. A host of C patching a roster entry of course D through
    C's URL on `/api/host/...` → 404, the admin doing the same on `/api/admin/...` → 404, and
    the D entry is unchanged.
15. **Guests** — a room created with `allow_guests: false` → `POST /api/auth/guest` 403 with the
    Section 4 message. A guest token obtained from a default room, then used to `JOIN_ROOM` the
    no-guests room over the socket, gets an `error` (the `authorise_player` backstop). A default
    room still accepts guests (also covered by the existing game-flow test).
16. **No host game delete** — `DELETE /api/host/games/{id}` → 405, and the game still exists.
17. **Session delete** — a host whose HOST on the session's course was revoked gets 403 on
    `DELETE /api/game/sessions/{id}` for their own LOBBY room, and the room still exists; the
    admin's delete then succeeds. A co-host on the same course who didn't host it → 403.
18. **Socket HOST join** — a host creates a room; an admin revokes their HOST; a `JOIN_ROOM` as
    HOST with the host's token (via the test engine's socket client) gets an `error`, and
    `my-active-sessions` no longer lists the room. The admin can still join the room as HOST.

---

## 6. Implementation order (one atomic commit per step, per T1)

1. Migration `004` + model columns + schema changes + `UnprocessableError`.
2. Extract `game_management_service` and the roster functions; rewire `admin.py` onto them.
   Apply the Section 5.10 caller audit (the `conftest.py` fixture, `seed_demo.py`, the other
   scripts). **Run the existing integration suite and `tests/unit` now** to prove the refactor
   didn't change admin behavior.
3. `game_service` rule changes (`user_can_host_session` and the assertions built on it),
   `create_room`, `authorise_player`, and the grant/revoke side effects in `admin.py` and
   `update_game`. Re-run both suites (the new grant 409 is where a missed caller shows up).
4. `routers/host.py` + mounting; `routers/game.py` changes (including `my-courses`,
   `my-active-sessions`, session delete); the `gateway.py` HOST-join check; `/api/auth/me`;
   guest-endpoint check.
5. `test_t4_access.py`.
6. Host app: `api.ts`, `me.tsx`, `HostShell`, routes, Play, then Games/QuestionEditor/Rosters/
   Roster/Sessions pages.
7. Admin app: `hostApp.ts`, `api.ts`, `RequireAdmin`, nav, routes, `LoginPage`, `CoursesPage`,
   `UserDetailPage`, delete the moved pages.
8. Player `api.ts` and the `NamePage` "Use a different account" link.
9. Update the context hierarchy (Section 10).

**Land steps 2–7 in one merge request.** From step 2 until step 7 deletes it, admin's old
`GamesPage` can't create or import games (they now require `course_id`), and the host app's
replacement arrives only in step 6. Each commit still builds and passes its tests; the gap is
only in the UI.

---

## 7. Alternatives considered

| Alternative | Why it lost |
|---|---|
| **Copy admin's authoring pages into the host app and keep admin's copies** as secondary | Two copies of the ~790-line question editor and its `points_value` derivation. T7 (new question types), T8 (images), and T9 (theming) all modify the editor, so each would be done twice. Duplicated logic is already this codebase's main bug source (roster CSV parsing ×2, Levenshtein ×3, histogram bucketing ×2, each pair already disagreeing). |
| **Shared frontend package** imported by host and admin | The right long-term shape, but it's a build-infrastructure project inside a UI task: three independent Vite projects with no workspace, per-app Tailwind content scanning, per-app `tsc --noEmit` in CI (T2 risk), and `api.ts`/`Button` differ per app. |
| **Loosen `admin.py` endpoints** to admit course hosts | Breaks the invariant "everything in `admin.py` is admin-only"; hosts would call `/api/admin/...` URLs; the handlers discard the user object, so each would need rewiring for per-resource checks anyway. Rule adopted: widen an endpoint only if host and admin use it identically. |
| **New host endpoints that duplicate handler bodies** | Would duplicate bleach (a stored-XSS guard for the HTML report), the course requirement, and the bug fixes. Hence the shared service. |
| **Course HOST alone grants all the course's games** | Simpler and friendlier for co-hosts, but it makes `user_game_access` meaningless, and T4 names "granting course/game access" as a core admin task. |
| **A shared "dummy" course for legacy games** | Room creation requires the room's course to equal the game's, so dummy-course games could only run in a course with no roster. Making them work needs a special-case bypass of the permission model, and a course everyone can use undoes "course-specific games". |
| **NOT NULL `course_id` with backfill** | No correct backfill exists for games played in several courses or never played. Nullable + unassigned fails closed with no special-case code. |
| **Read the role from the JWT** instead of `/me` | The claim can be up to 2 hours stale after a role change, and the backend deliberately doesn't trust it. |
| **Block guests entirely** / **leave guests unrestricted** | Blocking breaks the existing guest-based game-flow test and party games (T6/T7). Leaving them unrestricted reads as a bypass of "only players of that course". The per-room toggle lets the host decide. |
| **Archive (soft-delete) answered questions** | Every question query would need to choose include vs exclude (live game excludes, historical reports include), and that touches game-loop code. Deferred; the 409 is honest and safe. |
| **Delete the scores along with an answered question** | Rewrites past results, possibly grades already exported to Canvas. |
| **Let hosts delete games** | Not in T4, and a game delete erases every session and score ever recorded for it. |
| **Download rule on `host_user_id` alone** | A host removed from a course would keep downloading its students' scores forever. |
| **Leave game grants in place when a game changes course** | Grants of users who don't HOST the new course would be dead but still listed in `UserDetailPage`, and would silently come back to life if that user later got HOST on the new course for an unrelated reason. Inconsistent with revoking on HOST loss. |
| **Leave session delete and the socket HOST join keyed on `host_user_id`** | Contradicts "`host_user_id` is a historical record, not a permission". A former host could destroy a completed session's scores, or keep driving a room. |
| **Clear the player's token when the socket refuses a guest** | Needs the client to recognise one specific error message — brittle. A "Use a different account" link fixes the dead end with no special-casing. |
| **No game delete anywhere in the UI** | Admins would lose a capability they have today, against T4's "full access retained". |

---

## 8. Defaults chosen without explicit discussion

Review these; change them here before implementing if you disagree.

- All three apps' `api.ts` read the backend's `message` before `detail`. Without this, every new
  409/403 in this spec shows as a bare `HTTP <status>`.
- The host app's `/` and unknown paths go to Play (`/home`) when signed in (today they always
  go to `/login`, even when signed in).
- The new host export defaults to `format=canvas` (matching admin's endpoint); the legacy
  `/api/game/sessions/{id}/export` stays raw.
- Non-admin game creators get an automatic game grant; admin creators don't.
- Hosts may edit, reorder, and delete (unanswered) questions of a game while a LOBBY or
  IN_PROGRESS session of it exists — the same as admins can today (see Section 9).
- The admin console's "Host tools" links open the host app in a new tab.
- Play auto-selects both the course and the game when each has exactly one option.

(These three were added during the goldfish revision, 2026-09-23.)

---

## 9. Risks, limitations, and out of scope

- **Refactor risk.** Moving handler logic into a service can silently change admin behavior, and
  edit/delete/reorder have no existing test coverage. Mitigation: step 2's regression run and
  tests 8–10, which exercise the admin endpoints as well as the host ones.
- **Import error body change.** Admin import failures change from `{"detail": "..."}`
  (HTTPException) to `{"error": "UNPROCESSABLE", "message": "..."}`. The `api.ts` fix handles
  both.
- **Legacy games become invisible to hosts** after the migration, until an admin assigns a
  course on the host app's Games page. Hosts who previously ran a game through a game grant alone
  lose it until then. On fresh installs (seed script, graders) there are no unassigned games.
- **ABANDONED sessions aren't downloadable from the UI.** Downloads require COMPLETED for every
  role, for consistency with T4's "completed session". A session abandoned mid-game (the host was
  away more than 5 minutes) still has real partial scores. Admins can fetch them only through the
  unchanged admin API endpoints `GET /api/admin/sessions/{id}/export` and `/report`. If that
  becomes a real need, extend the rule to allow ABANDONED rather than loosening it per role.
- **Admin reaches game import through "Host tools".** T6 says games import "via the Admin UI or
  API"; the admin now imports in the host app, reached from the admin navigation.
- **Editing a game while it's being played.** Nothing blocks question edits, reorders, or
  deletes while a LOBBY or IN_PROGRESS session of the game exists. That was already true for
  admins, but hosts — the people running games — are now far more likely to do it. A mid-game
  change can alter what later questions show or how they score. Accepted for now. If it bites,
  the fix is a 409 from the question-write service functions while such a session exists.
- **HOST revoked mid-game.** The socket HOST check runs only at join. A host revoked during a
  game keeps driving it until their socket drops; the reconnect is refused, and the existing
  5-minute abandon timer ends the game. Forcing an immediate disconnect needs gateway
  bookkeeping that's out of scope.
- **A demoted admin loses games they authored.** Admin creators get no game grant, since they
  don't need one. If an admin is later demoted to USER, they need HOST on the game's course and
  a grant, like anyone else.
- **Cross-app links in dev.** Admin (:5175) links to host (:5173) with a separate origin and
  token, so an admin signs in again there. End-to-end link behavior is verified under nginx
  (:8080), where both apps share one origin and token.
- **Out of scope (noted, not scheduled):**
  - Archiving questions instead of refusing deletion.
  - A course-centric access view in admin.
  - The roster title-casing divergence between the wizard and `roster_service`.
  - A shared frontend package.
  - The manual `fetch` for the SSO temp-token exchange in the player's and host's `LoginPage`
    (they bypass `api.ts`, so the new error helper doesn't reach them).
  - `README.md` — its admin-interface table and its game-import `curl` (which now needs a
    `course_id` form field) go stale with this change; left unchanged here by owner decision.

---

## 10. Context hierarchy updates (per `.claude/rules/context-sync.md`)

After implementation, update — by editing only what's stale — these `CLAUDE.md` files:
- `backend/app/routers/` — new `host.py`; `admin.py` now delegates to services; grant side
  effects; `/me`; guest check; resolved "delete answered question 500s" and "PUT skips
  validation".
- `backend/app/services/` — new `game_management_service`; `game_service` rule changes and new
  functions (`user_can_host_session` and the assertions on it); `roster_service` additions.
- `backend/app/websocket/` — the "Auth and access" gotcha: HOST join now requires a live HOST
  grant, not just `host_user_id`.
- `backend/app/schemas/` — new/changed schemas; remove the `QuestionUpdate` gotcha, since update
  now validates.
- `backend/app/models/` — `games.course_id`, `game_sessions.allow_guests`.
- `backend/app/CLAUDE.md` and `backend/CLAUDE.md` — migration `004`, the resolved data-model
  gotchas, and the "adding a question type" checklist's admin entry moving to host.
- `frontend/CLAUDE.md`, `frontend/host/src/**`, `frontend/admin/src/**`,
  `frontend/player/src/lib/`, `frontend/player/src/pages/` — the shell, the moved pages, `/me`,
  the host-app links, `NamePage`'s "Use a different account", and the resolved shared
  `err.detail` bug.

---

## 11. Verification

- `alembic upgrade head` on a fresh database, and on an existing database that has games
  (existing games show `course_id = NULL`). `alembic downgrade -1` then `upgrade head` works.
- `cd tests/integration && python -m pytest -v` (venv active) against the compose stack: the
  existing game-flow tests still pass unchanged apart from the Section 5.10 input adjustments,
  and all of `test_t4_access.py` passes. Run with `--fast` as well.
- `cd tests/unit && PYTHONPATH=../../backend python -m pytest -v` passes (CI runs it too).
- `ruff check backend/ scripts/` and `ruff format --check backend/ scripts/`; `tsc --noEmit` in
  each of the three apps.
- Manual pass under nginx (`npm run build`, then `http://localhost:8080`):
  - admin lands on Users; Host tools links open the host app already signed in;
  - a non-admin opening `/admin/` sees the administrators-only notice;
  - a host creates a game in their course, imports a sample game with a course, edits, reorders,
    and deletes an unanswered question;
  - deleting an answered question shows the 409 message;
  - a roster CSV upload works;
  - Play offers only the chosen course's games;
  - "Allow guests" off → a guest join is refused with the message; a guest who already holds a
    token uses "Use a different account" and reaches NetID sign-in;
  - Sessions shows the completed session, and both downloads work;
  - an admin sees the Unassigned group, can assign a course, and can delete a game (two-step
    confirm); a host sees no delete button;
  - the host app with a guest token shows the guest notice, not a page full of 403s.

---

## 12. Revision history

- **2026-09-23 — goldfish test.** A fresh session with only this spec and the context hierarchy
  found these gaps, all now fixed in the sections named:
  - **Security and consistency:**
    - the roster PATCH could cross courses (§4, §5.5);
    - the `my-courses` contract was unstated (§5.6);
    - session delete and the socket HOST join trusted `host_user_id` alone (§3, §4, §5.5,
      §5.6);
    - grants outlived a course change (§4).
  - **Missing behavior:**
    - admins had no game delete in any UI (§5.7);
    - returning guests were stuck behind a stored guest token (§5.9);
    - `HostShell` had no guest-token or non-401 handling (§5.7);
    - course labels were ambiguous across semesters (§5.3, §5.7).
  - **Precision:**
    - `update_game` field handling (§5.4);
    - `GameUpdate` `extra="forbid"` (§5.3);
    - admin wrappers' 404 source (§5.6);
    - outer joins in `list_sessions_for` (§5.5);
    - the caller audit (§5.10);
    - teardown order (§5.11);
    - a single merge request for steps 2–7 (§6).
  - **Tests:** tests 6, 7, 12, 13, 14, and 15 strengthened; 17 and 18 added.
  - **Owner clarification:** every change works within the existing role and permission system
    (§3 principle, §4 compliance table).
- **2026-09-23 — implementation questions.** Before writing any code, the implementer raised
  these gaps; the owner's decisions are now in the sections named:
  - an explicit `null` in a question `PUT` means "unchanged" — the literal `exclude_unset` would
    have written NULL into `order_index` (§5.4);
  - socket HOST denials keep today's message for users who never hosted the session, and
    `REJOIN_ROOM` refuses a revoked host instead of seating them as a player (§4, §5.6);
  - UserDetailPage warns only on HOST revoke, since no downgrade control exists, and shows the
    "Grant HOST first" message only when the user hosts no courses (§5.8);
  - the host Games page keeps admin's max players field (§5.7);
  - open UI details were settled: sidebar header and icons, group order, import card, course
    pre-selection, confirm style, headings, empty-state copy, link targets, and the 422 message
    separator (§5.4, §5.7, §5.8);
  - `README.md` stays unchanged in this change (§9).
- **2026-09-24 — implementation, step 6.** §5.3's "Name (Semester)" applies to every course
  label, including Play's Active Sessions card, the host SessionsPage rows, and UserDetailPage's
  course-access panel (§5.7, §5.8).
