# T8 — Image Support in Questions

**Destination:** `docs/plans/t8-image-support.md`
**Status:** **Implemented** (2026-09-29, on `feature/dexter-t8-image-support`).
- Every §4.13 step is done and the §4.11 checks pass, including the lock tests, the nginx-route
  tests and the opt-in load and burst checks. The §4.12 manual run is complete, including a
  physical-phone check that the user ran and reported as passed; the results are under "Manual run
  results" in §4.12.
- Reviews 1 (comprehension), 2 (criticism) and 3 (implementation readiness, run 2026-09-28 with
  source verification): their findings are addressed in this version.
- Final re-check (2026-09-28, a fresh session, against the spec, the context files and the cited
  source): no blockers. Its corrections are applied: lock tests wait for every request (§4.11),
  reads after post-response commits poll (§4.11), the image error mapping is scoped (§2.5), two
  interface contradictions are resolved (`live_sessions`, `export_inputs`, §4.6), and
  `create_room` checks the course after the lock (§4.8). Its decisions are in §3.12; its
  remaining notes are in §4.15, to verify during implementation.
- Implementation step 1 (2026-09-28): Pillow 12.3 behaviour found while building the pipeline is
  written into §2.5 step 5 (only a size failure of normalized output is a 413) and §4.6 (opening,
  modes, metadata, ICC, small copy). The §4.15 JPEG-comment note is resolved.
- Implementation steps 2–7 and the mean review (2026-09-29), each written into the section it
  changes: a non-list `option_images` in a bundle refused first (§2.15 step 5, adopted from
  §4.15); `replace_image` takes the new file's name (§4.6); question rows, and the export's image
  rows, sorted in Python rather than SQL (§4.6, with the pre-existing case in §5); the redis-py
  pin (§4.2); `Game.questions` relying on the database cascade (§4.3); the serving endpoint
  committing before it responds (§4.7); and the load generator's HTTP client (§4.11).
- Final implementation review (2026-09-29). The earlier "`FLOAT` rounding exception" claimed a
  stored question never exports longer than it was measured; that was false (`1e-40` exports as
  `9.99995e-41`, and MySQL's JSON columns change some numbers too). Question writes are now
  measured as stored (§2.6, §2.15 step 8, §4.8), with boundary tests (§4.11); the decision is in
  §3.13. Link refreshes are also never scheduled less than 60 s apart (§4.10).

Commit before any implementation (T3). If implementation reveals a flaw, revise this document first.
**Read first:** `backend/app/CLAUDE.md` (architecture, invariants, question-type checklist),
`backend/app/services/CLAUDE.md`, `backend/app/routers/CLAUDE.md`, `backend/app/websocket/CLAUDE.md`,
`frontend/CLAUDE.md`, and the `pages/` context files of the host and player apps.
**Related:** T7's Spot It question depends on this feature. T7 is designed separately and is not
part of this work; §2.16 records only what T8 must supply to it.

**Course references.** Task ids come from the course instructions:
- **T3** — design before code (this document and its Goldfish reviews)
- **T4** — moved authoring into the host app
- **T5** — integration tests
- **T6** — sample game files; allows a new bundle version
- **T7** — two new question types:
  - **Sort It** — sort cards into labelled boxes
  - **Spot It** — a canvas question over an image
- **T9** — light and dark themes

R5 and R8 are the rubric items for integration testing and image support.

---

## 1. Problem

Buzzer questions are text only. A question stores a sanitized text `prompt`, a JSON `config` whose
`options` is a list of strings, and a JSON `answer_data`. No table can hold binary data. A host
therefore can't ask "What is this building?" or "Which of these graphs shows a positive
correlation?", and T7's planned image-based canvas question has nothing to draw.

T8 adds images to questions. It must deliver:

- **Upload and storage.** Images are uploaded through the host interface, which admins have also used
  for authoring since T4, and are stored **entirely in MySQL**, not on disk or in a file store.
- **Prompt images.** A question of any type can show an image alongside its text prompt.
- **Image options.** Multiple-choice and multi-select options can be images, with or without a
  caption.
- **Canvas support.** T7's canvas question can use an uploaded image.
- **Management.** Authors can view, replace and delete images in the interface.
- **Portability.** Games that use images survive export and import.

The feature must hold up under realistic use (rubric R8):
- invalid or malicious uploads are refused with clear errors
- repeated use doesn't corrupt data or degrade images — the same image in several questions,
  repeated replacement, repeated export and import

The new capabilities need integration tests that check behavior, including error and access-control
cases (T5, R5).

### Constraints from the existing system

- **Access rules.** Every image access goes through the existing permission system: roles, course
  HOST grants, game grants, and the `game_service` assertions. A host must never reach another
  course's images. Players, including guests (real `GUEST` users admitted per session by
  `authorise_player`), receive only what the game shows them.
- **Browser loading.** Clients authenticate with a bearer token kept in `localStorage`. An `<img>`
  element or a canvas image load can't send that header, and the only cookie the server sets is the
  refresh cookie, restricted to `/api/auth`.
- **Two screens.** The projector (host app) shows the prompt; phones (player app) show only the answer
  controls. The server broadcasts each question to the whole room, so anything in that message
  reaches every phone.
- **Compatibility.** Version 1 game bundles, including every file in `sample_games/`, must keep
  importing unchanged. Games with images may use a new bundle version (T6 allows this).
- **Scale.** Up to 150 phones answer the same question at once, served by one backend process while
  the question timer runs.
- **Infrastructure.** nginx accepts request bodies of at most 1 MB today, and SQLAlchemy's default
  binary column becomes a 64 KB MySQL `BLOB`.

### Not in scope

- Images on Sort It cards, or as options for true/false and fill-in-the-blank questions.
- Image editing (cropping, rotation controls), deduplication, and an image interface in the admin
  app.
- Implementing T7. Its Spot It decisions appear here only as integration requirements.
- Fixing existing limitations found during design; they are listed in §5.

---

## 2. Technical Plan

### 2.1 Overview

Each game gets its own **image library**, stored in a new MySQL table. Authors manage it from the host
app's game editor, where admins also work. Questions refer to library images by id inside their
existing JSON `config`, so the `questions` table doesn't change. Browsers load images through
short-lived signed links. The server creates these only at points where access has already been
checked, and each screen receives links only for the images it displays. Games with images export as
version 2 bundles that carry the image bytes; version 1 is unchanged.

### 2.2 Terms

- **Library** — the images of one game (`images.game_id`). Access to a library is access to its game,
  through the game rule `game_service.assert_host_can_use_game`: ADMIN, or HOST on the game's course
  plus a game grant.
- **Projector** — the host app's game screens (`frontend/host/src/pages/game/`). **Phone** — the
  player app. **Editor** — the host app's game editor page (`/games/:gameId`, `QuestionEditorPage`).
- **Full image** — the stored image (`images.data`). **Small copy** — a derived, reduced image
  (`images.small_data`, §2.5) used on phones, for thumbnails and in the report.
- **Link** — a root-relative URL `/api/images/{id}/v{version}/{variant}?t={token}` (variant `full` or
  `small`), defined in §2.9. Clients never build links; they only use links the server sent.
- **Live session** — a `game_sessions` row with status `LOBBY` or `IN_PROGRESS` **and** a Redis
  `room:{code}` key that still exists. Without that key a game can't continue.
- **Letter / caption / description:**
  - **Letter** — an option's position (A, B, C…), always shown.
  - **Caption** — the option's text, `config.options[i]`; optional when the option has an image.
  - **Description** — the image's accessible text (alt text); required, stored with the image.
- **Label** — how an option is named on a screen: its letter, followed by its caption when the
  caption isn't empty, in that screen's existing format (§2.11).
- **Question number** — "Q{n}", a question's 1-based position in the game's current order
  (`order_index`), as the editor lists questions. It changes when questions are reordered;
  `question_id` is the stable reference.
- **Question list** — the list of the game's questions on the editor page, below the Images panel.
- **Units** — 1 KiB = 1,024 bytes; 1 MiB = 1,048,576 bytes; 1 MP = 1,000,000 pixels. nginx's `m`
  suffix also means MiB.

### 2.3 Data model

New table `images` (migration `005`), model `Image`:

| Column | Type | Notes |
|---|---|---|
| `id` | int, primary key | |
| `game_id` | int, FK `games.id` `ON DELETE CASCADE`, indexed | the owning game |
| `content_type` | varchar(32) | of `data`; detected from the bytes (§2.5) |
| `data` | `MEDIUMBLOB` | the full image; loaded only on demand |
| `small_data` | `MEDIUMBLOB` | the small copy (§2.5); loaded only on demand |
| `width`, `height` | int | of the full image, after orientation |
| `byte_size` | int | `len(data)`; counted by the library budget (§2.6) |
| `sha256` | char(64) | hash of `data` |
| `filename` | varchar(255) | original basename; shown in the editor and written into exported bundles (§2.15); never sent in socket payloads, embedded in reports, or used in image responses |
| `description` | varchar(300), NOT NULL | required, trimmed, non-empty |
| `version` | int, NOT NULL, default 1 | increases by 1 on every replace |
| `replaced_at` | datetime, NULL | time of the last replace, set by the database's `NOW()` — the same clock and whole-second precision as `game_sessions.created_at` (§2.14) |
| `created_at`, `updated_at` | datetime | |

- The blob columns are explicitly `MEDIUMBLOB` (up to 16 MiB). SQLAlchemy's default binary type
  becomes a 64 KB `BLOB`.
- The blobs are loaded lazily, so listing a library never reads image bytes.
- Deleting a game (admin only; it already erases the game's sessions) removes its images through the
  database cascade, without loading them.
- There is no uploader column: a foreign key to `users` would block `DELETE /api/admin/users/{id}`.
- `questions` is unchanged (§2.7).

### 2.4 Limits

This table is the only definition of each limit; other sections refer to them by name.

| Name | Value | Meaning |
|---|---|---|
| Input limit | 10 MiB (10,485,760 bytes) | the largest raw image accepted: an uploaded or replacement file, or one decoded bundle image |
| Pixel limit | 40 MP | the largest image accepted, checked from the header before decoding (§2.5) |
| Full-image edge | 1,600 px | the longest side of a stored full image |
| Full-image size | 4 MiB (4,194,304 bytes) | the largest stored full image |
| Small-copy edge | 400 px | the longest side of a small copy |
| Small-copy size | 1 MiB (1,048,576 bytes) | the largest stored small copy |
| Image count | 50 | images per library |
| Library budget | 24 MiB (25,165,824 bytes) | the sum of `byte_size` (full images only) per library; small copies are not counted |
| Export limit | 40 MiB (41,943,040 bytes) | the largest export a write may leave a game with (§2.6) |
| Import limit | 40 MiB (41,943,040 bytes) | the largest bundle file import accepts |
| nginx body limits | 11 MiB on the image upload and replace routes; 41 MiB on the two import routes; nginx's 1 MiB default everywhere else | each is just above its route's limit, leaving room for multipart overhead; the two import routes also set `proxy_read_timeout 300s`, above the import processing deadline (§4.12) |
| Processing slots | 2 | images processed at the same time, across all uploads, replaces and imports (§2.5) |
| Slot wait limit | 20 s | the longest a request waits for a processing slot before a 503 (§2.5) |
| Import processing deadline | 240 s | the deadline for an import's image-processing phase, checked after each image; it can be overrun by one image's processing (§2.5, §2.15) |

- **Where body size is really enforced.**
  - FastAPI reads the whole request body before any dependency or handler runs. A multipart body is
    parsed, and its file parts are spooled to temporary files; a JSON body is read into memory.
    `require_user` and the capped read therefore can't stop a large body from being received.
  - nginx's per-route limits are the actual gate.
- **The capped read** bounds only the handler's own copy. It reads at most limit + 1 bytes from the
  spooled file; anything longer → 413.
- **Direct access to port 8000** bypasses nginx and has no body limit. That covers the Vite dev
  proxy, the integration suite, and anything reaching the published backend port. This is an
  existing exposure, listed in §5.
- One image row holds at most 5 MiB of blobs (full-image size + small-copy size), which is below
  MySQL's default `max_allowed_packet` (64 MiB).
- A library's blobs total at most 74 MiB: the library budget plus 50 small copies.

### 2.5 Image processing and conformance

Every image entering the system — upload, replace, or import — goes through this pipeline. It is the
only code that accepts image bytes.

0. **Capacity.** Image processing takes one of the processing slots.
   - A request waits at most the slot wait limit for a slot; otherwise **503** "Image processing is
     busy — try again in a moment." (`ServiceUnavailableError`).
   - An upload or replace takes its slot **before** reading its file, so a waiting request holds only
     Starlette's spooled temp file, not the bytes in memory. The slot is released after processing,
     before the game lock is taken.
   - An import takes one slot per image, in turn, so its images interleave with other uploads. Its
     processing phase has the import processing deadline:
     - **Waiting** for each slot is limited to the remaining deadline (or the slot wait limit, if
       shorter).
     - **An active decode can't be interrupted.** The pipeline runs in a worker thread
       (`anyio.to_thread.run_sync`, whose default finishes the call even when the caller is
       cancelled). If the deadline passes mid-decode, that image finishes processing, and its slot is
       released only when it finishes.
     - **The deadline is checked after every image, including the last.** Once the current time is
       past it, the import stops with 503, discards every processed result, and creates nothing. No
       database write happens before that final check passes (§2.15 steps 7–8).
     - So the deadline can be overrun by up to one image's processing time. The checks and writes
       after it aren't bounded by it.
   - **What these limits don't do.**
     - The slot wait limit bounds only waiting for a slot. Processing time, database lock waits (up
       to MySQL's lock-wait timeout), and the writes and commit aren't bounded by it or by the import
       deadline.
     - A request can therefore still outlast nginx's timeout (60 s for uploads, 300 s on the import
       routes). The client then gets nginx's 504 while the backend may still finish and commit (§5).
     - The values are operational choices that make that uncommon, not a guarantee.
   - The slots **limit concurrent processing; they don't bound total memory.** An import holds its
     parsed JSON and decoded bytes outside the slots, and no container memory limit is set (§5).
1. **Size.** Over the input limit → **413**.
2. **Format.** Pillow identifies the format from the bytes themselves (`Image.open`, which reads only
   the header).
   - Accepted inputs: PNG, JPEG, WebP and GIF.
   - Anything else → **415**, including SVG, HEIC, TIFF, text, empty files, and MPO (the multi-picture
     JPEG some phones and cameras write; support deferred, §5).
   - SVG is refused deliberately: served from the apps' own origin, it can carry a script that reads
     the `localStorage` token.
3. **Pixels, then decode.**
   - **Pixel check.** Before decoding, width × height from the header must be within the pixel limit;
     otherwise **422**. This explicit check is the enforcement.
     - Pillow's decompression-bomb error, with `Image.MAX_IMAGE_PIXELS` set to the pixel limit, only
       fires at twice that, so it's just a backstop.
     - The pipeline doesn't use warning filters: `warnings.catch_warnings()` isn't thread-safe in
       Python 3.12, and uploads run in worker threads.
   - **Decode.** A complete decode of the first frame (`load()`) must then succeed. Truncation or
     corruption → **422**.
   - **Frames are never counted by decoding.** PNG and WebP report animation in their headers
     (`is_animated`). GIF never conforms (step 4), and normalization takes only its first frame, so
     a GIF with thousands of frames costs one frame's work.
   - **Pillow errors map to statuses.** Pillow isn't installed in the development environment used
     for design, so this follows Pillow's documentation, and the tests confirm it.
     - **Scope.** The mapping wraps only Pillow's image operations on the input bytes: opening,
       reading the header, decoding, transforming and encoding. It isn't a catch-all around the
       rest of `process_image` or the library operations.
     - **Order.** `UnidentifiedImageError` is a subclass of `OSError`, so it is handled first.
     - `UnidentifiedImageError` → 415.
     - `DecompressionBombError` → 422 with the pixel message. Pillow raises it from `Image.open`
       itself above twice `MAX_IMAGE_PIXELS`, before the explicit check can run.
     - `OSError`, `SyntaxError`, `ValueError` and `EOFError` from those operations → 422 "The
       image couldn't be read."
     - The app's own `BuzzerError`s (the 413, 415 and 422 raised by the pipeline's checks) pass
       through unchanged.
     - **Anything else stays a server error.** That includes application bugs, resource failures
       such as `MemoryError`, and an exception type Pillow raises that isn't listed above. It
       reaches the existing handler as a logged 500. If testing finds another exception type
       that Pillow raises for malformed input, it is added to this list deliberately.
     - **Guarantee.** A malformed or unsupported image gets a 4xx with the messages above; an
       unexpected failure is a logged server error, not a client error.
   - **The pixel message** states the image's megapixels and the limit, and suggests resizing, for
     example "This image is 48 MP; the limit is 40 MP. Resize it and upload again."
4. **Conformance.** The bytes *conform* only if all of the following are true:
   1. the format is PNG, JPEG or WebP
   2. it isn't animated (`is_animated` is false)
   3. width and height are at least 1 px, and the longest side is within the full-image edge
   4. there is no EXIF, XMP, IPTC/Photoshop, comment or PNG text metadata (an ICC colour profile is
      allowed)
   5. the length is within the full-image size

   **Conforming bytes are stored unchanged.**
