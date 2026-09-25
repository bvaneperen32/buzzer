# frontend/host/src/lib

## Purpose

Shared non-page plumbing for the host app: the HTTP client, the current-user context, and a
class-name merger. There is no socket helper here — the socket lives directly in `GameLayout`.

## Contents

- `api.ts` — `apiFetch<T>` plus the `api` object: `get` / `post` / `put` / `patch` / `delete`,
  and two helpers copied from the admin app:
  - **`postForm(path, formData)`** — multipart upload (game JSON import). It deliberately omits
    `Content-Type` so the browser supplies the multipart boundary.
  - **`download(path)`** — fetches with the bearer header, reads the filename from
    `Content-Disposition`, and saves via a temporary `<a download>`. Used for session reports,
    CSV exports, and game JSON export.

  Every request prefixes `/api`, attaches `Authorization: Bearer <token>` from `localStorage`,
  and tolerates empty bodies (204s) by returning `{}`. Every failure throws an **`ApiError`**
  (extends `Error`, adds the numeric `status`) built by one helper, `toApiError`. The helper uses
  the server's `message` if it's a string, else a string `detail`, else a 422 `detail` array's
  `msg`s joined with `"; "`, else `HTTP <status>`.
- `me.tsx` — the current user: type `Me` (`id`, `username`, `netid`, `display_name`, `role`),
  `MeContext`, and `useMe()`. `HostShell` loads it from `GET /auth/me` and provides it.
  `useMe()` throws outside the shell. It's held **in memory only** — never write the role to
  `localStorage`, where it would go stale the same way the token's role claim does.
- `utils.ts` — `cn(...)`, the standard `clsx` + `tailwind-merge` combination used by every UI
  component so caller-supplied classes win over defaults.

## How it fits in

`api.ts` is the only place REST calls are configured; pages import `api` rather than calling
`fetch` directly. One deliberate exception: `LoginPage` calls `fetch` by hand for
`/api/auth/exchange-temp`, because that request must send the *temp* token rather than the stored
one. It therefore bypasses the error helper and still reads only `detail`.

Auth token handling is split across the app rather than centralized here — `lib` reads
`localStorage.token`, while `LoginPage` writes it, `HostShell` removes it (Sign out, and a 401
from `/auth/me`), and `App`'s `RequireAuth` checks that it exists. There is no auth module.

`useMe().role` drives display only (admin-only controls on Games and Sessions). Every action
behind those controls calls an endpoint that enforces `ADMIN` itself.

## Gotchas

- **Only `HostShell` acts on a 401**, and only for `/auth/me` on entry to the shell. A token that
  expires while a page is open still surfaces as that page's error message (now the server's
  real one), with no redirect. There's no refresh integration: the backend issues a 7-day refresh
  cookie and exposes a refresh endpoint, but the client never calls it.
- **The token lives in `localStorage`**, so it is readable by any script on the origin and is not
  protected the way the backend's `httpOnly` refresh cookie is. It is also **shared**: all three
  apps use the key `token`, and in production they share one origin, so a player or admin sign-in
  in the same browser replaces the host's token — see [frontend/CLAUDE.md](../../../CLAUDE.md).
- `download` revokes the object URL synchronously right after `a.click()`. That works in current
  browsers because the download is already queued, but it's timing-dependent. Its filename regex
  only matches a quoted `filename="..."` (the backend always quotes).
- A non-JSON error body (a proxy's HTML 502, say) falls through to `HTTP <status>`.
- Responses are cast to `T` with no runtime validation, so a backend shape change surfaces as an
  undefined property at render time rather than at the call site.
- REST responses use **snake_case** (`room_code`, `game_title`, `access_token`) while socket
  payloads use **camelCase**. Both conventions are live in this app; match whichever transport
  you're on.
- `api.ts` here and admin's are now functionally identical (same verbs, same helper), but they're
  still two files. A fix in one needs the same fix in the other.
