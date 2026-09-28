# T8 — Image Support in Questions

**Destination:** `docs/plans/t8-image-support.md`
**Status:** **Not yet Goldfish-tested.** Commit before any implementation (T3). If implementation
reveals a flaw, revise this document first.
**Read first:** `backend/app/CLAUDE.md` (architecture, invariants, question-type checklist),
`backend/app/services/CLAUDE.md`, `backend/app/routers/CLAUDE.md`, `backend/app/websocket/CLAUDE.md`,
`frontend/CLAUDE.md`, and the `pages/` context files of the host and player apps.
**Related:** T7's Spot It question depends on this feature. T7 is designed separately and is not
part of this work; §2.16 records only what T8 must supply to it.

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
| `filename` | varchar(255) | original basename; shown only in the editor |
| `description` | varchar(300), NOT NULL | required, trimmed, non-empty |
| `version` | int, NOT NULL, default 1 | increases by 1 on every replace |
| `replaced_at` | datetime, NULL | time of the last replace |
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
| Pixel limit | 40 MP | the largest decoded image |
| Full-image edge | 1,600 px | the longest side of a stored full image |
| Full-image size | 4 MiB (4,194,304 bytes) | the largest stored full image |
| Small-copy edge | 400 px | the longest side of a small copy |
| Small-copy size | 1 MiB (1,048,576 bytes) | the largest stored small copy |
| Image count | 50 | images per library |
| Library budget | 24 MiB (25,165,824 bytes) | the sum of `byte_size` (full images only) per library; small copies are not counted |
| Export limit | 40 MiB (41,943,040 bytes) | the largest export a write may leave a game with (§2.6) |
| Import limit | 40 MiB (41,943,040 bytes) | the largest bundle file import accepts |
| nginx body limit | 50 MiB, on `/api/` | above the import limit, leaving room for multipart overhead |

- Uploads are read through a capped reader (limit + 1 byte), never read whole first.
- One image row holds at most 5 MiB of blobs (full-image size + small-copy size), which is below
  MySQL's default `max_allowed_packet` (64 MiB).
- A library's blobs total at most 74 MiB: the library budget plus 50 small copies.

### 2.5 Image processing and conformance

Every image entering the system — upload, replace, or import — goes through this pipeline. It is the
only code that accepts image bytes.

1. **Size.** Over the input limit → **413**.
2. **Format.** Pillow identifies the format from the bytes themselves. Accepted inputs are PNG, JPEG,
   WebP and GIF; anything else → **415**, including SVG, HEIC, TIFF, text and empty files. SVG is
   refused deliberately: served from the apps' own origin, it can carry a script that reads the
   `localStorage` token.
3. **Decode.** A complete decode must succeed, within the pixel limit and with no decompression-bomb
   warning; otherwise **422**.
4. **Conformance.** The bytes *conform* only if all of the following are true:
   1. the format is PNG, JPEG or WebP
   2. there is exactly one frame
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
   - the result must conform (step 4), otherwise **413**
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

A failed check → **409**, naming the limit and the value the change would have produced.

**Export size.**
- `export_size(G)` is the exact byte length of the bundle the exporter produces for game G, in the
  §2.15 format.
- It is computed without building any base64: serialize the bundle with the exporter's own
  serializer (`bundle_service`, §4.5) and every image's `data` left empty, then add
  Σ 4·⌈`byte_size`/3⌉.
- That is exact, because base64 characters need no JSON escaping.

**Export rule.** A write that can change a game's exported content is accepted only if, after the
write, either:
- `export_size` is within the export limit, or
- `export_size` did not increase.

Otherwise it is refused with **409** and nothing changes.

- Writes covered:
  - question create and update
  - game title, description and max-players update
  - image upload, replace, and description edit
  - import, measured on the game as it would be stored
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

**Concurrent writes.**
- **Game row lock.** Every covered write, and every image delete, first locks the game's row
  (`SELECT … FOR UPDATE` on `games`).
  - Writes to one game therefore run one at a time; different games don't wait on each other.
  - The lock is held until the transaction ends, so the state that was checked can't change before
    the write commits.
- **Locking reads.** The check's inputs, the game's images and questions, are read with locking
  reads, which in InnoDB always see the latest committed rows.
  - A plain read could reuse the snapshot taken earlier in the same transaction, because MySQL's
    default isolation is REPEATABLE READ and the access check has already read.
  - It would then miss a write committed while this one waited for the lock.
- **References.** The same lock serializes the same-library reference check (§2.7) with image
  delete, so a question can't gain a reference to an image that is being deleted.
