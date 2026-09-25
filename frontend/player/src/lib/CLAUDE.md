# frontend/player/src/lib

## Purpose

Shared non-React utilities for the player app: the HTTP client, the `cn` class-name helper, and a
JWT expiry check.

## Contents

- `api.ts` — `apiFetch<T>` plus `api.get` / `api.post`. Prefixes `/api`, attaches
  `Authorization: Bearer <token>` from `localStorage`, throws on non-2xx, returns `{}` for empty
  bodies.
- `utils.ts` — `cn(...)` (clsx + tailwind-merge), and **`isTokenExpired(token)`**, which
  base64url-decodes the JWT payload and compares its `exp` claim against the current time.
  Returns `true` for a missing or malformed token.

## How it fits in

REST is a minor transport in this app — it's used only for joining (`/game/rooms/{code}/ping`,
`/auth/guest`, `/auth/login`). Once the player is in a room, everything runs over the socket
owned by `GameLayout`. `LoginPage` calls `fetch` directly for `/api/auth/exchange-temp`, because
that request carries the temp token rather than the stored one.

`isTokenExpired` is the meaningful difference from the other two apps: it is checked in
`NamePage` (to decide whether to offer sign-in or a straight Join button) and in `GameLayout`
(before opening the socket). A stale token sends the player back to the name screen instead of
failing at connect time.

## Gotchas

- **The error-message extraction doesn't match the backend's error shape.** `api.ts` reads
  `err.detail`, but `BuzzerError` responses are `{"error": code, "message": ...}` — there is no
  `detail` key. Every 401/403/404/409 therefore surfaces as a bare `HTTP <status>` and the
  server's real message is thrown away. This matters more here than elsewhere: a guest who picks
  an email already tied to a real account gets a specific, actionable message from the server
  ("Please sign in instead") that the UI replaces with `HTTP 409`. Only 422 carries `detail`, and
  there it's an array that stringifies into noise. The identical bug exists in the host and admin
  copies of this file.
- **`isTokenExpired` does not verify the signature** — it only reads the `exp` claim. That's
  correct for a UI hint (the server verifies for real), but don't repurpose it as a security
  check.
- It also doesn't account for clock skew: a client whose clock runs fast will discard a token the
  server still considers valid.
- `api` exposes only `get` and `post` — no `delete` or `put` (the host copy has `delete`). The
  three apps' clients have drifted; they aren't interchangeable.
- The token is in `localStorage`, so it survives a tab close. `GameOverPage`'s "Play Again"
  deliberately clears it — which also signs out a NetID user, not just a guest. And because all
  three apps use the key `token` on one origin in production, clearing it (or a guest join
  writing it) also affects a host or admin signed in in the same browser — see
  [frontend/CLAUDE.md](../../../CLAUDE.md).
- Responses are cast to `T` with no runtime validation.
