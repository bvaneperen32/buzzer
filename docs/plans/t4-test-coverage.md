# T4 follow-up — integration test coverage

Status: design agreed on 2026-09-27; goldfish-tested and revised on 2026-09-28 (§11);
implemented on 2026-09-28 (§10). The manual checks in §6 are still to be run.
Follows `docs/plans/t4-ui-restructuring.md` (the T4 spec — its §5.11 defines the 18 existing
tests). Source requirement: `instructions.md` → T5 (integration tests that verify behavior, cover
success paths and error/access-control cases, and never delete or weaken existing tests).
This document is the implementation contract. If implementation reveals a flaw, update this file
first, then the code.

---

## 1. Problem

T4 (merged to `main` as `0b8b3f2`) is covered by 18 integration tests in
`tests/integration/test_t4_access.py`. Every access *rule* has at least one test, but coverage is
thin at the endpoint level:

- 7 of the 16 `/api/host/*` endpoints have no test showing they refuse a user without access.
  Only 9 of the 16 have both a success test and a refusal test.
- One new endpoint (game export) and one new success path (host game edit) are never exercised.
- Several behaviors the T4 spec names have no test: import rejections in the new error format,
  questions scoped to the game in the URL, explicit `null` in a question edit, and "a game's
  course can be set or changed but never cleared".

This plan adds backend integration tests for those gaps, testing only behavior T4 introduced
(§3 defines the scope rule exactly).

---

## 2. Background the implementer needs

Read `backend/CLAUDE.md`, `backend/app/routers/CLAUDE.md`, and `backend/app/services/CLAUDE.md`
for the architecture and access model; T4 spec §4 has the normative access rules. The facts this
plan depends on:

**Running the suite.** Integration tests run on the host machine against the docker compose dev
stack (`APP_ENV=development`, which disables rate limiting). Bring the stack up with
`docker compose up`; the database must be at migration `004`
(`docker compose exec backend alembic upgrade head`). Then, with the venv active (`.venv\Scripts\`
on Windows): `cd tests/integration && pytest test_t4_access.py -v`. At `0b8b3f2` the full suite
(`pytest -v`) was recorded as 24 passed + 2 skipped, or 26 passed with `--fast`. **CI runs no
pytest suite** — these tests only run when someone runs them locally.

**The test file** (`tests/integration/test_t4_access.py`):
- `T4Env` is a per-test helper; the `t4` fixture creates one and calls `cleanup()` afterwards.
  Its methods:
  - `req(method, path, token=None, **httpx_kwargs)` and `admin(method, path, **kwargs)` send a
    request (the latter with the admin token);
  - `create_user(name) -> (user_id, token)` makes a local USER and logs it in;
  - `create_course(label) -> course_id` makes a course named `f"T4 {label} {tag}"`, semester
    `"T4 Test"`;
  - `user_detail(user_id)` returns `GET /api/admin/users/{id}`'s body (`course_access`,
    `game_access`, …);
  - `grant_course(user_id, course_id, role="HOST")`, `revoke_course(...)`, and
    `grant_game(user_id, game_id)` change access and wait until the change is visible;
  - `make_host(name, course_id, game_id=None) -> (user_id, token)` is `create_user` plus HOST on
    the course (plus a game grant if given);
  - `create_game(course_id, token=None) -> game_id` creates through `/api/host/games` when a token
    is given, else `/api/admin/games`, with title `f"T4 Game {tag}"`;
  - `questions(game_id, token=None)` lists a game's questions through the host router (admin
    token by default); `question(game_id, question_id)` returns one or `None`;
  - `create_room(token, course_id, game_id, **extra) -> (room_code, session_id)` creates a room
    and waits until it is visible.
- `cleanup()` deletes, with the admin token, every room in `session_ids`, then every game in
  `game_ids`, then every user in `user_ids`. It doesn't assert statuses. Anything a test creates
  outside these helpers must be appended to the matching list (test 13 does this for an imported
  game). Courses and guest users are left behind, as in the rest of the suite.
- `wait_until(check, timeout=5.0, what=...)` polls `check()` until it returns something truthy.
- `create_guest_tokens(base_url, room_code, n)` (imported from `conftest.py`) registers `n` guests
  in a room and returns their tokens.
- Tests 1–18 match T4 spec §5.11; each docstring starts with its number (`"""7 — ..."""`).
- The file is **not** ruff-formatted, and CI neither lints nor formats `tests/`. **Don't run
  `ruff format` on it** — that would rewrite the 18 existing tests. Match the existing style by
  hand.

**Error bodies.** Application errors are `{"error": CODE, "message": str}`, where CODE is
`UNAUTHORIZED` (401), `FORBIDDEN` (403), `NOT_FOUND` (404), `CONFLICT` (409), or `UNPROCESSABLE`
(422; raised by the service for import and question-structure failures). FastAPI's own
request-validation failures are also 422, but shaped `{"error": "VALIDATION_ERROR", "detail":
[...]}` with no `message`. Unhandled exceptions become 500
`{"error": "INTERNAL_ERROR", "message": "An unexpected error occurred"}`.

**Guards, and the order they run in.** Every `/api/host/*` endpoint declares
`Depends(require_user)` in its own signature (the router has no router-level guard); the course,
game, or download rule is then called *inside* the handler. FastAPI runs dependencies before it
checks the request body against its schema, and runs the handler only after both succeed. So:
- no token → 401 (`get_current_user`: `Authentication required`);
- a guest token → 403 `Authenticated account required` (`require_user`), whatever the body;
- a signed-in user without access → the handler's 403 — **but only if the body is valid**. An
  invalid body is a 422 before the handler runs, which would hide a missing access check.

**Commit timing — read before writing any "unchanged" assertion.** Game and question create,
game import, and roster import commit before responding. Everything else this plan touches —
game `PUT`, question `PUT`/`DELETE`/reorder, grants and revokes, room creation, user delete —
commits in `get_db`'s cleanup *after the response has been sent*. Two consequences:
1. Poll (`wait_until`) before asserting on the effect of those writes.
2. **A write that fails at commit still returns its 2xx.** The client never sees the failure; the
   database just keeps the old state. So "nothing changed" after a 200 can mean either "correctly
   ignored" or "tried to write something invalid and was silently rolled back". Where a test must
   prove a *successful* request left a field alone, it sends a harmless **canary** change in the
   same request (a new `prompt` or `title`) and waits for the canary to land. The canary lands
   only if the whole transaction committed. B5 and B6 depend on this.

**Schema facts.**
- `QuestionUpdate` accepts `null` for every field. The service drops explicit `null`s ("leave
  unchanged") and then validates the merged question with `QuestionCreate`. If the service
  stopped dropping them, every field except `order_index` would fail that validation visibly
  (422). `QuestionCreate` allows `order_index: null`, because create auto-assigns it, so a leaked
  `null` there passes validation. It still isn't silent: the handler returns the question under
  `response_model=QuestionResponse`, whose `order_index` is `int`. Serialization fails, and the
  client gets a 500. (The `questions.order_index` column is also NOT NULL.)
- `GameUpdate` (host `PUT`) forbids extra fields, so any `course_id` is a 422 (test 7 covers
  this). `AdminGameUpdate` (admin `PUT`) adds `course_id: int | None` (> 0); `null` or absent
  means "don't change the course".
- The export bundle is `{"format": "buzzer/game", "version": 1, "game": {"title", "description",
  "max_players"}, "questions": [ ... ]}`, where each question has exactly `type`,
  `grading_type`, `prompt`, `config`, `answer_data`, `time_limit_seconds`, and `points_value` —
  no ids, no `order_index`, no course. The download filename is the title with every character
  other than letters, digits, space, `_`, and `-` replaced by `_`, plus `.json`.
- A minimal valid question body: `{"type": "true_false", "grading_type": "COMPLETENESS",
  "prompt": "...", "points_value": 1}`. A valid `multiple_choice` body is test 8's `mc` dict.
- `GET /api/game/my-games` orders by course name, then semester, then title.
- Session ids are UUID strings; game and course ids are integers.
- Deleting a user (`DELETE /api/admin/users/{id}`) should leave the sessions they hosted in place
  with `host_user_id` NULL: `User.hosted_sessions` has no delete cascade, so SQLAlchemy nulls the
  foreign key. This is inferred from the models and not yet observed; B7d verifies it first.

---

## 3. Technical plan

1. **Append 15 test functions (33 collected tests) to `tests/integration/test_t4_access.py`,**
   after test 18, under a section header naming this document. They're numbered B1a–B7d
   (B1c included) to match §5, independently of T4 spec §5.11's 1–18. Update the module
   docstring to mention this document.
2. **Helper changes are limited to the backward-compatible additions in §5.1.** The 18 existing
   tests are not modified.
3. **Scope rule: test only behavior T4 introduced.** Behavior that existed before T4 and was
   already untested stays out, even where T4 moved its code. Three exceptions are in scope,
   because pre-existing code became reachable by hosts, who are less trusted than the admins who
   could reach it before:
   - questions are looked up within the game in the URL — now the only thing isolating one host's
     game from another's (B2);
   - imported question prompts are sanitized — they appear as raw HTML in the session report
     (B3b);
   - an explicit `null` in a question edit leaves the field unchanged — T4 rewrote the code
     behind it (B5).
4. **Games with no course (legacy data) are checked manually** (§6), not automated.
5. **Success criterion:** every gap in §4 maps to a passing test, a recorded manual check, or a
   recorded out-of-scope reason, and every new test is shown to fail when the behavior it guards
   is broken (§8). Not a coverage percentage.

---

## 4. Gap → test mapping

| # | Gap | Disposition |
|---|---|---|
| 1 | 7 host game-rule endpoints have no refusal test (game edit; question list, create, update, delete, reorder; game export) | B1a (all 8 game-rule endpoints, including get game) |
| 2 | Guest and no-token requests to `/api/host/*` | B1a (the 8 game-rule endpoints), B1c (the other 8 host endpoints) |
| 3 | A non-admin gets 403 (not 404) for a nonexistent game; an admin gets 404 | B1b |
| 4 | Editing or deleting a question through another game's URL | B2 |
| 5 | The five import rejections in the `UNPROCESSABLE` format | B3a |
| 6 | Prompt sanitization on import | B3b |
| 7 | Admin import succeeding with the now-required `course_id` | B3c |
| 8 | Host game edit (`PUT /api/host/games/{id}`) success | B4a |
| 9 | Game export (`GET /api/host/games/{id}/export`) | B4b |
| 10 | Explicit `null` in a question `PUT` | B5 |
| 11 | Admin `PUT` with `course_id: null` must not clear the course; a nonexistent course → 404 | B6 |
| 12 | `GET /api/auth/me` with a guest token | B7a |
| 13 | A nonexistent session → 404 on download and delete | B7b |
| 14 | Admin `my-games` course fields and ordering | B7c |
| 15 | The admin session list keeps sessions whose host was deleted (outer join) | B7d |
| 16 | Games with no course (legacy data from before migration `004`) | Manual — §6 |
| 17 | Import returns 500 for some malformed files | Out of scope: a bug, separate MR — §9.1 |
| 18 | Deprecated status constant in `UnprocessableError` | Out of scope: separate MR — §9.2 |
| 19 | Session-export options (`title`, `sis_domain`, `roster_only`, `per_question`) on the host router | Out of scope: they exercise the pre-existing `export_service` |
| 20 | Reorder with the wrong set of ids → 409 | Out of scope: pre-existing behavior |
| 21 | Admin question list, reorder, export, and successful delete | Out of scope: pre-existing and untested before T4; the shared service code is exercised through the host router |
| 22 | A live host deleting their own session | Out of scope: pre-existing; the predicate's success path is already covered by test 11's downloads |
| 23 | `REJOIN_ROOM` by a live host | Out of scope: pre-existing |
| 24 | Migration `004` upgrade and downgrade | Out of scope: verified by hand during T4; automating needs a scratch database |
| 25 | All frontend logic | Out of scope pending an instructor answer — §9.3 |

---

## 5. Detailed implementation

### 5.1 Helper and constant additions

In `T4Env`:
1. `create_game(course_id, token=None, *, title=None)` — `title` defaults to today's
   `f"T4 Game {tag}"`. Nothing else changes.
2. New `upload_game(token, raw: bytes, course_id: int | None, *, router="host") -> httpx.Response`
   — multipart `POST /api/{router}/games/import` with
   `files={"file": ("game.json", raw, "application/json")}` and
   `data={"course_id": str(course_id)}` (as test 13 sends it; `data` omitted when `course_id` is
   `None`). On a 201, append `response.json()["game_id"]` to `game_ids` so cleanup removes it.
   Return the response unasserted. Test 13 keeps its own local `upload`; don't refactor it.

At module level, next to the existing message constants:
- `NO_GAME_ACCESS = "You do not have access to this game"`
- `ACCOUNT_REQUIRED = "Authenticated account required"`
- `_TF_QUESTION` — the minimal valid `true_false` body from §2. Never mutate it; build variants
  with `{**_TF_QUESTION, "prompt": ...}`.
- `_BASE_BUNDLE` — the smallest valid import bundle:
  `{"format": "buzzer/game", "version": 1, "game": {"title": "Import Check"}, "questions": []}`.
  Never mutate it; build variants with `{**_BASE_BUNDLE, "questions": [...]}` and serialize them
  with `json.dumps(...).encode()`. B1c, B3a, B3b, B3c, and B4b build their files from it.
- `_NO_SUCH_ID = 2_000_000_000` — below MySQL's signed INT maximum, and never a real id in a dev
  database.

### 5.2 Tests

Each docstring starts with the test's ID (`"""B2 — ..."""`). Function names are suggestions; the
IDs are what the mapping in §4 relies on. "H", "X", "C" etc. are local names for the users and
courses a test creates.

#### B1a — `test_every_host_game_endpoint_refuses_without_access`

Parametrized over the 8 cases below (use the ids given). Setup, fresh for each case:
- course C; H = `make_host("owner", C)`; G = `create_game(C, H)` (H is auto-granted as creator);
- Q = a question on G, created by H with `{**_TF_QUESTION, "prompt": "Original"}`;
- X = `make_host("cohost", C)` — HOST on C but no grant on G;
- `(code, _) = create_room(H, C, G)`; `[guest] = create_guest_tokens(base_url, code, 1)`.

| id | Request (every body is valid) |
|---|---|
| `get-game` | `GET /api/host/games/{G}` |
| `put-game` | `PUT /api/host/games/{G}` with `{"title": "Changed"}` |
| `list-questions` | `GET /api/host/games/{G}/questions` |
| `create-question` | `POST /api/host/games/{G}/questions` with `{**_TF_QUESTION, "prompt": "Sweep"}` |
| `update-question` | `PUT /api/host/games/{G}/questions/{Q}` with `{"prompt": "Changed"}` |
| `delete-question` | `DELETE /api/host/games/{G}/questions/{Q}` |
| `reorder-questions` | `POST /api/host/games/{G}/questions/reorder` with `{"order": [Q]}` |
| `export-game` | `GET /api/host/games/{G}/export` |

Send the case's request three times and assert, in this order:
1. as X → 403, `error == "FORBIDDEN"`, `message == NO_GAME_ACCESS`;
2. as the guest → 403, `message == ACCOUNT_REQUIRED`;
3. with no token → 401, `error == "UNAUTHORIZED"`.

Then, as admin: `GET /api/admin/games/{G}` still has the original title, and G's question list is
exactly `[Q]` with prompt `"Original"`. Refused requests write nothing, so an immediate read is
fine; the status assertions are the primary check.

Mechanics: the paths and bodies need G and Q, which exist only at run time, so the
parametrize values can't be literal requests. Parametrize over the case ids alone, and build each
request inside the test from a module-level map of `id -> lambda G, Q: (method, path, kwargs)`.

Why it's shaped this way:
- Bodies must be valid, or FastAPI's 422 would pre-empt the handler's access check (§2).
- X is the only actor that can detect a deleted per-endpoint check. The guest checks that each
  endpoint declares `require_user`: swapping it for `get_current_user` changes the guest's
  message.
- Parametrizing, rather than one looping test, keeps cases independent: a wrongly allowed
  `DELETE` can't turn the next case's 403 into a 404, and one failure doesn't hide the rest.
- The table is static. Put a comment above it saying a row must be added whenever a game-rule
  endpoint is added to `host.py`.

#### B1c — `test_every_other_host_endpoint_requires_an_account`

This covers the 8 `/api/host/*` endpoints B1a doesn't: those guarded by the course rule or the
download rule, plus the session list. Their access rules already have refusal tests (tests 2,
11, 12, 13, 14). What they lack is a check that each one declares `require_user`. Without that
check, swapping in `get_current_user` would go unnoticed: a guest would get the course or download
rule's message instead, and for the session list a 200 with `[]`.

Parametrized over the 8 cases below, with the same mechanics as B1a. Setup, fresh for each case:
course C; H = `make_host("sweeper", C)`; G = `create_game(C, H)`;
`(code, sid) = create_room(H, C, G)`; `[guest] = create_guest_tokens(base_url, code, 1)`.

| id | Request (every body is valid) |
|---|---|
| `create-game` | `POST /api/host/games` with `{"title": "Sweep", "course_id": C}` |
| `import-game` | `upload_game(token, json.dumps(_BASE_BUNDLE).encode(), C)` |
| `list-roster` | `GET /api/host/courses/{C}/roster` |
| `import-roster` | `POST /api/host/courses/{C}/roster/import` with `{"rows": [row]}`, `row` shaped as in test 14 |
| `patch-roster` | `PATCH /api/host/courses/{C}/roster/{_NO_SUCH_ID}` with `{"is_active": false}` |
| `list-sessions` | `GET /api/host/sessions` |
| `session-report` | `GET /api/host/sessions/{sid}/report` |
| `session-export` | `GET /api/host/sessions/{sid}/export` |

Send the case's request twice and assert:
1. as the guest → 403, `error == "FORBIDDEN"`, `message == ACCOUNT_REQUIRED`;
2. with no token → 401, `error == "UNAUTHORIZED"`.

Then, as admin: `GET /api/game/my-games` filtered to C's games is exactly `[G]`, and
`GET /api/admin/courses/{C}/roster` is `[]`.

There's no same-course actor without access, as B1a has: the guard runs before the handler, so no
id needs to exist, and the handler's own refusals are the existing tests' job. Put the same
"add a row whenever an endpoint is added" comment above this table.

#### B1b — `test_game_rule_hides_whether_a_game_exists`

Course C; H = `make_host("prober", C)`.
- `GET /api/host/games/{_NO_SUCH_ID}` as H → 403 with `message == NO_GAME_ACCESS` — the same
  answer as for a real game H can't access.
- The same request as admin → 404 with `message == f"Game {_NO_SUCH_ID} not found"`.

#### B2 — `test_questions_are_scoped_to_the_game_in_the_url`

Course C; H = `make_host("a", C)`; X = `make_host("b", C)`; A = `create_game(C, H)`;
B = `create_game(C, X)`; qA on A created by H (prompt `"A question"`); qB on B created by X
(prompt `"B question"`).
1. Premise: `GET /api/host/games/{B}/questions` as H → 403.
2. `PUT /api/host/games/{A}/questions/{qB}` as H with `{"prompt": "Hijacked"}` → 404,
   `message == f"Question {qB} not found in game {A}"`.
3. `DELETE /api/host/games/{A}/questions/{qB}` as H → 404, same message.
4. As X, B's questions are still exactly `[qB]` with prompt `"B question"`.

#### B3a — `test_import_rejections_use_the_unprocessable_format`

Parametrized over the 5 cases below. Setup: course C; H = `make_host("importer", C)`; record the
ids in H's `GET /api/game/my-games`. The base bundle is `_BASE_BUNDLE` (§5.1).

| id | File bytes | `message` starts with |
|---|---|---|
| `not-json` | `b"{nope"` | `"Invalid JSON"` |
| `wrong-format` | base bundle with `"format": "other"` | `"Unrecognised file format"` |
| `wrong-version` | base bundle with `"version": 2` | `"Unsupported version"` |
| `bad-game` | base bundle with `"game": {"title": ""}` | `"Invalid game metadata"` |
| `bad-question` | base bundle with `"questions": [{"type": "nope"}]` | `"Question 1 invalid"` |

`upload_game(H, raw, C)` → 422 with `error == "UNPROCESSABLE"` and `message` starting with the
prefix. Afterwards, H's `my-games` ids are unchanged.

Assert prefixes, never whole messages: the bug-fix MR in §9.1 may improve the wording. **Don't
add** files whose top level isn't a JSON object, whose `questions` is `null`, a number, or a
boolean, or that aren't valid UTF-8. Those return 500 today (§9.1) and belong to the fix MR.

#### B3b — `test_imported_prompts_are_sanitized`

Course C; H = `make_host("sanitizer", C)`. H uploads `_BASE_BUNDLE` with one question,
`{**_TF_QUESTION, "prompt": "<script>x</script><b>y</b>"}`, via `upload_game(H, raw, C)` → 201.
The imported game's only question has a prompt that contains no `"<script>"` and does contain
`"<b>y</b>"` (the same check as test 9).

#### B3c — `test_admin_import_puts_the_game_in_the_given_course`

Course C. The admin uploads `_BASE_BUNDLE` with two `_TF_QUESTION`-based questions via
`upload_game(t4.admin_token, raw, C, router="admin")` → 201.
- `GET /api/admin/games/{id}` has `course_id == C`, and the game has 2 questions.
- The admin was not given a game grant: take the admin's id from `GET /api/auth/me`, and check
  that `user_detail(admin_id)["game_access"]` doesn't contain the new game.

#### B4a — `test_host_edits_game_details`

Course C; H = `make_host("editor", C)`; G = `create_game(C, H)`.
`PUT /api/host/games/{G}` as H with `{"title": "Renamed", "description": "Edited",
"max_players": 42}` → 200, and the response carries those three values. Then `wait_until`
H's `GET /api/host/games/{G}` shows all three; its `course_id` is still C.

#### B4b — `test_export_is_portable_and_round_trips`

Courses C and C2; H = `make_host("exporter", C)` plus HOST on C2;
G = `create_game(C, H, title="Export Check")`. H adds two questions in this order: test 8's `mc`
body (prompt `"First?"`), then `{**_TF_QUESTION, "prompt": "Second?"}`. Keep titles and prompts
to letters, digits, spaces, and `?`, so bleach's entity escaping can't affect the comparison.
1. `GET /api/host/games/{G}/export` as H → 200; `content-type` starts with `application/json`;
   `content-disposition` contains `attachment` and `filename="Export Check.json"`.
2. The body's top-level keys are exactly `{"format", "version", "game", "questions"}`, with
   `format == "buzzer/game"` and `version == 1`. `game`'s keys are exactly
   `{"title", "description", "max_players"}`, with title `"Export Check"`. There are two
   questions, in the order they were added, each with exactly the seven keys listed in §2.
3. `upload_game(H, response.content, C2)` → 201. The new game's `course_id` is C2, and its
   questions equal G's pairwise on those seven fields, in order.

#### B5 — `test_explicit_nulls_leave_question_fields_unchanged`

Course C; H = `make_host("nuller", C)`; G = `create_game(C, H)`; one question created from test
8's `mc` body. Read it back as `before`. Then, as H:
`PUT /api/host/games/{G}/questions/{q}` with
`{"type": null, "grading_type": null, "config": null, "answer_data": null,
"time_limit_seconds": null, "points_value": null, "order_index": null, "prompt": "Canary"}`
→ 200. `wait_until` the stored prompt is `"Canary"`; then every other field equals `before`.

Why the canary: this PUT commits after the response (§2), so reading the question back right away
can return the pre-edit row. The field comparison would then pass whatever the edit did. Waiting
for the canary means the comparison runs against the committed row. (A regression that stopped
dropping nulls wouldn't reach the comparison anyway: most fields would fail merged validation
with a 422, and `order_index` would fail response serialization with a 500. See §2.)

#### B6 — `test_admin_course_field_cannot_be_cleared_or_dangle`

Course C; G = `create_game(C)` (admin-created).
1. `PUT /api/admin/games/{G}` as admin with `{"course_id": null, "title": "Canary"}` → 200.
   `wait_until` the title is `"Canary"`, then check `course_id == C`. The canary works as in B5:
   a regression that cleared the course would commit in the same transaction as the title.
2. `PUT /api/admin/games/{G}` as admin with `{"course_id": _NO_SUCH_ID}` → 404, with
   `message == f"Course {_NO_SUCH_ID} not found"`. The game's `course_id` is still C.

#### B7a — `test_me_reports_a_guest`

Course C; H = `make_host("guesthost", C)`; G = `create_game(C, H)`;
`(code, _) = create_room(H, C, G)`. `POST /api/auth/guest` with
`{"display_name": "T4 Guest", "email": f"t4me{tag}@example.com", "room_code": code}` → 200.
With that access token, `GET /api/auth/me` → 200 with `role == "GUEST"` and
`display_name == "T4 Guest"`.

#### B7b — `test_missing_session_is_404_for_download_and_delete`

`sid = str(uuid.uuid4())`; course C; H = `make_host("nobody", C)`. For each of H and the admin,
each of these → 404, `error == "NOT_FOUND"`, `message == f"Session {sid} not found"`:
- `GET /api/host/sessions/{sid}/report`
- `GET /api/host/sessions/{sid}/export`
- `GET /api/game/sessions/{sid}/export`
- `DELETE /api/game/sessions/{sid}`

The existence check runs before the ownership check, which is why a non-admin gets 404 too.

#### B7c — `test_admin_my_games_carries_course_fields_in_course_order`

Courses cA = `create_course("A")` and cB = `create_course("B")`. Create three games in this
order — the opposite of the expected sort, so insertion order can't make the test pass:
g1 = `create_game(cB, title="Alpha")`, g2 = `create_game(cA, title="Zulu")`,
g3 = `create_game(cA, title="Mike")`.
Admin `GET /api/game/my-games`; keep only the items whose id is g1, g2, or g3, preserving order.
Their ids are `[g3, g2, g1]`, and each item's `course_id`, `course_name`, and `course_semester`
match its course (names `f"T4 A {tag}"` and `f"T4 B {tag}"`, semester `"T4 Test"`).

#### B7d — `test_admin_session_list_keeps_sessions_whose_host_was_deleted`

**Verify the precondition by hand first.** Create a host and a LOBBY room, then delete the host
with `DELETE /api/admin/users/{id}` and confirm it returns 204. The 204 alone proves nothing: user
delete commits after the response (§2), so a failed commit still returns 204. So next, poll
`GET /api/admin/users/{id}` until it returns 404, which shows the delete committed. Only then
check that the room still exists (`GET /api/game/rooms/{code}` as admin → 200). If the user never
disappears, or the session does, stop: don't write B7d, record the finding in §10, and raise it as
its own bug MR.

Test: course C; `(H_id, H) = make_host("leaver", C)`; G = `create_game(C, H)`;
`(code, sid) = create_room(H, C, G)`; `DELETE /api/admin/users/{H_id}` as admin → 204.
`wait_until` `GET /api/admin/users/{H_id}` is a 404 (`what="delete of the host to commit"`), so a
rolled-back delete fails with a clear message. Then `wait_until` the admin's
`GET /api/host/sessions?status=LOBBY` has a row with
`session_id == sid` and `host_display_name` null. That row has `course_id == C` and
`game_id == G`. (Cleanup's later delete of H returns 404; cleanup already ignores statuses.)

### 5.3 Commits

On the current branch, in imperative mood, one logical change per commit (T1):
1. This document — before any test code (T3).
2. The constants and helper additions from §5.1, plus the module docstring update.
3. One commit per group: B1 (B1a, B1b, B1c), B2, B3, B4, B5–B6, B7.

Each commit leaves the suite passing. The manual checks (§6) and mutation checks (§8) are
recorded in the MR description, not committed.

---

## 6. Manual verification: games with no course

Migration `004` left every game created before T4 without a course (`games.course_id` NULL). No
API path can create such a game anymore, so these behaviors are checked by hand once before the
MR, and the results are recorded in the MR description (date, who ran it, pass or fail per step).

Setup:
- Open a MySQL shell: `docker compose exec mysql mysql -u app_user -p buzzer` (the password is
  `MYSQL_PASSWORD` in `.env`).
- Find a legacy game L: `SELECT id, title FROM games WHERE course_id IS NULL;`. If there is none,
  pick a disposable game and run `UPDATE games SET course_id = NULL WHERE id = <L>;`.
- A local USER account X (admin console → Users) with HOST on a course C. X's id is in the admin
  console URL (`/users/<id>`).
- A leftover pre-T4 grant:
  `INSERT INTO user_game_access (user_id, game_id) VALUES ('<X id>', <L>);`

| Step | Action | Expected |
|---|---|---|
| a | As X, open the host app's Games and Play pages | L isn't listed. `GET /api/host/games/<L>` as X → 403 `You do not have access to this game` |
| b | As admin, open the host app's Games page | L is listed under "Unassigned", the last group, with an "Assign a course…" select |
| c | As admin, `POST /api/game/rooms` with `{"course_id": C, "game_id": L}` | 409 `This game belongs to a different course` |
| d | As admin, open X's page in the admin console | L isn't offered under Add Games. `POST /api/admin/users/<X>/game-access` with `{"game_id": L}` → 409 `Assign this game to a course before granting access` |
| e | As admin, assign L to a course C2 that X does **not** host, from the Games page select (a confirmation dialog appears) | After reloading X's page, X's game access no longer lists L |
| f | Redo the setup's NULL course and grant for L, then assign L to C, which X hosts | X keeps the grant, now sees L on Games and Play, and can create a room for it in C |

Why this is manual rather than automated: a game with no course fails closed by construction.
The host game list joins on the course, and NULL matches nothing. Room creation compares course
ids, and NULL never equals one. The HOST lookup for a NULL course matches nothing even without the
explicit checks. Assigning a course runs the same grant-removal code test 7 already covers.

The remaining risk is two future edits:
- switching the admin's game list to an inner join, so legacy games vanish from the only UI that
  can assign them a course;
- relaxing room creation for games with no course.

Automating would mean database writes from the test process. And since CI doesn't run this suite,
it would only catch regressions when someone runs it locally. Revisit if T7 or T8 changes
`game_service`'s access functions or `my-games`.

---

## 7. Alternatives considered

| Alternative | Why it lost |
|---|---|
| A new test file that imports the helpers from `test_t4_access.py` | One test file depending on another's internals; importing a fixture across files also trips pyflakes F811 |
| A new test file, with `T4Env`, `t4`, and `wait_until` moved into `conftest.py` | The best long-term shape (T7 and T8 will want these helpers), but it deletes ~200 lines from a passing, graded test file. The owner chose not to restructure the existing file now |
| A new test file plus a separate helper module | Splits the helper class from its fixture; differs from the suite's existing convention of sharing helpers through `conftest.py` |
| Copying the helpers into a new file | ~200 duplicated lines; duplicated logic is this codebase's recurring source of bugs |
| A `tests/integration/t4/` package | Renames a file that the T4 spec and several `CLAUDE.md` files reference by path |
| Automating §6 with direct database writes | See §6: it fails closed by construction, the remaining risk is small, and CI doesn't run the suite |
| Mocked unit tests for §6 | They would test the explicit checks, which are already backed up by the data model, not the SQL where the remaining risk lies |
| A line-coverage target | Access-check lines run on every successful request, so line coverage reports them as covered; the largest gap (no refusal test per endpoint) is invisible to it |
| Including the import-500 files in B3a | They fail today, so they'd force a code fix; T1 puts a bug fix in its own branch and MR |
| One looping test instead of a parametrized B1a | A wrongly allowed `DELETE` would corrupt later iterations, and the first failure would hide the rest |
| Listing host routes from `/openapi.json` for B1a | Would catch future endpoints automatically, but needs a valid request body built for every route; the static table plus a comment is enough for now |
| Frontend tests now (Vitest and Testing Library) | Deferred pending an instructor answer — §9.3 |
| Playwright end-to-end tests | Browser downloads and a seeded, running stack; can't run in current CI; slow; re-tests rules the backend suite already covers |

---

## 8. Verification

1. With the stack up, run `cd tests/integration && pytest -v`, then `pytest -v --fast`. Expected:
   57 passed and 2 skipped without `--fast`; 59 passed with it. That's the existing 26 plus the 33
   new collected tests (15 functions; B1a and B1c have 8 cases each, and B3a has 5).
2. `git diff main --stat -- . ':!ai_log' ':!docs/plans'` lists only
   `tests/integration/test_t4_access.py`. In that file's diff, tests 1–18 are untouched: the only
   changes to existing lines are the module docstring and `create_game`'s signature and title.
   The exclusions are there because this branch also carries `ai_log/` session records and
   `docs/plans/fix-import-malformed-files.md`, the plan for §9.1's separate MR. The MR
   description should say so, unless that plan has been moved to its own branch first.
3. **Mutation checks.** Every new test must fail when the behavior it guards breaks. Locally, make
   each change below, run the named test, confirm it fails, then revert. Never commit these
   changes. List the checks you ran in the MR description.

| Break this | Expect this test to fail |
|---|---|
| Delete the `assert_host_can_use_game` call in `host.py`'s `update_question` | B1a `update-question` (X gets 200) |
| Change `require_user` to `get_current_user` in `host.py`'s `list_questions` | B1a `list-questions` (the guest gets the game-rule message instead) |
| Make `assert_host_can_use_game` 404 a missing game before its ADMIN check, i.e. for everyone | B1b (the non-admin gets 404) |
| Change `require_user` to `get_current_user` in `host.py`'s `list_sessions` | B1c `list-sessions` (the guest gets 200 `[]`) |
| Remove `Question.game_id == game_id` from `game_management_service._get_question` | B2 |
| Raise `ConflictError` instead of `UnprocessableError` for "Unrecognised file format" in `import_game` | B3a `wrong-format` (409) |
| Skip `sanitize_prompt` in `import_game` | B3b |
| Give admin creators the game auto-grant in `create_game` | B3c (the admin's `game_access` lists the game) |
| Have `update_game` skip `max_players` | B4a (`wait_until` times out) |
| Add `"id": q.id` to each exported question in `export_game` | B4b step 2 (key set) |
| Add `question.order_index = body.order_index` just before `return question` in `update_question` | B5 (the PUT returns 500: `QuestionResponse.order_index` is `int`, §2) |
| Make an explicit `course_id: null` clear the course in `update_game` | B6 step 1 |
| Remove the course-existence check in `update_game` | B6 step 2 |
| Change `/auth/me`'s guard from `get_current_user` to `require_user` | B7a (the guest gets 403) |
| Run the session-host check before the existence check in `assert_can_download_session` and `assert_can_delete_session` | B7b (the host gets 403 or a 500, not 404) |
| Change `my-games`' `order_by` to `Game.title` alone | B7c |
| Change `outerjoin(User, …)` to `join(User, …)` in `list_sessions_for` | B7d |

4. No `CLAUDE.md` changes are expected: `tests/` has no context file, and `routers/CLAUDE.md`'s
   note that this file polls with `wait_until` stays true.

---

## 9. Out of scope, recorded for follow-up

### 9.1 Import returns 500 for some malformed files (bug — separate MR)

`game_management_service.import_game`, which T4 moved unchanged out of `admin.py`, returns a 500
`INTERNAL_ERROR` instead of a 422 for three kinds of file. This was verified on 2026-09-27 by
calling the function directly with 24 different files:

| File | Exception | Cause |
|---|---|---|
| Valid JSON whose top level isn't an object (`[]`, `"x"`, `42`, `null`) | `AttributeError` | `bundle.get(...)` runs with no type check |
| `"questions"` is `null`, a number, or a boolean | `TypeError` | The `enumerate(questions_raw)` loop sits outside the per-question `try`. Also, `bundle.get("questions", [])` returns `None` when the key is present with a `null` value |
| Bytes that aren't valid UTF-8 (a Windows-1252 file containing "é"; a PNG picked by mistake) | `UnicodeDecodeError` | `json.loads` on bytes decodes them first, and that error isn't a `JSONDecodeError`, so the `except` misses it |

**Impact.**
- The host sees "An unexpected error occurred", with no hint about what's wrong with the file.
- Nothing is written, since every failure happens before the game is created.
- Only signed-in hosts can trigger it: the course-access check runs before the file is read.
- The backend logs a stack trace.

T4 widened who can reach this, from admins to every host. The likeliest trigger is T6 game
authoring: a file that's a bare array of questions, or `"questions": null` in a draft.

**Related oddities (422s, not 500s).**
- A `questions` that is a string or object, a `game` that is `null` or a list, or a `null`
  question each produce a message containing internal names
  (`app.schemas.admin.QuestionCreate() argument after ** must be a mapping`).
- `"version": true` is accepted, because Python treats `True == 1`.

**Fix options.**
- **(a)** Catch `ValueError` around the parse (covering both decode and JSON errors), and return
  a 422 for a non-object top level and for a non-list `questions`. This is the smallest diff.
- **(b)** Validate the whole file with one Pydantic model. This also cleans up the messages, but
  changes wording the frontends display.

The fix MR's test: each malformed shape above → 422 `UNPROCESSABLE`, and no game created. B3a's
structure extends naturally to it.

### 9.2 Deprecated status constant (separate MR)

`UnprocessableError`, added by T4 in `backend/app/common/exceptions.py`, uses
`status.HTTP_422_UNPROCESSABLE_ENTITY`. The installed Starlette marks that constant deprecated in
favor of `HTTP_422_UNPROCESSABLE_CONTENT`. Backend requirements only set lower bounds, and the
constant is read at the moment the error is raised. So a Starlette release that drops the old
name would turn every 422 from this class into a 500. The pre-existing
`validation_error_handler` in the same file uses the same constant. It's a one-word fix and can go
in the same MR as §9.1.

### 9.3 Frontend tests

None of the three frontend apps has any test tooling. These tests are deferred until the
instructor answers two questions: do frontend tests count toward T5, and is adding a frontend test
job to `.gitlab-ci.yml` acceptable under T2?

If approved, these are the candidates, all new in T4, ranked by the harm if they broke:
1. the `/auth/me` checks in the host app's `HostShell` and the admin app's `RequireAdmin`;
2. the error-message helper, in all three `api.ts` copies;
3. Play's course-first selection;
4. `UserDetailPage`'s filter for which games can be granted;
5. `NamePage`'s "Use a different account" link;
6. the Games page's `groupByCourse`, which is the only thing putting "Unassigned" last (MySQL
   sends those games first).

Suggested tooling: Vitest with jsdom and React Testing Library. Stub `fetch` at the network
boundary and find elements by role and text, since T9 will restyle every page. No production code
changes are needed. Use a separate `vitest.config.ts` kept out of `tsconfig.node.json`.

---

## 10. Findings during implementation

Record anything implementation reveals here, such as the outcome of B7d's precondition, before
changing the plan or the code.

- **2026-09-28 — B7d precondition holds.** Checked by hand against the dev stack at migration
  `004`: a USER with HOST created a LOBBY room, then `DELETE /api/admin/users/{id}` returned 204,
  and `GET /api/admin/users/{id}` polled to 404 (so the delete committed). Afterwards,
  `GET /api/game/rooms/{code}` as admin still returned 200, and the admin's
  `GET /api/host/sessions?status=LOBBY` still listed the session, with `host_display_name` null.
  SQLAlchemy nulls `host_user_id` as §2 inferred. B7d is written as specified.
- **2026-09-28 — Deviations in the implementation.**
  - B1c's map holds `id -> lambda t4, token, course_id, session_id: Response`, not B1a's
    `(method, path, kwargs)` triple, because the `import-game` row sends its request through
    `upload_game` as the table specifies. Parametrizing over ids alone is unchanged.
  - B4b and B5 each build test 8's `mc` body inline. §3.2 limits helper changes to §5.1, and test
    8 stays untouched.
  - Test functions follow §5.2's order (B1a, B1c, B1b, B2, …).
  - Commits: the owner chose coarser commits than §5.3 lists. The test code for B1–B7 went in
    as one commit, after the §5.1 helper commit.
- **2026-09-28 — Mutation checks (§8.3): all 17 were caught.** Each was applied to the dev stack's
  backend, the backend restarted, and the named test run, then reverted. None was committed. Two
  failed at a different point than the table predicts, and both still fail the test:
  - *`update_game` skips `max_players`*: B4a fails at the assertion on the PUT's response body
    (`max_players` 150), before `wait_until` runs. The response is the edited in-memory game.
  - *Remove the course-existence check*: B6 step 2 gets a 500, not a 200. The grant-revocation
    `DELETE` autoflushes the dangling `course_id`, and the foreign key rejects it.
  
  As predicted: B5's mutation gives a 500 (response serialization), and B7b's gives the host a
  500 (`user_can_host_session` reads `host_user_id` off a missing session).
- **2026-09-28 — Suite results (§8.1).** `pytest -v`: 57 passed, 2 skipped. `pytest -v --fast`:
  59 passed. Both as expected.

---

## 11. Revision history

- **2026-09-27** — Written from the design discussion that followed a review of T4 MR `0b8b3f2`.
  Owner decisions: backend only; tests appended to `test_t4_access.py`; games with no course
  checked manually; the import bug and the deprecated constant go to a separate MR; frontend tests
  deferred.
- **2026-09-28** — Revised after a goldfish test (a fresh session with only this document, the
  context hierarchy, and the test file; the messages and schemas asserted here were checked
  against the backend source). Changes:
  - §2 and B5: a leaked `order_index: null` gives a 500 from response serialization
    (`QuestionResponse.order_index` is `int`), not a silent 200. B5 keeps the canary, but its
    stated reason is now waiting for the post-response commit, and the B5 mutation row expects a
    500.
  - B7d: the precondition and the test now poll the deleted user to a 404 before trusting the
    204, since a failed post-response commit still returns 204.
  - §8.2: the diff check excludes `ai_log/` and `docs/plans/`, which this branch also carries.
  - §5.1: `_BASE_BUNDLE` added; `upload_game` sends `course_id` as a string. B1a gained a note on
    parametrizing with run-time ids. §5.3 places the docstring update in commit 2.
  - Owner decisions: **add B1c**, a guest and no-token sweep over the 8 host endpoints B1a
    doesn't cover (15 functions and 33 collected tests; 57 + 2 skipped, or 59 with `--fast`), and
    **one mutation row per test function** in §8.3, which keeps §3.5's criterion as written.