5. **Normalization** (non-conforming input only):
   - apply the EXIF orientation, keep only the first frame, and drop all metadata except ICC
   - scale down to the full-image edge; never scale up
   - encode JPEG as JPEG (quality 85), PNG and GIF as PNG, WebP as WebP (quality 85); if the result
     exceeds the full-image size, re-encode as WebP (quality 85)
   - if the result still exceeds the full-image size → **413**
   - the result must conform (step 4). Normalization guarantees every other condition, so any other
     failure is a pipeline bug and a logged 500, not a client error (see "Guarantee" above).
6. **Small copy.**
   - Made from the stored full image, **always WebP** (quality 85, transparency kept), scaled down to
     the small-copy edge; never scaled up.
   - If it exceeds the small-copy size, re-encode at quality 50; if it still exceeds it → **413**.
7. **Derived fields.** `content_type`, width, height, `byte_size` and `sha256` always come from the
   stored bytes. Client- or bundle-supplied metadata is never used.

Normalized output conforms, so the pipeline is **idempotent**: stored bytes come back unchanged if
processed again. Export writes the stored bytes and import keeps conforming bytes (§2.15), so full
images survive any number of export/import cycles byte for byte.

Why normalize at all:
- phone photos carry location metadata
- browsers apply the orientation tag, so the unrotated width and height would be swapped
- unbounded photo sizes would be downloaded by every phone at the start of a timed question

### 2.6 Budgets and the export-size guarantee

**Budget checks.** Upload, replace and import run the same checks, in the same code:
- **Count** — at most the image count; a replace doesn't change the count.
- **Library** — the sum of `byte_size` is within the library budget. A replace counts the total minus
  the old image plus the new one; an import counts the whole new set.
- **Export** — the export rule below.
- **Order.** Checks that need no decoding run first. An import checks its image count before
  decoding any image (§2.15), so a bundle of thousands of tiny images is refused without processing
  them. The library budget is checked on processed sizes, because images can shrink under
  normalization.

A failed check → **409**, naming the limit and the value the change would have produced.

**Export size.**
- `export_size(G)` is the exact byte length of the bundle the exporter produces for game G, in the
  §2.15 format.
- It is computed without building any base64: serialize the bundle with the exporter's own
  serializer (`bundle_service`, §4.5) and every image's `data` left empty, then add
  Σ 4·⌈`byte_size`/3⌉.
- That is exact, because base64 characters need no JSON escaping.
- **Questions are measured as stored** (found in the final review). MySQL doesn't store every
  question value as it was sent, and the stored form, which is what export writes, can be longer
  or shorter:
  - `questions.points_value` is a single-precision `FLOAT`, read back to about 6 significant
    digits. `1234.5678` exports as `1234.57`, but a subnormal value gains digits: `1e-40` exports
    as `9.99995e-41`, 6 bytes longer.
  - The `config` and `answer_data` JSON columns store an integer outside 64 bits as a double
    (`18446744073709551616` exports as `1.8446744073709552e+19`, 2 bytes longer), and MySQL's JSON
    parser doesn't read every double exactly (`1.0011675e+32` comes back as
    `1.0011675000000002e+32`).
  - So a question write (create, update, import) is measured on the rows as stored: they are
    written, flushed and read back in the request's transaction, then measured. A refusal raises,
    and the transaction rolls back (§2.15 step 8, §4.8). This is exact whatever the database
    changes, including the prompt, which is stored sanitized (sanitizing can lengthen it: `&`
    becomes `&amp;`).
  - Game metadata and image fields are strings and integers, which MySQL stores exactly in strict
    mode (the compose default: `STRICT_TRANS_TABLES`), so those writes are measured before they're
    applied.
- Stored rows are always measured exactly.

**Export rule.** A write that can change a game's exported content is accepted only if, after the
write, either:
- `export_size` is within the export limit, or
- `export_size` did not increase.

Otherwise it is refused with **409** and nothing changes.

- Writes covered:
  - question create and update
  - game title, description and max-players update
  - image upload, replace, and description edit
  - import, measured on the game as stored
- Deletes and reorders can't increase the export size and aren't checked.
- The admin and host routers share these service functions, and future question types (T7) inherit
  the rule.

**Guarantee.**
- A game whose export is within the export limit stays within it after every accepted write.
- New games start far below the limit, and imports are checked.
- So every game created or imported after T8 exports to a bundle within the import limit, which
  means **its export re-imports**.
- This covers every write through `game_management_service`, which is where all game and question
  writes in the application already happen.

**Pre-existing games.** T8 doesn't scan or change games written before it. Such a game has no
images, so it could exceed the export limit only with more than 40 MiB of question content. If one
does:
- it stays playable and exportable
- writes that would grow it are refused, while edits that don't grow it, including shrinking edits,
  are allowed
- import refuses its bundle (413) until it is reduced
- the editor's export-size meter shows that it is over

**Concurrent writes and fresh reads.**

- **Game row lock.** Every covered write, every image delete, question reorder, `create_room` and
  `start_game` first call `lock_game`: `SELECT … FROM games WHERE id = :id FOR UPDATE`, with `populate_existing`.
  - Writes to one game therefore run one at a time; different games don't wait on each other.
  - The lock is held until the transaction ends, so what was checked can't change before the write
    commits.
  - Gameplay never locks the `games` row. Answers insert `session_scores`, whose foreign keys point
    at sessions, users and questions, so this lock doesn't slow a live game.
- **Why reads after the lock must be fresh.** There are two separate problems:
  - **The database snapshot.** MySQL's default isolation is REPEATABLE READ. A plain read reuses the
    snapshot taken at the transaction's first read — usually the access check, before the wait for
    the lock — so it misses writes committed during the wait. A locking read always sees the latest
    committed rows.
  - **The ORM identity map.** For a row the session has already loaded, SQLAlchemy returns the
    existing object without updating it, even from a locking read, unless the query sets
    `populate_existing`. The app's sessions use `expire_on_commit=False`, so nothing else refreshes
    them.
- **The fresh-read rule.** After `lock_game`, every read whose result feeds a check, a merge, a
  version increment or a start is a locking read **with `populate_existing`**. The complete list:

  | Read | Feeds | Lock |
  |---|---|---|
  | the question being updated (`_get_question`) | `update_question`'s merge | shared |
  | the game's questions and images (`export_inputs(locking=True)`) | the export rule; `used_by` in delete | shared |
  | the referenced images (`assert_in_library`) | the same-library check | shared |
  | the game's sessions (`live_sessions(..., locking=True)`) | the live guard | shared |
  | the image row being replaced or deleted | the version increment; the delete | exclusive |
  | the session being started | `start_game`'s status check | exclusive |
  | the game's questions in `start_game` | the session's question list | shared |
  | `get_image_meta(..., locking=True)` | T7's confirmation checks | shared |
  | the game's questions in `create_question` (the same read the export rule uses) | the next `order_index` | shared |
  | the game's questions in `reorder_questions` | the reorder's id-set check | shared |
  | the game row in `create_room` (the `Game` that `lock_game` refreshes) | the course-match check | exclusive (the lock itself) |

- **Question order stays unique.** `order_index` has no unique constraint, so these two fresh reads
  are what keep indexes and question numbers (§2.2) unique when creates and reorders overlap. A
  reorder whose id list no longer matches, because a create committed while it waited, gets the
  existing 409.

- **Lock strength.**
  - **Exclusive (`FOR UPDATE`):** only the game row, the image row being changed, and the session
    being started. The game row lock already serializes writers.
  - **Shared (`FOR SHARE`):** every other read in the table. A shared read still sees the latest
    committed rows. This matters for question rows: every answer's `session_scores` insert takes a
    shared lock on its question row for the foreign-key check. Shared reads don't conflict with it,
    whereas exclusive ones would make answers wait during an upload or edit in a live game.
  - A question row that is then updated gets its exclusive lock from the `UPDATE` itself.
  - **No locks:** `list_library` and export. They don't check or change anything, and a plain read
    is current for their own transaction. `list_library` reads live sessions with
    `live_sessions(..., locking=False)`, so a library request never holds locks on session rows.
- **Order, because `populate_existing` overwrites unsaved changes.** It replaces an object's
  attributes with the database values, including changes the code made but hasn't flushed.
  - So every function takes all its locking reads first, runs its checks on plain values, and only
    then assigns to ORM objects or adds rows.
  - `update_game` changes the `Game` returned by the access check. `lock_game` refreshes that same
    object before anything is assigned.
- **Lock errors.** MySQL's lock-wait timeout (1205) and deadlock (1213) both become 409 "This game
  is being changed by another request; try again", with nothing changed.
- **References.** The same lock serializes the same-library reference check (§2.7) with image
  delete, so a question can't gain a reference to an image that is being deleted.
- **Import.** Creates a new game, so it doesn't lock an existing one.
- **Sessions and image versions.** A session must never start with an image version that changes
  underneath it. Stale links can't be relied on to fail, because a cached `immutable` URL is never
  requested again, and phones retry the same link.
  - `create_room` and `start_game` take the game row lock right after their existing access checks,
    before they create the session or change its status. `create_room` runs its course-match check
    after the lock, on the refreshed `Game`, so a course move committed while it waited is seen.
  - Room creation, game start, image writes and question writes for one game therefore run one at
    a time.
  - The live guard (§2.13) reads the game's sessions fresh, under that lock. `create_room` writes the
    room's Redis state **before** its transaction commits, and that order must be kept.
  - A replace or delete therefore either:
    - commits before the room is created, and the room uses the new version from the start; or
    - waits for the room's creation to commit, sees a live session, and is refused (409).
  - From the moment a room exists until its session stops being live, **no image of the game changes
    version**. Every link the session receives stays correct for its whole life, so `immutable`
    caching and the phones' same-link retry are safe.
  - The room-create endpoint (`POST /api/game/rooms`) commits before responding. The host only
    receives a room code once the session is visible to the live guard, and the lock is released
    promptly.
  - **Starting a game.** `start_game` re-reads the session, exclusively, under the lock.
    - If a concurrent start has already moved it to `IN_PROGRESS`, `start_game` returns "already
      started" and the gateway ignores that advance: no second start and no `error` event.
    - Any other non-`LOBBY` state still raises the existing 409.
    - Its question list is read fresh, so questions committed while it waited are included.
  - Uploads are processed (§2.5) **before** the lock is taken, so image decoding never holds it.
  - **T7:** its confirmation checks go right after this lock in `create_room` (after its
    course-match check) and in `start_game`, and read images with
    `get_image_meta(..., locking=True)`. They then see the image versions and grids the session
    will use, and no image or question write can interleave.
  - Question edits during a live session remain unblocked (an existing limitation, §5); T7 decides
    whether Spot It needs more than its start-time checks.

**A refused change leaves existing data intact.**
- Locking reads happen before any assignment (see "Order" above), so a check never runs on
  half-applied state.
- Every check runs before the change is applied to any row, except the export rule on question
  writes, which measures the rows as stored (see "Export size" above). Those rows are written
  only inside the request's transaction, which the refusal rolls back before anything commits.
- A refusal raises an application error, and the request's transaction rolls back (`get_db` rolls
  back on any exception). Every router that commits explicitly does so only after the service
  function has returned.
- A replace prepares the new bytes and small copy in memory, and updates the row only after every
  check has passed.
- An import validates the whole bundle before creating any row, then measures the game it created;
  all of it is one transaction, so a refusal at any step creates nothing.
- The image endpoints commit before responding, so a commit failure is reported as an error rather
  than after a success response.

### 2.7 How questions reference images

References are ids stored in `config`:

| Key | Allowed on | Shape |
|---|---|---|
| `config.prompt_image_id` | **every** question type | int, or absent/null |
| `config.option_images` | `multiple_choice` and `multi_select` only | a list the same length as `config.options`; each entry an int or null |
| `config.image_id` | canvas question types only — none exist until T7; **422** when present and not null on the four current types (a legacy `null` is tolerated) | int |

- **Options.**
  - **When a question uses image options** — `config.option_images` is present and not null — the
    stricter rules apply:
    - each option is a string
    - an option's caption may be empty only if it has an image
    - `option_images` has one entry per option
  - **When `option_images` is absent or null,** options are validated exactly as today, with no type
    or blankness check. Text-only questions therefore behave as before, including version 1 bundles
    with blank or non-string options, and blank rows saved from the editor.
  - The rule is identical on create, merged update and import, because all three run
    `validate_structure` on the whole question.
  - Captions stay in `config.options`, so every existing reader of `options` keeps working.
- **Other types.** `option_images` on any other question type → 422.
- **Same game only.** Every referenced id must be in the **same game's library**, otherwise 422
  "Image N isn't in this game's library". The message is identical for a missing id and another
  game's id, so a host can't probe other courses' ids. Without this check, a host could reference
  another course's image and have it served.
- **Every lookup is scoped to the question's game.** Any code that follows a reference loads images
  with `Image.game_id` equal to the question's game (the session's game, for the report). An id that
  belongs to another game is therefore unresolved, exactly like a missing one.
- **Stored references that don't resolve.** A question saved before T8 may already hold one of these
  keys (`config` accepts extra keys today), with a value that isn't an image of this game.
  - The phone's `images` map, `used_by`, the projector and the report treat it as **no image**.
  - `image_refs`, `map_image_refs` and `bundle_size` never raise on such values, so one legacy
    question can't break export, the meters, or writes to its game.
  - **Export repairs it so the bundle re-imports:**
    - an unresolved `prompt_image_id` or `image_id` is omitted from the exported `config`
    - unresolved `option_images` entries become `null`; if every entry is then `null`, the key is
      omitted, and the question is validated as text-only, where blank captions are allowed
    - if other entries still hold images, a blank caption whose image was dropped is exported as
      "Missing image", so every option keeps a caption or an image
  - These repairs make every export of such legacy data pass the §2.15 checks. Stored questions that
    fail today's rules for other reasons remain outside the guarantee, as in §2.6 "Pre-existing
    games".
  - Writing that question again still goes through validation, so its next edit is refused until the
    reference is fixed. The editor's picker shows "Missing image" for it, so the author can choose
    another image or none.
- **Where the checks run.**
  - Shape rules are in `QuestionCreate.validate_structure`.
  - The library check (`image_service.assert_in_library`) is called by `game_management_service`,
    under the game row lock (§2.6).
  - Both run on create, on update (against the merged question) and on import, which are all three
    question write paths.
- **What reaches phones.** `config` goes to phones unchanged (§2.17). Image ids aren't sensitive.
  Filenames never appear in `config` or in any socket payload.
- **One definition of the reference fields.**
  - The keys in this table, and their roles (`prompt`, `option`, `canvas`), are defined once, in
    `services/image_refs.py` (§4.5).
  - T8 implements all three roles, so the library check, `used_by`, the delete refusal and both
    bundle directions already handle canvas references.
  - `validate_structure` refuses `image_id` on every current type, so no question can hold one
    until T7 permits it on its canvas type (§2.16).
- **Version 1 bundles** can't carry images, so a non-null image reference in a version 1 import is
  refused (§2.15).

### 2.8 Management API and authoring

These endpoints are on the host router. Each requires a signed-in account that isn't a guest
(`require_user`) and passes the game rule, so admins use them too. An image id from another game
→ 404.

| Endpoint | Success | Purpose | Errors |
|---|---|---|---|
| `GET /api/host/games/{game_id}/images` | 200 `LibraryResponse` | the library, live sessions and meters | 401, 403, 404 |
| `POST /api/host/games/{game_id}/images` | 201 `ImageItem` | upload (multipart `file` + `description`) | 401, 403, 404, 413, 415, 422, 409 (budget or export), 503 (busy) |
| `PUT /api/host/games/{game_id}/images/{image_id}` | 200 `ImageItem` | replace the bytes (multipart `file`) | 401, 403, 404, 413, 415, 422, 409 (budget, export or live), 503 (busy) |
| `PATCH /api/host/games/{game_id}/images/{image_id}` | 200 `ImageItem` | edit the description (JSON `{"description": …}`) | 401, 403, 404, 422, 409 (export) |
| `DELETE /api/host/games/{game_id}/images/{image_id}` | 204, no body | delete | 401, 403, 404, 409 (referenced or live) |

Schemas are defined in §2.17. The error statuses come from the existing guards:
- **401** — no token, or an invalid one.
- **403** — a guest (`require_user`), or a user who fails the game rule. Non-admins get the same
  403 whether or not the game exists.
- **404** — an image that isn't in this game, or, for admins, a missing game.
- **503** — no processing slot within the slot wait limit, or an import past its deadline (§2.5).
  The editor shows the server's message.

Error bodies use the existing `{"error", "message"}` shape.

In the editor:
- **Images panel.**
  - Each image shows its thumbnail, description, dimensions and size, and where it's used ("Used by
    Q2 (prompt), Q5 (option B)", using question numbers, §2.2; or "Unused").
  - Actions: Upload, Replace, Edit description, Delete.
  - Meters for the image count, library budget and export size.
  - A banner when a live session blocks Replace and Delete (§2.13).
- **Question form.**
  - An image picker for the prompt (every type) and for each multiple-choice and multi-select
    option.
  - The author picks from the library or uploads a new image, which goes into the library and is
    then selected.
  - If the question is cancelled, the upload stays in the library, marked "Unused".
- **Admin app.** No image interface. Admins use the host app; T8 allows "the admin or host
  interface".
- **Size checks before sending.**
  - The picker and panel refuse a file over the input limit before uploading it.
  - The Games page's import refuses a bundle over the import limit.
  - Each shows a message naming the limit. This avoids nginx's plain HTML 413 page; if one arrives
    anyway, `lib/api.ts` shows "The file is larger than the server accepts."

### 2.9 Signed links and caching

