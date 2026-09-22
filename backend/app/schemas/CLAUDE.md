# backend/app/schemas

## Purpose

Pydantic v2 request/response models — the HTTP boundary contract. These decide what a client may
send, what shape it must be in, and (just as importantly) which model fields are allowed back
out.

## Contents

- `auth.py` — `LoginRequest` (username+password, or dev-only `netid`; a `model_validator`
  requires one of the two), `TokenResponse` / `RefreshResponse` / `TempTokenResponse`, and
  `GuestJoinRequest` (display name, email, 6-char room code).
- `game.py` — the play-side contract: room create/info responses, host's course/game list items,
  and `ScoreResult` (an internal value object returned by scoring, not an HTTP response). It also
  defines `QuestionPublic`, a question view without `answer_data` or `grading_type` — but
  **nothing uses it**. Live question payloads are hand-built dicts in `websocket/gateway.py`
  (`_question_payload`), which do send `gradingType`. Don't count `QuestionPublic` as a safeguard.
- `admin.py` — the authoring contract, by far the largest file: courses, roster
  import/patch, games, questions, local user accounts, access grants, and the admin session
  list. `QuestionCreate.validate_structure` is where the per-question-type JSON shape is actually
  enforced.

## How it fits in

Sits between `routers/` and everything below. Routers declare these as parameter and
`response_model` types; FastAPI does validation and serialization from them, and derives the
OpenAPI docs from them too.

These are intentionally *not* generated from [models/](../models/CLAUDE.md). The separation is
what keeps model columns out of HTTP responses — adding a column to a model does not expose it,
and it shouldn't. Socket payloads don't pass through these schemas at all; the gateway builds them
by hand (see [websocket/CLAUDE.md](../websocket/CLAUDE.md)).

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