- **Import.** Creates a new game, so it doesn't lock an existing one.
- **Sessions and image versions.** A session must never start with an image version that changes
  underneath it. Stale links can't be relied on to fail, because a cached `immutable` URL is never
  requested again, and phones retry the same link.
  - `game_service.create_room` and `game_service.start_game` take the same game row lock, right
    after their existing access checks and before they create the session or change its status.
  - Room creation, game start, image writes and question writes for one game therefore run one at
    a time.
  - The live guard (§2.13) reads the game's sessions with a locking read under that lock.
    `create_room` writes the room's Redis state **before** its transaction commits, and that order
    must be kept.
  - A replace or delete therefore either:
    - commits before the room is created, and the room uses the new version from the start; or
    - waits for the room's creation to commit, sees a live session, and is refused (409).
  - From the moment a room exists until its session stops being live, **no image of the game changes
    version**. Every link the session receives stays correct for its whole life, so `immutable`
    caching and the phones' same-link retry are safe.
  - The room-create endpoint (`POST /api/game/rooms`) commits before responding. The host only
    receives a room code once the session is visible to the live guard, and the lock is released
    promptly.
  - Uploads are processed (§2.5) **before** the lock is taken, so image decoding never holds it.
  - **T7:** its confirmation checks go immediately after this lock in `create_room` and
    `start_game`. They then see the same image versions and grids the session will use, and no image
    or question write can interleave.
  - Question edits during a live session remain unblocked (an existing limitation, §5); T7 decides
    whether Spot It needs more than its start-time checks.

**A refused change leaves existing data intact.**
- Every check runs before the change is applied to any row.
- A refusal raises an application error, and the request's transaction rolls back (`get_db` rolls
  back on any exception).
- A replace prepares the new bytes and small copy in memory, and updates the row only after every
  check has passed.
- An import validates the whole bundle, and the game it would create, before creating any row, all
  in one transaction.
- The image endpoints commit before responding, so a commit failure is reported as an error rather
  than after a success response.

### 2.7 How questions reference images

References are ids stored in `config`:

| Key | Allowed on | Shape |
|---|---|---|
| `config.prompt_image_id` | **every** question type | int, or absent/null |
| `config.option_images` | `multiple_choice` and `multi_select` only | a list the same length as `config.options`; each entry an int or null |
| `config.image_id` | reserved for T7's Spot It | int |

- **Options.** Every option needs a non-blank caption **or** an image. Captions stay in
  `config.options` as strings, with `""` allowed only when the option has an image. Every existing
  reader of `options` keeps working, and version 1 bundles are unaffected.
- **Other types.** `option_images` on any other question type → 422.
- **Same game only.** Every referenced id must be in the **same game's library**, otherwise 422
  "Image N isn't in this game's library". The message is identical for a missing id and another
  game's id, so a host can't probe other courses' ids. Without this check, a host could reference
  another course's image and have it served.
- **Where the checks run.**
  - Shape rules are in `QuestionCreate.validate_structure`.
  - The library check (`image_service.assert_in_library`) is called by `game_management_service`,
    under the game row lock (§2.6).
  - Both run on create, on update (against the merged question) and on import, which are all three
    question write paths.
- **What reaches phones.** `config` goes to phones unchanged. Image ids aren't sensitive; filenames
  never leave the editor.
- **One definition of the reference fields.** The keys in this table are defined once, in
  `services/image_refs.py` (§4.5). T7 adds `image_id` handling there.

### 2.8 Management API and authoring

These endpoints are on the host router. Each requires a signed-in account that isn't a guest
(`require_user`) and passes the game rule, so admins use them too. An image id from another game
→ 404.

| Endpoint | Purpose | Main errors |
|---|---|---|
| `GET /api/host/games/{game_id}/images` | the library: metadata, links, where each image is used, live sessions, budget and export-size usage | 403 |
| `POST /api/host/games/{game_id}/images` | upload (multipart `file` + `description`) | 413, 415, 422, 409 (budget or export) |
| `PUT /api/host/games/{game_id}/images/{image_id}` | replace the bytes (multipart `file`) | 413, 415, 422, 409 (budget, export or live) |
| `PATCH /api/host/games/{game_id}/images/{image_id}` | edit the description | 422, 409 (export) |
| `DELETE /api/host/games/{game_id}/images/{image_id}` | delete | 409 (referenced or live) |

In the editor:
- **Images panel.**
  - Each image shows its thumbnail, description, dimensions and size, and where it's used ("Used by
    Q2 (prompt), Q5 (option B)", or "Unused").
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
  - Errors: invalid or expired token → 403; missing image or superseded version → 404.
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
- **Accepted exposure.** Anyone holding a link can use it until it expires, and links can appear in
  nginx access logs.

### 2.10 Delivering links to each screen

| Screen | Receives | How | Reconnect and refresh |
|---|---|---|---|
| Phone | small links for option images only | an `images` map `{id: {url, width, height, description}}` added to `new_question` | a reconnecting player who hasn't answered already receives a late-join `new_question`, built the same way, so it carries fresh links; answered players need none, since their results are text; a failed load retries the same link once, which is sound because the link stays valid for at least 2 hours after it is sent (longer than any question's 300 s maximum) and no image changes version during a live session (§2.6) |
| Projector | full and small links for every image in the game | new `GET /api/game/sessions/{session_id}/images`, guarded by `require_user` + `user_can_host_session` (the rule that seats a HOST on the socket) | fetched on mount (covers a page refresh), after a socket reconnect, when a question uses an image id missing from the map, after an image error that survives a retry, and within 10 minutes of `expires_at` |
| Editor | full and small links for the library | the list endpoint and mutation responses | reload the list after an error or near expiry |

- Phones never receive links for prompt images or canvas images.
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