- **Token.** A JWT signed with the existing RS256 keys:
  - claims `{token_type: "image", img, v, var: "full" | "small", exp}`, with no user subject
  - existing HTTP (`get_current_user`) and socket (`authenticate_socket`) authentication accept only
    `access` and `temp` tokens, so an image token can't authenticate anything else, and an access
    token can't open an image
- **Expiry.**
  - `exp` is the start of the current UTC hour plus 3 hours, so each link is valid for between 2 and
    3 hours after it is created.
  - Within one hour, the same image, version and variant produce an identical token and URL, so
    browsers can reuse cached bytes across the question, results and game-over screens.
- **Serving.** `GET /api/images/{id}/v{version}/{variant}?t=…` needs no bearer token.
  - Checks: signature, type and expiry; the path's id, version and variant must match the token;
    the version must equal the image's current version.
  - Errors: invalid or expired token → 403; missing image or superseded version → 404. Success is
    200 with the image bytes. The endpoint takes no bearer token, so it never returns 401.
  - Headers:
    - the stored `content_type` (`image/webp` for small copies)
    - `X-Content-Type-Options: nosniff`
    - `Content-Disposition: inline`, with no filename
    - `Cache-Control: private, max-age=<seconds until expiry>, immutable`
- **After a replace.**
  - The version increases, so every link created afterwards is a new URL. Browsers fetch the new
    bytes, and old links return 404.
  - Clients get current links only from server responses that read the current row:
    - the editor's list and mutation responses (a replace returns the new links)
    - the host link map
    - `new_question`
  - A cached old image can survive only where an old link is still on screen:
    - Never in a live game: room creation is serialized with replacement, and replacement is
      refused while a game is live (§2.6, §2.13). No link a session receives is ever superseded
      during that session, which is what makes `immutable` safe.
    - In another open editor tab, until that tab reloads the list. This matches the app's existing
      "save, then reload" staleness.
- **Issuing new links versus links already issued.**
  - **Issuing.**
    - **REST.** The library list and the session link map check access on every request, so someone
      who loses access — a revoked HOST or game grant, or a game moved to another course — gets no
      new links from REST from that moment.
    - **Sockets.** `new_question` goes to every socket currently in the room. Membership is checked
      only when a socket joins, not on each question; this is existing gateway behavior that T8
      doesn't change. A socket admitted before a revocation keeps receiving `new_question`'s small
      option links until it disconnects. What happens at the next join depends on what was revoked:
      - **The host loses HOST on the session's course:** `user_can_host_session` fails, so the next
        join is refused.
      - **The host loses only the game grant, or the game moves to another course while they keep
        HOST on the session's course:** `user_can_host_session` checks HOST on the session's course,
        not the game grant, so the host can still rejoin, drive the game and receive `new_question`.
        The session link map (REST) is refused, so the projector's prompt images and full-size option
        images show the unavailable placeholder (§2.12).
      - **A player** is re-checked by `authorise_player` at each join (roster, PLAYER grant, or guest
        admission). Losing that admission refuses the next join.
    - §5 records these as existing limitations.
  - **Already issued.** Links keep working until their `exp`, at most 3 hours after issue. They are
    bearer links with no revocation list; only a replace (new version) or a delete ends them early.
  - **Accepted exposure.** Anyone holding a link can use it until it expires, and links can appear in
    nginx access logs. This bounded window is accepted.

### 2.10 Delivering links to each screen

| Screen | Receives | How | Reconnect and refresh |
|---|---|---|---|
| Phone | small links for option images only | an `images` map `{id: {url, width, height, description}}` added to `new_question` | a reconnecting player who hasn't answered already receives a late-join `new_question`, built the same way, so it carries fresh links; answered players need none, since their results are text; a failed load is retried once with the same link (§2.12), which is sound because the link stays valid for at least 2 hours after it is sent (longer than any question's 300 s maximum) and no image changes version during a live session (§2.6) |
| Projector | full and small links for every image in the game | new `GET /api/game/sessions/{session_id}/images`, allowed only when the caller passes **both** `user_can_host_session` for the session **and** the current game rule for the session's game | fetched on mount (covers a page refresh), after a socket reconnect, when a question uses an image id missing from the map, on an image's first error (§2.12), and within 10 minutes of `expires_at`; requests made at the same time share one fetch |
| Editor | full and small links for the library | the list endpoint and mutation responses | reload the list after an error or near expiry |

- **Who gets the projector's links.**
  - `user_can_host_session` alone isn't enough: it checks only HOST on the course the session was
    played in. The game rule adds the caller's current access to the game itself.
  - **Revoked grant:** a host whose game grant is revoked gets 403 at their next fetch.
  - **Moved game:** moving a game to another course revokes the game grants of hosts who don't HOST
    the new course (existing behavior), so they get 403 too. A host who keeps access can already see
    the same images in the editor.
  - **Expired room:** the endpoint doesn't depend on the Redis room. Anyone who passes both checks
    could fetch the same images from the library endpoint, so requiring the room would protect
    nothing; it would only break a projector refreshed late on the game-over screen.
  - Admins pass both checks.
- Phones never receive links for prompt images or canvas images.
- A phone treats an option whose id is missing from `images` as having no image: it shows the letter
  and caption.
- `config` itself, including `prompt_image_id` and `option_images`, already reaches both apps
  unchanged; `images` is the only new socket field (§2.17).
- Why the projector uses REST:
  - one path covers live play, reconnects and refreshes
  - the projector already calls `GET /api/game/rooms/{code}`, which returns `session_id`
  - the socket protocol stays unchanged

### 2.11 What each screen shows

| | Projector | Phone |
|---|---|---|
| Prompt image (any type) | below the prompt text, as large as fits (about 55% of the viewport height); stays visible when the question locks | never shown |
| Image options (multiple choice, multi-select) | a lettered grid of full images with captions, during the question | 2-column tiles: letter badge, small image, caption if any. Multiple choice still submits on one tap; multi-select still toggles, then Submit |
| Results | prompt thumbnail; option bars labelled letter + thumbnail + caption | letter + caption (letter alone if there's no caption); no images |
| Game over | as for results, per question | letter + caption |

- **Labels.** Every screen already labels multiple-choice and multi-select options with letters, in
  its own format:

  | Screen | Existing format |
  |---|---|
  | Host results | `A  Paris` |
  | Host game over | `A.` then the text |
  | Phone | `A — Paris` |
  | Report | `A Paris` |

  - T8 keeps each format.
  - The one change applies to every multiple-choice and multi-select question: when a caption is
    empty, the label is the letter alone, with no separator.
  - Text-only questions therefore look exactly as they do today.
- **Question list.** The editor's question list (§2.2) shows a prompt thumbnail, and each
  multiple-choice and multi-select option as letter, thumbnail (if any) and caption (if any).
- **Join panel.** The projector's fixed QR/room-code panel must not cover images.
- **Prompt text.** It still hides when the question locks, as it does today.

### 2.12 Failed loads and accessibility

- **Loading and failure — the one sequence.** Every displayed image first shows a loading
  placeholder. On an image's first error:
  - **Projector and editor** (they have a link source):
    - First request current links: the session link map or the library list. Simultaneous requests
      share one fetch.
    - Then retry once: with the new URL if the link changed, otherwise with the same URL by
      remounting the image element.
    - Links are identical within an hour, and error responses carry no caching headers, so a remount
      sends a new request instead of reusing the failure.
  - **Phones** (links arrive only in `new_question`): retry the same URL once, by remounting.
  - **If the retry fails:** a visible "Image unavailable" placeholder with the image's description.
    The projector and editor also show a Retry button, which repeats this sequence. The browser's
    broken-image icon never appears.
  - **Why one retry is enough:** no image changes version during a live session (§2.6). A failure is
    therefore either transient or an expired link, and a refresh after expiry produces a new URL.
- **Failures never block play.** An option whose image failed stays selectable by its letter and
  caption. A failed prompt image never blocks the game.
- **Letter, caption and description are separate.**
  - The letter is always visible.
  - The caption is optional visible text.
  - The description is the alt text and accessible name.
- **Accessible names:**
  - an option with a caption: "Option B: {caption}. Image: {description}"
  - an image-only option: "Option B. Image: {description}"
  - a prompt image: its description
  - a text-only option: unchanged
- **Where descriptions appear.** Descriptions are visible only in the editor and in the failure
  placeholder. The editor asks authors to describe what the image shows, not which answer is
  correct.
- **Limitation.** Alt text makes prompt and option images usable with a screen reader. It does
  **not** make a visual-search canvas question (T7's Spot It) accessible, and T7 must say so.

### 2.13 Replace, delete, and live games

- **Replace.**
  - Runs the pipeline (§2.5) and the budget checks (§2.6). The id stays the same, the version
    increases by 1, and `replaced_at` is set to now.
  - Every question using the image shows the new one on its next load.
  - The image row is read fresh and exclusively under the lock (§2.6), so two replaces of the same
    image produce versions n+1 and n+2, never n+1 twice.
  - `filename` becomes the new file's cleaned basename; `description` is kept (it describes what the
    image shows and can be edited separately).
  - Any aspect ratio is accepted. The consequences for T7's canvas questions are T7's (§2.16).
- **Delete.** Refused (409) while any question uses the image as its prompt image, an option image
  or a canvas image; the message lists the question numbers (§2.2) and roles, for example "Used by
  Q2 (prompt), Q5 (option B)". Otherwise the row is removed.
- **Live guard.** Replace and delete are refused (409) while a live session of the game exists.
  - The check runs under the game row lock and is coordinated with room creation (§2.6).
  - The Redis condition in the definition of a live session stops a long-abandoned lobby, whose room
    key has expired, from blocking the library forever.
  - The 409 names each blocking session (room code, status, host name, creation time) and explains
    the fix:
    - the session's host can rejoin and finish it, or delete it from Play (two-step Delete, which
      discards its partial scores)
    - anyone else asks that host or an admin; admins can delete sessions from Sessions
  - The list endpoint returns the same live sessions, so the panel shows a banner and disables
    Replace and Delete before the author tries. The 409 remains the backstop.
  - Uploads and description edits stay allowed while a session is live.

### 2.14 Completed sessions and the report

- After a session finishes, the host can download an HTML report and CSV score exports. The CSVs
  contain no images.
- **Report contents.**
  - The report (`report_service`) embeds **small copies** as data URIs, with `alt` set to the
    description: prompt images on any type and option images.
  - Option labels follow §2.11.
  - Multiple-choice options appear in the existing bars, with a thumbnail beside each option that
    has an image.
  - Multi-select questions have no chart in the report today (§5). T8 adds a simple labelled option
    list for them: each option's label (§2.11), with its thumbnail if it has an image and its
    caption if it isn't empty. It shows no counts and no correctness, and it adds no new
    answer-distribution chart.
  - Each embedded image is within the small-copy size.
  - **Access.** Images are embedded only if the downloader currently passes the game rule for the
    session's game. Admins always do.
    - Otherwise each image appears as the text "[image withheld — you no longer have access to this
      game]".
    - Why: downloading a report needs only the session check (HOST on the course the session ran
      in), and the report shows the game's current content. Without this rule, a host who lost
      access could see images added later, possibly in another course.
    - The report's text still comes from current content for anyone who can download it. That is
      existing behavior (§5).
- **After a replace.** The report is built from the current questions and images when it's
  downloaded; it isn't stored per session.
  - It shows the new image; the old bytes no longer exist.
  - Each image replaced after that session was created (`replaced_at` later than the session's
    `created_at`) carries the note "Replaced after this session — players saw an earlier version."
    `replaced_at` and `created_at` both come from the database's `NOW()`, so a replace committed
    just before the room was created never produces the note.
  - If an image was removed from a question and then deleted, that can't be detected: the report
    simply shows no image, as it already does for edited prompt text.
  - A faithful historical report would need per-session question snapshots, which don't exist (out
    of scope).

### 2.15 Export and import

**Bundle format.** This is the only definition of it.
- A game with an empty library exports exactly as today, as version 1.
- Otherwise it exports version 2, which includes the whole library, unused images too.
- Each entry in `images` has **exactly four fields**: `key`, `filename` (the original basename, so
  the name survives import), `description` and `data` (base64 of the stored full-image bytes).
- There is no `content_type`, no dimensions and no hash; import derives all of them (§2.5).
- Questions refer to images by key.
- Keys follow the images' order by id (`img-1`, `img-2`, …), and serialization matches the current
  exporter (UTF-8, `ensure_ascii=False`, indent 2), so exporting the same game twice gives
  identical bytes.

```json
{
  "format": "buzzer/game",
  "version": 2,
  "game": { "title": "…", "description": "…", "max_players": 150 },
  "images": [
    { "key": "img-1", "filename": "tower.jpg", "description": "Stone tower on a hill", "data": "<base64 of the stored bytes>" }
  ],
  "questions": [
    {
      "type": "multiple_choice",
      "grading_type": "ACCURACY",
      "prompt": "Which city is this?",
      "config": { "prompt_image_id": "img-1", "options": ["Paris", "Rome"], "option_images": [null, null] },
      "answer_data": { "answer_points": [1000, 0] },
      "time_limit_seconds": 30,
      "points_value": 1000
    }
  ]
}
```

**Import** accepts version 1 unchanged and version 2; any other version → 422. A file over the
import limit → 413.

Import runs these steps in this order; this is the only definition of the order. Everything up to
step 7 happens before any row is created.
1. **Parse and version.**
   - Parse the JSON; `format` must be `buzzer/game`; `version` must be 1 or 2, otherwise 422.
   - A version 1 bundle with an `images` key → 422 "Version 1 bundles can't contain images".
2. **Game metadata:** `GameCreate`, unchanged.
3. **Image count, before touching any entry** (version 2).
   - `images` must be a list (otherwise 422), with at most the image count (otherwise 409).
   - `[]` is accepted; the imported game then has an empty library.
4. **Each image entry, without decoding the image** (version 2).
   - Each has exactly the four fields; unknown fields → 422.
   - `key` matches `^[A-Za-z0-9_-]{1,64}$` and is unique.
   - `data` is strict base64 decoding to within the input limit.
   - `description` passes the upload rules. `filename` becomes a display-only basename (control
     characters removed, at most 255 characters).
5. **References and questions.**
   - **Shape first** (adopted from §4.15). A non-null `option_images` that isn't a list → 422
     "Question 3: config.option_images must be a list". Without this, the key mapping below turns
     it into `null` and the malformed value is silently dropped instead of refused.
   - **Version 1:** every image reference (`prompt_image_id`, `option_images` entries, `image_id`)
     must be absent or null. Otherwise 422 "Question 3: version 1 bundles can't reference images".
   - **Version 2:** every non-null reference must be a key string from `images`. An integer or
     unknown key → 422 "Question 3: unknown image key …". Keys become provisional integers (the
     image's position + 1).
   - Every question then validates with `QuestionCreate`, so `image_id` is still refused on the
     current types (§2.7).
6. **Pipeline.** Each image passes §2.5, one processing slot at a time. Errors are prefixed
   "Image img-3: …".
   - The import processing deadline is checked after every image, including the last. Past it → 503;
     every processed result is discarded, and nothing is created.
   - An image already being decoded when the deadline passes finishes first (§2.5), so the phase can
     overrun by up to one image's processing time.
   - Steps 7 and 8 start only after the final check passes.
7. **The library budget** (§2.6), on the processed sizes, which are exactly what will be stored.
8. **Create** the game, its images and its questions in one transaction, with provisional integers
   mapped to the new ids, then apply the export rule (§2.6) to the game as stored, for both
   versions. Any failure, including that refusal, rolls the transaction back and creates nothing.
   - A bundle refused here has had its rows written, up to the library budget of image bytes,
     before the rollback. That is rare, and it comes after the image processing that dominates an
     import's time. A check on the values as sent can't safely refuse earlier, because storage
     can shorten them as well as lengthen them.

**Round trips.** Full images that conform are stored unchanged, so exporting, importing and exporting
again reproduces the same image bytes and, for the same game content, the same bundle. Small copies
are regenerated on import and aren't part of the bundle.

### 2.16 T7 integration contract

T7 (two new question types, including the canvas type Spot It) is designed separately. This section
records only its interface with T8.

**T8 supplies:**
- `images.version` and `replaced_at`
- `image_service.get_image_meta(db, game_id, image_id, *, locking=False)`, returning id, version,
  width, height and description for an image in the same game, for T7's create, update and import
  checks. T7's confirmation checks after the game row lock must pass `locking=True` (§2.6).
- canvas references: `config.image_id` is implemented in `image_refs` (§4.5) and handled by the
  library check, `used_by`, the delete refusal and both bundle directions; `validate_structure`
  refuses it on the four current types
- full-size links only in the host's session link map, never in phone payloads
- `image_service.data_uri(image, variant)` for the report
- replacement at any aspect ratio; the live guard; the export rule for T7's question writes
- the game row lock in `create_room` and `start_game`, implemented and tested in T8 (§4.11), and
  taken before any T7 confirmation check, so those checks see the image versions and grids the
  session will use (§2.6)
- `start_game`'s fresh session and question reads, and the gateway ignoring a concurrent second
  Start (§2.6)

**T7 owns** (recorded here as requirements; not built in T8):
- **Canvas references.** Allowing, and requiring, `config.image_id` on its canvas type in
  `validate_structure`.
- **Whole-cell Spot It data and scoring:**
  - `config.grid = {cols, rows}`
  - `answer_data.accepted_cells`, selected directly by the author; at least one for ACCURACY
  - ACCURACY scoring is all or nothing on an accepted cell; COMPLETENESS is unchanged
  - answers are `{col, row}` integers (not booleans), with one answer-count key per cell
- **Phone screens.**
  - A canvas that shows only the grid, with the same labels and proportions as the projector.
  - Text-only results: the selected cell, whether it was correct, the correct cells, the points,
    and an explicit "No answer".
- **Projector.**
  - The image and grid drawn on a canvas.
  - The heatmap, counts, legend, and the "Correct" outline and label appear only on entering
    results, whether through Show results or host-enabled auto-advance.
- **Reconfirmation.**
  - Each Spot It question records the image version and grid its accepted cells were confirmed
    against.
  - A grid change or a new image version invalidates that confirmation, and the editor shows
    "Needs review".
  - Reconfirming refreshes `config.width` and `config.height` from `get_image_meta`.
  - `create_room` and `start_game` refuse (409, naming the questions) while any question is
    unconfirmed. The check runs under the game row lock that T8 adds, right after it (in
    `create_room`, after the course-match check) (§2.6).
  - Tests of these confirmation checks.
- **Accessibility.** A statement that Spot It is visual-only (§2.12).

### 2.17 Data contracts

This is the only definition of the new payload shapes. REST fields are snake_case; socket fields
built by the server are camelCase; `config` passes through with its stored snake_case keys.

**Timestamps** are ISO 8601 UTC with a `Z` suffix and whole seconds, for example
`2026-09-28T18:00:00Z`. `expires_at` equals the link token's `exp`. Database timestamps (`created_at`,
`replaced_at`) are written by MySQL's `NOW()` and formatted with a `Z`. That assumes MySQL's time
zone is UTC — the compose MySQL container's default, which T8 doesn't change.

