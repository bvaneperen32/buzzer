# frontend/admin/src/pages

## Purpose

The whole admin console: courses and rosters, users and access grants, games and questions, guest
reconciliation, and session exports. Eight pages, each a CRUD view over `/api/admin/*`.

## Contents

- `LoginPage.tsx` — username/password only. No SSO option (admins are local accounts).
- `CoursesPage.tsx` — list, create, inline edit. Links to each course's roster. No delete.
- `RosterPage.tsx` — roster list plus a **CSV column-mapping wizard**: client-side RFC-4180
  parser, header auto-detection, per-column transforms (strip `@domain`, reverse
  `"Last, First"`), a live 3-row preview, then `POST .../roster/import`. Also inline row editing
  via `PATCH`.
- `UsersPage.tsx` — list local accounts and create new ones. Rows link to the detail page.
- `UserDetailPage.tsx` — edit a user, delete them, and grant/revoke course access (with
  HOST/PLAYER role) and game access via multi-select panels.
- `GuestsPage.tsx` — list guest accounts, merge one into a real user by netid, or delete it. If no
  user has that netid, the backend **promotes the guest in place** into a `USER` with that netid
  instead of failing — so a typo'd netid creates a bogus account rather than an error.
- `GamesPage.tsx` — list, create, inline edit, delete, and **import a game JSON bundle**.
- `QuestionEditorPage.tsx` — the largest page (~790 lines). Per-type question authoring forms,
  reordering, delete, and JSON export.
- `SessionsPage.tsx` — session history with status filter, an expandable **export options panel**
  (Canvas gradebook vs raw CSV, SIS domain, roster-only, per-question), HTML report download, and
  delete.

## How it fits in

`App.tsx` holds the shell: `RequireAdmin` → `AdminLayout` (sidebar + `<Outlet />`) → page. There
is no shared state layer — each page owns its own `useState` and refetches with a local `load()`
after every mutation. No socket, no context, no cache.

The pattern is uniform and worth matching: `load()` on mount, `loading` / `error` / `saving`
flags, mutate then `await load()`.

## Gotchas

### `points_value` is derived here, and that's load-bearing

`QuestionEditorPage.formToPayload` does not ask the author for a points value on ACCURACY
questions — it computes one:

| Type | `points_value` |
|---|---|
| `multiple_choice` | max of the option points |
| `true_false` | max(true, false) |
| `fill_in_the_blank` | max of the accepted-answer points |
| `multi_select` | **sum** of the positive option points |
| any COMPLETENESS | the value the author typed |

This is what makes the backend's reveal rule (`answer_points[i] >= points_value` ⇒ correct) work
for the first three: only the top-scoring option(s) hit the max. For `multi_select` the
"correct" set can't be expressed that way, which is why the server sends the raw `answerPoints`
array instead of `correctIndices` and lets clients mark each option with `points > 0`. Hand-built
JSON that sets `points_value` independently will break both the reveal display and `is_correct`.

One flaw in the derivation itself: for `multi_select` and `fill_in_the_blank`, `formToPayload`
drops rows with blank text from the saved options/answers but computes `points_value` over *all*
rows. A blank row that still carries points inflates `points_value` above anything an answer can
score, so `is_correct` can never be true for that question.

### Editing a question skips validation the API applies on create

`updateQuestion` sends `PUT`, and the backend's `QuestionUpdate` schema has **no** structural
validator — unlike `QuestionCreate`. Since this form always submits a complete, consistent
payload, that gap isn't reachable through this UI, but it means the editor is the only thing
enforcing shape on the edit path. Loosen the form and there's nothing behind it.

### Literal `…` renders on screen

Several strings use `…` / `±` escapes. Inside a JS expression (`{saving ? 'Saving…'
: …}`) that's fine — it's a real string literal. **In JSX text or a JSX attribute it is not**:
those are not JS string literals, so the escape is never processed and the characters appear
verbatim. Currently broken:

- `UsersPage.tsx:96`, `GuestsPage.tsx:98`, `GamesPage.tsx:207`,
  `QuestionEditorPage.tsx:604` — all render `Loading…`
- `QuestionEditorPage.tsx:226` — `placeholder="Question text…"`
- `QuestionEditorPage.tsx:703` — `{q.time_limit_seconds}s · {q.points_value}pts`
- `QuestionEditorPage.tsx:741` — `(±{...} edit distance)`

`SessionsPage` and `CoursesPage` use the literal characters and are fine. Use `…` and `·`
directly, or `{'…'}`.

### Roster import duplicates backend parsing, differently

`RosterPage.mapRow` reimplements what `roster_service.process_roster_csv` does server-side, and
the two don't agree:

- **Title-casing.** The client does `full_name.replace(/\b\w/g, c => c.toUpperCase())`, which
  uppercases each word's first letter but leaves the rest alone — `"MCDONALD"` stays
  `"MCDONALD"`. Python's `.title()` on the server path yields `"Mcdonald"`. The same student
  imported through the wizard versus a direct CSV upload gets a different stored name, and that
  name is what players see on screen.
- The wizard stores the mapped `full_name` as-is, while the server's own CSV path keeps only the
  *given* name. Two import paths, two meanings for one column.
- There's also a third CSV parser in play: this file's `parseCSV` is hand-rolled, while the
  server uses Python's `csv` module.

### Destructive actions and their guards

- **`GamesPage.deleteGame` destroys play history.** The backend deletes every `SessionScore` and
  `GameSession` for that game first. The `confirm()` says "Delete this game and all its
  sessions?" — accurate, but it's a native `confirm`, and the blast radius (every past session's
  results) deserves more than that.
- `GuestsPage.deleteGuest` and `UserDetailPage.deleteUser` also cascade to score rows.
- **Deleting a question fails for any question that has been answered** in any session — the
  backend returns a 500 because it can't null `session_scores.question_id` (see
  [routers/CLAUDE.md](../../../../backend/app/routers/CLAUDE.md)). With the `err.detail` bug the
  author sees only `HTTP 500`.
- `SessionsPage` uses a two-step inline confirm instead of `confirm()` — the better pattern,
  inconsistently applied.
- `SessionsPage.handleDelete` calls `/game/sessions/{id}`, an endpoint on the **game** router, not
  the admin one. It works (admins pass the host-or-admin check) but it's the only cross-router
  call in the app.

### Smaller issues

- **`UserDetailPage.saveEdit` silently drops username edits for SSO users.** The guard is
  `if (user.username !== null && editUsername !== user.username)`, so a netid-only account (whose
  `username` is `null`) can never have one set through this form.
- The same function sends `email: ""` when the field is cleared, which fails the backend's
  `EmailStr` validation with a 422 rather than clearing the address.
- **Access grants fire one request per selection** via `Promise.all`. Ten courses is ten POSTs,
  and a partial failure leaves some granted with a single error message shown.
- **`moveQuestion` sends the full reorder array and then sets local state optimistically** — but
  only on success, so a failed reorder leaves the list visually unchanged. Good; just note that
  each arrow click is a full round trip.
- `SessionsPage` sends `per_question` for the `raw` format too, where the backend ignores it
  (`build_session_csv` takes no such argument and always emits per-question columns). Harmless,
  but the UI implies a choice that doesn't exist.
- `GuestsPage`'s merge form takes a raw UUID; the "Use ID" button on each row is the only
  practical way to fill it.
- Every page refetches its whole list after every mutation. Fine at course/game scale; `RosterPage`
  with ~1000 entries reloads all of them after a single inline edit.
