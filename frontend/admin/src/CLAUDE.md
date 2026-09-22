# frontend/admin/src

## Purpose

The Admin console: the authoring and administration surface. Create courses and import rosters,
manage users and their access grants, author games and questions, reconcile guest accounts, and
export session results to Canvas. Nothing here participates in a live game.

## Contents

**Application-level files**

- `main.tsx` — mounts `<App />` into `#root` inside `<StrictMode>`.
- `App.tsx` — router *and* shell. Defines `RequireAdmin` (a layout route guard) and
  `AdminLayout` (fixed sidebar with five `NavLink`s plus logout, and `<Outlet />` for the page).
  Unmatched paths redirect to `/courses`.
- `index.css` — Tailwind directives and the dark slate background.
- **No `types/` directory** — unlike the host and player apps. Each page declares its own local
  interfaces (`Course`, `Game`, `UserItem`, `Question`, …), so the same shape is redefined in
  several files.

**Subdirectories** — each has its own `CLAUDE.md`:

- [`pages/`](pages/CLAUDE.md) — the eight CRUD screens.
- [`components/`](components/CLAUDE.md) — `Button`, `Card`, `Input` (all copies of the host's).
- [`lib/`](lib/CLAUDE.md) — the REST client, with upload and download helpers.

## Architecture

```
main.tsx → App.tsx
              ├── RequireAdmin  (token present?)
              └── AdminLayout   (sidebar + <Outlet />)
                     └── page   (own useState, own load(), calls lib/api)
```

**REST-only, no shared state.** There is no socket, no context, no cache, no data-fetching
library. Every page owns its data: `load()` on mount, mutate, `await load()` again. The pattern
is consistent across all eight pages — match it rather than introducing a store.

All payloads are **snake_case**, matching the backend's Pydantic schemas directly
(`display_name`, `max_players`, `time_limit_seconds`). There is no camelCase layer here, which is
the opposite of the host and player apps' socket payloads.

## Gotchas

### The guard doesn't check the role

`RequireAdmin` only tests that a token exists in `localStorage` — not that it's valid, unexpired,
or that the user is actually an `ADMIN`. A signed-in non-admin (a host, say) can load the entire
console; every request then 403s and each page renders its own error string. The backend is safe
— `require_admin` guards every endpoint — but the UI gives no useful signal about what went
wrong, and there's no redirect back to `/login`. In production this is the *normal* case, not an
edge case: the token is shared with the host and player apps (same origin, same `token` key), so
anyone signed into the host app — or who joined a game as a guest — in that browser lands in the
console.

### Question authoring owns an invariant the rest of the stack depends on

`QuestionEditorPage` *derives* `points_value` from the per-option points rather than asking for
it (max for choice types, sum of positives for `multi_select`). Both the host and player apps
read correctness off that relationship. It is an undocumented contract between this form and four
other files — see [pages/CLAUDE.md](pages/CLAUDE.md) for the table and the consequences.

### Literal escape sequences render on screen

Seven spots use `…` or `±` in JSX text or attribute position, where the escape is not
processed and the characters appear verbatim (`Loading…`). Listed precisely in
[pages/CLAUDE.md](pages/CLAUDE.md). Same-file JS-expression uses of the escape are fine, which is
why it's easy to miss.

### Duplicated logic

- **Roster CSV parsing exists twice**, once here (`RosterPage.parseCSV` + `mapRow`) and once in
  `roster_service`, and the two disagree on title-casing — the same student gets a different
  stored name depending on which import path was used.
- **Types are redeclared per page.** `Course` appears in `CoursesPage`, `UserDetailPage`, and
  elsewhere; `Game` in three files. Nothing keeps them in sync with each other or with the API.
- `Button`, `Card`, `Input`, and `utils.ts` are byte-identical copies of the host app's.

### Destructive operations

This app is where irreversible things happen. Deleting a game removes every session and score
ever recorded for it; deleting a user removes their scores. Most confirmations are native
`confirm()` dialogs; `SessionsPage` uses a nicer two-step inline confirm. The guards are thinner
than the consequences.

### Development behavior

- `basename={import.meta.env.BASE_URL}` — a production build only routes correctly under
  `/admin/`.
- `StrictMode` double-invokes effects in dev, so each page's `load()` runs twice on mount.
  Harmless here (all reads), but it doubles the request count you'll see in the network tab.