**Dimensions.** Every `width`/`height` is the **full image's** size in pixels. The small copy has the
same aspect ratio, so clients use these only to reserve layout space.

**REST schemas** (`schemas/image.py`):

| Schema | Fields |
|---|---|
| `ImageItem` | `id`, `filename`, `description`, `content_type`, `width`, `height`, `byte_size`, `version`, `replaced_at` (timestamp or null), `created_at`, `url`, `small_url`, `expires_at`, `used_by` (list of `UsedBy`) |
| `UsedBy` | `question_id`, `number` (question number, §2.2), `role` (`"prompt"`, `"option"` or `"canvas"`), `option_letter` (a letter for `option`, else null) |
| `LiveSessionItem` | `session_id`, `room_code`, `status` (`"LOBBY"` or `"IN_PROGRESS"`), `host_name` (null if unknown), `created_at` |
| `LibraryMeters` | `image_count`, `image_limit`, `library_bytes`, `library_budget`, `export_bytes`, `export_limit` |
| `LibraryResponse` | `images` (`ImageItem` list, ordered by id), `live_sessions`, `meters` |
| `LinkEntry` | `url`, `small_url`, `width`, `height`, `description`, `expires_at` |
| `SessionImages` | `images`: an object keyed by image id as a string, each value a `LinkEntry` |
| `DescriptionPatch` | `description` only (`extra="forbid"`) |
| `BundleImage` | `key`, `filename`, `description`, `data` only (`extra="forbid"`, §2.15) |

**Socket payloads:**

| Event | T8 change |
|---|---|
| `new_question`, and the host's `sync_state` question (both built by `_question_payload`) | Existing fields unchanged; `config` unchanged, so `config.prompt_image_id` and `config.option_images` appear exactly as stored. **New:** `images`, `{"<image id>": {"url": <small link>, "width", "height", "description"}}`, for option images only; `{}` when there are none |
| `question_results` | Unchanged. It carries no `config`. Both apps' results screens use the current question already received in `new_question`, with its `config` and `images` |
| `game_over` | Unchanged. Its question items already carry `config` (`game_service.get_player_question_summary`, `get_host_question_summary`). The projector resolves ids through the session link map; phones show labels only |
| The player's `sync_state` | Unchanged. Its `currentQuestion` carries no content; a player who hasn't answered receives the late-join `new_question` |

---

## 3. Alternatives

Spot It's own alternatives (exact positions, target rectangles, the image on phones, partial credit)
belong in T7's spec and aren't repeated here.

### 3.1 Storage and ownership

| Alternative | Why it lost |
|---|---|
| Files on disk or an object store | T8 requires images to be stored in the database |
| Per-course library | A course HOST without a grant for a game would see that game's images; moving a game to another course would break its references; export would have to gather images from outside the game |
| Per-question images, no library | The same picture used twice must be uploaded twice; there is no single place to view or replace images |
| Base64 text inside question JSON | About a third larger, and loaded with every question read |
| A separate table for image bytes | Deferred blob columns give the same "list without loading bytes" benefit with one table |

### 3.2 How questions reference images

| Alternative | Why it lost |
|---|---|
| A `questions.prompt_image_id` foreign-key column | Gives database integrity for prompts only; option references stay in JSON anyway, so two mechanisms and a `questions` migration for partial benefit |
| Options as objects `{text, image_id}` | Breaks every reader of `options`: the phone, host and report renderers, `simulate_players.py`, and version 1 bundles |
| `<img>` tags inside the prompt HTML | Needs the sanitizer to allow `img` and `src`; the apps render prompts as escaped text; and the report renders prompts as raw HTML, so it widens the stored-XSS surface |
| A join table indexing image use | A second copy of the references to keep in sync; scanning one game's questions is enough |

### 3.3 Loading images and access

| Alternative | Why it lost |
|---|---|
| Authenticated fetch into a blob URL | Needs a new per-request "is this player in an active session of this game" rule, plus blob-URL lifecycle code in two apps |
| Unguessable ids on a public endpoint | Access becomes "anyone who ever saw the id", with no expiry, outside the permission system |
| Cookie authentication for images | Makes every endpoint accept cookies (a cross-site request forgery surface) and must mirror the `localStorage` token |
| Image data inside socket payloads | Every question broadcast would carry image bytes to every phone, with no browser caching |
| A separate secret for signing links | New configuration with the same multi-worker trap as the JWT keys; reusing the RS256 keys adds none |
| A fresh token in every payload | Every payload would produce a new URL and defeat browser caching |
| One payload with all links for every screen | Phones would receive prompt-image links, and later T7's Spot It image |
| A host-only socket event for projector links | Three emit sites (advance, `sync_state`, late join), a client-side merge, and a second event to request fresh links |

### 3.4 What screens show

| Alternative | Why it lost |
|---|---|
| Prompt images on phones as well | Every phone downloads every prompt image at question start, and the projector/phone split is lost |
| Image options on the projector only, letters on phones | Unusable for anyone who can't see the screen well |
| Image options on phones only | Small images lose detail, and the room sees nothing until results |
| Required captions | Captions can give answers away (for example, painting titles) |

### 3.5 Processing

| Alternative | Why it lost |
|---|---|
| Store uploads untouched | Keeps location metadata, leaves width and height inconsistent with the displayed orientation, and allows unbounded sizes |
| Always re-encode | Every export/import cycle would lose JPEG quality, and round trips couldn't be checked byte for byte |
| Resize in the browser before upload | The server must validate and derive everything anyway; client output can't be trusted |
| Accept SVG | Scripts in an SVG would run on the apps' origin |
| Keep GIF animation | Complicates conformance and canvas use; the first frame is enough for a question |
| Deduplicate identical uploads | Replacing one "copy" would change the other's uses unexpectedly; little benefit at this scale |

### 3.6 Budgets and the size guarantee

| Alternative | Why it lost |
|---|---|
| A budget on image bytes only | Non-image JSON has no size limit, so an image budget can't promise re-import |
| New schema limits on question count and option lengths | Changes validation for every existing game to enforce the constraint indirectly; the export rule enforces it directly |
| Check export size only for games with images, or only on image writes | Leaves a path by which a game can outgrow the import limit |
| Refuse every write while a game is over the limit | Pre-existing oversized games could never be reduced; "must not increase" allows shrinking |
| Refuse exporting an oversized game | Blocks backups; the export rule prevents new oversized games instead |
| Rely on default transaction isolation, without locks | Two concurrent writes could each pass the check and together exceed a limit |
| A Redis lock around budget checks | Adds a second failure mode; the database row lock is transactional and released automatically |

### 3.7 Replace, delete, history and live games

| Alternative | Why it lost |
|---|---|
| Keep old image versions for reports | Storage, retention and delete rules for little gain, since question edits aren't versioned either, so reports still wouldn't be faithful |
| Refuse replacement once a game has been played | Re-pointing the question to a new image changes the report the same way |
| Deleting an image removes it from questions | Silently changes questions and can leave them invalid |
| Allow replace/delete during live games, like question edits | Screens could switch images mid-game, and a T7 question could become unconfirmed mid-game |
| Treat MySQL status alone as "live" | A lobby abandoned long ago would block its library forever |
| Let room creation race with replacement and rely on stale links failing | A cached `immutable` link is never requested again, so it can't fail; phones retry the same link; and T7's start-time checks could see a version that then changes |
| Require the same aspect ratio on replace | T7's reconfirmation handles the consequences directly and allows any replacement |

### 3.8 Export, import and the report

| Alternative | Why it lost |
|---|---|
| Export only images used by questions | Export/import would silently drop unused library images |
| Always export version 2 | Needless incompatibility for games without images |
| Zip bundles | Changes the single `.json` file that the course and T6 expect |
| Trust metadata in the bundle | It can be wrong or hostile; every derived field comes from the bytes |
| Full-size images in the report | Report size would grow with the library |
| No images in the report | Image questions would be meaningless in the summary |

### 3.9 Authoring interface

| Alternative | Why it lost |
|---|---|
| A separate Images page | Authors switch pages while writing questions; the panel sits beside the questions that use the images |
| Inline upload in the question form only | No place to view, replace or delete shared images |
| An image interface in the admin app | T4 made the admin app admin-first and host work lives in the host app, which admins already use |

### 3.10 Decisions from the criticism review

| Alternative | Why it lost |
|---|---|
| Every read after the lock as `FOR UPDATE` | Makes answer inserts wait on question rows during a live edit; a shared read returns the same fresh data |
| The game lock alone, with plain reads | Plain reads can return the pre-lock snapshot, or stale objects from SQLAlchemy's identity map |
| One raised nginx limit on all of `/api/` | Large bodies would reach every endpoint, including unauthenticated login and guest routes, before any check runs |
| Tie the projector's links to the Redis room | Adds no protection over the game rule, and breaks a late game-over refresh |
| Report images for anyone who can download the report | Exposes images added after the downloader lost access |
| Count bundle images after decoding them | Thousands of tiny images would cost minutes of CPU before the refusal |
| Check the budget from base64 lengths before decoding | Would refuse images that shrink under normalization; only the count is checked first |
| The caption-or-image rule on every multiple-choice and multi-select question | Would break today's text-only bundles and blank editor rows |
| Treat unresolved legacy references as errors when reading | One old question could break export, the meters, and every write to its game |
| `warnings.catch_warnings()` for the pixel limit | Not thread-safe in Python 3.12; the explicit header check is |
| `replaced_at` from the Python clock | Mixes clocks and rounding with `created_at`, producing false "replaced" notes |
| Accept MPO now | Deferred with other formats; recorded in §5 |

### 3.11 Decisions from the readiness review

| Alternative | Why it lost |
|---|---|
| Unbounded waits for a processing slot | Waits could grow without limit under load. A bounded wait caps the waiting part and gives a clear 503; it can't prevent every nginx 504 (§5) |
| A separate worker process to enforce a hard processing deadline | Disproportionate for this project; the per-image deadline check and the documented limits are enough |
| A queue bound on waiting requests | The bounded wait already stops piling up; waiting uploads hold only spooled temp files |
| Take the slot after reading the upload | Every waiting request would hold up to 10 MiB in memory |
| Count check only under the lock | A 51st upload would be fully decoded before refusal; an advisory pre-check refuses early, and the locked check stays authoritative |
| Exporting unresolved option images as all-null entries | Blank captions then fail the image-option rules, and the export wouldn't re-import |
| An in-process cache of small copies now | Unmeasured need. It's the first remedy if the load check fails (§4.11) |
| Filter blank multiple-choice rows for text-only questions too | Would change today's text-only behavior; filtering applies only when an option has an image |

### 3.12 Decisions from the final re-check

| Alternative | Why it lost |
|---|---|
| Map every exception inside `process_image` to 422 | Would report application bugs and resource failures (such as `MemoryError`) as a bad image and hide them. The mapping covers only Pillow's image operations; anything else stays a logged 500 (§2.5) |
| Commit after the access check, before processing, to return the pooled connection early | Unmeasured need, and it changes transaction shape on every upload and import. Recorded in §4.15 to verify |
| `create_room`'s course-match check before the game lock | Reads the `Game` loaded before the wait, so a course move committed meanwhile would go unseen. The check now runs on the refreshed `Game` (§4.8) |
| One `live_sessions` that always takes locks | Every library GET would hold shared locks on the game's session rows until its post-response commit, contradicting "no locks" for `list_library` (§2.6) |
| Lock tests that release without waiting for every request | A request that arrives after release runs sequentially, so the start and order tests could pass without exercising the paths they claim to test (§4.11) |

### 3.13 Decisions from the final implementation review

How to measure a question write when MySQL can store it longer than it was sent (§2.6):

| Alternative | Why it lost |
|---|---|
| Measure the values as sent, and document `FLOAT` rounding as only ever shortening them | False: a subnormal `points_value` gains digits (`1e-40` → `9.99995e-41`), and the JSON columns change numbers too |
| Model the storage in Python (`FLOAT` as float32 printed to 6 digits) | Matches `FLOAT` on every value tried, but MySQL's JSON parser isn't exact for doubles and its behavior isn't documented; a model can't be relied on to keep the guarantee |
| Ask MySQL first (`SELECT CAST(… AS FLOAT)`, `CAST(… AS JSON)`) and measure the answers | Keeps checks before writes, but assumes a cast stores exactly as a column does; reading the written row back assumes nothing |
| Refuse values the database would change (subnormals, integers past 64 bits) | Changes validation for every existing game and bundle, and each newly found conversion would need another rule |
| An early check on the values as sent for imports, before the insert | Storage can also shorten values, so it could refuse a bundle that fits once stored; an import refused at step 8 is rare |

---

## 4. Detailed Implementation

§4.1 lists every file. Later subsections say what changes in each. The rules themselves are in §2 and
aren't repeated. Work proceeds in the order of §4.13.

### 4.1 File inventory

**Backend**

| File | Status | Change |
|---|---|---|
| `backend/requirements.txt` | changed | add `Pillow`; pin `redis<8` (§4.2) |
| `backend/app/models/image.py` | new | `Image` (§4.3) |
| `backend/app/models/game.py` | changed | `Game.images` (§4.3) |
| `backend/app/models/__init__.py` | changed | register `Image` (§4.3) |
| `backend/app/migrations/versions/005_images.py` | new | the `images` table (`version` server default `1`) (§4.3) |
| `backend/app/common/exceptions.py` | changed | 413, 415 and 503 errors (§4.4) |
| `backend/app/services/game_lock.py` | new | `lock_game`, `fresh()` (§4.4) |
| `backend/app/services/image_refs.py` | new | the reference fields: scan and rewrite (§4.5) |
| `backend/app/services/bundle_service.py` | new | bundle builder, serializer, `bundle_size`, export rule (§4.5) |
| `backend/app/services/image_service.py` | new | pipeline, library operations, links, live sessions, budgets (§4.6) |
| `backend/app/services/auth_service.py` | changed | `create_image_token` (§4.7) |
| `backend/app/services/game_management_service.py` | changed | locks, reference and export checks, export/import version 2 (§4.8) |
| `backend/app/services/game_service.py` | changed | lock and fresh reads in `create_room`/`start_game`; `user_can_use_game` predicate (§4.8) |
| `backend/app/services/report_service.py` | changed | images in the report (§4.9) |
| `backend/app/schemas/admin.py` | changed | reference shapes, caption-or-image (§4.9) |
| `backend/app/schemas/image.py` | new | image API and bundle-image schemas (§4.9) |
| `backend/app/routers/host.py` | changed | five image endpoints; capped import read; report withholds images without game access (§4.9) |
| `backend/app/routers/admin.py` | changed | capped import read; report embeds images (admins pass the game rule) (§4.9) |
| `backend/app/routers/game.py` | changed | session links with both checks; room-create commit (§4.9) |
| `backend/app/routers/images.py` | new | link serving (§4.7) |
| `backend/app/main.py` | changed | mount the images router (§4.7) |
| `backend/app/websocket/gateway.py` | changed | async `_question_payload` with the `images` map; ignore an "already started" start (§4.9) |

**Infrastructure**

| File | Status | Change |
|---|---|---|
| `nginx/nginx.dev.conf` | changed | per-route body limits; `proxy_read_timeout 300s` on the import routes (§4.12) |

**Host app (`frontend/host/src/`)**

| File | Status | Change |
|---|---|---|
| `types/game.ts` | changed | config fields; `images` map (§4.10) |
| `types/images.ts` | new | REST image types (§4.10) |
| `lib/api.ts` | changed | `postForm` method parameter (§4.10) |
| `lib/imageLibrary.ts` | new | `useImageLibrary` (§4.10) |
| `lib/sessionImages.ts` | new | `useSessionImageLinks` (§4.10) |
| `components/ui/SafeImage.tsx` | new | loading, retry, placeholder (§4.10) |
| `components/images/ImageLibraryPanel.tsx` | new | the Images panel (§4.10) |
| `components/images/ImagePicker.tsx` | new | the picker (§4.10) |
| `pages/QuestionEditorPage.tsx` | changed | panel, pickers, option images (§4.10) |
| `pages/GamesPage.tsx` | changed | import size pre-check (§4.10) |
| `pages/game/GameLayout.tsx` | changed | session link map in `GameContext`; room-fetch type reads `session_id` (§4.10) |
| `pages/game/QuestionPage.tsx` | changed | prompt image, option grid (§4.10) |
| `pages/game/ResultsPage.tsx` | changed | thumbnails and letters (§4.10) |
| `pages/game/GameOverPage.tsx` | changed | thumbnails and letters (§4.10) |

