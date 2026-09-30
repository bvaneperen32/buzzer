# Import returns 500 for some malformed game files

Status: bug report and fix options, written 2026-09-27 against `main` at `0b8b3f2`. Not scheduled.
**No fix has been agreed yet.** This file is the starting context for the fix branch's design
discussion. Once a fix is agreed, this file can grow into the fix's spec.

It was found while reviewing the T4 merge request (`0b8b3f2`). It is summarized in
`docs/plans/t4-test-coverage.md` §9.1, which may not be on `main` yet. Sections or notes marked
*(new — not yet discussed)* were observed while writing this file and weren't part of the original
discussion.

---

## 1. Summary

Importing a game file (`POST /api/host/games/import`, and the admin twin
`POST /api/admin/games/import`) returns **500** `{"error": "INTERNAL_ERROR", "message": "An
unexpected error occurred"}` instead of a 422 with a readable message, for three kinds of
malformed file:

1. a file that is valid JSON but not a JSON object (e.g. `[]`);
2. a file whose `"questions"` is `null`, a number, or a boolean;
3. a file that isn't valid UTF-8 text.

**Impact.**
- Nothing is written.
- It isn't a security problem.
- The user gets no hint about what's wrong with their file.
- The backend logs a stack trace.

**History.** The bug predates T4. T4 moved the code unchanged into a shared service, and in doing
so widened who can hit it from admins to every host.

**Fix options.** Two are on the table: (a) a minimal fix, and (b) validating the whole file with
one schema (§8).

---

## 2. Where the code is (line numbers at `0b8b3f2`)

- **The function:** `backend/app/services/game_management_service.py`, `import_game`, L240–293:
  - parse: L243–246;
  - format and version checks on the top-level value: L248–254;
  - `game` section validation: L256–260;
  - questions loop: L262–268;
  - game and question creation: L270–293, reached only after all validation passes.
- **Callers:**
  - `backend/app/routers/host.py` L69–82 (`require_user`). The course-access check (L77) runs
    *before* the file is read (L79), and the handler commits after the service returns.
  - `backend/app/routers/admin.py` L330–341 (`require_admin`). The `course_id` form field is
    required.
- **Error handling:** `backend/app/common/exceptions.py`:
  - `UnprocessableError` (L40–45) returns a 422 `{"error": "UNPROCESSABLE", "message": ...}`;
  - FastAPI's request-validation handler (L59–74) returns a 422
    `{"error": "VALIDATION_ERROR", "detail": [...]}`;
  - the catch-all handler (L76–85) turns any other exception into the 500 above and logs
    `unhandled_error` with a stack trace.
- **Frontend:** `frontend/host/src/pages/GamesPage.tsx`, `handleImport` (L148–165), sends
  `api.postForm('/host/games/import', form)` (L157). A failure's `message` is shown in a single
  `<p>` (L214). The admin app no longer has an import UI; T4 moved it to the host app.
- **History:**
  - The parsing code was written in `backend/app/routers/admin.py` (the pre-T4 handler at
    L484–556).
  - Commit `6ba2952` ("Extract game and roster logic from the admin router (T4 step 2)") moved it
    verbatim into the service.
  - T4's spec (`docs/plans/t4-ui-restructuring.md` §5.4) required import to keep "the same
    messages as today" during that move, which is why none of the wording changed.

---

## 3. Reproduction

### 3.1 Observed behavior

**Method.** The real `import_game` was called directly (backend venv, `0b8b3f2`) with a stand-in
database object whose every attribute access raises a marker exception. All validation happens
before the function touches the database, so the outcome can be read off directly:
- an `UnprocessableError` means the API answers 422;
- the marker exception means validation passed, and the API would go on to create the game;
- any other exception means the API answers 500, via the catch-all handler.

The throwaway script isn't in the repo; the method above is enough to recreate it.

Unless a row says otherwise, each file is the valid bundle
`{"format": "buzzer/game", "version": 1, "game": {"title": "t"}, "questions": []}` with the one
field shown changed. 24 files in total:

