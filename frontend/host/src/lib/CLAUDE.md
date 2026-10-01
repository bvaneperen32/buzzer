# frontend/host/src/lib

## Purpose

Shared non-page plumbing for the host app: the HTTP client, the current-user context, and a
class-name merger. There is no socket helper here — the socket lives directly in `GameLayout`.

## Contents

- `api.ts` — `apiFetch<T>` plus the `api` object: `get` / `post` / `put` / `patch` / `delete`,
  and two helpers copied from the admin app:
  - **`postForm(path, formData, method = 'POST')`** — multipart upload (game JSON import, image
    upload; `'PUT'` for an image replace). It deliberately omits `Content-Type` so the browser
    supplies the multipart boundary.
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
- `imageLibrary.ts` (T8) — `useImageLibrary(gameId)`: the editor's library (images with links and
  uses, live sessions, meters), with `reload`, `upload`, `replace`, `setDescription`, `remove`.
  Mutations adopt the returned item at once, then reload. `reload` coalesces simultaneous calls
  into one request, and a response that started before a write is dropped, so it can't overwrite
  the adopted item. It reloads again within 10 minutes of the earliest link expiry.
- `sessionImages.ts` (T8) — `useSessionImageLinks(sessionId)`: the projector's link map, with a
  coalescing `refresh` and a `failed` flag (the last fetch failed, e.g. a 403 after the host lost
  the game), and the same expiry refresh.
- `linkExpiry.ts` — the "within 10 minutes of the earliest `expires_at`" delay both hooks share,
  never less than 60 s. Each fetch schedules the next, so without that floor a client clock
  about 2 h fast computes 0 every time and refetches in a tight loop.
- `questionImages.ts` — reads image ids out of a question's `config` defensively (only a positive
  integer counts), option letters, "has a caption", and the image options' accessible names.
  T7: `canvasImageId` (a Spot It question's image), which `referencedImageIds` now includes, so
  `GameLayout` refreshes the link map for a canvas image it lacks.
- `spotGrid.ts` (T7) — Spot It's grid geometry in the backend's integer arithmetic (`maxGrid`,
  `defaultGrid`, `gridFits`), canvas layout and hit-testing, cell labels, the heat ramp's steps and
  legend, and the "Needs review" texts for the server's reason codes. The limits are a deliberate
  copy of the backend's `spot_it` module, the authority. The player app's `spotGrid.ts` repeats
  only the labels, readers and canvas layout (no limits).
- `sortResults.ts` (T7) — the one TypeScript port of `sort_it.results_breakdown`, used by both
  `ResultsPage` and `GameOverPage`: integer half-up percentages (never `Math.round`), the most
  common wrong box (ties to the lowest index), "—" with no answers.
- `canvasColors.ts` (T7, themed by T9 §4.12) — the Spot It canvas's colours, read from the theme's
  tokens. `canvasColors()` returns every colour the canvas draws with; call it **at draw time**,
  never at module load, because the theme can change after load. `token(name, alpha)` returns
  `rgb(<channels> / <alpha>)` and **throws on an unknown name**: an empty value would leave the
  canvas silently drawing in its previous colour. Colours over the image (grid lines, the viridis
  heat ramp at `HEAT_OPACITY`, counts, Correct outlines, the editor's accepted cells and cursor)
  are fixed tokens (`--canvas-ink`, `--canvas-paper`, `--heat-1` … `--heat-5`,
  `--canvas-accepted`, `--canvas-cursor`); the grid area and labels follow the theme. Only the
  alphas are numbers here.
- `format.ts` — byte counts and limits in KiB/MiB for the image UI.

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
- A non-JSON error body (a proxy's HTML 502, say) falls through to `HTTP <status>` — except a 413,
  which becomes "The file is larger than the server accepts." (nginx's own page for a body over a
  route's limit). This app's `api.ts` now differs from admin's in that one line.
- Responses are cast to `T` with no runtime validation, so a backend shape change surfaces as an
  undefined property at render time rather than at the call site.
- REST responses use **snake_case** (`room_code`, `game_title`, `access_token`) while socket
  payloads use **camelCase**. Both conventions are live in this app; match whichever transport
  you're on.
- `api.ts` here and admin's are the same apart from `postForm`'s method parameter and the 413
  message (T8, host only: admin has no uploads that large). A fix in one needs the same fix in
  the other.
