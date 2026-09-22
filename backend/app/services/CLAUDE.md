# backend/app/services

## Purpose

All business logic. Routers and the WebSocket gateway are thin — they authenticate, validate, and
delegate here. This is where game rules, scoring, room lifecycle, Redis state, roster parsing, and
export/report generation actually live.

## Contents

- `auth_service.py` — password hashing (bcrypt, 12 rounds), RS256 JWT mint/verify, and user
  lookup/creation (`get_or_create_user_by_netid`, `create_guest_user`, `authenticate_local`).
  Token lifetimes: access 2h, refresh 7d, temp 15min.
- `bootstrap.py` — `bootstrap_admin()`, run once from the lifespan hook. Creates the first ADMIN
  from `ADMIN_USERNAME`/`ADMIN_PASSWORD` if no admin exists; retries in a loop while the schema is
  missing rather than crashing (see its module docstring for why).
- `state_service.py` — the Redis layer and the authoritative description of the key namespace
  (documented at the top of the file: `room:{code}`, `session:{id}:players`,
  `:player:{uid}`, `:question`, `:answered:{qid}`, `:dist:{qid}`). Also `restore_from_mysql`.
- `game_service.py` — the core. Room code generation, host access assertions, session lifecycle
  (`create_room` → `start_game` → `complete_game` / `abandon_game`), `authorise_player`,
  `calculate_score`, `record_answer`, and the leaderboard / game-over summary builders.
- `roster_service.py` — two roster import paths: `process_roster_csv` (Canvas gradebook export,
  parsed server-side) and `process_roster_rows` (pre-mapped rows from the frontend column
  wizard). Both upsert by `(course_id, netid)` and deactivate netids absent from the upload.
- `export_service.py` — `build_session_csv` (raw Player × Question grid) and `build_canvas_csv`
  (Canvas gradebook import format, with `per_question` and `roster_only` modes).
- `report_service.py` — `build_session_report`, a self-contained HTML session report with
  aggregate stats only. Includes its own CSS, bar chart, word cloud, and histogram renderers.

## How it fits in

Sits between `routers`/`websocket` above and `models`/Redis below. The dependency rule that
matters: services may import from `models`, `schemas`, and `common.exceptions`, but must **not**
import `common.dependencies` — that module imports `auth_service`, so doing so creates a cycle.

Services take `db: AsyncSession` and `redis: Redis` as arguments rather than resolving them
themselves, which is what makes them testable without a request context.

## Gotchas

### Scoring and question types

- **Adding a question type means touching at least five places**, and the current `multi_select`
  type demonstrates what happens when you miss one: `report_service` has no `multi_select` branch
  in `_answer_reveal`, `_extract_answer_key`, or its type-badge map, so those questions render in
  the HTML report as a bare card with no distribution and an unstyled raw-slug badge. The other
  four sites are `calculate_score`, `record_answer`'s distribution keys,
  `get_player_question_summary`, and `get_host_question_summary`.
- **`is_correct` is computed by exact float equality** (`points == question.points_value`) for
  `multiple_choice` and `true_false`. Authoring `0.1 + 0.2` style values, or any float that
  doesn't round-trip, silently marks a right answer wrong. `fill_in_the_blank` and `multi_select`
  use `>=`, which is safer — the inconsistency is not deliberate as far as the code shows.
- **"Correct" in the reveal payloads means `answer_points[i] >= points_value`.** If every option
  is worth 0 and `points_value` is 0, *every* option is reported correct.
- `calculate_score` clamps `multi_select` to a floor of 0, so negative `answer_points` reduce a
  score but can't drive it below zero.
- `COMPLETENESS` grading awards full points for *any* non-empty answer and always sets
  `is_correct=True`.

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
- `create_room` calls `await db.get(Game, game_id)` and discards the result — dead code; access was
  already validated above it.
- `complete_game` and `abandon_game` take a `redis` argument but `complete_game` never uses it.
  Its comment claims Redis state is kept "10 more minutes"; nothing sets a 10-minute TTL — the
  state simply rides out the remaining 90-minute room TTL.
- `start_game` and `complete_game` `commit()` deliberately (so concurrent WebSocket handlers in
  other DB sessions observe the new status); `abandon_game` only `flush()`es. Don't "harmonize"
  these without understanding the race each one is addressing.

### Redis state

- **Only `room:{code}` has a TTL.** The `players`, `player:*`, `question`, `answered:*`, and
  `dist:*` keys have none. They're removed only by an explicit `delete_room_state`, which
  `complete_game` doesn't call — so a completed session leaks those keys indefinitely.
- **`update_player_score` is a read-modify-write, not atomic.** Two answers scored concurrently
  for the same player can lose an update. MySQL remains correct (the leaderboard is aggregated
  from `session_scores`), so this affects the Redis-cached running score only.
- Its signature says `delta: int -> int`, and `add_player` says `score: int = 0`, but points are
  floats throughout. The annotations are wrong, not the behavior.
- **`restore_from_mysql` does not write to Redis**, despite its docstring ("Rebuild Redis state")
  and its unused `redis` parameter. It only reads MySQL and returns a dict; whatever calls it owns
  the actual repopulation.
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
- **Both exporters derive the player list from `session_scores`**, so a player who joined but never
  answered a single question does not appear in either CSV at all.
- `build_canvas_csv` writes empty `ID` and `SIS User ID` columns; Canvas matches on SIS Login ID
  alone. `roster_only=True` (the default) drops guests and local accounts, since they have no netid.
- **`report_service` embeds `q.prompt` as raw HTML**, relying on the bleach sanitization done at
  write time in `routers/admin.py`. That invariant is the only thing preventing stored XSS in the
  report — any new write path for questions must bleach too.
- **The report claims "no personally identifiable information", but the fill-in-the-blank word
  cloud renders raw player-typed text.** Players can type anything, including their own or
  someone else's name. It's escaped, so it's not an injection issue — it's a privacy claim the
  code can't actually guarantee.
- `report_service._levenshtein` is a hand-rolled reimplementation of what `game_service` gets from
  `rapidfuzz`. Two implementations of the same matching rule is a divergence risk.