| File | Result today |
|---|---|
| Valid bundle | accepted |
| Valid bundle with a UTF-8 BOM | accepted |
| `"version": true` | accepted (Python treats `True == 1`) |
| `"version": 1.0` | accepted |
| `{nope` (not JSON) | 422 `Invalid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)` |
| Empty file | 422 `Invalid JSON: Expecting value: line 1 column 1 (char 0)` |
| `"format": "other"` | 422 `Unrecognised file format` |
| `"version": 2` | 422 `Unsupported version 2; server supports version 1` |
| `"game": {"title": ""}` | 422 `Invalid game metadata: ...` (a multi-line dump — §6) |
| `"questions": [{"type": "nope"}]` | 422 `Question 1 invalid: ...` (a multi-line dump — §6) |
| `"questions": "abc"` | 422 `Question 1 invalid: app.schemas.admin.QuestionCreate() argument after ** must be a mapping, not str` |
| `"questions": {"a": 1}` | 422, the same kind of message |
| `"game": null` | 422 `Invalid game metadata: app.schemas.admin.GameCreate() argument after ** must be a mapping, not NoneType` |
| `"game": []` | 422, the same kind of message |
| `"questions": [null]` | 422 `Question 1 invalid: ... argument after ** must be a mapping ...` |
| `[]` (a whole file that's a JSON array) | **500** — `AttributeError: 'list' object has no attribute 'get'` |
| `"hello"` (a whole file that's a JSON string) | **500** — `AttributeError` (`'str'`) |
| `42` | **500** — `AttributeError` (`'int'`) |
| `null` | **500** — `AttributeError` (`'NoneType'`) |
| `"questions": null` | **500** — `TypeError: 'NoneType' object is not iterable` |
| `"questions": 5` | **500** — `TypeError: 'int' object is not iterable` |
| `"questions": true` | **500** — `TypeError: 'bool' object is not iterable` |
| The valid bundle saved as Windows-1252, with an "é" in the title | **500** — `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 ...` |
| The first bytes of a PNG image | **500** — `UnicodeDecodeError: ... byte 0x89 in position 0` |

### 3.2 Reproducing through the API

With the dev stack up, choose one of:
- **Easiest:** FastAPI's docs page at `http://localhost:8000/docs` (development only). Click
  *Authorize* and paste a token, then use `POST /api/host/games/import` with a file and a
  `course_id` the user hosts. An admin token works with any existing course.
- **Command line:** `curl.exe -s -X POST http://localhost:8000/api/host/games/import -H
  "Authorization: Bearer <token>" -F "course_id=<id>" -F "file=@bad.json;type=application/json"`,
  where `bad.json` contains just `[]`. On Windows, use `curl.exe`, because in PowerShell `curl`
  is an alias for `Invoke-WebRequest`.

To get a token, `POST /api/auth/login` with a local account's username and password. The admin's
are `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env`.

Expected today: a 500 with the `INTERNAL_ERROR` body, plus an `unhandled_error` stack trace in the
backend log (`docker compose logs -f backend`).

---

## 4. Root causes

1. **Non-UTF-8 bytes.** When `json.loads` is given bytes, it first decodes them (as UTF-8, unless
   a byte-order mark or null-byte pattern says otherwise). A decoding failure raises
   `UnicodeDecodeError`. That and `JSONDecodeError` both subclass `ValueError`, but neither
   subclasses the other, so the `except json.JSONDecodeError` at L245 misses it.
2. **The top level isn't a JSON object.** The code calls `bundle.get(...)` straight after parsing
   (L248), without checking the parsed value is a dict.
3. **`questions` isn't a list.** The `for i, q in enumerate(questions_raw)` loop (L264) sits
   *outside* the per-question `try`, so looping over something that can't be looped over escapes
   it. `bundle.get("questions", [])` (L262) doesn't help: the default only applies when the key
   is missing, not when it's present with a `null` value. The same is true of `game` (L256).

Every other failure is caught. The `game` section (L257–260) and each question (L265–268) are
validated inside `try … except Exception`. That's why a `null` game or a string question gives a
422 instead of a 500, though with a message built from the raw exception text (§6).

---

## 5. Impact

- **What the user sees:** "An unexpected error occurred" on the Games page, with no hint about
  what's wrong with the file.
- **What gets written:** nothing. Every failure happens before the game is created, so there are
  no partial games.
- **Who can hit it:** only signed-in accounts. On the host router, that means a non-guest account
  with HOST on the chosen course (checked before the file is read) or an admin; on the admin
  router, only admins. Guests and anonymous users can't reach it, so this isn't a security issue.
- **Logs:** each occurrence logs an error-level `unhandled_error` with a stack trace. That noise
  can hide real errors.
- **Exposure change:** before T4, only admins could import. T4's host import endpoint put it in
  front of every host.
- **Likeliest trigger: T6 game authoring,** where each team member writes game JSON files and
  imports them. For example:
  - a file that's a bare array of questions (a shape scripts and AI tools commonly produce);
  - `"questions": null` in a draft.

  The encoding case is less likely with modern editors, which save UTF-8 by default, but picking
  the wrong file would hit it.

---

## 6. Related oddities (422s, not 500s)

- **422 messages built from raw exception text.** Two forms reach the user today:
  - **Wrong type:** internal Python names leak, e.g. `app.schemas.admin.QuestionCreate()
    argument after ** must be a mapping, not str`.
  - **Validation errors:** the whole multi-line Pydantic error text. Here is the full `message`
    for `"game": {"title": ""}`:

    ```text
    Invalid game metadata: 1 validation error for GameCreate
    title
      String should have at least 1 character [type=string_too_short, input_value='', input_type=str]
        For further information visit https://errors.pydantic.dev/2.13/v/string_too_short
    ```

    For `"questions": [{"type": "nope"}]` it's three such blocks (a `type` pattern mismatch, plus
    missing `grading_type` and `prompt`), each with its own URL line. The Games page shows the
    message in a single `<p>`, so the line breaks collapse into one long run-on line.
- *(new — not yet discussed)* **A cleaner format already exists in the same file.** In
  `update_question` (L161–164), the 422 message joins each Pydantic error's `msg` with `"; "`,
  giving short readable messages. Either fix option could reuse it.
- **Loose version check.** `"version": true` and `"version": 1.0` are accepted, because Python
  compares `True == 1` and `1.0 == 1`. It's harmless, but worth deciding whether a stricter check
  is wanted.

---

## 7. Companion issue: a deprecated status constant

- **The problem.** `UnprocessableError` (`exceptions.py` L45, added by T4) and the pre-existing
  request-validation handler (L69) both use `status.HTTP_422_UNPROCESSABLE_ENTITY`. The venv's
  Starlette (1.6.0, alongside FastAPI 0.141.1) warns that this name is deprecated, and offers
  `HTTP_422_UNPROCESSABLE_CONTENT` instead.
- **The risk.** `backend/requirements.txt` only sets lower bounds (`fastapi>=0.111`; Starlette
  arrives through FastAPI), and the constant is looked up at the moment the error is raised. So a
  future Starlette release that drops the old name would make every `UnprocessableError`, and
  every request-validation failure, crash with `AttributeError` and become a 500.
- *(new — not yet discussed)* **A caution for the fix.** Older Starlette releases may not have
  the new name, and a teammate's Docker image may have been built with older packages than the
  venv. Writing the plain number `422` avoids depending on either name.
- **Agreed so far:** it's a one-line change that can go in the same merge request as this fix.

---

## 8. Fix options

### (a) Minimal fix

- Catch `ValueError` around the parse. That covers both `JSONDecodeError` and
  `UnicodeDecodeError`. Return a 422, either reusing "Invalid JSON" or with its own message for a
  file that isn't UTF-8 text.
- If the parsed value isn't a JSON object, return a 422.
- If `questions` isn't a list, return a 422. Also decide whether `"questions": null` should mean
  "no questions" or be an error, and the same for `"game": null`.

**Pros:** the smallest diff. The five existing rejection messages keep their wording, so the
frontend, and any test that checks message prefixes, is unaffected.
**Cons:** the raw-exception messages from §6 stay; `"questions": "abc"` would still get the
"argument after \*\*" message.
**The discussion's recommendation** for a bug-fix merge request, as the smallest change to
review, unless clearer messages for T6 authors are wanted.

### (b) Validate the whole file with one Pydantic model

A single schema describes the whole file: `format`, `version`, `game` (as `GameCreate`), and
`questions` (a list of `QuestionCreate`). It's validated in one step, and every failure becomes a
readable 422.

**Pros:** every shape of malformed file is handled the same way; no internal names in messages;
one place to evolve the file format.
**Cons:**
- It changes the wording users see, both the five existing messages and the Pydantic dumps. T4's
  spec deliberately kept today's wording during the refactor. A bug fix is allowed to change it,
  but it's a visible change and should be decided on purpose.
- It's a bigger diff.
- It must keep the `extra="forbid"` behavior of `GameCreate` and `QuestionCreate`.
- Unmodified version-1 files must keep importing (an `instructions.md` T6 requirement).

### Considerations for choosing *(new — not yet discussed)*

- **Future format changes.** T8 (images) may extend the game format and bump the version, while
  unmodified version-1 files must keep importing (`instructions.md` T6). If that's likely, a
  whole-file model gives version handling a single home.
- **Message style.** `update_question`'s `"; "`-joined format (§6) is an existing precedent that
  either option could adopt for game and question validation errors.

---

## 9. Tests for the fix

- **Integration tests** (T5 style, in `tests/integration/`, run against the stack). Every
  **500** row in §3.1 should give:
  - a 422 with `error == "UNPROCESSABLE"`;
  - a readable `message`;
  - no game created (the host's `GET /api/game/my-games` is unchanged).

  At minimum, cover the host router: it's the newly exposed path, and both routers share the
  service.
- **Connection to the test-coverage plan.** If `docs/plans/t4-test-coverage.md` has been
  implemented, its test B3a (`test_import_rejections_use_the_unprocessable_format` in
  `tests/integration/test_t4_access.py`) already covers the five designed rejections. It
  deliberately leaves out these 500 shapes, so the fix adds them as new cases.
  - B3a checks message *prefixes*: `"Invalid JSON"`, `"Unrecognised file format"`,
    `"Unsupported version"`, `"Invalid game metadata"`, `"Question 1 invalid"`.
  - Option (b) must either keep those prefixes or update B3a's expectations on purpose. Changing
    an expected string to match intentionally changed wording is not weakening a test (T5).
- **Prove the tests work.** Show that every new test fails without the fix; each gets a 500 today.
- *(new — not yet discussed)* **Optional faster tests.** Since `import_game` fails before touching
  the database, the parsing can also be tested directly with a stand-in database (the method in
  §3.1), with no Docker stack. Those tests could live in `tests/unit/`. Note that CI currently
  runs no pytest suite at all.
- **The constant change (§7)** has no behavior to test beyond the existing suites still passing.

---

## 10. Scope notes

- **In scope:** the three 500 causes; the message cleanup (§6), depending on the choice between
  (a) and (b); the companion constant (§7).
- **Related, but a separate decision:** `README.md`'s import `curl` example lacks the `course_id`
  form field that T4 made required. T4 left the README unchanged by owner decision
  (`docs/plans/t4-ui-restructuring.md` §9). Fix it here only if you decide to on purpose.

---

## 11. Suggested process on the fix branch

1. Branch from `main`, e.g. `fix/import-malformed-files`. T1: a bug found along the way gets its
   own branch and merge request.
2. Start the conversation from this file (e.g. with `/design-discussion`) to choose between (a)
   and (b) and answer the open questions:
   - what `"questions": null` and `"game": null` should mean;
   - whether to keep today's message wording;
   - whether to tighten the version check.
3. Record the decision by turning this file into the fix's spec (e.g. with `/write-spec`) before
   writing code.
4. Run the full integration suite before and after the change.
5. After the fix, check whether `backend/app/routers/CLAUDE.md` and
   `backend/app/services/CLAUDE.md` need a note, per `.claude/rules/context-sync.md`.
