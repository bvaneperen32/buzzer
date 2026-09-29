# frontend/player/src/lib

## Purpose

Shared non-React utilities for the player app: the HTTP client, the `cn` class-name helper, and a
JWT expiry check.

## Contents

- `api.ts` — `apiFetch<T>` plus `api.get` / `api.post`. Prefixes `/api`, attaches
  `Authorization: Bearer <token>` from `localStorage`, throws on non-2xx, returns `{}` for empty
  bodies. The thrown `Error`'s message comes from one helper, `errorMessage`: the server's
  `message` if it's a string, else a string `detail`, else a 422 `detail` array's `msg`s joined
  with `"; "`, else `HTTP <status>`. (The host and admin copies use the same helper and add an
  `ApiError` that carries the status; this app doesn't need the status.)
- `utils.ts` — `cn(...)` (clsx + tailwind-merge), and **`isTokenExpired(token)`**, which
  base64url-decodes the JWT payload and compares its `exp` claim against the current time.
  Returns `true` for a missing or malformed token.
- `questionImages.ts` (T8) — the phone's side of option images: `optionImageLinks` (one small link
  or `null` per option, from the question's `images` map; an unknown id is no image), the option
  letters, `choiceLabel` (`A — Paris`, or the letter alone), and image options' accessible names.
  A deliberate copy of the host's helpers; the apps share no code.

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

- **Server messages reach the player now, and they matter here most.** A guest who picks an email
  already tied to a real account sees the server's actionable "Please sign in instead", and a
  guest refused by a no-guests room sees "This game isn't open to guests — sign in with your
  school account", where both used to read `HTTP 409` / `HTTP 403`. `LoginPage`'s hand-rolled
  `exchange-temp` fetch bypasses the helper and still reads only `detail`.
- **`isTokenExpired` does not verify the signature** — it only reads the `exp` claim. That's
  correct for a UI hint (the server verifies for real), but don't repurpose it as a security
  check.
- It also doesn't account for clock skew: a client whose clock runs fast will discard a token the
  server still considers valid.
- `api` exposes only `get` and `post` — the host and admin copies have every verb plus upload and
  download helpers. The three apps' clients aren't interchangeable.
- The token is in `localStorage`, so it survives a tab close. `GameOverPage`'s "Play Again"
  deliberately clears it — which also signs out a NetID user, not just a guest. And because all
  three apps use the key `token` on one origin in production, clearing it (or a guest join
  writing it) also affects a host or admin signed in in the same browser — see
  [frontend/CLAUDE.md](../../../CLAUDE.md).
- Responses are cast to `T` with no runtime validation.
