# frontend/admin/src

## Purpose

The Admin console, admin-first: manage user accounts and their course and game access grants,
create courses, and reconcile guest accounts. Host work — authoring games and questions, rosters,
session exports — lives in the host app, which admins can also use; the sidebar's "Host tools"
links open it in a new tab. Nothing here participates in a live game.

## Contents

**Application-level files**

- `main.tsx` — mounts `<App />` into `#root` inside `<StrictMode>`.
- `App.tsx` — router *and* shell. Defines `RequireAdmin` (a layout route guard) and
  `AdminLayout`: a fixed sidebar with `NavLink`s for Users, Courses, and Guests, then a muted
  "Host tools" section of plain new-tab anchors into the host app (Play, Games, Rosters,
  Sessions), then Logout, with an `<Outlet />` for the page. Unmatched paths redirect to `/users`,
  as does `LoginPage` after sign-in.
- `index.css` — Tailwind directives and the dark slate background.
- **No `types/` directory** — unlike the host and player apps. Each page declares its own local
  interfaces (`Course`, `Game`, `UserItem`, …), so the same shape is redefined in several files.

**Subdirectories** — each has its own `CLAUDE.md`:

- [`pages/`](pages/CLAUDE.md) — the five CRUD screens.
- [`components/`](components/CLAUDE.md) — `Button`, `Card`, `Input` (all copies of the host's).
- [`lib/`](lib/CLAUDE.md) — the REST client (with `ApiError`), the host-app link helper
  `hostAppUrl`, and `cn`.

## Architecture

```
main.tsx → App.tsx
              ├── RequireAdmin  (token present? then GET /auth/me: role ADMIN?)
              └── AdminLayout   (sidebar + Host-tools links + <Outlet />)
                     └── page   (own useState, own load(), calls lib/api)
```

**REST-only, no shared state.** There is no socket, no context, no cache, no data-fetching
library. Every page owns its data: `load()` on mount, mutate, `await load()` again. The pattern
is consistent across all five pages — match it rather than introducing a store.

All payloads are **snake_case**, matching the backend's Pydantic schemas directly
(`display_name`, `course_access`, `course_id`). There is no camelCase layer here, which is
the opposite of the host and player apps' socket payloads.

## Gotchas

### The guard checks the role — for display only

`RequireAdmin` sends a missing token to `/login`, then asks the server (`GET /auth/me`): a 401
removes the token and goes to `/login`; any other failure shows the error with Retry (no sign-out);
a non-`ADMIN` gets a full-screen "This console is for administrators." notice with an "Open Buzzer
Host" link (same tab) and Sign out. The role is never read from the token or stored. This matters
because production shares one token across all three apps (same origin, same `token` key), so a
host or a guest who signed in anywhere in that browser lands here. It's display only: the backend's
`require_admin` on every endpoint is what actually protects the console.

### Host tools live in another app

The "Host tools" links and `CoursesPage`'s roster buttons point into the host app via
`hostAppUrl`, in a new tab. Under nginx that's the same origin and token, so they arrive signed
in. In dev it's a different port and origin, with its own token, so expect to sign in again. The
question editor and its `points_value` derivation now live in the host app — see
[host pages](../../host/src/pages/CLAUDE.md).

### Literal escape sequences render on screen

Two spots use `…` in JSX text, where the escape is not processed and the characters appear
verbatim (`Loading…`). Listed precisely in [pages/CLAUDE.md](pages/CLAUDE.md). Same-file
JS-expression uses of the escape are fine, which is why it's easy to miss.

### Duplicated logic

- **Types are redeclared per page.** `Course` appears in `CoursesPage` and `UserDetailPage`;
  `Game` is also redeclared by the host app's pages. Nothing keeps them in sync with each other or
  with the API.
- `Button`, `Card`, `Input`, and `utils.ts` are byte-identical copies of the host app's, and
  `api.ts` now matches the host's in behavior.

### Destructive operations

Deleting a user removes their scores, and deleting a guest does the same — both behind native
`confirm()` dialogs. Revoking a HOST row also removes that user's grants on the course's games
(it warns first). Game delete moved to the host app (admins only, two-step inline confirm).

### Development behavior

- `basename={import.meta.env.BASE_URL}` — a production build only routes correctly under
  `/admin/`.
- `StrictMode` double-invokes effects in dev, so each page's `load()` runs twice on mount.
  Harmless here (all reads), but it doubles the request count you'll see in the network tab.
