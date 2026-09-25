# backend/app/models

## Purpose

SQLAlchemy 2.0 ORM models — the persistent schema for users, courses, games, and play sessions.
This is the only place table structure is declared; everything else in the backend reads and
writes through these classes.

## Contents

- `__init__.py` — re-exports every model. **Import order matters**: tables with foreign keys are
  imported after the tables they reference, so `Base.metadata` resolves cleanly. Adding a model
  means adding it here, in dependency order, or Alembic autogenerate won't see it.
- `user.py` — `User`. Single table for all three identity kinds, distinguished by `role`
  (`ADMIN` / `USER` / `GUEST`). UUID string PK. Almost every column is nullable because a guest
  player has a `display_name` and nothing else, while an admin has `username` + `password_hash`
  and a NetID-authenticated student has `netid` + `email`.
- `course.py` — `Course` (name + semester), `CourseRoster` (the imported student list: netid,
  full_name, email, `is_active`), and `UserCourseAccess` (composite-PK join table granting a user
  `HOST` or `PLAYER` in a course).
- `game.py` — `Game` (title, description, `max_players`), `Question` (the content unit), and
  `UserGameAccess` (composite-PK join granting a user access to a game; no role column — access
  is binary).
- `session.py` — `GameSession` (one live playthrough: `room_code`, `status`, host, game, course)
  and `SessionScore` (one row per player per question: points, correctness, latency, and the raw
  submitted answer).

## How it fits in

Bottom of the backend stack. `services/` owns the game logic over these models, but routers and
the gateway also query them directly: most of `routers/admin.py` is inline CRUD, `routers/game.py`
does its own lookups, and `websocket/gateway.py` runs a few `select`s itself. Don't assume a
service sits between every caller and a table.
`schemas/` mirrors parts of this shape as Pydantic request/response types — the two are
deliberately separate, so a field added here is not automatically exposed over the API.

`Base` and the async engine live one level up in [database.py](../database.py); migrations that
realize these models live in [migrations/versions/](../migrations/versions/).

## Gotchas

- **`Question.type` is an unconstrained `String(50)`, not an `Enum`.** The real allowlist —
  `multiple_choice`, `true_false`, `fill_in_the_blank`, `multi_select` — is enforced only by a
  regex in [schemas/admin.py](../schemas/admin.py). The database will happily store a typo.
  Anything switching on question type must handle an unknown value.
- **`Question.config` and `Question.answer_data` are free-form JSON**, and their required shape
  depends on both `type` and `grading_type`. That contract is validated in `schemas/admin.py`
  and interpreted in `services/game_service.py` — not here.
- **`grading_type` (`ACCURACY` / `COMPLETENESS`) changes what `answer_data` means**, not just how
  points are tallied. Don't assume a single scoring path.
- `points_value` and `points_awarded` are floats (migration `003_float_points`), so partial
  credit is representable. Don't reintroduce integer assumptions when summing or displaying.
- `game.py`'s `TYPE_CHECKING` block imports `UserGameAccess` from `.user`, but the class is
  defined in `game.py` itself. It's harmless at runtime (never executed) but wrong — a type
  checker will flag it.
- Deletion behavior is uneven. `GameSession` cascades to its `SessionScore` rows, and
  courses/games cascade to their rosters, questions, and access rows — but `GameSession.game_id`
  and `course_id` have no `ondelete`, and `SessionScore.user_id` / `question_id` don't either.
  A bare ORM delete of a parent with history makes SQLAlchemy null the NOT NULL FK, which MySQL
  rejects. The admin endpoints for **games and users** work around that by deleting the dependent
  session/score rows first — so those deletes *silently erase results*
  ([routers/CLAUDE.md](../routers/CLAUDE.md)). **Questions** have no such workaround: deleting any
  question that has ever been answered fails with a 500.
- **`SessionScore` has no unique constraint on (`session_id`, `user_id`, `question_id`)** — only a
  non-unique index on (`session_id`, `user_id`). Nothing in the schema stops a player being scored
  twice for one question; the only guard is a non-atomic Redis check in the gateway. The
  leaderboard, exports, and report all aggregate `session_scores` and will count duplicates.
- **`User.email` is UNIQUE, and guests are identified by email.** A guest row holding a student's
  address blocks creating their real account with the same email — see
  [services/CLAUDE.md](../services/CLAUDE.md).
- `GameSession.host_user_id` is nullable; a session can outlive or predate a known host.
- `room_code` is `String(6)` and globally unique across all time, not just active sessions — code
  generation has to handle collisions with completed sessions.
