# backend/app/schemas

## Purpose

Pydantic v2 request/response models — the HTTP boundary contract. These decide what a client may
send, what shape it must be in, and (just as importantly) which model fields are allowed back
out.

## Contents

- `auth.py` — `LoginRequest` (username+password, or dev-only `netid`; a `model_validator`
  requires one of the two), `TokenResponse` / `RefreshResponse` / `TempTokenResponse`, and
  `GuestJoinRequest` (display name, email, 6-char room code).
- `game.py` — the play-side contract. `QuestionPublic` is the **client-safe** question view:
  `answer_data` and `grading_type` are deliberately absent so answers can't leak to players.
  Also room create/info responses, host's course/game list items, and `ScoreResult` (an internal
  value object returned by scoring, not an HTTP response).
- `admin.py` — the authoring contract, by far the largest file: courses, roster
  import/patch, games, questions, local user accounts, access grants, and the admin session
  list. `QuestionCreate.validate_structure` is where the per-question-type JSON shape is actually
  enforced.

## How it fits in

Sits between `routers/` and everything below. Routers declare these as parameter and
`response_model` types; FastAPI does validation and serialization from them, and derives the
OpenAPI docs from them too.

These are intentionally *not* generated from [models/](../models/CLAUDE.md). The separation is
what keeps `answer_data` out of player-facing payloads — adding a column to a model does not
expose it, and it shouldn't.

## Gotchas

- **`QuestionUpdate` does not re-run `QuestionCreate`'s structural validation.** It only applies
  per-field constraints, and [routers/admin.py](../routers/admin.py) assigns each present field
  straight through. A `PUT` can therefore leave a question in a state `POST` would have
  rejected — e.g. switching `type` to `multi_select` with no `config.options`, or shrinking
  `options` without shrinking `answer_points`. Downstream scoring in
  [services/game_service.py](../services/CLAUDE.md) has to tolerate that.
- **The question-type allowlist lives here and nowhere else.** The regex in `QuestionCreate` /
  `QuestionUpdate` is the only enforcement of `multiple_choice | true_false |
  fill_in_the_blank | multi_select`; the DB column is a plain string. Adding a question type means
  editing both patterns *and* every `if q.type == ...` branch in services and websocket.
- **`answer_data` key naming is inconsistent between types** and must be matched exactly:
  `multiple_choice` / `multi_select` use `answer_points`, but `fill_in_the_blank` uses
  `acceptedAnswers` / `answerPoints` / `editDistance`. `true_false` uses `answer_points` as a
  *dict* keyed `"true"`/`"false"`, not a list.
- **`COMPLETENESS` questions get almost no `answer_data` validation** — the structural checks are
  mostly gated on `grading_type == "ACCURACY"`. Only the `options` length check applies to both.
- **Length limits disagree with the database.** `RosterRowIn.netid` and `RosterEntryPatch.netid`
  allow 100 chars, but `CourseRoster.netid` is `String(50)`. A netid between 51 and 100 chars
  passes validation and then fails (or truncates) at the DB.
- `multi_select` ACCURACY allows *negative* `answer_points` (only the `multiple_choice` branch
  rejects them) — presumably intentional for penalizing wrong selections, but easy to trip over.
- Most write schemas set `extra="forbid"`, so an unrecognized field is a 422, not a silent
  ignore. Keep that when adding new ones; clients rely on the strictness.
- `UserUpdate.role` accepts only `USER|ADMIN` — there is no path to promote or demote a `GUEST`
  through this schema, even though the model allows that value.