- Letters are identical on every screen, in the report and in the editor preview.
- The projector's fixed QR/room-code panel must not cover images.
- The prompt text still hides when the question locks, as it does today.

### 2.12 Failed loads and accessibility

- **Loading and failure.**
  - Every displayed image shows a loading placeholder.
  - On error it retries once. On the host and editor screens, a stale or expired link is first
    replaced with a fresh one.
  - If it still fails, it shows a visible "Image unavailable" placeholder with the image's
    description, plus a Retry button on the host and editor screens. The browser's broken-image icon
    never appears.
- **Failures never block play.** An option whose image failed stays selectable by its letter and
  caption. A failed prompt image never blocks the game.
- **Letter, caption and description are separate.**
  - The letter is always visible.
  - The caption is optional visible text.
  - The description is the alt text and accessible name, for example "Option B: {caption}. Image:
    {description}". It is visible only in the editor and in the failure placeholder.
  - The editor asks authors to describe what the image shows, not which answer is correct.
- **Limitation.** Alt text makes prompt and option images usable with a screen reader. It does
  **not** make a visual-search canvas question (T7's Spot It) accessible, and T7 must say so.

### 2.13 Replace, delete, and live games

- **Replace.**
  - Runs the pipeline (§2.5) and the budget checks (§2.6). The id stays the same, the version
    increases by 1, and `replaced_at` is set to now.
  - Every question using the image shows the new one on its next load.
  - Any aspect ratio is accepted. The consequences for T7's canvas questions are T7's (§2.16).
- **Delete.** Refused (409) while any question uses the image as its prompt image, an option image
  or a canvas image; the message lists the question numbers and roles. Otherwise the row is removed.
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
  - Options are labelled letter + caption.
  - Each embedded image is within the small-copy size.
- **After a replace.** The report is built from the current questions and images when it's
  downloaded; it isn't stored per session.
  - It shows the new image; the old bytes no longer exist.
  - Each image replaced after that session was created (`replaced_at` later than the session's
    `created_at`) carries the note "Replaced after this session — players saw an earlier version."
  - If an image was removed from a question and then deleted, that can't be detected: the report
    simply shows no image, as it already does for edited prompt text.
  - A faithful historical report would need per-session question snapshots, which don't exist (out
    of scope).

### 2.15 Export and import

**Bundle format.** This is the only definition of it.
- A game with an empty library exports exactly as today, as version 1.
- Otherwise it exports version 2, which includes the whole library, unused images too.
- Each entry in `images` has **exactly four fields**: `key`, `filename`, `description` and `data`
  (base64 of the stored full-image bytes).
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

Version 2 checks, all run before anything is created:
- **Image objects.**
  - Each has exactly the four fields; unknown fields → 422.
  - `key` matches `^[A-Za-z0-9_-]{1,64}$` and is unique.
  - `data` is strict base64 that decodes to within the input limit.
  - The decoded bytes pass the full pipeline (§2.5); errors are prefixed "Image img-3: …".
  - `description` passes the upload rules; `filename` becomes a display-only basename (control
    characters removed, at most 255 characters).
- **The game as it would be stored.**
  - The budget checks and the export rule are met (§2.6).
  - Every question reference is a known key, and every question validates.

Then the game, its images and its questions are created in one transaction, with keys mapped to new
ids. Any failure creates nothing.

**Round trips.** Full images that conform are stored unchanged, so exporting, importing and exporting
again reproduces the same image bytes and, for the same game content, the same bundle. Small copies
are regenerated on import and aren't part of the bundle.

### 2.16 T7 integration contract

T7 (two new question types, including the canvas type Spot It) is designed separately. This section
records only its interface with T8.

**T8 supplies:**
- `images.version` and `replaced_at`
- `image_service.get_image_meta(db, game_id, image_id)`, returning id, version, width, height and
  description for an image in the same game, for T7's create, update and import checks
- `config.image_id` in `image_refs` (§4.5), so the reference scan, the delete refusal and the bundle
  remap recognize it
- full-size links only in the host's session link map, never in phone payloads
- `image_service.data_uri(image, variant)` for the report
- replacement at any aspect ratio; the live guard; the export rule for T7's question writes
- the game row lock in `create_room` and `start_game`, taken before any T7 confirmation check, so
  those checks see the image versions and grids the session will use (§2.6)

**T7 owns** (recorded here as requirements; not built in T8):
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
    unconfirmed. The check runs immediately after the game row lock that T8 adds (§2.6).
- **Accessibility.** A statement that Spot It is visual-only (§2.12).

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

---

## 4. Detailed Implementation

§4.1 lists every file. Later subsections say what changes in each. The rules themselves are in §2 and
aren't repeated. Work proceeds in the order of §4.13.

### 4.1 File inventory

**Backend**

