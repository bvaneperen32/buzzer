# frontend/admin/src/lib

## Purpose

Shared non-React utilities for the admin app: the REST client, the host-app link helper, and
`cn`.

## Contents

- `api.ts` — `apiFetch<T>` plus the `api` object. Covers all five verbs (`get`, `post`, `put`,
  `patch`, `delete`) plus two helpers (the host app now has identical copies):
  - **`postForm(path, formData)`** — multipart upload. It deliberately omits the `Content-Type`
    header so the browser supplies the multipart boundary. No admin page uploads any more (game
    import moved to the host app).
  - **`download(path)`** — fetches a file, reads the filename out of the `Content-Disposition`
    header, and triggers a save via a temporary `<a download>`. No admin page downloads any more
    either (session exports and game export moved to the host app).

  All three throw sites (`apiFetch`, `downloadFetch`, `postForm`) go through one helper,
  `toApiError`, and throw an **`ApiError`** (extends `Error`, adds the numeric `status`). The
  message is the server's `message` if it's a string, else a string `detail`, else a 422 `detail`
  array's `msg`s joined with `"; "`, else `HTTP <status>`.
- `hostApp.ts` — `HOST_APP_URL` (`VITE_HOST_APP_URL` if set, else `/host` in a production build,
  else `http://localhost:5173`) and `hostAppUrl(path)`. Used by the sidebar's "Host tools", the
  non-admin notice, and `CoursesPage`'s roster links. No `.env` is needed; the defaults cover dev
  and production.
- `utils.ts` — `cn(...)` (clsx + tailwind-merge). Byte-identical to the host app's.

## How it fits in

Unlike the other two apps, admin is REST-only — there is no socket here at all. Every page is a
thin CRUD wrapper over `/api/admin/*` (plus `GET /api/auth/me` in `RequireAdmin`), so `api.ts` is
the single integration point for the whole application.

`api.ts` and `utils.ts` are copies of the host app's files: `utils.ts` is identical, and `api.ts`
matches the host's in behavior (same verbs, same error helper, same `ApiError`). They're still two
files; a fix in one needs the same fix in the other.

## Gotchas

- **Error bodies need a JSON parse.** If a request fails with an HTML error page (a proxy 502,
  say), the parse fails quietly and the user gets a bare `HTTP 502`.
- **In dev, `hostAppUrl` points at another origin** (`:5173`), which has its own `localStorage` —
  so following a Host-tools link means signing in again there. Under nginx (`:8080`) both apps
  share one origin and one token, and the link arrives signed in.
- **`download` doesn't revoke the object URL safely.** `URL.revokeObjectURL(url)` runs
  synchronously right after `a.click()`; it works in current browsers because the download is
  already queued, but it's timing-dependent rather than guaranteed.
- The filename regex only matches a quoted `filename="..."`. The backend always quotes, so this
  holds — but it would silently fall back to `"download"` for an RFC 5987 `filename*` form.
- Only `RequireAdmin` acts on a 401, and only for `/auth/me` on entry. A token that expires while
  a page is open turns that page into an error state (now with the server's real message), with
  no redirect to `/login`. No refresh integration.
- The `token` key is shared with the host and player apps, which sit on the same origin in
  production — a host or guest sign-in in the same browser replaces the admin's token. See
  [frontend/CLAUDE.md](../../../CLAUDE.md).
- Responses are cast to `T` with no runtime validation.