**Player app (`frontend/player/src/`)**

| File | Status | Change |
|---|---|---|
| `types/game.ts` | changed | config fields; `images` map (§4.10) |
| `components/ui/SafeImage.tsx` | new | touch copy (§4.10) |
| `pages/game/QuestionPage.tsx` | changed | image option tiles; a missing map entry means no image (§4.10) |
| `pages/game/ResultsPage.tsx` | changed | letter + caption labels (§4.10) |
| `pages/game/GameOverPage.tsx` | changed | letter + caption labels (§4.10) |

**Tests**

| File | Status | Change |
|---|---|---|
| `tests/integration/requirements.txt` | changed | add `Pillow`, `PyMySQL` (§4.2) |
| `tests/integration/conftest.py` | changed | `read_repo_env()` extracted from `admin_token` (behavior unchanged); `--nginx-url` and `--image-load` options; `redis_cli()` helper; a `wait_until()` polling helper matching `test_t4_access.py`'s (that file is unchanged) (§4.11) |
| `tests/integration/test_t8_images.py` | new | integration tests (§4.11) |
| `tests/integration/test_t8_nginx_limits.py` | new | per-route nginx limits; runs only with `--nginx-url` (§4.11) |
| `tests/integration/test_t8_image_load.py` | new | opt-in load and burst checks; runs only with `--image-load` (§4.11) |
| `tests/unit/test_image_service.py` | new | pipeline and link unit tests (§4.11) |
| `tests/unit/test_bundle_service.py` | new | reference, bundle and size unit tests (§4.11) |

**Documentation**

`README.md`, `docs/realtime.md`, and the `CLAUDE.md` files listed in §4.14.

**Unchanged:**
- the admin app (`frontend/admin/`)
- `scripts/`
- `sample_games/` (T6 work)
- `.gitlab-ci.yml`
- `backend/Dockerfile` (rebuild only)
- `backend/app/websocket/events.py`
- `backend/app/services/state_service.py` (used, not changed)

### 4.2 Dependencies and infrastructure

- `backend/requirements.txt`: add `Pillow`, a release whose wheels include WebP support. The backend
  image must be rebuilt (`docker compose up --build`). Unit tests run in the backend virtualenv,
  which gets Pillow from the same file.
- `backend/requirements.txt`: pin `redis[hiredis]<8` (found in implementation). The rebuild also
  upgraded redis-py to 8.x, which caps each asyncio connection pool at 100 by default (every
  earlier release: 2³¹) and fails fast when it's exhausted. The gateway takes a connection per
  concurrent handler, so the §4.11 load check's 150 players answering at once raised
  `MaxConnectionsError`. The pin keeps the pre-T8 behaviour; lifting it means sizing the pool in
  `redis_client.py` first.
- `nginx/nginx.dev.conf`: per-route body limits (the nginx body limits of §2.4; location blocks in
  §4.12).
- `tests/integration/requirements.txt`: add `Pillow` and `PyMySQL`.

### 4.3 Data model and migration

- `backend/app/models/image.py`: `Image`, with the columns of §2.3.
  - `data` and `small_data` use `sqlalchemy.dialects.mysql.MEDIUMBLOB` with `deferred=True`.
  - A `game` relationship back to `Game`.
- `backend/app/models/game.py`: `Game.images` — `cascade="all, delete-orphan"`,
  `passive_deletes=True`, ordered by `Image.id`.
  - `Game.questions` also gets `passive_deletes=True` (found in implementation). Deleting a game
    otherwise loads all its questions, ordered by `order_index`, just to delete them, and that
    sort fails for a large question (§4.6). The database already cascades questions
    (`ON DELETE CASCADE` since migration `001`), and the admin game delete removes the game's
    session scores first.
- `backend/app/models/__init__.py`: import `Image` after the `game` models.
- `backend/app/migrations/versions/005_images.py`:
  - revision `"005"`, down_revision `"004"`, with a docstring header in the style of `004`
  - `upgrade()` creates `images`, with index `ix_images_game_id` created before FK
    `fk_images_game_id` → `games.id` `ON DELETE CASCADE` (as `004` does); blob columns
    `mysql.MEDIUMBLOB()`
  - `version` has `server_default="1"`, matching migration 004's style
  - `downgrade()` drops the FK, the index and the table

### 4.4 Errors, the game lock, and module dependencies

- `backend/app/common/exceptions.py`: `PayloadTooLargeError` (`PAYLOAD_TOO_LARGE`, 413) and
  `UnsupportedMediaTypeError` (`UNSUPPORTED_MEDIA_TYPE`, 415), and `ServiceUnavailableError`
  (`SERVICE_UNAVAILABLE`, 503), all `BuzzerError` subclasses. The existing handler formats them.
- `backend/app/services/game_lock.py`: `lock_game(db, game_id) -> Game`.
  - Runs `SELECT … FROM games WHERE id = :id FOR UPDATE`, with `populate_existing` so the returned
    object is fresh.
  - A missing game → `NotFoundError`.
  - MySQL's lock-wait timeout (error 1205) and deadlock (error 1213) both become
    `ConflictError("This game is being changed by another request; try again.")`.
- `fresh(stmt, *, exclusive=False)` (also in `game_lock.py`) returns
  `stmt.with_for_update(read=not exclusive)` with `populate_existing`. Every read in the §2.6
  fresh-read table uses it. For MySQL, SQLAlchemy renders the shared form as `LOCK IN SHARE MODE`.
- **Module dependencies.** Each arrow means "imports". Nothing may import against the direction
  shown:

  ```
  image_refs              → (nothing)
  game_lock               → models, common.exceptions
  bundle_service          → image_refs, common.exceptions
  image_service           → image_refs, bundle_service, game_lock, auth_service, state_service,
                            models, schemas.image, common.exceptions
  game_management_service → image_refs, bundle_service, image_service, game_lock, game_service (existing)
  game_service            → game_lock        (T7 will add image_service)
  report_service          → image_service, image_refs
  websocket/gateway       → image_service
  ```

  `image_service` and `bundle_service` never import `game_service` or `game_management_service`.
  This keeps T7's future `game_service → image_service` import cycle-free.

### 4.5 Reference fields and bundles

**`backend/app/services/image_refs.py`** — pure; the only definition of which `config` keys hold
image ids.
- `image_refs(config) -> list[Ref]`: each `Ref` is `(image_id, role, option_index)`, with role
  `prompt` (`prompt_image_id`), `option` (`option_images[i]`) or `canvas` (`image_id`). It returns
  only values that are positive ints (not bools); anything else is ignored. Callers then drop ids
  that aren't in the game's library (§2.7).
- `map_image_refs(config, fn) -> dict`: a copy of `config` with every reference slot replaced by
  `fn(value)`, whatever the value is; nulls stay null, and a non-list `option_images` becomes
  `None`.
- All three roles are implemented in T8. T7 changes only `validate_structure`, to allow `image_id`
  on its canvas type.

**`backend/app/services/bundle_service.py`** — pure: no database, no I/O. It holds the export limit
and import limit constants (§2.4).
- `serialize(bundle) -> bytes`: the only bundle serializer. It is exactly today's
  `json.dumps(bundle, indent=2, ensure_ascii=False).encode()`.
- Plain input records:
  - `GameMeta(title, description, max_players)`
  - `QuestionFields(type, grading_type, prompt, config, answer_data, time_limit_seconds,
    points_value)`, in order, always read from the database (a question is measured as stored,
    §2.6)
  - `ImageRecord(id, filename, description, byte_size, data)`, where `data` is `bytes | None`
- `build_bundle(meta, questions, images, *, include_data) -> dict`: the §2.15 bundle.
  - Version 1, exactly today's structure, when `images` is empty.
  - Otherwise version 2: keys `img-1`, `img-2`, … by id order; `data` is base64 when `include_data`,
    else `""`; each question's `config` rewritten to keys with `map_image_refs`.
  - In both versions, the export `fn` returns the key for an id in the library and `None` for
    anything else. The reference mapping then applies the §2.7 export repairs, so `bundle_size`
    measures exactly what is exported:
    - omit an unresolved `prompt_image_id` or `image_id`
    - omit `option_images` when every entry is `null`
    - write "Missing image" for a blank caption whose image was dropped while other options keep
      images
- `bundle_size(meta, questions, images) -> int`:
  `len(serialize(build_bundle(..., include_data=False)))` + Σ 4·⌈`byte_size`/3⌉. It never raises on
  malformed references.
- `check_export(before, after)`: raises `ConflictError` naming both sizes when
  `after > export limit` and `after > before` (§2.6).

### 4.6 `backend/app/services/image_service.py`

Its constants carry the §2.4 image limits. Contents:

- **Capacity.**
  - `PIPELINE_LIMITER = anyio.CapacityLimiter(2)` at module level. anyio 4.15 allows creating it
    before the event loop runs.
  - `processing_slot(deadline_at=None)`: an async context manager.
    - It acquires one token, waiting at most the slot wait limit or the time left until
      `deadline_at`, whichever is shorter (via `anyio.fail_after` around the acquire only), and
      raises `ServiceUnavailableError` on timeout.
    - It releases the token on exit, including after an exception or cancellation, which is always
      after any worker-thread run inside it has returned.
  - `process_upload(upload)`: inside `processing_slot()`, runs `read_capped(upload, input limit)`,
    then `anyio.to_thread.run_sync(process_image, raw)` with the default `abandon_on_cancel=False`.
    - A run that has started finishes even if the request is cancelled, and its token is released
      afterwards.
    - A cancelled waiter never runs.
    - No deadline applies to an upload's processing.
  - `process_bundle_images(entries, deadline_at)`: for each entry, in order:
    1. take a slot within the remaining time
    2. run the pipeline in a worker thread
    3. release the slot
    4. **check `anyio.current_time()` against `deadline_at`**

    The first check that finds the deadline passed — including after the final image — raises
    `ServiceUnavailableError`. All processed results are dropped, and the caller performs no
    database write. The check doesn't rely on cancellation being delivered.
- **Reading uploads.** `read_capped(upload, limit)` reads at most `limit + 1` bytes; anything more →
  `PayloadTooLargeError`. `process_upload` uses it with the input limit, inside its processing slot.
  The import routers use it with `bundle_service`'s import limit for bundles.
