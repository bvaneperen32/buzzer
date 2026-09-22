# frontend/admin/src/lib

## Purpose

Shared non-React utilities for the admin app. The richest of the three apps' `lib` folders — this
is the only client that does full CRUD, file uploads, and file downloads.

## Contents

- `api.ts` — `apiFetch<T>` plus the `api` object. Covers all five verbs (`get`, `post`, `put`,
  `patch`, `delete`) and adds two things the host and player clients don't have:
  - **`postForm(path, formData)`** — multipart upload, used for game JSON import. It deliberately
    omits the `Content-Type` header so the browser supplies the multipart boundary.
  - **`download(path)`** — fetches a file, reads the filename out of the `Content-Disposition`
    header, and triggers a save via a temporary `<a download>`. Used for CSV exports, the HTML
    session report, and game JSON export.
- `utils.ts` — `cn(...)` (clsx + tailwind-merge). Byte-identical to the host app's.

## How it fits in

Unlike the other two apps, admin is REST-only — there is no socket here at all. Every page is a
thin CRUD wrapper over `/api/admin/*`, so `api.ts` is the single integration point for the whole
application.

`api.ts` and `utils.ts` are copies of the host app's files: `utils.ts` is identical, and `api.ts`
is the same core with the extra verbs and helpers bolted on.

## Gotchas

- **The error-message extraction doesn't match the backend's error shape**, and it's repeated in
  *three* places here (`apiFetch`, `downloadFetch`, and `postForm`). All three read `err.detail`,
  but `BuzzerError` responses are `{"error": code, "message": ...}`. This is worst in this app,
  because admin endpoints raise the most specific errors: `ConflictError("Cannot delete an admin
  account")`, `ConflictError("Username 'x' is already taken")`, and every
  `QuestionCreate.validate_structure` failure. The admin sees `HTTP 409` / `HTTP 404` / a
  stringified array instead. Fixing this one file would make the whole app meaningfully more
  usable.
- **`download` and `postForm` assume a JSON error body.** If a download fails with an HTML error
  page (a proxy 502, say), `res.json()` rejects, the `.catch(() => ({}))` swallows it, and the
  user gets a bare `HTTP 502`.
- **`download` doesn't revoke the object URL safely.** `URL.revokeObjectURL(url)` runs
  synchronously right after `a.click()`; it works in current browsers because the download is
  already queued, but it's timing-dependent rather than guaranteed.
- The filename regex only matches a quoted `filename="..."`. The backend always quotes, so this
  holds — but it would silently fall back to `"download"` for an RFC 5987 `filename*` form.
- No 401 handling and no refresh integration. An expired token turns every page into an error
  state with no redirect to `/login`; the guard in `App.tsx` only checks that a token exists.
- The `token` key is shared with the host and player apps, which sit on the same origin in
  production — a host or guest sign-in in the same browser replaces the admin's token. See
  [frontend/CLAUDE.md](../../../CLAUDE.md).
- Responses are cast to `T` with no runtime validation.
