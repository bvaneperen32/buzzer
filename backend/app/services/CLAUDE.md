# backend/app/services

## Purpose

All business logic. Routers and the WebSocket gateway are thin — they authenticate, validate, and
delegate here. This is where game rules, scoring, room lifecycle, Redis state, roster parsing, and
export/report generation actually live.

## Contents

- `auth_service.py` — password hashing (bcrypt, 12 rounds), RS256 JWT mint/verify, and user
  lookup/creation (`get_or_create_user_by_netid`, `create_guest_user`, `authenticate_local`).
  Token lifetimes: access 2h, refresh 7d, temp 15min. `create_image_token` (T8) signs image
  links: `{token_type: "image", img, v, var, exp}`, with **no `sub` and no `iat`**, so the same
  image, version, variant and hour always give the same token and URL (RS256 signing is
  deterministic). HTTP and socket auth accept only `access`/`temp`, so an image token
  authenticates nothing else.
- `bootstrap.py` — `bootstrap_admin()`, run once from the lifespan hook. Creates the first ADMIN
  from `ADMIN_USERNAME`/`ADMIN_PASSWORD` if no admin exists; retries in a loop while the schema is
  missing rather than crashing (see its module docstring for why).
- `state_service.py` — the Redis layer and the authoritative description of the key namespace
  (documented at the top of the file: `room:{code}`, `session:{id}:players`,
  `:player:{uid}`, `:question`, `:answered:{qid}`, `:dist:{qid}`). An answer is counted by
  `apply_answer` alone: the running score, the answered mark and the distribution in one
  MULTI/EXEC, skipped if the player is already marked. It and `set_player_connected` watch the
  player's record, so neither overwrites the other's write. Also `restore_from_mysql` (never
  called — see Gotchas).