- **Pipeline.** `process_image(raw) -> ProcessedImage` implements §2.5 exactly, returning `data`,
  `small_data`, `content_type`, `width`, `height`, `byte_size` and `sha256`. `conforms(img, raw)`
  implements §2.5 step 4.
  - The pipeline is synchronous and CPU-bound. Async callers run it with `anyio.to_thread.run_sync`
    so the event loop, which also serves the game's sockets, stays responsive.
  - **Pixels and frames.** After `Image.open` (header only), check width × height against the
    pixel limit (422) before `load()`. Set `Image.MAX_IMAGE_PIXELS` to the pixel limit as a backstop.
    Use no warning filters. Check animation with `is_animated`; for GIF, never count frames.
  - **Errors.** Pillow's exceptions map to statuses as in §2.5: the `try` blocks wrap only the
    Pillow calls (open, header, `load()`, transforms, encoding), `UnidentifiedImageError` is
    caught before `OSError`, `BuzzerError`s pass through, and nothing else is caught, so an
    unexpected exception is a logged 500. The pixel message states the image's megapixels and
    the limit.
  - **Metadata test.** No `info` keys `exif`, `xmp`, `XML:com.adobe.xmp`, `photoshop` or `comment`;
    `getexif()` empty; PNG `text` empty; JPEG `applist` has no `APP1` or `APP13` segment.
    `icc_profile` is allowed and carried into re-encoded output.
  - **Opening.** `Image.open` is restricted to the parsers of the four accepted formats
    (`formats=[...]`), so no other format's parser ever runs on uploaded bytes. A JPEG with
    multi-picture data still opens, as format `MPO`, and is refused by the format check.
  - **Normalization.** `ImageOps.exif_transpose`, then the first frame, then `thumbnail` to the
    full-image edge with Lanczos resampling. Output is encoded per §2.5 step 5, without EXIF.
    - **Modes** are converted only where the output format or the resampling requires it. Pillow
      silently resamples palette (`P`) and 1-bit images with nearest-neighbour, so those are
      converted first (to `RGB`/`RGBA`, or `L`) whenever they're resized. 16-bit greyscale
      (`I;16`) is scaled to 8 bits, never clipped, where the output needs 8 bits.
    - **Metadata.** Pillow's JPEG and PNG encoders fall back to the image's own `info` for some
      metadata. With Pillow 12.3, JPEG writes `info["comment"]` back (§4.15, verified), and
      `info` survives `copy()`, transposes and resizes. So the output image's `info` is cleared
      before encoding, except `transparency`, and the ICC profile is passed explicitly.
    - **ICC.** The profile is carried only while the pixels stay in its colour space. It is
      dropped when CMYK or greyscale pixels are converted to RGB.
  - **Small copy.** Made from the stored full image: converted to `RGB`/`RGBA` first (WebP needs
    it, and resizing then isn't nearest-neighbour), `thumbnail` to the small-copy edge, WebP at
    quality 85 (then 50), transparency kept. The same metadata and ICC rules apply.
- **Validation helpers.**
  - `clean_description(text)`: trims; the result must be 1–300 characters, else 422.
  - `clean_filename(name)`: basename only, control characters removed, at most 255 characters,
    `"image"` if empty.
- **References.** `assert_in_library(db, game_id, ids)` makes one fresh shared read (`fresh(...)`)
  and raises 422 as in §2.7. The ids come from `image_refs.image_refs`.
- **Live sessions.** `live_sessions(db, redis, game_id, *, locking)` reads the game's
  `game_sessions` rows with status `LOBBY`/`IN_PROGRESS`. It keeps only those for which
  `state.get_room_state(redis, room_code)` returns a value. Each entry has session id, room code,
  status, host display name and `created_at`.
  - The live guards in `replace_image` and `delete_image` pass `locking=True`: a fresh shared read
    (`fresh(...)`), per §2.6.
  - `list_library` passes `locking=False`: a plain read, as it takes no locks (§2.6).
- **Export inputs.** `export_inputs(db, game_id, *, with_data, locking)` loads the game's
  `GameMeta`, its questions (in `order_index` order) and its `ImageRecord`s (by id).
  - Each question comes back as `(id, order_index, QuestionFields)`. The id feeds `used_by`
    (`UsedBy.question_id`) and question numbers; `order_index` feeds `create_question`'s next
    index (§4.8). Callers pass only the `QuestionFields` to `bundle_service`, whose inputs don't
    change.
  - Callers that check limits pass `locking=True` (fresh shared reads via `fresh(...)`), substitute
    their change, and call `bundle_service.bundle_size` and `check_export`. Metadata and image
    records are substituted as pending values; a question only as stored, read back after its
    write (§2.6, §4.8).
  - **Question rows are sorted in Python, not by SQL `ORDER BY`** (found in implementation).
    MySQL 8's filesort carries every selected column, `config` included, in its sort buffer
    (256 KB by default), and fails with error 1038 ("Out of sort memory") once a single
    question's row is larger. A question of about 300 KB is enough, well inside nginx's 1 MiB
    request limit. An `ORDER BY` here would make every write to such a game, its export, its
    meters and its report fail with a 500. So every T8 read that selects question content —
    `export_inputs`, `used_by`, `start_game`'s question list, the report's questions and
    `list_questions` — fetches the rows unordered and sorts them by `(order_index, id)`, which
    is the same order. The `id` tiebreak also makes the order and the question numbers
    deterministic when two questions share an `order_index`. The export's image read, which
    selects both blobs, sorts by id in Python too (found in the mean review: `EXPLAIN` showed
    a filesort when the optimizer chose a range plan, which would carry the blobs through the
    same sort buffer).
  - Export and `list_library` pass `locking=False`.
  - Export passes `with_data=True`.
- **Budgets.** `check_budgets(count, library_bytes)` raises `ConflictError` per §2.6.
- **Library operations.** Each write operation locks the game (`lock_game`), makes the §2.6 fresh
  reads, checks the would-be state, and only then modifies rows. `list_library` takes no locks. An
  image not in the game → `NotFoundError`.
  - `list_library(db, redis, game_id, now)` returns the items (metadata, `used_by` from scanning the
    game's questions with `image_refs`, links), `live_sessions(..., locking=False)`, and meters
    (image count and limit, library bytes and budget, export bytes and limit). It reads questions
    and images with `export_inputs(locking=False)`. `used_by.number` is computed from each
    question's position in `order_index` order.
  - `precheck_library(db, game_id)`: an advisory plain read of the image count and library bytes. It
    raises the count or budget 409 early when the library is already full, before any processing.
    The locked checks in `add_image` stay authoritative.
  - `add_image(db, game_id, processed, filename, description)`: the budgets and the export rule.
  - `replace_image(db, redis, game_id, image_id, processed, filename)`: reads the image row with
    `fresh(..., exclusive=True)`; the live guard, budgets and export rule; then the new bytes,
    `version + 1`, `replaced_at = func.now()`, and `filename` set to the new file's cleaned
    basename; `description` is kept.
  - `set_description(db, game_id, image_id, description)`: the export rule.
  - `delete_image(db, redis, game_id, image_id)`: reads the image row with
    `fresh(..., exclusive=True)`; the live guard and the reference check (409 listing question
    numbers and roles).
- **Links.**
  - `link(image, variant, now) -> (url, expires_at)`, where `exp` is the start of the current UTC
    hour plus 3 hours. `expires_at` uses the §2.17 timestamp format.
  - `link_entry(image, now)`: full and small URLs, width, height, description, `expires_at`.
  - `session_links(db, session, now)`: entries for every image of the session's game.
  - `player_images(config, images_by_id, now)`: option images only, small URLs.
  - `images_for_refs(db, game_id, ids)`: loads referenced images with `Image.game_id == game_id`
    (no blobs unless asked). The gateway and the report use it, so a reference to another game's
    image is unresolved (§2.7).
- **For the report and T7.** `get_image_meta(db, game_id, image_id, *, locking=False)` (with
  `locking=True` it uses `fresh(...)`, shared) and `data_uri(image, variant)`.
- **Bundle images on import**, in two separate steps:
  - `check_bundle_entries(raw_entries)`: the §2.15 step 4 checks — the strict `BundleImage` schema,
    key rules, strict base64 (`b64decode(validate=True)`) and the input limit — returning the decoded
    bytes. No image decoding.
  - `process_bundle_images(entries, deadline_at)`: the §2.15 step 6 pipeline (see "Capacity").

### 4.7 Tokens and the serving endpoint

- `backend/app/services/auth_service.py`: `create_image_token(image_id, version, variant, exp)`.
  - It encodes `{token_type: "image", img, v, var, exp}` with the existing private key.
  - It has **no `iat` claim**, unlike `_make_token`, so identical inputs give identical tokens
    (§2.9).
  - Verification reuses `decode_token`.
- `backend/app/routers/images.py`: `GET /images/{image_id}/v{version}/{variant}`, with query
  parameter `t` and no auth dependency.
  - Decodes `t`; failure → 403.
  - Requires `token_type == "image"` and claims that match the path; otherwise 403.
  - Loads the row with only the requested blob undeferred; missing → 404.
  - Requires `version` to equal the row's version; otherwise 404.
  - Returns the bytes with the §2.9 headers. Error responses carry no caching headers.
  - Commits (ends the read transaction) before returning (found in the mean review). `get_db`
    commits only after the response has been sent, so without this every download held a
    pooled connection, shared with answer handling, for as long as a slow phone took to receive
    the body.
- `backend/app/main.py`: include the router under `/api`, like the others.

### 4.8 Questions, games, export and import

`backend/app/services/game_management_service.py`:
- **`_as_stored(db, question)`**: flushes a question just written, refreshes it from the
  database, and returns its `QuestionFields` as export will read them (§2.6, "Questions are
  measured as stored"). The refresh reads the transaction's own write, so it needs no lock.
- **`create_question`**: `lock_game`, then `image_service.assert_in_library` for the question's
  references, then `export_inputs(locking=True)`, then the insert (prompt sanitized), then the
  export rule with `_as_stored` of the new question added to that fresh list. `order_index`, when
  not given, is the maximum `order_index` in that same fresh question list plus 1, never a
  separate plain read. The router has already validated the body as `QuestionCreate`, and commits
  only after this returns.
- **`update_question`**: `lock_game`, then `_get_question` with `fresh(...)` (shared,
  `populate_existing`), then the merge and `QuestionCreate(**merged)`, then `assert_in_library` on
  the merged `config`, then `export_inputs(locking=True)`. Only after every locking read, the
  `setattr` loop (prompt sanitized), then the export rule with `_as_stored` of the question in
  place of its old fields. A refusal raises, and `get_db` rolls the update back.
- **`update_game`**: `lock_game`, which refreshes the `Game` object the access check returned. When
  the title, description or `max_players` changes, the export rule runs on the would-be metadata;
  only then are the fields assigned. The course-change path is unchanged.
- **`reorder_questions`**: `lock_game`, then a fresh shared read of the game's questions, then the
  existing id-set check and assignment.
- **`delete_question`**: unchanged.
- **`export_game`**: loads `export_inputs(..., with_data=True, locking=False)`, then
  `bundle_service.serialize(bundle_service.build_bundle(meta, [fields for (_, _, fields) in
  questions], images, include_data=True))`. For a game without images this produces today's bytes.
- **`import_game`**. The router has already applied the import limit. It runs the §2.15 order,
  which is defined only there. The steps map to functions as follows:
  - steps 1–3: inline (parse, version, `GameCreate`, the `images` list and count)
  - step 4: `image_service.check_bundle_entries`
  - step 5: `map_image_refs` to provisional integers, the version 1 refusal, then `QuestionCreate`
  - step 6: `image_service.process_bundle_images(entries, deadline_at = anyio.current_time() +
    import processing deadline)`, whose final deadline check runs before step 7
  - step 7: `check_budgets`
  - step 8: `create_game`, insert the images (flushing for ids), map provisional integers to real
    ids, insert the questions (prompts sanitized), flush, then `check_export(0, bundle_size(...))`
    on `export_inputs(game.id, locking=False)`: a plain read, since every row of the new game is
    this transaction's own insert

  All of this runs in the request's single transaction; any failure raises before the commit.

`backend/app/services/game_service.py`:
- `create_room`, in this order:
  1. The existing access checks: `assert_host_can_use_course`, then `assert_host_can_use_game`.
  2. `lock_game(db, game_id)`, which refreshes the `Game` the access check returned
     (`populate_existing`).
  3. The existing course-match check (`game.course_id != course_id` → 409), on that refreshed
     `Game`, so an admin's course move committed while the request waited is seen.
  4. The room-cap check, the insert, and the Redis write, which stays before the commit.
- `start_game`:
  1. `lock_game(db, session.game_id)`.
  2. Re-read the session with `fresh(..., exclusive=True)`. If it's `IN_PROGRESS`, return `None`
     ("already started"); any other non-`LOBBY` state raises the existing `ConflictError`.
  3. Read the questions with `fresh(...)` (shared).
  4. Set the status and commit, as today.
- New `user_can_use_game(db, user, game_id) -> bool`, the game rule without raising.
  `assert_host_can_use_game` is refactored to use it, with unchanged behavior.
- T7 adds its confirmation checks right after each lock (in `create_room`, after the course-match
  check).

### 4.9 Schemas, routers, gateway and report

- **`backend/app/schemas/admin.py`**, in `QuestionCreate.validate_structure` (which covers create,
  the merged update, and import):
  - `prompt_image_id`, if present and not null, is a positive int (not a bool), on any type.
  - `option_images` is allowed only for `multiple_choice` and `multi_select`. When it is present and
    not null: it has one entry per option, each a positive int or null; each option is a string; and
    an option may be blank only if it has an image.
  - When `option_images` is absent or null, options are checked exactly as today (§2.7).
  - `image_id`: refused (422 "config.image_id is only allowed on canvas question types") when
    present and not null on all four current types; a legacy `null` is tolerated. T7 allows it on
    its canvas type.
- **`backend/app/schemas/image.py`**: the schemas of §2.17. `BundleImage`'s `key` uses the §2.15
  pattern.
- **`backend/app/routers/host.py`**: the five endpoints of §2.8.
  - Upload and replace run, in order (other endpoints skip the steps that don't apply):
    1. `assert_host_can_use_game`, before the handler reads the spooled file (FastAPI has already
       received the body; nginx limits its size, §2.4)
    2. `clean_description` (upload only)
    3. `precheck_library` (upload only)
    4. `image_service.process_upload`: slot, capped read, pipeline (§4.6)
    5. the `image_service` operation: lock, fresh reads, checks, write
    6. `await db.commit()`
    7. the response
  - `GET` returns `LibraryResponse`.
  - The existing import endpoint switches to `read_capped(file, import limit)`.
  - The Redis dependency is the one `routers/game.py` uses.
- **`backend/app/routers/admin.py`**: its import endpoint switches to `read_capped(file, import
  limit)`, and its report endpoint passes `include_images=True` (admins pass the game rule).
  Nothing else changes, because its game and question endpoints already call the service.
- **`backend/app/routers/host.py`, report endpoint**: passes
  `include_images=await game_service.user_can_use_game(db, user, session.game_id)`.
- **`backend/app/routers/game.py`**:
  - `GET /sessions/{session_id}/images`: requires `require_user`. A missing session → 404;
    otherwise 403 unless `user_can_host_session` **and** `user_can_use_game` both hold. Returns
    `session_links`.
  - `POST /rooms`: `await db.commit()` before returning (§2.6).
- **`backend/app/websocket/gateway.py`**:
  - `_question_payload` becomes `async def _question_payload(db, q, number, total)`.
  - It loads the question's option images with `images_for_refs(db, q.game_id, …)` (one query, no
    blobs, scoped to the question's game) and adds
    `"images": image_service.player_images(...)`. The key is always present (`{}` when there are
    none).
  - Its three call sites — `_host_question_payload`, the late-join emit and the advance emit —
    `await` it.
  - `question_results` doesn't change and carries no `config`. Results screens use the current
    question already received in `new_question`. The game-over summaries don't change either;
    their question items already carry `config` (§2.17).
  - When `start_game` returns `None` ("already started"), the advance handler returns without
    emitting anything.
  - Nothing else changes; `events.py` is untouched.
- **`backend/app/services/report_service.py`**: `build_session_report(db, session_id, *,
  include_images)` loads the images referenced by the session's questions (found with `image_refs`)
  with `images_for_refs(db, session.game_id, …)`, in one query, with `small_data` undeferred. When `include_images` is false, every image is
  replaced by the §2.14 withheld-image text. Then, for each question:
  - The prompt image renders as `<img src="data:image/webp;base64,…" alt="…">`, with the §2.14 note
    when `replaced_at` is later than the session's `created_at`.
  - In `_render_bar_chart` (multiple choice), labels follow §2.11, with a thumbnail when the option
    has an image.
  - A new `_render_option_list(q, images)` for `multi_select`:
    - each option's label (§2.11), its small-copy thumbnail (data URI, `alt` = description) when it
      has an image, and its caption when it isn't empty
    - no counts or correctness
    - the chart dispatch in `build_session_report` calls it for `multi_select`
  - A reference to an image that isn't in the library is skipped (§2.7).
  - All text goes through `_esc`.

### 4.10 Frontend

**Host app (`frontend/host/src/`)**

- **Types.**
  - `types/game.ts`: config fields `prompt_image_id?: number | null` and
    `option_images?: (number | null)[]` on question payloads and summary items; `QuestionPayload.images?:
    Record<string, { url: string; width: number; height: number; description: string }>`.
  - `types/images.ts`: the REST types from `schemas/image.py`.
- **`lib/`.**
  - `lib/api.ts`: `postForm(path, form, method = 'POST')`, so a replace can use `PUT`; existing
    callers are unchanged. A 413 response without a JSON body (nginx's page) becomes the message
    "The file is larger than the server accepts." A 503 shows the server's JSON message.
  - `lib/imageLibrary.ts`: `useImageLibrary(gameId)` returns the library, loading and error state,
    `reload`, `upload`, `replace`, `setDescription` and `remove`. Mutations adopt the returned item
    (with its new links) and then reload. It also reloads within 10 minutes of the earliest
    `expires_at`. `reload` coalesces calls made at the same time into one request.
  - `lib/sessionImages.ts`: `useSessionImageLinks(sessionId)` returns `{ links, refresh }`. It
    fetches once `sessionId` is known, and again within 10 minutes of the earliest `expires_at`.
    `refresh` coalesces calls made at the same time into one request.
  - `lib/linkExpiry.ts`: `refreshDelayMs`, the scheduled-refresh delay both hooks use. It is never
    less than 60 s (found in the final review). Every fetch replaces the links, which schedules
    the next refresh, so a client clock 1 h 50 min fast (fresh links expire 2–3 h after the
    server's time) would otherwise compute 0 each time and refetch in a tight loop. An image
    that fails sooner still refreshes at once, through `SafeImage`.
- **Components.**
  - `components/ui/SafeImage.tsx` (presentational): props `src`, `alt`, sizing classes, optional
    `refresh(): Promise<void>`, optional `showRetry`. It shows a loading state, then implements the
    §2.12 sequence:
    1. On the first error, await `refresh()` if given.
    2. Retry once, always by bumping the internal React `key`, which remounts the image element with
       the current `src`, whether or not it changed.
    3. On the second error, show the placeholder ("Image unavailable" and the `alt` text), plus
       Retry if `showRetry`; Retry restarts at step 1.
  - `components/images/ImageLibraryPanel.tsx` (presentational): the §2.8 panel.
    - The upload form accepts `image/png,image/jpeg,image/webp,image/gif` and requires a
      description.
    - Delete uses the two-step inline confirm pattern of `GamesPage`.
    - The live-session banner lists each blocking session and the fix from §2.13.
    - Upload and replace refuse a file over the input limit before sending it, naming the limit.
  - `components/images/ImagePicker.tsx` (presentational): library thumbnails, "No image", and an
    upload mini-form. It reports `onSelect(id | null)` and `onUpload(file, description)`. It refuses
    a file over the input limit before sending it, and shows "Missing image" for a reference that
    isn't in the library (§2.7).
- **`pages/GamesPage.tsx`.** The import refuses a bundle over the import limit before sending it,
  naming the limit.
- **`pages/QuestionEditorPage.tsx`.**
  - `useImageLibrary(gameId)` feeds the panel, placed above the question list.
  - Form state gains `promptImageId`, and an `imageId` on each multiple-choice and multi-select
    option.
  - `questionToForm` reads `prompt_image_id` and `option_images`.
  - `formToPayload` writes `prompt_image_id` when set, and `option_images` only when some option has
    an image, so text-only payloads are unchanged.
  - **Option rows.**
    - When no option has an image, multiple choice and multi-select behave exactly as today.
    - When some option has an image, both types first drop rows with neither text nor image, then
      derive `options`, `option_images`, `answer_points` and `points_value` from that one filtered
      list. A leftover blank row therefore can't block saving.
  - The question list renders per §2.11.
- **Game screens.**
  - `pages/game/GameLayout.tsx`:
    - types its existing `GET /game/rooms/{code}` fetch as
      `{ game_title: string; session_id: string }` (the response already includes `session_id`),
      keeps `session_id`, and calls `useSessionImageLinks`
    - exposes `imageLinks` and `refreshImageLinks` through `GameContext`
    - calls `refreshImageLinks` after a socket reconnect, and once when the current question
      references an id missing from `imageLinks`; an id still missing after that refresh is treated
      as no image (§2.7)
  - `pages/game/QuestionPage.tsx`:
    - The prompt image (a `SafeImage` with the full URL, `refresh = refreshImageLinks` and Retry
      shown) sits below the prompt text and renders regardless of `questionLocked`.
    - For multiple choice and multi-select with any option image: a lettered grid of full images
      with captions.
    - Image layouts reserve space for the fixed join panel.
  - `pages/game/ResultsPage.tsx`: the prompt thumbnail. `buildBars` labels follow §2.11, and bars
    show a thumbnail for image options.
  - `pages/game/GameOverPage.tsx`: the same, in each `QuestionCard`.
  - Label helpers on each screen keep their existing format and drop the separator when the caption
    is empty (§2.11).

**Player app (`frontend/player/src/`)**

- `types/game.ts`: the same config fields and `images` map as the host.
- `components/ui/SafeImage.tsx`: the touch copy. No `refresh`; one retry by remount with the same
  `src`, then the placeholder; no Retry button (§2.12).
- `pages/game/QuestionPage.tsx`: multiple choice and multi-select render 2-column image tiles when
  any option has an image.
  - Each tile shows a letter badge, a `SafeImage` with `currentQuestion.images[String(id)].url`, and
    the caption if any. An option whose id is missing from `images` renders as text-only: letter and
    caption.
  - Each tile's accessible name follows §2.12.
  - Tap behavior is unchanged.
- `pages/game/ResultsPage.tsx` and `GameOverPage.tsx`: labels keep the existing `A — Paris` format,
  and show the letter alone when the caption is empty (§2.11).

### 4.11 Tests

No frontend app has a test setup, so frontend behavior is verified manually (§4.12).

**Integration: `tests/integration/test_t8_images.py`.** It builds a self-contained `T8Env` in the
style of `test_t4_access.py`:
- its own course, a HOST user with a game grant, a second course and host, and a guest
- test-created games
- in-memory image factories built with Pillow: PNG, a JPEG with an EXIF orientation tag, GIF, WebP,
  SVG text, a truncated PNG, a pixel bomb, a PNG whose header declares 48 MP, and noise JPEGs of
  chosen sizes
- a direct MySQL connection (the lock-test settings below), used for the lock harness and to plant
  legacy data
- a finalizer that deletes every session and game it created (the admin game delete cascades to
  images)

Tests:

- **Upload:** `test_upload_accepts_each_format`, `test_exif_rotation_normalized`,
  `test_large_image_downscaled`, `test_conforming_bytes_stored_unchanged`,
  `test_small_copy_is_webp_within_edge`.
- **`test_invalid_uploads_refused`:** the 415, 422 and 413 cases, and a missing or overlong
  description. Each leaves the library unchanged.
- **`test_count_and_budget_limits`:** the 51st image, and the library budget → 409, each naming
  its limit, with nothing changed. The export limit has its own test below.
- **`test_library_access_rules`:**
  - no grant, another course's host, and a guest → refused
  - admin allowed
  - another game's image → 404
- **`test_links_verify_token_path_and_version`:**
  - tampered, expired, wrong-variant, other-image and stale-version links → refused
  - an access token as `t` → refused; an image token as a bearer token and as socket auth →
    refused
  - response headers
  - the URL is stable within the hour and changes after a replace
- **`test_references_checked_on_every_write_path`:**
  - same-library checks on create, update and import
  - caption-or-image
  - `option_images` on other types → 422
  - `prompt_image_id` accepted on all four types
- **Replace and delete:** `test_replace_updates_every_use`, `test_delete_refused_while_referenced`,
  `test_refused_replace_leaves_image_intact`.
- **`test_live_guard`:** a LOBBY room blocks replace and delete, with the room's details. Both are
  allowed after the session is deleted, after it completes, and once its Redis room is gone.
  - The "Redis room gone" case uses `redis_cli("DEL", "room:{code}")` from `conftest.py`. The helper
    runs `BUZZER_TEST_REDIS_CLI` (default `docker compose exec -T redis redis-cli`) from the repo
    root, because Redis publishes no port.
  - That case is skipped, with the reason, if the command can't run.
- **`test_concurrent_uploads_respect_count`** (a lock test, below):
  1. With 49 images, the test holds the game lock and starts two uploads.
  2. The processlist shows both lock statements.
  3. The test releases the lock.
  4. Exactly one 201 and one 409 result, and the library ends at 50.
  - A plain (non-fresh) count read would let both succeed.
- **`test_room_creation_and_replace_are_serialized`** (detailed below).
- **`test_new_room_after_replace_uses_new_version`.**
- **`test_players_receive_only_small_option_links`:**
  - a player's `new_question` carries only small option links
  - a guest can fetch its link
  - the host's session links are full
  - a late-join player gets fresh links
- **`test_export_import_cycles_preserve_bytes`:**
  - three generations give identical `sha256` for every full image and byte-identical bundles
  - a non-conforming image in a hand-built bundle is normalized once, then stays stable
  - unknown bundle fields → 422
- **`test_budget_sized_library_round_trips`:** a library at the library budget, with questions,
  exports within the export limit and re-imports.
- **`test_version1_samples_still_import`.**
- **`test_report_embeds_small_copies`:**
  - a multiple-choice question: thumbnails beside the bars
  - a multi-select question: the labelled option list, with a thumbnail for each image option and
    captions where present; a caption-less option shows the letter alone
  - the prompt image for any type, as a data URI
  - the replaced note: a replace, then room creation, then completion → no note; a replace after
    completion → the note
- **`test_game_delete_removes_images`.**
- **`test_image_id_refused_on_current_types`:** create, update, and import in both versions → 422.
- **`test_version1_bundle_with_image_refs_refused`:** 422 with no game created. A version 2 bundle
  with an integer reference → 422.
- **`test_description_patch`:**
  - 200 with the updated item and unchanged URLs
  - empty or overlong description → 422
  - no grant → 403; another game's image → 404
  - allowed while a session is live
- **`test_export_rule_at_the_boundary`:**
  - **Helper.** `set_headroom(h)` resizes the padding question's padding string. The padding
    question's `config` carries a large extra string, which today's free-form `config` accepts. The
    string is plain ASCII, so each character is exactly one serialized byte. After resizing, the
    helper asserts that the `export_bytes` meter equals the export limit minus `h`, **exactly**.
  - **Reading after a write.** Question update and game update commit after sending their response
    (`get_db`; see `backend/app/routers/CLAUDE.md`). So every meter read that follows one of them —
    in `set_headroom`, after the accepted title edits, and for "meter unchanged" — polls with
    `wait_until` until it sees the expected value, and fails at its timeout. The refusals
    themselves need no polling: each write waits on the game lock until the previous one commits.
  - **Setup:** a library of about 16 MiB (well under the budget), plus the padding question.
  - **At headroom 0,** each growing mutation must be refused with 409 naming the export limit, with
    the meter unchanged. Any growth of one byte or more crosses, whatever the mutation's size:
    - question create and question update
    - title update (+1 character)
    - description `PATCH` (+1 character)
    - image upload (1 MiB, within the budget)
    - replace with a larger image
  - **At headroom 0,** a shrinking edit (title −1 character) is accepted.
  - **Exact fit:** `set_headroom(k)`, then a title growth of exactly k ASCII characters is accepted,
    leaving the meter exactly at the export limit. The next +1 character is refused.
  - **Import at the boundary:**
    - A game exactly at the export limit exports to exactly 41,943,040 bytes and re-imports.
    - A minified version 1 bundle that grows past the export limit when re-serialized → 409, with no
      game created.
- **Measured as stored** (found in the final review, §2.6). Four tests, each on a game held at an
  exact headroom by a padding question plus a small probe question. Every refusal would have been
  accepted by a check on the values as sent; each was run against that measurement and failed at
  its refusal step. Every exact fit lands on the export limit to the byte.
  - `test_question_update_measured_as_stored_float`: `points_value` 1 → `1e-40` grows 2 bytes as
    sent and 8 as stored. At headroom 7 → 409, meter and question unchanged; at headroom 8 → 200,
    the response shows `9.99995e-41`, and the export is exactly the limit. Shrinking back at the
    limit is accepted.
  - `test_question_update_measured_as_stored_json`: a `config` integer changed from 2⁶⁴−1 to 2⁶⁴
    (both 20 digits, so no growth as sent) grows 2 bytes as stored. At headroom 1 → 409; at
    headroom 2 → 200 and exactly the limit.
  - `test_question_create_measured_as_stored`: a question with `points_value` `1e-40`; its stored
    growth g is measured, then at headroom g − 1 → 409 with the question count unchanged, and at
    headroom g → 201 and exactly the limit.
  - `test_import_measured_as_stored`: a version 1 bundle written exactly as the exporter would
    write it, with `points_value` `1e-40`. At 3 bytes under the limit as sent → 409, no game; at
    6 bytes under → 201, and its export is exactly the limit and equal to the bundle with the
    stored value.
- **`test_game_start_reads_fresh_questions`** (a lock test, below):
  1. The test locks the game and inserts a question in the same transaction.
  2. The host emits `host_advance` in the lobby.
  3. The processlist shows the start's lock statement.
  4. The test commits.
  5. The first `new_question`'s `totalQuestions` must include the new question, and the session is
     `IN_PROGRESS`.
- **`test_partial_update_reads_fresh_question`** (a lock test, below):
  1. A question has 4 options.
  2. The test locks the game and, in the same transaction, updates the question to 5 options and
     5 points.
  3. A `PUT` sending only 4 `answer_points` starts.
  4. The processlist shows its lock statement.
  5. The test commits.
  6. The `PUT` → 422, and the stored question keeps 5 options and 5 points.
- **`test_concurrent_start_is_ignored`** (a lock test, below):
  1. The test holds the game lock.
  2. The host emits `host_advance` twice.
  3. The processlist shows **two** lock statements, one per advance, so both are waiting in
     `start_game`.
  4. The test releases the lock.
  5. Exactly one `new_question` arrives (question 1), and neither an `error` nor a
     `question_results` arrives within 3 s. The absence check is bounded by that window.
  6. The session is `IN_PROGRESS`.
  - Why step 3 and the `question_results` check matter: a second advance that arrived after the
    first had finished would take the existing QUESTION → RESULTS branch instead. The other checks
    would still pass, and the "already started" path would never run.
- **`test_session_links_follow_current_game_access`:**
  - the session host with a grant → 200
  - grant revoked → 403
  - grant restored, then the game moved to a course the host doesn't HOST → 403
  - admin → 200
  - completed session → 200
  - Redis room deleted (helper) → still 200, or skipped with a reason
- **`test_report_withholds_images_without_game_access`:** a host who lost the game grant downloads a
  completed session's report and gets no data URIs, only the withheld text. With the grant, the
  images are embedded.
- **`test_import_counts_images_before_decoding`:** 51 entries with invalid base64 → 409 on the count,
  not 422 on base64. That tells the two orders apart without relying on timing.
- **`test_text_only_version1_import_unchanged`:** a version 1 bundle with a blank option and a
  numeric option → 201, stored unchanged.
- **`test_option_rules_apply_only_with_option_images`:**
  - text-only create with a blank option → 201
  - create with `option_images` and a blank caption without an image → 422
  - merged update adding `option_images` to a question with a blank option → 422
  - the same content in a version 2 import → 422
- **`test_pixel_limit_checked_before_decoding`:** a PNG whose header declares 8000 × 6000 (48 MP,
  between the pixel limit and Pillow's own error threshold) → 422.
- **`test_legacy_image_keys_treated_as_missing`**, with each case planted through the direct MySQL
  connection:
  - **A missing id:** `prompt_image_id: 999999` and `option_images: [999999, null]`, with non-blank
    captions.
    - The library list succeeds, with no `used_by` entry for it.
    - The export omits `prompt_image_id`, and omits `option_images` because every entry becomes
      `null`.
    - The export re-imports.
    - A started session's `new_question` has `images: {}`.
  - **A cross-game id:** another game's real image, in `prompt_image_id` and `option_images`.
    - It is absent from `new_question`'s `images`, from the report and from the host map.
    - The export omits it.
  - **All null after mapping:** every `option_images` entry unresolved, next to blank captions.
    - The export omits `option_images`, and the export re-imports (201): the question is validated
      as text-only, where blank captions are allowed.
  - **Partial:** one resolved and one unresolved entry, the unresolved one with a blank caption.
    - That option is exported with the caption "Missing image".
    - The export re-imports.
- **`test_question_order_stays_unique_under_concurrency`** (a lock test): while the test holds the
  game lock, two creates (without `order_index`) and a reorder listing the pre-existing ids are
  sent. The processlist must show **three** lock statements before the test releases the lock, so
  both creates took their snapshot before either could commit. After release (the reorder commits
  after responding, so its result is read with `wait_until`):
  - all `order_index` values in the game are unique, and the two new questions got distinct indexes
  - the reorder either succeeded before both creates, or got the existing 409
- **`test_pixel_limit_errors_are_mapped`:**
  - a header declaring 48 MP → 422 from the explicit check
  - a header declaring 90 MP → 422 from the mapped `DecompressionBombError`
  - each message states the megapixels and the limit; neither returns 500
- **`test_bundle_images_field_edge_cases`:**
  - a non-list `images` → 422
  - `[]` in version 2 → 201
  - an `images` key in version 1 → 422
- **`test_import_checks_references_before_processing`:** a version 2 bundle with an unknown key
  reference and an undecodable image → 422 on the reference.
- **`test_replace_updates_filename_keeps_description`.**

**Through nginx: `tests/integration/test_t8_nginx_limits.py`.**
- Runs only when `--nginx-url` is given (for example `http://localhost:8080`); otherwise it is
  skipped with that reason.
- Checks:
  - a 2 MiB login body → nginx 413
  - a 2 MiB question-create JSON → nginx 413
  - 10 MiB upload and replace files reach the backend (a JSON response, not nginx's HTML); 12 MiB
    → nginx 413
  - 40 MiB bundles on the host and admin import routes reach the backend; 42 MiB → nginx 413

**Opt-in load and burst checks: `tests/integration/test_t8_image_load.py`.** Runs only with
`--image-load`; otherwise it is skipped with that reason.
- **Load.**
  - **Scenario:** 150 guest players. The host starts a multiple-choice question with 4 option images.
    On `new_question`, each player fetches its 4 small links concurrently, then submits.
  - **Measured:**
    - image-fetch latency (from `new_question`) and errors: non-200 responses and timeouts
    - answer-ack latency (`submit_answer` → `answer_received`) and errors: `error` events, or no ack
      within 10 s
    - p50, p95, max and error counts for both
    - a no-image baseline run with the same players
  - **Acceptance:** zero image-fetch errors, zero answer-ack errors or timeouts, and p95 ≤ 2 s for
    both. If it fails, the first remedy is an in-process cache of small copies keyed by image id and
    version, which is safe because a version's bytes never change.
  - **Limits:** it runs from one machine in development mode, so rate limits are off and the socket
    manager is in-memory. It measures the backend and the database pool, not phone networks.
  - **The load generator must not be the bottleneck** (found in implementation). From one Python
    process, httpx tops out near 100 requests/s at this concurrency. Every endpoint then looks
    equally slow — even `/api/health` and a request refused before any database access — which
    measures the client, not the backend. ApacheBench and aiohttp both show the same backend
    serving about 500 health requests/s. The image fetches therefore use aiohttp, already an
    integration dependency through the socket.io client.
- **Burst.**
  - 6 concurrent 30 MP non-conforming uploads.
  - It records status codes (201 or 503), latencies, and the backend's peak memory sampled from
    `docker stats`.
  - These are observations, with no ceiling.

**The lock tests** — `test_room_creation_and_replace_are_serialized`,
`test_game_start_reads_fresh_questions`, `test_partial_update_reads_fresh_question`,
`test_concurrent_uploads_respect_count`, `test_concurrent_start_is_ignored` and
`test_question_order_stays_unique_under_concurrency` — share this harness.

- **Connection.** PyMySQL, with settings from environment variables:

  | Variable | Default |
  |---|---|
  | `BUZZER_TEST_DB_HOST` | `127.0.0.1` |
  | `BUZZER_TEST_DB_PORT` | `3306` |
  | `BUZZER_TEST_DB_NAME` | `buzzer` |
  | `BUZZER_TEST_DB_USER` | `app_user` |
  | `BUZZER_TEST_DB_PASSWORD` | `MYSQL_PASSWORD` from the repo `.env` |

  - The `.env` is read by `read_repo_env()`, extracted in `conftest.py` from the `admin_token`
    fixture, whose behavior is unchanged.
  - Connection options: `connect_timeout=5`, `read_timeout=30`, autocommit off, and
    `SET SESSION innodb_lock_wait_timeout = 10`.
  - If the database can't be reached, the test is skipped with that reason.
- **Serialization test setup.** A test-created game with a question whose option uses image A
  (version 1), and a second image file for the replace.
- **Serialization test steps.**
  1. The test connection runs `SELECT id FROM games WHERE id = %s FOR UPDATE`.
  2. Start `POST /api/game/rooms` and `PUT …/images/{A}` as concurrent `httpx.AsyncClient` requests
     with a 30 s timeout.
  3. Wait, bounded at 10 s, until the processlist shows both requests' lock statements (below), then
     assert that neither request has finished.
  4. Release the lock by rolling back the test transaction.
  5. Wait for both requests, bounded at 20 s.
- **Final state.** Exactly one of:
  - (a) Room 201 and replace 409 naming that room. Image A is still version 1 with its original
    `sha256`. The session's links, and its first `new_question` (started through the host socket),
    carry `/v1/`.
  - (b) Replace 200, then room 201. Image A is version 2. The session's links and its first
    `new_question` carry `/v2/` only.
- **Repeat** with the two requests started in the opposite order.
- **Cleanup**, in `finally`: roll back and close the test connection; delete the session (which
  clears its Redis state) and the game.

**What the lock tests show, and what they don't.**
- **How the lock is observed.** While the test holds the lock, it polls
  `information_schema.PROCESSLIST` for other connections of the same MySQL account running
  `SELECT … FROM games … FOR UPDATE`.
  - Without extra privileges, an account sees only its own connections, so the test must use the
    backend's account (the defaults above).
  - **Every request must be waiting before release.** Before releasing the lock, each lock test
    waits until the processlist shows one lock statement per concurrent request it started:
    - one: `test_game_start_reads_fresh_questions`, `test_partial_update_reads_fresh_question`
    - two: `test_room_creation_and_replace_are_serialized`, `test_concurrent_uploads_respect_count`,
      `test_concurrent_start_is_ignored`
    - three: `test_question_order_stays_unique_under_concurrency`
  - If it can't see all of them within 10 s, it fails with a message naming the setting to fix.
    It never falls back to timing alone.
- **Evidence of waiting.** The processlist shows the backend issued the lock statement and it hadn't
  finished while the test held the lock. Together with the requests finishing only after the
  release, that is strong evidence they waited on this lock.
- **Evidence of fresh reads.** The fresh-data tests (start, partial update, concurrent uploads)
  commit a change while the request waits, and the result must include it. A plain read or a stale
  ORM object would return the old result, so these tests fail if the fresh-read rule isn't followed.
  They don't depend on how long the request waited.
  - The order test is evidence of the same kind: both creates are waiting before release, so the
    second one's snapshot predates the first one's commit. A plain `max(order_index)` read would
    give both the same index.
- **Not proof.** The processlist shows a statement in progress, not what it is waiting for.
- **Timing alone.** A request being unfinished after a fixed time shows only that it didn't
  complete in that window; a slow request would look the same. It isn't used as evidence of locking
  on its own.
- **Correctness.** It is checked by the outcome: whatever the timing, the final state must be one of
  the two consistent outcomes above.

**Unit: `tests/unit/test_image_service.py`.**
- conformance, one test per condition
- each normalization branch
- idempotence: processing twice gives the same bytes
- the small-copy quality fallback
- token determinism, bucketing and claims
- the header pixel check runs before `load()`
- `is_animated` is used, and GIF frames are never counted
- `fresh()` compiles to `FOR UPDATE` / `LOCK IN SHARE MODE` with `populate_existing`
- **Capacity behavior**, through the real `processing_slot`, `process_upload` and
  `process_bundle_images` with an instrumented stub pipeline:
  - 5 concurrent calls → at most 2 in flight and 3 waiting; all complete after release
  - a run that raises releases its token
  - a cancelled waiter never runs
  - a waiter past a short test wait limit → `ServiceUnavailableError`
  - **deadline expiring while waiting:** a short `deadline_at` with both slots held elsewhere →
    `ServiceUnavailableError`, and no stub run starts
  - **deadline expiring during an active decode:**
    - a stub run that takes longer than the remaining deadline, on a middle image
    - the run completes (it isn't interrupted)
    - the limiter shows its token still borrowed after the deadline has passed, and released only
      after the run returns
    - `ServiceUnavailableError` is raised, and no later image runs
  - **deadline expiring during the final image's decode:** the same assertions, proving that the
    check after the last image catches it
  - **no writes after an overdue result:** called through `import_game` with the pipeline stubbed and
    a spy on the insert step; each overdue case → 503, with the spy never called and no game created.
    The deadline is passed in by the test; the production value is unchanged.

**Unit: `tests/unit/test_bundle_service.py`.**
- `image_refs` and `map_image_refs` for every role
- key assignment by id order
- version 1 output identical to today's exporter for a game without images
- `bundle_size` equals `len(serialize(build_bundle(..., include_data=True)))`, for games with and
  without images
- `check_export` for "within the limit", "over the limit but not increased", and "over the limit
  and increased"
- `bundle_size` with prompts containing `&` and `<` equals the export length after sanitizing
- tolerant `image_refs` / `map_image_refs` / `bundle_size` on malformed values
- the three export repairs (§2.7), and `bundle_size` equal to the export length for repaired legacy
  questions

**Frontend, manual (§4.12):** a multiple-choice question with one image option and a leftover
blank row saves, and the blank row is dropped.

**Deferred to T7:**
- Spot It metadata checks
- the full link appearing only in the host map
- reconfirmation and its start checks
- version 2 bundles with Spot It
- the report heatmap

### 4.12 Verification

- **CI checks, run locally:** `ruff check backend/ scripts/`, `ruff format --check backend/
  scripts/`, and `tsc --noEmit` in each app.
- **Migrations, on a disposable database.** Never downgrade the dev `buzzer` database; that would
  drop every image.
  1. Create the database and grant access:
     `docker compose exec mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE
     buzzer_migration_test; GRANT ALL PRIVILEGES ON buzzer_migration_test.* TO
     '"'"'app_user'"'"'@'"'"'%'"'"';"'`
  2. Confirm the connection URL has no query string: `docker compose run --rm backend sh -c 'echo
     ${DATABASE_URL##*/}'` must print `buzzer`.
  3. Run the migrations against the disposable database:
     `docker compose run --rm backend sh -c 'export
     DATABASE_URL="${DATABASE_URL%/*}/buzzer_migration_test"; alembic upgrade head && alembic
     current && alembic downgrade -1 && alembic current && alembic upgrade head && alembic check'`.
     Expected: `005`, then `004`, then `005`, and no drift.
  4. `SHOW CREATE TABLE images` in `buzzer_migration_test` shows `mediumblob` and
     `ON DELETE CASCADE`.
  5. `DROP DATABASE buzzer_migration_test`.
- **Dev database:** `alembic upgrade head` only.
- **Test suites:** `cd tests/unit && pytest -v`, then `cd tests/integration && pytest -v` with the
  stack up. Existing tests must remain intact.
- **Supported nginx setup.**
  - The compose `nginx` service (port 8080) mounts `nginx/nginx.dev.conf`, the only nginx
    configuration in the repository.
  - T8 adds these locations, each repeating the `/api/` block's proxy settings:
    - `location ~ ^/api/host/games/[0-9]+/images(/[0-9]+)?$` with `client_max_body_size 11m`, for
      upload and replace
    - `location = /api/host/games/import` and `location = /api/admin/games/import` with
      `client_max_body_size 41m`
    - the `/api/` block states nginx's default explicitly: `client_max_body_size 1m`
  - nginx checks exact matches first, then regular expressions, then the `/api/` prefix, so these
    rules win for their paths.
  - `nginx/Dockerfile` copies `nginx/nginx.prod.conf`, which doesn't exist in the repository, so
    that image doesn't build today. This is an existing problem outside T8 (§5); T8 neither creates
    nor edits that file.
  - Any other proxy in front of the backend, such as the oauth2-proxy setup described in
    `backend/app/CLAUDE.md`, needs the same per-route limits.
- **nginx configuration:** after editing `nginx.dev.conf`, run `docker compose exec nginx nginx -t`.
- **Required lock tests.** The lock-harness tests use the MySQL connection on the existing `3306`
  mapping. If they are skipped, **verification is incomplete**, not passed.
- **Load and burst checks:** run once before merge (`pytest test_t8_image_load.py --image-load`),
  and record their numbers in the merge request description.
- **Checks through nginx.**
  - After `npm run build`, with the compose stack up, run `cd tests/integration && pytest
    test_t8_nginx_limits.py --nginx-url http://localhost:8080`.
  - The rest of the suite calls port 8000 directly and never exercises nginx; direct requests have
    no body limit (§2.4).
- **Manual run** under the nginx build (`npm run build`, then `http://localhost:8080`), with a phone
  on the LAN joined as a guest:
  - upload an image over 1 MiB
  - image options on the phone and the projector
  - prompt images on all four question types
  - the live-session banner
  - export and import of a budget-sized library through nginx (also covered by the nginx test
    module)
  - download a report for a game with multiple-choice and multi-select image options, and check both
    render their images
  - a blocked image URL showing the placeholder
  - a multiple-choice question with one image option and a leftover blank row saves (the row is
    dropped)
  - a 503 shown as the server's message when processing is busy (force it by holding both slots
    during the burst check)
  - `scripts/simulate_players.py` playing an image-option game
- **A phone needs the laptop's LAN address** (found in the manual run). Open the host at
  `http://<LAN address>:8080/host/`, because the QR code encodes the host page's own address, and
  add `http://<LAN address>:8080` to `CORS_ORIGINS` for the run, because socket.io refuses any
  other origin. `.env` reaches the backend only when its container is recreated
  (`docker compose up -d --no-deps --force-recreate backend`); remove the entry, and recreate the
  container again, afterwards.

**Manual run results (2026-09-29).**
- **Scripted browser runs** (Playwright driving Chrome through nginx; the projector at 1440×900,
  the phone emulated at 390×844) covered every item above except the physical phone:
  - a 3.28 MiB upload through the editor
  - image options on both screens
  - prompt images on all four question types
  - the live-session banner
  - export and import through the editor
  - a report whose multiple-choice and multi-select image options both render
  - a blocked link showing the placeholder
  - a leftover blank row dropped on save
  - The busy 503 couldn't be forced as planned: six 30 MP uploads finish in about 1.5 s on two
    slots, well inside the 20 s wait (§4.15). It was checked by answering an upload with the
    backend's exact 503 body. The editor showed the server's message, and a retry uploaded once.
  - `simulate_players.py` played an image-option game with 12 players to the end.
- **Desktop Chrome check (user-reported).** Run by the user at `http://localhost:8080`, with the
  player at 390×844. It reported these as passing:
  - image rendering, including the option with no caption
  - multi-select toggles
  - results thumbnails
  - the final 3-point score
  - Two same-browser tabs share one sign-in (an existing behavior; see `frontend/CLAUDE.md`). A
    guest joining in one tab therefore replaced the host's sign-in. Only a clean repeat with one
    account counts.
- **Physical phone (user-reported).** Run by the user on an iPhone, through the laptop's LAN
  address, with the prepared test game.
  - The checklist it was given covered QR joining, image loading, answering (including the
    option with no caption and the multi-select toggles), leaving and returning to the browser
    mid-question, results, and the projector's thumbnails.
  - The user reported the check as passed, not step by step.
  - The iPhone model, iOS version and browser version weren't recorded.
  - Afterwards, the temporary `CORS_ORIGINS` entry was removed and the backend recreated.
- **Test game kept for re-runs:** "T8 phone check" (a prompt image; multiple-choice image options,
  one without a caption; multi-select image options), in the development database.

### 4.13 Implementation order

1. Dependencies, model, migration, exceptions (including `ServiceUnavailableError`), `game_lock`,
   `image_refs`, `bundle_service`, the pipeline, `PIPELINE_LIMITER` and `processing_slot`; unit
   tests.
2. Tokens, the serving router, library endpoints and operations.
3. Reference rules, the export rule and the lock in question and game writes, including
   `create_question`'s ordering from the fresh list and the `reorder_questions` lock; the
   `create_room`/`start_game` locks; the room-create commit.
4. The gateway `images` map; session links; projector and phone rendering; `SafeImage`.
5. The editor panel and picker; `formToPayload` changes, including filtered option rows when an
   option has an image.
6. Export/import version 2, capped reads, nginx; the report.
7. Integration tests; the load and burst checks; docs and context files (§4.14).

### 4.14 Documentation and context files

- `README.md`: the version 2 bundle format, the §2.4 limits, rebuilding after the Pillow addition,
  and the per-route nginx limits in §4.12.
- `docs/realtime.md`: the `images` map in `new_question`, and the projector's session link map.
- `CLAUDE.md` updates. Edit only what's stale, per `.claude/rules/context-sync.md`:
  - `backend/`:
    - Pillow.
    - FastAPI reads request bodies before dependencies run, so nginx's per-route limits are the
      only body-size gate.
    - Any new route that accepts large bodies needs its own nginx location (§4.12); otherwise it
      keeps the 1 MiB default.
  - `backend/app/`: the new router and services, migration `005`, the lock-order and
    image-reference invariants, the §4.4 module dependencies, and a question-checklist entry for
    image references.
  - `backend/app/models/`, `routers/`, `services/`, `schemas/`, `common/`, `websocket/`.
  - `frontend/`: image links as a cross-app convention; `SafeImage` duplicated in two apps.
  - `frontend/host/src/` and its `pages/`, `components/` and `lib/`.
  - `frontend/player/src/` and its `pages/` and `components/`.

### 4.15 Implementation considerations (verify; not adopted)

The final re-check (2026-09-28) raised these. None is part of the design. Check each during
implementation; if one proves real, revise this document first, as for any flaw.

- **Pooled connections during processing.** An upload keeps its database connection, and its
  snapshot, from the access check through the slot wait and processing; an import keeps one for up
  to the import processing deadline. `database.py` uses SQLAlchemy's default pool (5 + 10 overflow,
  30 s timeout), shared with gameplay. Verify whether this matters under realistic concurrent
  authoring; an early commit after the access check is one possible remedy, not adopted (§3.12).
  *Checked in implementation:* no pool timeouts in the burst check or the 150-player load check.
  Still not adopted: about 15 uploads waiting at once (the pool's 5 + 10) would make every other
  request, gameplay included, wait for a connection, but one editor uploads one file at a time.
  The serving endpoint, whose requests are many and can be slow on phone networks, does release
  its connection before sending (§4.7).
- **JPEG comments on re-encode.** *Verified in implementation step 1: real.* Pillow 12.3's JPEG
  encoder writes `im.info["comment"]` back unless a comment is passed, and `info` survives
  `copy()` and resizing. Resolved in §4.6 (the output's `info` is cleared before encoding). The
  unit case "a JPEG with a COM segment normalizes to conforming bytes" guards it.
- **Forcing a 503 in the manual run.** §4.12's "hold both slots during the burst check" may not
  produce a 20 s wait, since six decodes on two slots usually finish sooner. Find a reliable way
  (for example a temporarily lower slot wait limit in a dev run) or rely on the unit tests and the
  503 message in `lib/api.ts`. *Outcome:* the burst can't produce one (six 30 MP uploads finish
  within about 1.5 s on two slots; the wait limit is 20 s). The unit tests cover the server side
  (both slots held by gates). The editor's side was checked in a browser by answering an upload
  with the backend's exact 503 body: the editor showed the busy message, and a retry uploaded once.
- **Test plumbing.** The "no writes after an overdue result" unit test passes its own deadline,
  but `import_game`'s signature (§4.8) has none: a keyword-only override or a patched constant
  both work. The module-level `PIPELINE_LIMITER` may need replacing per test, because
  pytest-asyncio gives each test a new event loop. *Step 1:* `processing_slot` reads the
  module-level limiter at call time, and the unit tests replace it with a fresh one per test.
  *Later steps:* the deadline arithmetic reads the clock through one module-level function in
  `image_service` (`_clock`, which is `anyio.current_time`), and `import_game` takes its deadline
  from `image_service.import_deadline()`. The deadline tests then move a fake clock past the
  deadline while a gated decode is running, instead of racing real sleeps. That was a reported
  source of flakiness. Slot waits still use real time, but only where both slots are held by
  gates, so a timeout is certain.
- **A non-list `option_images` in a version 2 bundle.** *Adopted:* §2.15 step 5 now refuses it
  (422) before mapping, instead of letting `map_image_refs` turn it into `None` and drop it.
- **Scope of the order-uniqueness claim** (§2.6). It covers creates without an explicit
  `order_index`. The API still accepts one; the editor never sends one.
- **nginx location breadth.** The upload/replace regex (§4.12) also matches GET, PATCH and DELETE
  on those paths, so a description PATCH could carry up to 11 MiB. Harmless, but broader than
  "upload and replace".
- **Byte identity with today's exporter** (§4.8, unit test in §4.11). It holds for games whose
  configs have no image keys. A stored explicit null reference or an all-null `option_images` is
  dropped by the export repairs, so such a game's version 1 bytes differ from today's.

---

## 5. Existing limitations and risks

**Existing limitations, recorded and not fixed by T8:**
- `answer_received` sends correctness and points to the submitting phone immediately, for every
  question type. It's hidden on screen but visible in developer tools.
- Question edits during a live session aren't blocked; image replace and delete are (§2.13).
- Refreshing the projector during results loses the results.
- Reports are rebuilt from current data, not stored per session (§2.14).
- `nginx/Dockerfile` copies `nginx/nginx.prod.conf`, which isn't in the repository.
- The report has no answer-distribution chart for multi-select questions. T8 adds only a labelled
  option list with thumbnails (§2.14).
- Requests that reach port 8000 directly (the Vite dev proxy, the integration suite, the published
  backend port) have no body-size limit; only nginx enforces one.
- The report's text is rebuilt from current content for anyone who may download it. Its images are
  withheld without current game access (§2.14).
- A socket admitted to a room keeps receiving `new_question`'s small option links after its access
  is revoked, until it disconnects; membership is checked only at join (existing gateway behavior,
  §2.9).
- The socket's host check (`user_can_host_session`) doesn't include the game grant. A host who loses
  only the game grant, or whose game moves to another course while they keep HOST on the session's
  course, can still rejoin and drive that session. Only their REST image links stop (§2.9).
- A lock-wait timeout or deadlock during `start_game` reaches the projector as the existing fatal
  `error`. It's unlikely, because nothing holds the game lock for long.
- **Question rows over about 256 KB break SQL sorts that select them** (found in T8
  implementation; MySQL error 1038, see §4.6). T8's own reads sort in Python. The pre-existing
  game-over summaries (`game_service.get_player_question_summary` and
  `get_host_question_summary`) still `ORDER BY order_index` while selecting `config`, so a game
  with such a question fails at game over. Not changed by T8.

**Follow-ups found during the manual run** (in `nginx/nginx.dev.conf`, which T8 changed only for
`/api/` routes; each is separate and not fixed by T8):
- **An app path without its trailing slash is a 404.** `/player`, `/host` and `/admin` return 404;
  only `/player/`, `/host/` and `/admin/` are served. The lobby tells players to type
  `<address>/player`, which is one of the broken paths (`LobbyPage.tsx`). The QR code isn't
  affected, because it links to `/player/join?code=…`. Possible fixes: `location = /player
  { return 302 /player/; }` for each app, or show `/player/` in the lobby.
- **The bare address redirects to the wrong port.** `location = /` returns `302 /player/`. nginx
  makes that absolute from its own container port, 80, so `http://<address>:8080/` redirects to
  `http://<address>/player/`, and the connection fails. `absolute_redirect off;` would send a
  relative `Location` and keep the port.

**Risks to watch during implementation and testing:**
- **Load** when many phones fetch option images at once, including **database pool contention**
  between image serving and answer handling. The opt-in load check measures it against its
  acceptance criterion (§4.11); the first remedy is the small-copy cache.
- **nginx 504 and late completion remain possible.**
  - The slot wait limit bounds only waiting. The import deadline is checked after each image and can
    be overrun by one image's processing.
  - Processing, database lock waits and the writes are bounded by neither.
  - When nginx times out first, the backend may still complete and commit, and a retry can create a
    duplicate image or imported game. The editor's reload shows a duplicate image; a duplicate game
    appears on the Games page.
  - Accepted for this project; no separate worker process is added to enforce a hard deadline.
- **Memory isn't bounded by the processing slots.** An import holds its parsed bundle and decoded
  bytes (several times the bundle's size, unmeasured) outside the slots, and the container has no
  memory limit. The burst check observes upload peaks only.
- **Link exposure:** links appear in nginx access logs until they expire.
- **Lock waits** when writes to one game coincide. A stuck lock or a deadlock surfaces as a 409
  (§2.6).
- **Deferred formats:** MPO and HEIC photos are refused with 415.
- **Links outlive revoked access:** already-issued links stay valid until they expire (at most
  3 hours) after access is revoked (§2.9).
- **Pillow upgrades** could change encoder output. Stored bytes are unaffected, since conforming
  bytes pass through, but normalizing the same input after an upgrade could give different bytes.
- **T9** must theme the new surfaces.
