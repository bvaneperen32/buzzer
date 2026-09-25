# frontend/admin/src/pages

## Purpose

The admin console's screens: users and their access grants, courses, and guest reconciliation.
Five pages, each a CRUD view over `/api/admin/*`. Games, questions, rosters, and session exports
moved to the host app ([host pages](../../../host/src/pages/CLAUDE.md)); this app links there.

## Contents

- `LoginPage.tsx` — username/password only. No SSO option (admins are local accounts). Lands on
  `/users` after sign-in, matching `App.tsx`'s catch-all.
- `UsersPage.tsx` — list local accounts and create new ones. Rows link to the detail page.
- `UserDetailPage.tsx` — edit a user, delete them, and grant/revoke course access (with
  HOST/PLAYER role) and game access via multi-select panels. The game panel offers only games in
  courses where this user holds **HOST** and that aren't already granted, labelled "Title (Course,
  Semester)". If the user HOSTs no course, "Grant HOST on a course first — …" replaces **Add
  Games**. Revoking a **HOST** row warns first with `confirm()`, because the server also removes
  the user's grants on that course's games.
- `CoursesPage.tsx` — list, create, inline edit. No delete. Each course's **Roster** button is an
  anchor to the host app's `/rosters/{id}` (`hostAppUrl`), opened in a new tab.
- `GuestsPage.tsx` — list guest accounts, merge one into a real user by netid, or delete it. If no
  user has that netid, the backend **promotes the guest in place** into a `USER` with that netid
  instead of failing — so a typo'd netid creates a bogus account rather than an error.

## How it fits in

`App.tsx` holds the shell: `RequireAdmin` → `AdminLayout` (sidebar + `<Outlet />`) → page. There
is no shared state layer — each page owns its own `useState` and refetches with a local `load()`
after every mutation. No socket, no context, no cache.

The pattern is uniform and worth matching: `load()` on mount, `loading` / `error` / `saving`
flags, mutate then `await load()`. The host app's management pages follow the same pattern.

## Gotchas

### Literal `…` renders on screen

Several strings use `…` / `±` escapes. Inside a JS expression (`{saving ? 'Saving…'
: …}`) that's fine — it's a real string literal. **In JSX text or a JSX attribute it is not**:
those are not JS string literals, so the escape is never processed and the characters appear
verbatim. Currently broken:

- `UsersPage.tsx:96`, `GuestsPage.tsx:98` — both render `Loading…`

`CoursesPage` uses the literal characters and is fine. Use `…` and `·` directly, or `{'…'}`.
(The pages that had the other broken spots moved to the host app, where they were fixed.)

### Access grants

- **Game access only makes sense inside a HOSTed course.** The server refuses (409) to grant a
  game that's unassigned or whose course the user doesn't HOST, so the panel never offers one.
  Losing HOST — by revoking the row here, or by a PLAYER downgrade through the API — makes the
  server delete that user's grants on the course's games. The page has no control for downgrading
  HOST to PLAYER.
- **"Save, then reload" can briefly show stale data.** The backend commits these writes *after*
  sending the response (see [routers/CLAUDE.md](../../../../backend/app/routers/CLAUDE.md)), so
  right after a HOST revoke the reload can still list the game grants the server is removing.
  *Noted, not scheduled.*
- **Access grants fire one request per selection** via `Promise.all`. Ten courses is ten POSTs,
  and a partial failure leaves some granted with a single error message shown.

### Destructive actions and their guards

- `GuestsPage.deleteGuest` and `UserDetailPage.deleteUser` cascade to score rows, behind native
  `confirm()` dialogs.
- Game delete (which erases every session and score ever recorded for the game) is no longer
  here: admins reach it from the host app's Games page, behind a two-step inline confirm.

### Smaller issues

- **`UserDetailPage.saveEdit` silently drops username edits for SSO users.** The guard is
  `if (user.username !== null && editUsername !== user.username)`, so a netid-only account (whose
  `username` is `null`) can never have one set through this form.
- The same function sends `email: ""` when the field is cleared, which fails the backend's
  `EmailStr` validation with a 422 rather than clearing the address.
- `GuestsPage`'s merge form takes a raw UUID; the "Use ID" button on each row is the only
  practical way to fill it.
- Every page refetches its whole list after every mutation. Fine at course/user scale.