- `game_lock.py` — T8's concurrency primitive. `lock_game(db, game_id)` is `SELECT … FROM games
  … FOR UPDATE` with `populate_existing` (it refreshes the `Game` the access check loaded);
  `fresh(stmt, exclusive=False)` makes any select a locking read with `populate_existing`
  (a shared lock, which SQLAlchemy renders as `FOR SHARE` on MySQL 8 — T7's lock tests match that
  text in PROCESSLIST — or `FOR UPDATE` if exclusive); `lock_conflicts()` turns MySQL 1205/1213
  into 409. See [app/CLAUDE.md](../CLAUDE.md) for the rules that use them.
- `image_refs.py` — pure, imports nothing: the only definition of which `config` keys hold image
  ids (`prompt_image_id`, `option_images`, `image_id`) and their roles. `image_refs` lists the
  positive-int references; `map_image_refs` rewrites every reference slot (export: id → key;
  import: key → provisional id → real id). Neither raises on malformed values.
- `bundle_service.py` — pure: the one bundle builder and serializer (`serialize` is the pre-T8
  `json.dumps(…, indent=2, ensure_ascii=False)`), the export repairs for references that don't
  resolve, `bundle_size` (the exact export length without building base64), and `check_export`
  (the export rule). An empty library exports version 1, byte for byte as before; otherwise
  version 2 with the whole library. T7: `ImageRecord` also carries each image's `version`,
  `width` and `height` (required fields), so `build_bundle` can apply `spot_it.export_fields` —
  Spot It questions export without `width`/`height`, and with `"confirmed": true` only when
  confirmed against the exported image. Measurement and export share that one transform.
- `sort_it.py` (T7) — pure, imports nothing from the application: Sort It's rules
  (`validate`, `parse_answer`, `score` with the corrected-for-guessing credit floored at 0,
  `dist_keys`, `reveal`, `card_order`) and `results_breakdown`, the per-card percentages the
  report shows (integer half-up; the host app's `lib/sortResults.ts` is a port). Only `validate`
  raises; readers of stored data return `None`/empty instead.
- `spot_it.py` (T7) — pure, imports only `image_refs`: Spot It's grid geometry (`max_grid`,
  `default_grid`, integer arithmetic — the host app's `lib/spotGrid.ts` copies it for the editor;
  the player app has no copy), `validate`,
  `parse_answer`, `score` (set membership), `reveal` (row-major), `review_status` (the one
  confirmation rule, with reasons in a fixed order), `reason_texts`, and `export_fields`. Readers
  of stored data never raise; malformed data reads as `not_confirmed`.
- `image_service.py` — T8's image code, the only code that accepts image bytes. **The pipeline**
  (`process_image`: size, format, pixel check before decoding, conformance, normalization, small
  copy — see its docstring and the spec's §2.5); **capacity** (`PIPELINE_LIMITER`, two slots,
  `processing_slot`, `process_upload`, `process_bundle_images` with the import deadline);
  **libraries** (`list_library`, `add_image`, `replace_image`, `set_description`, `delete_image`,
  each under the game lock with fresh reads, checking budgets, the export rule, the live guard and
  references before changing anything); **links** (`link`, `link_entry`, `verify_link`,
  `session_links`, `player_images`); and **helpers** for others (`export_inputs`,
  `assert_in_library`, `live_sessions`, `images_for_refs`, `get_image_meta` for T7, `data_uri` for
  the report, `check_bundle_entries` for import, and `live_rooms_text`, the room list T8's live
  guard and T7's live freeze both print). T7: `export_inputs` selects each image's `version`,
  `width` and `height`, and `add_image` / `replace_image` measure their pending or substituted
  record with them (a replace increments the version, which can drop `"confirmed": true`).
  Deadlines are read through one clock function, `_clock`, so tests can move time.
- `game_service.py` — the core. Room code generation, the access rules, session lifecycle
  (`create_room` → `start_game` → `complete_game` / `abandon_game`), `authorise_player`,
  `calculate_score`, `record_answer`, and the leaderboard / game-over summary builders. T7: the
  Spot It room gates (`spot_it_review_blockers`, run in `create_room` after the course check and
  in `start_game` as a backstop, both reading questions and images fresh under the game lock),
  and the Sort It / Spot It branches in scoring, answer counts and both summaries. The
  access rules:
  - `has_host_grant` — the single per-resource HOST check (a `user_course_access` row with role
    `HOST`; a `PLAYER` row isn't one). Every rule below uses it, as does the admin router's grant
    validation.
  - `assert_host_can_use_course` (the course rule) and `assert_host_can_use_game` (the game rule,
    for running *and* editing; returns the `Game`). Both let `ADMIN` through first, and 404 a
    missing resource for admins. `user_can_use_game` is the game rule as a predicate (T8: the
    projector's link map and the report's images use it); the assertion is built on it.
  - `user_can_host_session` — the session-host predicate behind `assert_can_download_session`,
    `assert_can_delete_session`, `my-active-sessions`, and the socket HOST join.
  - Grant side effects: `revoke_game_grants_for_course` (a user lost HOST on a course) and
    `revoke_game_grants_without_host` (a game moved course). Both only ever delete rows.
  - `list_sessions_for` — the role-scoped session list behind `GET /api/host/sessions`.
  - `GUESTS_NOT_ALLOWED` — the guest-refusal message shared by `/auth/guest` and
    `authorise_player`.
- `game_management_service.py` — every write to `games` and `questions`, shared by the admin
  and host routers: `create_game` (requires a course; a non-admin creator gets a game grant),
  `update_game` (only this path changes `course_id`, and it triggers the grant revocation),
  question list/create/update/delete/reorder, and bundle `export_game` / `import_game`. It owns
  `sanitize_prompt`, the **only** place prompts are bleach-sanitized. Access checks are the
  caller's job; nothing here decides who may do what. Since T8, question create/update, game
  metadata updates and reorders take the game lock and read fresh; create and update check image
  references, then write the question and apply the export rule to it **as stored**
  (`_as_stored`: flush, refresh, measure — MySQL's `FLOAT` and JSON columns can store a number
  longer than it was sent, e.g. `1e-40` → `9.99995e-41`); a refusal raises and the transaction
  rolls back. `create_question` takes the next `order_index` from that same fresh list. T7:
  `create_question` / `update_question` take `redis`. Every Spot It write stores the image's
  trusted `width`/`height` (even when the request omitted `config`), and a sent stamp is an
  attempted confirmation (409 if stale, then 422 if the grid doesn't fit); a null stamp is
  removed before writing. While a session is live, Sort It and Spot It questions are frozen
  (`_frozen_fields`, compared as stored). Question list and write responses carry
  `needs_review` / `review_reasons`. Import takes a boolean `answer_data.confirmed` and restamps
  it against the imported image.
  `import_game` runs the spec's §2.15 order (parse; metadata; image count *before* decoding
  anything; entries; references and questions; the pipeline against a deadline; the budget; then
  create, and the export rule on the created game as stored), all in one transaction, so a
  failure at any step creates nothing.
- `roster_service.py` — two roster import paths: `process_roster_csv` (Canvas gradebook export,
  parsed server-side) and `process_roster_rows` (pre-mapped rows from the frontend column
  wizard). Both upsert by `(course_id, netid)` and deactivate netids absent from the upload.
  Also `list_roster` and `patch_roster_entry`, shared by the admin and host routers.
  `patch_roster_entry` 404s an entry from another course than the one in the URL — the only
  thing stopping a host of course A from editing course B's roster through A's URL.
- `export_service.py` — `build_session_csv` (raw Player × Question grid) and `build_canvas_csv`
  (Canvas gradebook import format, with `per_question` and `roster_only` modes).
- `report_service.py` — `build_session_report`, a self-contained HTML session report with
  aggregate stats only. Includes its own CSS, bar chart, word cloud, and histogram renderers.
  T8: embeds small copies as data URIs (prompt images; option images beside multiple-choice
  bars and in a new multi-select option list), only when `include_images` (the downloader passes
  the game rule now), otherwise the withheld text; images replaced after the session was created
  carry a note. T7: Sort It's per-card table (`_render_sort_table`) and Spot It's heatmap, an
  inline SVG over the small copy (`_render_spot_svg`) — pure renderers that escape every label
  and description — plus a note counting stored answers that no longer fit the question, "N%
  all cards correct" on Sort It's stats line, and T7 questions' maximum points through
  `format_points` (`Decimal(repr(x))`, half up, never `normalize()`).

## How it fits in

Sits between `routers`/`websocket` above and `models`/Redis below. The dependency rule that
matters: services may import from `models`, `schemas`, and `common.exceptions`, but must **not**
import `common.dependencies` — that module imports `auth_service`, so doing so creates a cycle.

Services take `db: AsyncSession` and `redis: Redis` as arguments rather than resolving them
themselves, which is what makes them testable without a request context.

`game_management_service` imports `game_service` (for the grant revocation on a course change),
never the reverse. T8's modules add their own one-way rules, listed in
[app/CLAUDE.md](../CLAUDE.md): notably `image_service` and `bundle_service` never import
`game_service` or `game_management_service`.

## Gotchas

### Access and authoring

- **HOST is a live, per-course grant, checked on every request.** `game_sessions.host_user_id` is
  a historical record, not a permission: `user_can_host_session` always pairs it with a current
  HOST grant, so a host removed from a course can't download, delete, or rejoin its sessions.
- **Game grants never outlive what makes them effective.** Losing HOST on a course deletes the
  user's grants for that course's games; moving a game deletes grants held by users who don't
  HOST the new course. Neither side effect ever *adds* a grant — the only grant a non-admin can
  cause is `create_game`'s auto-grant to its creator.
- Admin creators get no auto-grant (they bypass the rule), so **an admin later demoted to USER
  loses the games they authored** until they get HOST on the course and a grant.
- The game rule gives non-admins the same 403 whether or not the game exists, so it doesn't
  reveal which ids exist.
- `list_sessions_for`'s host join and score-count join **must stay outer**: `host_user_id` is
  nullable and a LOBBY session has no scores, so inner joins would silently drop sessions.
- `update_question` validates the merged question with `QuestionCreate` (explicit `null`s mean
  "unchanged"). A question already stored in an invalid state therefore refuses every edit until
  the edit makes it valid; the 422 message says why.
- `delete_question` refuses (409) once any session has answered the question, rather than deleting
  score rows and rewriting past results.
- **Nothing blocks edits, reorders, or deletes of the four original question types while a
  LOBBY or IN_PROGRESS session of the game exists.** That was always true for admins, but hosts —
  the people running games — now have the same access. A mid-game edit can change what later
  questions show or how they score. T7's two types *are* frozen while live (only the prompt, time
  limit and Sort It's prompt image can change); extending that freeze to the other types is the
  fix if the hazard bites.
- `game_created` and `game_imported` are logged inside the service, *before* the router commits,
  so a log line can exist for a create that then fails to commit. Grant revocations log as
  `game_grants_revoked_host_lost` / `game_grants_revoked_game_moved` with the row count.
- `authorise_player` refuses a `GUEST` when the session has `allow_guests` off. It's the
  backstop: a guest holding a still-valid token skips `/auth/guest` and goes straight to the
  socket.

### Scoring and question types

- **Adding a question type touches about two dozen sites** — the canonical checklist, backend and
  frontend, is in [app/CLAUDE.md](../CLAUDE.md). In this directory: `calculate_score`,
  `answer_dist_keys` (live play's distribution key), `get_player_question_summary`,
  `get_host_question_summary` (*two* switches — reveal and distribution), and four in
  `report_service`. The current
  `multi_select` type shows what missing some looks like: `report_service` has no `multi_select`
  branch in `_answer_reveal`, `_extract_answer_key`, its type-badge map, or the chart dispatch in
  `build_session_report`, so those questions render in the HTML report as a bare card with no
  distribution and an unstyled raw-slug badge.
- `record_answer` writes a `SessionScore` row unconditionally, **commits it**, and only then
  counts it in Redis (`state.apply_answer`). A failure therefore leaves nothing, or a committed
  row not yet counted, which `finish_recorded_answer` counts: when the answer is retried
  (`recorded_answer` finds it), or when the results are shown (the gateway counts every stored
  row Redis hasn't before reading totals).
  - Deduplication is the caller's job. The gateway runs one player's attempts at a question one
    at a time and looks for the row first.
  - Nothing in MySQL backs it up — see [models/CLAUDE.md](../models/CLAUDE.md).
  - Its commit is deliberate, like `start_game`'s: the answer is acknowledged only once stored.
    `answer_dist_keys` gives an answer's distribution keys.
- **`is_correct` is computed by exact float equality** (`points == question.points_value`) for
  `multiple_choice` and `true_false`. Authoring `0.1 + 0.2` style values, or any float that
  doesn't round-trip, silently marks a right answer wrong. `fill_in_the_blank` and `multi_select`
  use `>=`, which is safer — the inconsistency is not deliberate as far as the code shows.
  `multi_select` additionally requires `points_value > 0`.
- **"Correct" in the reveal payloads means `answer_points[i] >= points_value`.** If every option
  is worth 0 and `points_value` is 0, *every* option is reported correct.
- `calculate_score` clamps `multi_select` to a floor of 0, so negative `answer_points` reduce a
  score but can't drive it below zero.
- `COMPLETENESS` grading awards full points for *any* non-empty answer and always sets
  `is_correct=True` — except T7's types, which score in their own modules *before* that shortcut:
  a malformed answer scores `(0, False)` in either mode. Sort It's `is_correct` means every card
  in its box (partial credit is `points < points_value` with `is_correct` false).

### Answer distributions

- **`fill_in_the_blank` distribution keys are raw player text**, used directly as Redis hash
  fields. Cardinality is unbounded (one field per distinct typed answer) and the content is
  arbitrary user input.
- **Normalization of those keys is inconsistent.** `record_answer` collapses internal whitespace
  (`" ".join(text.lower().split())`), while `get_host_question_summary` and
  `report_service._extract_answer_key` only do `.strip().lower()`. `"foo  bar"` is therefore one
  bucket live and a different bucket in the host summary and report.

### Room lifecycle

- **`generate_room_code` checks Redis only, but `game_sessions.room_code` is UNIQUE in MySQL
  forever.** Once a room's Redis key expires (90 min) its code is free to regenerate, and the
  insert then fails with an integrity error rather than retrying. The collision check is against
  the wrong source of truth.
- `create_room` enforces the global room cap with `redis.keys("room:*")` plus a `GET` per key —
  O(N) and blocking. `state_service.delete_room_state` uses `redis.keys()` the same way. Fine at
  50 rooms; the wrong primitive if this ever grows.
- `create_room` runs the course rule, then the game rule, then requires
  `game.course_id == course_id` — **for admins too** (409 "This game belongs to a different
  course"). So an unassigned game can't be run by anyone until an admin assigns it a course.
- `complete_game` and `abandon_game` take a `redis` argument but `complete_game` never uses it.
  Its comment claims Redis state is kept "10 more minutes"; nothing sets a 10-minute TTL — the
  state simply rides out the remaining 90-minute room TTL.
- **`create_room` and `start_game` take the game row lock** (T8), right after their access checks:
  a room's creation is serialized with image replace/delete, and `start_game` re-reads the
  session (exclusively) and its questions fresh. If a concurrent Start already moved the session
  to `IN_PROGRESS`, it returns `None` ("already started") rather than raising, and the gateway
  ignores that advance. `create_room`'s course-match check runs on the `Game` the lock
  refreshed, so a course move committed while it waited is seen.
- `start_game` and `complete_game` `commit()` deliberately (so concurrent WebSocket handlers in
  other DB sessions observe the new status); `abandon_game` only `flush()`es. Don't "harmonize"
  these without understanding the race each one is addressing.

### Redis state

- **Only `room:{code}` has a TTL.** The `players`, `player:*`, `question`, `answered:*`, and
  `dist:*` keys have none. They're removed only by an explicit `delete_room_state`, which
  `complete_game` doesn't call — so a completed session leaks those keys indefinitely.
- **The player record is JSON, read-modify-written.** Its two writers during a game,
  `apply_answer` (the score) and `set_player_connected` (the flag), both WATCH it and retry if it
  changed. Before that, a reconnect's flag write could overwrite an answer's score (the lost
  update was reproduced alongside the double-recording it hid). `add_player` still overwrites the
  record outright, but only for a player who has none.
- `add_player` says `score: int = 0`, but points are floats throughout. The annotation is wrong,
  not the behavior.
- **`restore_from_mysql` is dead code.** Nothing calls it, and it wouldn't rebuild Redis if
  something did: despite its docstring ("Rebuild Redis state") and its unused `redis` parameter,
  it only reads MySQL and returns a dict. There is no Redis-recovery path — see
  [app/CLAUDE.md](../CLAUDE.md).
- `all_players_answered` returns `False` when the player count is 0, so an empty room never
  auto-advances.

### Auth

- **JWT keys auto-generate in memory when `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY` are unset**, and also
  when they're set but fail base64 decoding — the `except Exception: pass` swallows a malformed
  key and silently falls back. Every token is invalidated on restart, and with more than one
  worker process each worker signs with a *different* key, so tokens won't verify across workers.
  A misconfigured production deploy looks like random intermittent 401s.
- Env keys are expected base64-encoded, not raw PEM.
- `create_access_token` embeds `role`, but `get_current_user` re-reads the user from the DB, so a
  role change takes effect immediately rather than at token expiry. Don't start trusting the
  token's `role` claim instead.
- **`create_guest_user` identifies a guest by email alone**, with no verification: an existing
  guest's email returns that same user (display name updated), so anyone who knows the address
  gets the account and its scores. And since `users.email` is UNIQUE, `get_or_create_user_by_netid`
  will fail with an IntegrityError (a 500 at SSO login) when a guest row already holds the
  student's email — e.g. they once joined as a guest with their school address. (Found by reading
  the code, not reproduced.)
- `authorise_player` returns the roster's `full_name` as the display name; the inline comment
  calling it "first name from CSV" reflects `process_roster_csv`'s behavior (below), not the
  column's meaning.

### Roster, export, report

- **The two import paths store different things in `full_name`.** `process_roster_csv` runs
  `_parse_display_name`, which keeps only the *given* name from Canvas's `"Last, First"` format
  ("Abboud, Masa" → "Masa"). `process_roster_rows` stores whatever the frontend mapped, typically
  the real full name. Same column, two meanings, and it's what players see on screen.
- `MAX_ROWS = 1000` counts *unique* netids, so duplicate rows don't consume the budget. Rows past
  the limit are skipped with an error appended, not rejected outright.
- Deactivation is skipped entirely when `seen_netids` is empty, so a malformed upload can't wipe a
  roster — that guard is load-bearing.
- **But rows that don't make it in get deactivated.** Deactivation runs against `seen_netids`, so
  any existing student past the `MAX_ROWS` cap, or whose row was skipped for an error (a blank
  email in the wizard path, say), is marked inactive and can no longer join.
- **Both exporters derive the player list from `session_scores`**, so a player who joined but never
  answered a single question does not appear in either CSV at all.
- `build_canvas_csv` writes empty `ID` and `SIS User ID` columns; Canvas matches on SIS Login ID
  alone. `roster_only=True` (the default) drops guests and local accounts, since they have no netid.
- **`report_service` embeds `q.prompt` as raw HTML**, relying on the bleach sanitization done at
  write time in `game_management_service.sanitize_prompt`. That invariant is the only thing
  preventing stored XSS in the report — any new write path for questions must go through that
  service.
- **The report claims "no personally identifiable information", but the fill-in-the-blank word
  cloud renders raw player-typed text.** Players can type anything, including their own or
  someone else's name. It's escaped, so it's not an injection issue — it's a privacy claim the
  code can't actually guarantee.
- `report_service._levenshtein` is a hand-rolled reimplementation of what `game_service` gets from
  `rapidfuzz`. Two implementations of the same matching rule is a divergence risk.
- `report_service._build_histogram` truncates `max_possible` and each score with `int()` — another
  integer assumption left over from before migration `003`.