| File | Status | Change |
|---|---|---|
| `backend/requirements.txt` | changed | add `Pillow` (§4.2) |
| `backend/app/models/image.py` | new | `Image` (§4.3) |
| `backend/app/models/game.py` | changed | `Game.images` (§4.3) |
| `backend/app/models/__init__.py` | changed | register `Image` (§4.3) |
| `backend/app/migrations/versions/005_images.py` | new | the `images` table (§4.3) |
| `backend/app/common/exceptions.py` | changed | 413 and 415 errors (§4.4) |
| `backend/app/services/game_lock.py` | new | `lock_game` (§4.4) |
| `backend/app/services/image_refs.py` | new | the reference fields: scan and rewrite (§4.5) |
| `backend/app/services/bundle_service.py` | new | bundle builder, serializer, `bundle_size`, export rule (§4.5) |
| `backend/app/services/image_service.py` | new | pipeline, library operations, links, live sessions, budgets (§4.6) |
| `backend/app/services/auth_service.py` | changed | `create_image_token` (§4.7) |
| `backend/app/services/game_management_service.py` | changed | locks, reference and export checks, export/import version 2 (§4.8) |
| `backend/app/services/game_service.py` | changed | lock in `create_room` and `start_game` (§4.8) |
| `backend/app/services/report_service.py` | changed | images in the report (§4.9) |
| `backend/app/schemas/admin.py` | changed | reference shapes, caption-or-image (§4.9) |
| `backend/app/schemas/image.py` | new | image API and bundle-image schemas (§4.9) |
| `backend/app/routers/host.py` | changed | five image endpoints; capped import read (§4.9) |
| `backend/app/routers/admin.py` | changed | capped import read (§4.9) |
| `backend/app/routers/game.py` | changed | session links; room-create commit (§4.9) |
| `backend/app/routers/images.py` | new | link serving (§4.7) |
| `backend/app/main.py` | changed | mount the images router (§4.7) |
| `backend/app/websocket/gateway.py` | changed | async `_question_payload` with the `images` map (§4.9) |

**Infrastructure**

| File | Status | Change |
|---|---|---|
| `nginx/nginx.dev.conf` | changed | `client_max_body_size 50m` on `/api/` (§4.2) |

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
| `pages/game/GameLayout.tsx` | changed | session link map in `GameContext` (§4.10) |
| `pages/game/QuestionPage.tsx` | changed | prompt image, option grid (§4.10) |
| `pages/game/ResultsPage.tsx` | changed | thumbnails and letters (§4.10) |
| `pages/game/GameOverPage.tsx` | changed | thumbnails and letters (§4.10) |

**Player app (`frontend/player/src/`)**

| File | Status | Change |
|---|---|---|
| `types/game.ts` | changed | config fields; `images` map (§4.10) |
| `components/ui/SafeImage.tsx` | new | touch copy (§4.10) |
| `pages/game/QuestionPage.tsx` | changed | image option tiles (§4.10) |
| `pages/game/ResultsPage.tsx` | changed | letter + caption labels (§4.10) |
| `pages/game/GameOverPage.tsx` | changed | letter + caption labels (§4.10) |

**Tests**

| File | Status | Change |
|---|---|---|
| `tests/integration/requirements.txt` | changed | add `Pillow`, `PyMySQL` (§4.2) |
| `tests/integration/conftest.py` | changed | extract `read_repo_env()` from `admin_token`, behavior unchanged (§4.11) |
| `tests/integration/test_t8_images.py` | new | integration tests (§4.11) |
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
- the gateway's `start_game` call site (the lock is inside `start_game`)

### 4.2 Dependencies and infrastructure

- `backend/requirements.txt`: add `Pillow`, a release whose wheels include WebP support. The backend
  image must be rebuilt (`docker compose up --build`). Unit tests run in the backend virtualenv,
  which gets Pillow from the same file.
- `nginx/nginx.dev.conf`: `client_max_body_size 50m;` in the `/api/` location (the nginx body limit,
  §2.4).
- `tests/integration/requirements.txt`: add `Pillow` and `PyMySQL`.

### 4.3 Data model and migration

- `backend/app/models/image.py`: `Image`, with the columns of §2.3.
  - `data` and `small_data` use `sqlalchemy.dialects.mysql.MEDIUMBLOB` with `deferred=True`.
  - A `game` relationship back to `Game`.
- `backend/app/models/game.py`: `Game.images` — `cascade="all, delete-orphan"`,
  `passive_deletes=True`, ordered by `Image.id`.
- `backend/app/models/__init__.py`: import `Image` after the `game` models.
- `backend/app/migrations/versions/005_images.py`:
  - revision `"005"`, down_revision `"004"`, with a docstring header in the style of `004`
  - `upgrade()` creates `images`, with index `ix_images_game_id` created before FK
    `fk_images_game_id` → `games.id` `ON DELETE CASCADE` (as `004` does); blob columns
    `mysql.MEDIUMBLOB()`
  - `downgrade()` drops the FK, the index and the table

### 4.4 Errors, the game lock, and module dependencies

- `backend/app/common/exceptions.py`: `PayloadTooLargeError` (`PAYLOAD_TOO_LARGE`, 413) and
  `UnsupportedMediaTypeError` (`UNSUPPORTED_MEDIA_TYPE`, 415), both `BuzzerError` subclasses. The
  existing handler formats them.
