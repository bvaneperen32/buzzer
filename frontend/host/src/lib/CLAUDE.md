# frontend/host/src/lib

## Purpose

Shared non-React utilities for the host app. Two small files: the HTTP client and a class-name
merger. There is no socket helper here — the socket lives directly in `GameLayout`.

## Contents

- `api.ts` — `apiFetch<T>` plus the `api` object (`get` / `post` / `delete`). Prefixes `/api`,
  attaches `Authorization: Bearer <token>` from `localStorage`, sets JSON content type, throws on
  non-2xx, and tolerates empty response bodies (204s) by returning `{}`.
- `utils.ts` — `cn(...)`, the standard `clsx` + `tailwind-merge` combination used by every UI
  component so caller-supplied classes win over defaults.

## How it fits in

`api.ts` is the only place REST calls are configured; pages import `api` rather than calling
`fetch` directly. One deliberate exception: `LoginPage` calls `fetch` by hand for
`/api/auth/exchange-temp`, because that request must send the *temp* token rather than the stored
one.

Auth token handling is split across the app rather than centralized here — `lib` reads
`localStorage.token`, while `LoginPage` writes it, `HomePage.logout` removes it, and `App`'s
`RequireAuth` checks it. There is no auth module.

## Gotchas

- **The error-message extraction doesn't match the backend's error shape.** `api.ts` reads
  `err.detail`, but the backend's `BuzzerError` handler returns `{"error": code, "message": ...}`
  — there is no `detail` key. So every 401/403/404/409 falls through to the generic
  `HTTP <status>` string and the server's actual message is discarded. Only 422 validation errors
  carry `detail`, and there it's an *array*, so `new Error(detail)` stringifies it into something
  unreadable. Reading `message` first, then falling back, would fix both.
- **The token lives in `localStorage`**, so it is readable by any script on the origin and is not
  protected the way the backend's `httpOnly` refresh cookie is. It is also **shared**: all three
  apps use the key `token`, and in production they share one origin, so a player or admin sign-in
  in the same browser replaces the host's token — see [frontend/CLAUDE.md](../../../CLAUDE.md).
- **There's no 401 handling and no refresh integration.** The backend issues a 7-day refresh
  cookie and exposes a refresh endpoint, but the client never calls it. When the 2-hour access
  token expires mid-session the user simply starts getting errors; `RequireAuth` only checks that
  *some* token string exists, never that it's still valid.
- `api` exposes only `get`, `post`, and `delete` — no `put` or `patch`. Anything needing those
  (question editing, roster patching) can't use this client as-is.
- Responses are cast to `T` with no runtime validation, so a backend shape change surfaces as an
  undefined property at render time rather than at the call site.
- REST responses use **snake_case** (`room_code`, `game_title`, `access_token`) while socket
  payloads use **camelCase**. Both conventions are live in this app; match whichever transport
  you're on.
