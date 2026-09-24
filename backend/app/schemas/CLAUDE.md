# backend/app/schemas

## Purpose

Pydantic v2 request/response models — the HTTP boundary contract. These decide what a client may
send, what shape it must be in, and (just as importantly) which model fields are allowed back
out.

## Contents

- `auth.py` — `LoginRequest` (username+password, or dev-only `netid`; a `model_validator`
  requires one of the two), `TokenResponse` / `RefreshResponse` / `TempTokenResponse`,
  `GuestJoinRequest` (display name, email, 6-char room code), and `MeResponse` (`id`, `username`,
  `netid`, `display_name`, `role` — what `GET /api/auth/me` returns).
- `game.py` — the play-side contract: room create/info responses, host's course/game list items,
  and `ScoreResult` (an internal value object returned by scoring, not an HTTP response).
  `RoomCreateRequest.allow_guests` defaults to true, so older callers still validate under
  `extra="forbid"`. `MyCourseItem` carries `semester` (courses are name + semester, so same-name
  courses are expected). `MyGameItem` carries everything the host Games page renders and
  pre-fills, plus `course_id` / `course_name` / `course_semester`, which default to null for an
  unassigned game (only admins are ever sent one). It also
  defines `QuestionPublic`, a question view without `answer_data` or `grading_type` — but
  **nothing uses it**. Live question payloads are hand-built dicts in `websocket/gateway.py`
  (`_question_payload`), which do send `gradingType`. Don't count `QuestionPublic` as a safeguard.
- `admin.py` — the authoring contract, by far the largest file: courses, roster
  import/patch, games, questions, local user accounts, access grants, and the admin session
  list. `QuestionCreate.validate_structure` is where the per-question-type JSON shape is actually
  enforced. The game schemas are layered so a course can't sneak in where it doesn't belong:
  - `GameCreate` validates the `game` section of import bundles as well as requests, so it has
    **no** `course_id` and must not gain one (it's `extra="forbid"`, and course ids are
    database-specific while bundles are portable).
  - `GameCreateRequest(GameCreate)` adds the required `course_id`; it's the create body on both
    routers.
  - `GameUpdate` (`extra="forbid"`) is the host update body, so a `course_id` there is a 422.
  - `AdminGameUpdate(GameUpdate)` adds an optional `course_id`, which can be set or changed but
    never cleared.
  - `GameResponse` includes `course_id` (null means unassigned).

  `AdminSessionItem` is also the item type of `GET /api/host/sessions`.

## How it fits in

Sits between `routers/` and everything below. Routers declare these as parameter and
`response_model` types; FastAPI does validation and serialization from them, and derives the
OpenAPI docs from them too.

These are intentionally *not* generated from [models/](../models/CLAUDE.md). The separation is
what keeps model columns out of HTTP responses — adding a column to a model does not expose it,
and it shouldn't. Socket payloads don't pass through these schemas at all; the gateway builds them
by hand (see [websocket/CLAUDE.md](../websocket/CLAUDE.md)).

## Gotchas

- **`QuestionUpdate` itself only applies per-field constraints** — the structural check lives in
  the service. `game_management_service.update_question` overlays the sent fields on the stored
  question and validates the result with `QuestionCreate` (see
  [services/CLAUDE.md](../services/CLAUDE.md)). Anything that writes a question without going
  through that service skips the check.
- **The question-type allowlist lives here and nowhere else.** The regex in `QuestionCreate` /
  `QuestionUpdate` is the only enforcement of `multiple_choice | true_false |
  fill_in_the_blank | multi_select`; the DB column is a plain string. Adding a question type means
  editing both patterns, `validate_structure`, *and* every type branch in services, websocket, and
  all three frontend apps — see the checklist in [app/CLAUDE.md](../CLAUDE.md).
- **`answer_data` key naming is inconsistent between types** and must be matched exactly:
  `multiple_choice` / `multi_select` use `answer_points`, but `fill_in_the_blank` uses
  `acceptedAnswers` / `answerPoints` / `editDistance`. `true_false` uses `answer_points` as a
  *dict* keyed `"true"`/`"false"`, not a list.
- **`COMPLETENESS` questions get almost no `answer_data` validation** — the structural checks are
  mostly gated on `grading_type == "ACCURACY"`. Only the `options` length check applies to both.
- **Length limits disagree with the database.** `RosterRowIn.netid` and `RosterEntryPatch.netid`
  allow 100 chars, but `CourseRoster.netid` is `String(50)`. A netid between 51 and 100 chars
  passes validation and then fails (or truncates) at the DB.
- `multi_select` ACCURACY allows *negative* `answer_points` (the `multiple_choice` and
  `fill_in_the_blank` branches reject negatives; `multi_select` doesn't) — presumably intentional
  for penalizing wrong selections, but easy to trip over.
- Most write schemas set `extra="forbid"`, so an unrecognized field is a 422, not a silent
  ignore. Keep that when adding new ones; clients rely on the strictness.
- `UserUpdate.role` accepts only `USER|ADMIN`, so nothing can be *set to* `GUEST` — but a `GUEST`
  can be promoted to `USER` or `ADMIN` through it (`PUT /api/admin/users/{id}`), and admin
  guest-merge also promotes guests in place. There's no last-admin guard either: an admin can be
  demoted this way even though `DELETE` refuses to remove one.