- `backend/app/services/game_lock.py`: `lock_game(db, game_id) -> Game`.
  - Runs `SELECT … FROM games WHERE id = :id FOR UPDATE`, with `populate_existing` so the returned
    object is fresh.
  - A missing game → `NotFoundError`.
  - MySQL's lock-wait timeout (error 1205) becomes `ConflictError("This game is being changed by
    another request; try again.")`.
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
  `prompt` (`prompt_image_id`), `option` (`option_images[i]`) or `canvas` (`image_id`).
- `map_image_refs(config, fn) -> dict`: a copy of `config` with every reference replaced by
  `fn(value)`; nulls stay null.
- T7 extends only this module to add `image_id` behavior.

**`backend/app/services/bundle_service.py`** — pure: no database, no I/O. It holds the export limit
and import limit constants (§2.4).
- `serialize(bundle) -> bytes`: the only bundle serializer. It is exactly today's
  `json.dumps(bundle, indent=2, ensure_ascii=False).encode()`.
- Plain input records:
  - `GameMeta(title, description, max_players)`
  - `QuestionFields(type, grading_type, prompt, config, answer_data, time_limit_seconds,
    points_value)`, in order
  - `ImageRecord(id, filename, description, byte_size, data)`, where `data` is `bytes | None`
- `build_bundle(meta, questions, images, *, include_data) -> dict`: the §2.15 bundle.
  - Version 1, exactly today's structure, when `images` is empty.
  - Otherwise version 2: keys `img-1`, `img-2`, … by id order; `data` is base64 when `include_data`,
    else `""`; each question's `config` rewritten to keys with `map_image_refs`.
- `bundle_size(meta, questions, images) -> int`:
  `len(serialize(build_bundle(..., include_data=False)))` + Σ 4·⌈`byte_size`/3⌉.
- `check_export(before, after)`: raises `ConflictError` naming both sizes when
  `after > export limit` and `after > before` (§2.6).

### 4.6 `backend/app/services/image_service.py`

Its constants carry the §2.4 image limits. Contents:

- **Reading uploads.** `read_capped(upload, limit)` reads at most `limit + 1` bytes; anything more →
  `PayloadTooLargeError`. Routers use it with the input limit for images and
  `bundle_service`'s import limit for bundles.
- **Pipeline.** `process_image(raw) -> ProcessedImage` implements §2.5 exactly, returning `data`,
  `small_data`, `content_type`, `width`, `height`, `byte_size` and `sha256`. `conforms(img, raw)`
  implements §2.5 step 4.
  - The pipeline is synchronous and CPU-bound. Async callers run it with `anyio.to_thread.run_sync`
    so the event loop, which also serves the game's sockets, stays responsive.
  - **Decode limits.** `Image.MAX_IMAGE_PIXELS` is set to the pixel limit;
    `DecompressionBombWarning` is turned into an error inside a `warnings.catch_warnings()` block;
    `img.load()` performs the full decode.
  - **Metadata test.** No `info` keys `exif`, `xmp`, `XML:com.adobe.xmp`, `photoshop` or `comment`;
    `getexif()` empty; PNG `text` empty; JPEG `applist` has no `APP1` or `APP13` segment.
    `icc_profile` is allowed and carried into re-encoded output.
  - **Normalization.** `ImageOps.exif_transpose`, then the first frame, then `thumbnail` to the
    full-image edge with Lanczos resampling. Modes are converted only where the output format
    requires it. Output is encoded per §2.5 step 5, without EXIF.
  - **Small copy.** Made from the stored full image: `thumbnail` to the small-copy edge, WebP at
    quality 85 (then 50), transparency kept.
- **Validation helpers.**
  - `clean_description(text)`: trims; the result must be 1–300 characters, else 422.
  - `clean_filename(name)`: basename only, control characters removed, at most 255 characters,
    `"image"` if empty.
- **References.** `assert_in_library(db, game_id, ids)` makes one locking read and raises 422 as in
  §2.7. The ids come from `image_refs.image_refs`.
- **Live sessions.** `live_sessions(db, redis, game_id)` makes a locking read of the game's
  `game_sessions` rows with status `LOBBY`/`IN_PROGRESS`. It keeps only those for which
  `state.get_room_state(redis, room_code)` returns a value. Each entry has session id, room code,
  status, host display name and `created_at`.
- **Export inputs.** `export_inputs(db, game_id, *, with_data, locking)` loads the game's
  `GameMeta`, `QuestionFields` (in `order_index` order) and `ImageRecord`s (by id), as
  `bundle_service` inputs.
  - Callers that check limits pass `locking=True`, substitute their pending change, and call
    `bundle_service.bundle_size` and `check_export`.
  - Export passes `with_data=True`.
- **Budgets.** `check_budgets(count, library_bytes)` raises `ConflictError` per §2.6.
- **Library operations.** Each one locks the game (`lock_game`), makes locking reads, checks the
  would-be state, and only then modifies rows. An image not in the game → `NotFoundError`.
  - `list_library(db, redis, game_id, now)` returns the items (metadata, `used_by` from scanning the
    game's questions with `image_refs`, links), `live_sessions`, and meters (image count and limit,
    library bytes and budget, export bytes and limit).
  - `add_image(db, game_id, processed, filename, description)`: the budgets and the export rule.
  - `replace_image(db, redis, game_id, image_id, processed)`: the live guard, budgets and export
    rule; then the new bytes, `version + 1` and `replaced_at = now`.
  - `set_description(db, game_id, image_id, description)`: the export rule.
  - `delete_image(db, redis, game_id, image_id)`: the live guard and the reference check (409
    listing question numbers and roles).
- **Links.**
  - `link(image, variant, now) -> (url, expires_at)`, where `exp` is the start of the current UTC
    hour plus 3 hours.
  - `link_entry(image, now)`: full and small URLs, width, height, description, `expires_at`.
  - `session_links(db, session, now)`: entries for every image of the session's game.
  - `player_images(config, images_by_id, now)`: option images only, small URLs.
- **For the report and T7.** `get_image_meta(db, game_id, image_id)` and `data_uri(image, variant)`.
- **Bundle images on import.** `read_bundle_images(raw_entries) -> list[DecodedImage]` checks each
  entry against the strict `BundleImage` schema, requires strict base64
  (`b64decode(validate=True)`), applies the input limit, then runs `process_image`.

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
- `backend/app/main.py`: include the router under `/api`, like the others.

### 4.8 Questions, games, export and import

`backend/app/services/game_management_service.py`:
- **`create_question`**: `lock_game`, then `image_service.assert_in_library` for the question's
  references, then the export rule on the would-be game (`export_inputs` with the new question
  added), then the existing insert. The router has already validated the body as `QuestionCreate`.
- **`update_question`**: `lock_game`, then the existing merge and `QuestionCreate(**merged)`, then
  `assert_in_library` on the merged `config`, then the export rule with the question replaced by its
  merged version, then the existing `setattr` loop. Nothing is assigned before the checks pass.
- **`update_game`**: `lock_game`. When the title, description or `max_players` changes, the export
  rule runs on the would-be metadata. The course-change path is unchanged.
- **`delete_question`, `reorder_questions`**: unchanged.
- **`export_game`**:
  `bundle_service.serialize(bundle_service.build_bundle(*export_inputs(..., with_data=True,
  locking=False), include_data=True))`. For a game without images this produces today's bytes.
- **`import_game`**. The router has already applied the import limit.
  1. Parse the JSON and check `format`; the version must be 1 or 2, otherwise 422.
  2. Validate the metadata with `GameCreate` (unchanged).
  3. Version 2 only: `image_service.read_bundle_images`, in a worker thread; keys must be unique.
  4. For each question, use `map_image_refs` to rewrite image keys to provisional integers (the
     image's position + 1). An unknown key → 422 "Question 3: unknown image key …". Then validate
     with `QuestionCreate`; the error format is unchanged.
  5. Check the image count and the library budget over the decoded set. Apply the export rule with
     `bundle_service.bundle_size` on the would-be game, for version 1 as well, because a minified
     file can grow when it is re-serialized.
  6. Only then: `create_game`, insert the images (flushing for ids), map provisional integers to
     real ids, and insert the questions.

  All of this runs in the request's single transaction; any failure raises before the commit.

`backend/app/services/game_service.py`:
- `create_room` calls `lock_game(db, game_id)` immediately after the course-match check, before the
  room-cap check and the insert. Its Redis write stays before the commit.
- `start_game` calls `lock_game(db, session.game_id)` before its status check.
- T7 adds its confirmation checks immediately after each lock.

### 4.9 Schemas, routers, gateway and report

- **`backend/app/schemas/admin.py`**, in `QuestionCreate.validate_structure` (which covers create,
  the merged update, and import):
  - `prompt_image_id`, if present and not null, is a positive int (not a bool), on any type.
  - `option_images` is allowed only for `multiple_choice` and `multi_select`: a list with one entry
    per option, each a positive int or null.
  - For `multiple_choice` and `multi_select`, each option is a string and must not be blank unless
    that option has an image.
  - A question already stored with a blank option is refused on its next edit until the option is
    fixed. That follows the existing merged-validation behavior.
- **`backend/app/schemas/image.py`**:
  - `ImageItem`, `UsedBy`, `LiveSessionItem`, `LibraryMeters`, `LibraryResponse`
  - `DescriptionPatch` (`extra="forbid"`)
  - `LinkEntry`, `SessionImages`
  - `BundleImage`: exactly `key`, `filename`, `description` and `data`; `extra="forbid"`; `key`
    uses the §2.15 pattern
- **`backend/app/routers/host.py`**: the five endpoints of §2.8.
  - Each runs, in order:
    1. `assert_host_can_use_game`, **before** reading any file
    2. `read_capped`
    3. `process_image` in a worker thread
    4. the `image_service` operation
    5. `await db.commit()`
    6. the response
  - `GET` returns `LibraryResponse`.
  - The existing import endpoint switches to `read_capped(file, import limit)`.
  - The Redis dependency is the one `routers/game.py` uses.
- **`backend/app/routers/admin.py`**: its import endpoint switches to `read_capped(file, import
  limit)`. Nothing else changes, because its game and question endpoints already call the service.
- **`backend/app/routers/game.py`**:
  - `GET /sessions/{session_id}/images`: requires `require_user`. A missing session → 404;
    `user_can_host_session` false → 403. Returns `session_links`.
  - `POST /rooms`: `await db.commit()` before returning (§2.6).
- **`backend/app/websocket/gateway.py`**:
  - `_question_payload` becomes `async def _question_payload(db, q, number, total)`.
  - It loads the question's option images in one query, with no blobs, and adds
    `"images": image_service.player_images(...)`. The key is always present (`{}` when there are
    none).
  - Its three call sites — `_host_question_payload`, the late-join emit and the advance emit —
    `await` it.
  - Nothing else changes; `events.py` is untouched.
- **`backend/app/services/report_service.py`**: `build_session_report` loads the images referenced
  by the session's questions (found with `image_refs`) in one query, with `small_data` undeferred.
  Then, for each question:
  - The prompt image renders as `<img src="data:image/webp;base64,…" alt="…">`, with the §2.14 note
    when `replaced_at` is later than the session's `created_at`.
  - In `_render_bar_chart`, labels become letter + caption, with a thumbnail when the option has an
    image.
  - A reference to a missing image renders "[image unavailable]".
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
    callers are unchanged.
  - `lib/imageLibrary.ts`: `useImageLibrary(gameId)` returns the library, loading and error state,
    `reload`, `upload`, `replace`, `setDescription` and `remove`. Mutations adopt the returned item
    (with its new links) and then reload. It also reloads within 10 minutes of the earliest
    `expires_at`.
  - `lib/sessionImages.ts`: `useSessionImageLinks(sessionId)` returns `{ links, refresh }`. It
    fetches once `sessionId` is known, and again within 10 minutes of the earliest `expires_at`.
- **Components.**
  - `components/ui/SafeImage.tsx` (presentational): props `src`, `alt`, sizing classes, optional
    `onStale()`, optional `showRetry`.
    - Shows a loading state.
    - On the first error it calls `onStale` if given, so the parent supplies a fresh `src`;
      otherwise it retries the same `src`.
    - On the second error it shows the placeholder: "Image unavailable" and the `alt` text, plus
      Retry if `showRetry` is set.
  - `components/images/ImageLibraryPanel.tsx` (presentational): the §2.8 panel.
    - The upload form accepts `image/png,image/jpeg,image/webp,image/gif` and requires a
      description.
    - Delete uses the two-step inline confirm pattern of `GamesPage`.
    - The live-session banner lists each blocking session and the fix from §2.13.
  - `components/images/ImagePicker.tsx` (presentational): library thumbnails, "No image", and an
    upload mini-form. It reports `onSelect(id | null)` and `onUpload(file, description)`.
- **`pages/QuestionEditorPage.tsx`.**
  - `useImageLibrary(gameId)` feeds the panel, placed above the question list.
  - Form state gains `promptImageId`, and an `imageId` on each multiple-choice and multi-select
    option.
  - `questionToForm` reads `prompt_image_id` and `option_images`.
  - `formToPayload` writes `prompt_image_id` when set, and `option_images` only when some option has
    an image, so text-only payloads are unchanged.
  - The multi-select row filter keeps rows that have an image.
  - The question list shows thumbnails.
- **Game screens.**
  - `pages/game/GameLayout.tsx`:
    - keeps `session_id` from its existing `GET /game/rooms/{code}` and calls
      `useSessionImageLinks`
    - exposes `imageLinks` and `refreshImageLinks` through `GameContext`
    - calls `refreshImageLinks` after a socket reconnect, and once when the current question
      references an id missing from `imageLinks`
  - `pages/game/QuestionPage.tsx`:
    - The prompt image (a `SafeImage` with the full URL, `onStale = refreshImageLinks` and Retry
      shown) sits below the prompt text and renders regardless of `questionLocked`.
    - For multiple choice and multi-select with any option image: a lettered grid of full images
      with captions.
    - Image layouts reserve space for the fixed join panel.
  - `pages/game/ResultsPage.tsx`: the prompt thumbnail. `buildBars` labels carry letter and caption,
    and bars show a thumbnail for image options.
  - `pages/game/GameOverPage.tsx`: the same, in each `QuestionCard`.

**Player app (`frontend/player/src/`)**

- `types/game.ts`: the same config fields and `images` map as the host.
- `components/ui/SafeImage.tsx`: the touch copy. One automatic retry of the same `src`, then the
  placeholder; no Retry button.
- `pages/game/QuestionPage.tsx`: multiple choice and multi-select render 2-column image tiles when
  any option has an image.
  - Each tile shows a letter badge, a `SafeImage` with `currentQuestion.images[String(id)].url`, and
    the caption if any.
  - Each tile's accessible name follows §2.12.
  - Tap behavior is unchanged.
- `pages/game/ResultsPage.tsx` and `GameOverPage.tsx`: option labels become letter + caption, or the
  letter alone when the caption is empty.

### 4.11 Tests

**Integration: `tests/integration/test_t8_images.py`.** It builds a self-contained `T8Env` in the
style of `test_t4_access.py`:
- its own course, a HOST user with a game grant, a second course and host, and a guest
- test-created games
- in-memory image factories built with Pillow: PNG, a JPEG with an EXIF orientation tag, GIF, WebP,
  SVG text, a truncated PNG, a pixel bomb, and noise JPEGs of chosen sizes
- a finalizer that deletes every session and game it created (the admin game delete cascades to
  images)

Tests:

- **Upload:** `test_upload_accepts_each_format`, `test_exif_rotation_normalized`,
  `test_large_image_downscaled`, `test_conforming_bytes_stored_unchanged`,
  `test_small_copy_is_webp_within_edge`.
- **`test_invalid_uploads_refused`:** the 415, 422 and 413 cases, and a missing or overlong
  description. Each leaves the library unchanged.
- **`test_count_budget_and_export_limits`:** the 51st image, the library budget, and a question
  update that would exceed the export limit → 409, with the question unchanged.
- **`test_library_access_rules`:**
  - no grant, another course's host, and a guest → refused
  - admin allowed
  - another game's image → 404
  - session links refused to non-hosts
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
- **`test_concurrent_uploads_respect_count`:** with 49 images, two concurrent uploads → one 201 and
  one 409, ending at 50 images.
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
- **`test_report_embeds_small_copies`:** data URIs, letter labels, and the replaced note.
- **`test_game_delete_removes_images`.**

**The serialization test.**

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
- **Setup.** A test-created game with a question whose option uses image A (version 1), and a second
  image file for the replace.
- **Steps.**
  1. The test connection runs `SELECT id FROM games WHERE id = %s FOR UPDATE`.
  2. Start `POST /api/game/rooms` and `PUT …/images/{A}` as concurrent `httpx.AsyncClient` requests
     with a 30 s timeout.
  3. After 2 s, assert that neither task has finished.
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

**Unit: `tests/unit/test_image_service.py`.**
- conformance, one test per condition
- each normalization branch
- idempotence: processing twice gives the same bytes
- the small-copy quality fallback
- token determinism, bucketing and claims

**Unit: `tests/unit/test_bundle_service.py`.**
- `image_refs` and `map_image_refs` for every role
- key assignment by id order
- version 1 output identical to today's exporter for a game without images
- `bundle_size` equals `len(serialize(build_bundle(..., include_data=True)))`, for games with and
  without images
- `check_export` for "within the limit", "over the limit but not increased", and "over the limit
  and increased"

**Deferred to T7:**
- Spot It metadata checks
- the full link appearing only in the host map
- reconfirmation and start checks, including the `start_game` lock
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
- **Manual run** under the nginx build (`npm run build`, then `http://localhost:8080`), with a phone
  on the LAN joined as a guest:
  - upload an image over 1 MiB
  - image options on the phone and the projector
  - prompt images on all four question types
  - the live-session banner
  - export and import of a budget-sized library through nginx
  - download a report
  - a blocked image URL showing the placeholder
  - `scripts/simulate_players.py` playing an image-option game

### 4.13 Implementation order

1. Dependencies, model, migration, exceptions, `game_lock`, `image_refs`, `bundle_service`, the
   pipeline; unit tests.
2. Tokens, the serving router, library endpoints and operations.
3. Reference rules, the export rule and the lock in question and game writes; the
   `create_room`/`start_game` locks; the room-create commit.
4. The gateway `images` map; session links; projector and phone rendering; `SafeImage`.
5. The editor panel and picker; `formToPayload` changes.
6. Export/import version 2, capped reads, nginx; the report.
7. Integration tests; docs and context files (§4.14).

### 4.14 Documentation and context files

- `README.md`: the version 2 bundle format, the §2.4 limits, and rebuilding after the Pillow
  addition.
- `docs/realtime.md`: the `images` map in `new_question`, and the projector's session link map.
- `CLAUDE.md` updates. Edit only what's stale, per `.claude/rules/context-sync.md`:
  - `backend/`: Pillow, the nginx limit.
  - `backend/app/`: the new router and services, migration `005`, the lock-order and
    image-reference invariants, the §4.4 module dependencies, and a question-checklist entry for
    image references.
  - `backend/app/models/`, `routers/`, `services/`, `schemas/`, `common/`, `websocket/`.
  - `frontend/`: image links as a cross-app convention; `SafeImage` duplicated in two apps.
  - `frontend/host/src/` and its `pages/`, `components/` and `lib/`.
  - `frontend/player/src/` and its `pages/` and `components/`.

---

## 5. Existing limitations and risks

**Existing limitations, recorded and not fixed by T8:**
- `answer_received` sends correctness and points to the submitting phone immediately, for every
  question type. It's hidden on screen but visible in developer tools.
- Question edits during a live session aren't blocked; image replace and delete are (§2.13).
- Refreshing the projector during results loses the results.
- Reports are rebuilt from current data, not stored per session (§2.14).

**Risks to watch during implementation and testing:**
- **Load** when many phones fetch option images at once. To be observed in the manual run, not
  assumed.
- **Link exposure:** links appear in nginx access logs until they expire.
- **Lock waits** when writes to one game coincide. A stuck lock surfaces as a 409 after MySQL's
  lock-wait timeout.
- **Pillow upgrades** could change encoder output. Stored bytes are unaffected, since conforming
  bytes pass through, but normalizing the same input after an upgrade could give different bytes.
- **T9** must theme the new surfaces.
