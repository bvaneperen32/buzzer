# T7 — Two New Question Types: Sort It and Spot It

**Destination:** `docs/plans/t7-question-types.md`
**Status:** Revised on top of T8 (2026-09-29). **Not yet Goldfish-tested.**
- The pre-T8 Elephant draft is preserved in commit `a3a0f50`; §7 lists what changed.
- This branch has T8 merged (merge `07c0bf8`, T8 tip `7fbd2b2`, implementation `990fed5`), so the
  T8 code and `docs/plans/t8-image-support.md` are the baseline.
- Implementation starts only after a fresh Goldfish review and explicit authorization. If
  implementation reveals a flaw, revise this document first, then the code.

**Read first:** `docs/plans/t8-image-support.md` (especially §2.6 locks and fresh reads, §2.7
references, §2.13 live guard, §2.15 bundles, §2.16 the T7 contract), `backend/app/CLAUDE.md`
(architecture, invariants, the question-type checklist), `backend/app/services/CLAUDE.md`,
`backend/app/websocket/CLAUDE.md`, `backend/app/schemas/CLAUDE.md`, `frontend/CLAUDE.md`, and the
`pages/` context files of the host and player apps.

**Course references:** T3 design before code (this document, its Goldfish review); T5 integration
tests; T6 sample games (separate work); T7 this feature; T8 image support (implemented); T9 themes
(separate work).

---

## 1. Problem

Buzzer ships `multiple_choice`, `true_false`, `fill_in_the_blank`, `multi_select`. T7 adds two
types as full vertical slices: authoring (host app editor, used by hosts and admins), validation on
every write path, storage, phone interaction, projector screens, realtime state, scoring, results,
game-over summaries, HTML report, export/import, automated tests, browser and physical-phone
checks. Constraints: one classroom type and one party type; at least one uses an HTML `<canvas>`
as the player's interaction surface; each has a defined scoring approach; clean integration with
the existing architecture (R7). Spot It builds on T8 and must keep T8's guarantees: same-game image
access, host-only image delivery, fresh reads under the game lock, and the export rule measured on
stored values.

## 2. Technical plan

### 2.1 The two types

| | **Sort It** | **Spot It** |
|---|---|---|
| `type` | `sort_into_boxes` | `find_in_image` |
| Audience | Classroom | Party |
| Player does | Places cards into 2–4 labelled boxes (drag or tap-tap), then Submit | Finds the object in the projected image, selects the matching labelled cell on a phone grid, then Lock in |
| Phone surface | Normal page elements | **Interactive `<canvas>`**: only the grid, labels and proportions |
| Projector | Prompt, boxes, cards | Prompt, **image with a transparent labelled grid on a `<canvas>`** |
| Scoring | Per card, corrected for guessing, floored at 0 | All-or-nothing on an accepted cell |
| Uses T8 | Optional prompt image | Canvas image, versions, lock, live guard, export v2 |

Rationale (retained): Sort It is card sorting — examples vs non-examples in any subject; the reveal
shows which cards the class misclassified. Spot It is a room-wide "Where's Waldo": no background
knowledge, everyone searches the projector together, and the results heatmap is a shared moment.

### 2.2 Terms and existing rules relied on (as on the T8 branch)

- Question fields: `type` (allowlist only in the Pydantic regex), `grading_type`, `prompt`,
  `config` (**sent to phones as-is**), `answer_data` (never sent; only derived reveals at results),
  `time_limit_seconds` (2–300), `points_value` (float, 0–100000).
- Scoring happens per answer at submit (`gateway.on_submit_answer` → `game_service.record_answer`
  → `calculate_score`), writes a `SessionScore`, and depends only on the question and that answer.
- **One question write path**: `game_management_service` create / merged update / import, all
  validated by `QuestionCreate.validate_structure`; create/update run under T8's game row lock
  (import creates a new game). T8's fresh-read rule: every read after `lock_game` that feeds a
  check is `fresh(...)`/`locking=True`, taken before any ORM assignment.
- Naming: `config`/`answer_data` keys snake_case; server-built socket payloads camelCase; REST
  snake_case.
- **Projector** = host game screens; **phone** = player app; **editor** = host
  `QuestionEditorPage`. **Live session** = T8's definition (`LOBBY`/`IN_PROGRESS` and the Redis
  room exists; `image_service.live_sessions`). **Question number** "Qn" = T8's definition.
- **Integer** means `type(x) is int`: booleans are rejected wherever T7 expects an integer
  (config, answer key, confirmation stamp, player answers).
- **As stored** = the row written, flushed and read back in the request's transaction
  (`_as_stored`), T8's basis for exact measurement.

### 2.3 Sort It (`sort_into_boxes`) — n = cards, k = boxes

**Data** (enforced in `validate_structure`; unknown keys → 422):

| Field | Rule |
|---|---|
| `config.boxes` | list of 2–4 strings; each 1–30 chars, no leading/trailing whitespace; unique by `casefold()` |
| `config.cards` | list of 4–10 strings; each 1–60 chars, no leading/trailing whitespace; unique by `casefold()` |
| `config.prompt_image_id` | optional (T8: every type may have a prompt image) |
| `answer_data.correct_boxes` | ACCURACY: required, length n, each an integer in `[0, k)`; a box may be empty. COMPLETENESS: optional; if present, same rules |
| `points_value` | ACCURACY: > 0, typed by the author (not derived) |
| Player answer | exactly `{"placements": [...]}`; length n; each `null` or an integer in `[0, k)`; **at least one non-null** |

The editor trims text before sending; the server rejects untrimmed text rather than normalizing,
because `update_question` writes the raw merged changes, not the validated model.

**Scoring (ACCURACY)**
```
right  = #{i : placements[i] == correct_boxes[i]}
wrong  = #{i : placements[i] is not null and != correct_boxes[i]}
credit = max(0, right − wrong/(k−1)) / n
points = points_value × credit
is_correct = (right == n)            # credit is exactly 1.0 only here
```
COMPLETENESS: any valid answer → `(points_value, True)`. A malformed answer under either mode →
`(0, False)`: the T7 branch runs **before** the existing COMPLETENESS shortcut.

**Why this rule, stated accurately.** Per card, a random placement is worth
`1/k·(+1) + (k−1)/k·(−1/(k−1)) = 0` on average, and a card left out is worth exactly 0 — before
the floor. The per-question floor at 0 (kept for consistency with `multi_select`, and so a question
never subtracts from a total) truncates the downside, so **a player who places every card at random
does not average zero**. Exact expected credit for all-random placement (enumerated; asserted by a
unit test):

| n \ k | 2 | 3 | 4 |
|---|---|---|---|
| 4 | 18.8% | 14.8% | 10.5% |
| 6 | 15.6% | 11.0% | 9.9% |
| 8 | 13.7% | 10.2% | 7.8% |
| 10 | 12.3% | 9.1% | 7.5% |

compared with 1/k (50%, 33%, 25%) under plain fraction-right. Guessing is roughly neutral for a
player already well above 0, and slightly positive for one who knows almost nothing.

Examples (n = 8, k = 3): 8 right → 1.0; 6 right, 2 wrong → 0.625; 6 right, 2 left out → 0.75;
4 right, 4 wrong → 0.25; 2 right, 6 wrong → 0 (floor). k = 2: 6 right, 2 wrong → 0.5.

**Rule shown on the phone:** "Right: +1 card. Wrong: −1/(k−1) card (e.g. −½). Left out: 0. This
question's score never goes below 0. Unsure? Leaving a card out costs nothing."

**Answer counts, reveal, display order**
- Distribution: for each card i, `"{i}:{box}"` or `"{i}:none"` → n keys per answer, at most
  n·(k+1) ≤ 50 keys per question. Only validated answers are counted.
- Reveal: ACCURACY `{type: "sort_into_boxes", correctBoxes: int[]}`; COMPLETENESS
  `{type: "completeness"}`.
- **Shared card order.** `_question_payload` adds `cardOrder: int[]`, a permutation of `0..n−1`
  from `random.Random(question.id)` (rotated by one if it equals the author's order). Projector and
  phones display that order, so the projector doesn't leak the author's grouping; answers use
  original indices; late join, reconnect and host sync agree.

**Screens**
- **Phone:** card pool (wrapping chips) above the boxes (2-column grid). Place by **drag**
  (pointer events, `setPointerCapture`, 8 px threshold between tap and drag, `touch-action: none`
  on chips only so the page still scrolls, drop target via `elementFromPoint`) or **tap a card,
  then a box**. Tapping a placed card selects it to move or "Return to pool". Keyboard: cards and
  boxes are buttons (Enter/Space); accessible names "Card Frog, in pool" / "Card Frog, in box
  Amphibian"; a polite live region announces moves. Submit enabled once ≥1 card is placed; with
  unplaced cards it asks "2 cards are left out and score 0. Submit anyway?". No auto-submit at
  time-out. Inputs disabled after submit and while locked.
- **Projector (question):** prompt, box labels, cards in `cardOrder`. Nothing revealed.
- **Projector (results):** ACCURACY — per card: its correct box, bar of % of answering players who
  placed it there, % left out, most common wrong box. COMPLETENESS — each card's most common box
  and its %.
- **Phone (results):** from the results payload alone (§2.7): "6 of 8 correct, 1 left out" + "640
  of 1000 points"; COMPLETENESS "Answer recorded — you placed 7 of 8 cards"; "No answer — 0 of
  1000 points" when unanswered.

### 2.4 Spot It (`find_in_image`)

**Data** (shape in `validate_structure`; image-dependent rules in the service; unknown keys → 422):

| Field | Rule |
|---|---|
| `config.image_id` | **required**, positive integer; in the same game's library (T8 `assert_in_library`, canvas role) |
| `config.grid` | exactly `{"cols", "rows"}`; integers, each 2–10 (structural; always enforced). The image-dependent **density limits** (§2.4 geometry) gate **confirmation and play**, not saving or importing an unconfirmed question |
| `config.width`, `config.height` | **server-derived, stored, never exported**: set on every create/update/import from the current image (`get_image_meta` / the processed import image); sent values are ignored. Phones use them for proportions |
| `config.prompt_image_id` | absent or null (the canvas image is the question's image) |
| `answer_data.accepted_cells` | list of exactly-`{"col","row"}` objects; integers in grid bounds; no duplicates; ACCURACY 1…⌊cols·rows/2⌋; COMPLETENESS optional, 0…⌊cols·rows/2⌋ |
| `answer_data.confirmed_for` | absent/null, or exactly `{"image_id","image_version","cols","rows"}`, positive integers (§2.5) |
| `points_value` | ACCURACY: > 0 |
| Player answer | exactly `{"col": int, "row": int}`; `0 ≤ col < cols`, `0 ≤ row < rows` of the question's grid |

**Scoring:** ACCURACY — answer in `accepted_cells` → `(points_value, True)`, else `(0, False)`.
COMPLETENESS — any valid answer → `(points_value, True)` (unchanged behaviour; only valid answers
reach scoring). Malformed → `(0, False)` under either mode (T7 branch before the shortcut).

**Answer counts / reveal:** one key `"{col},{row}"` per answer, ≤ cols·rows ≤ 100 keys. Reveal:
ACCURACY `{type: "find_in_image", acceptedCells: [{col,row}…]}`; COMPLETENESS
`{type: "completeness"}`.

**Labels:** columns A–J (index 0 = A), rows 1–10; cell "B3"; accessible "column B, row 3".
Identical on projector, phone, editor, results and report.

**Grid geometry and density (both cell width and height matter).** One pure function,
`spot_it.max_grid(width, height)`, with a copy in each app's `lib/spotGrid.ts` (a deliberate copy,
like T8's `SafeImage`), defines the limits from a **reference phone**: a 360×640 CSS px portrait
viewport → canvas box 328×352 (16 px gutters; 55% of the height), minus 20 px header bands (column
letters on top, row numbers on the left) → grid box 308×332. For image aspect `a = width/height`:
`gridW = min(308, 332·a)`, `gridH = gridW / a`; `max_cols = min(10, ⌊gridW/28⌋)`,
`max_rows = min(10, ⌊gridH/28⌋)`. A grid **fits** the image only if `cols ≤ max_cols` and
`rows ≤ max_rows`, so every cell is **≥ 28 px wide and ≥ 28 px tall** on the reference phone
(above WCAG 2.2's 24 px minimum). An image with `max_cols < 2` or `max_rows < 2` (wider than 5.5:1
or taller than 1:5.9) fits no grid: a question using it can be saved but never confirmed ("This
image is too wide (6:1) for a Spot It grid on phones; crop it to at most 5.5:1").

| Image | Grid box | Max grid | Editor default | Default cell |
|---|---|---|---|---|
| 16:9 landscape | 308×173 | 10×6 | 6×3 | 51×58 |
| 4:3 | 308×231 | 10×8 | 6×4 | 51×58 |
| 1:1 | 308×308 | 10×10 | 6×6 | 51×51 |
| 3:4 portrait | 249×332 | 8×10 | 6×8 | 42×42 |
| 9:16 portrait | 187×332 | 6×10 | 6×10 | 31×33 |
| 3:1 panorama | 308×103 | 10×3 | 6×2 | 51×51 |
| 1:3 tall | 111×332 | 3×10 | 3×9 | 37×37 |
| 6:1 / 1:6 | — | refused | — | — |

- **Editor default** (when an image is chosen and the grid is untouched):
  `cols = min(6, max_cols)`, `rows = clamp(round(cols·height/width), 2, max_rows)`. The editor shows
  the limits ("up to 10×6 for this image") and the resulting phone cell size, and disables
  confirmation while the grid doesn't fit (the draft still saves); the server's confirmation check
  (422 naming the limits) is authoritative.
- **Where density applies — confirmation and playability only:**
  - saving or importing an **unconfirmed** question never checks density; its grid and accepted
    cells are stored exactly as sent (structural rules still apply) and are never resized or
    remapped;
  - an **attempted confirmation** (a sent stamp, or `confirmed: true` in a bundle) whose grid
    doesn't fit the current image is refused with 422 naming the limits;
  - a question whose grid doesn't fit its current image **needs review** (reason
    `grid_too_dense`), so room creation and start stay blocked until the author fixes the grid
    and cells and reconfirms. After a replace with a new shape (e.g. a 10×6 landscape grid on a
    portrait image, limit 6×10), that is exactly what happens.
- **Phone runtime fit:** the canvas fits its container width and ≤ 55vh with header bands; cell
  size therefore varies by device. **Fallback for smaller or landscape screens:** an always-visible
  control row under the canvas — "Selected: B3" plus four 44 px buttons (← → ↑ ↓) that move the
  selection exactly like the arrow keys (from A1 when none) — keeps the question answerable at any
  cell size; in-cell labels are drawn only when cells are ≥ 28 px (headers always); when the
  smallest cell is < 24 px in landscape, a hint says "Turn your phone upright for a bigger grid".
- **Matching coordinates and proportions:** projector, phone, editor, results, game over and the
  report all place the grid area with aspect `config.width : config.height` (equal to the image
  while the question is confirmed) and index cells by `(col, row)` from the top-left; only the scale
  differs. Canvases expose `data-grid-cols`, `data-grid-rows` and the computed grid rectangle as
  data attributes for automated checks.

**Canvases (per app)**
- Backing store = CSS size × `min(devicePixelRatio, 3)`; redraw on `ResizeObserver`, on a DPR
  change (`matchMedia` resolution listener), and on state change only. Grid lines drawn twice (dark
  3 px at 50%, then white 1 px at 80%) to read over any image.
- **Projector:** full image from T8's session link map (`imageLinks[String(config.image_id)].url`)
  in the grid area, transparent grid and header labels, in the image area T8 reserves beside the
  join panel (~60vh). Failure follows T8 §2.12: first error → `refreshImageLinks()`, retry once;
  then the grid stays with "Image unavailable — {description}" and Retry. Play is never blocked.
  The canvas stays visible when the question locks (the prompt text still hides, as today).
- **Phone:** grid and labels only — **never the image, at any phase**. Pointerdown inside the grid
  selects the cell under it (capture); pointermove moves the selection; header taps are ignored;
  `touch-action: none` on the canvas. Keyboard: `tabIndex=0`, visible focus ring, arrows move,
  Enter/Space = Lock in; `aria-label` describes the grid; a polite live region and visible text
  show "Selected: B3". "Lock in B3" is disabled until a cell is chosen, after submit, and while
  locked. Rotation keeps the selection.
- **Projector results (only on entering RESULTS):** heatmap — each cell with ≥ 1 answer filled with
  one colour from a 5-step colour-blind-safe sequential ramp (viridis stops, 55% opacity; step =
  count when max ≤ 5, else `ceil(5·count/max)`); the count in every non-empty cell; a legend
  (swatches with count ranges, "blank = no answers", "N of M answered"); for ACCURACY, every
  accepted cell outlined (white inner, black outer) with the text **"Correct"**. COMPLETENESS:
  heatmap, counts and legend only. Colours in one constants module per app (T9 themes it later).
- **Phone results (text, from the results payload alone, §2.7):** "You picked B3 — Correct" /
  "Incorrect", "Correct cells: B3, C3", "1000 of 1000 points"; "No answer" + correct cells + "0 of
  1000 points" when unanswered; COMPLETENESS "You picked B3 — answer recorded".
- **Game over:** projector cards show a small heatmap canvas (small link) with counts and Correct
  outlines; phones show text per question.

**Accessibility statement (required by T8 §2.12):** Sort It is fully keyboard and screen-reader
operable. Spot It's phone input is keyboard operable, but finding the object requires seeing the
projected image: Spot It is visual-only. The editor's Spot It help text says so.

### 2.5 Confirmation and reconfirmation (server-validated)

- **Stamp:** `answer_data.confirmed_for = {image_id, image_version, cols, rows}` records the image
  version and grid the accepted cells were checked against (T8 §2.16).
- **Status (computed, never stored):** *confirmed* iff the stamp exists, `image_id ==
  config.image_id`, the image is in the game, `image_version ==` its current `version`,
  `cols/rows == config.grid`, **and the grid fits the current image** (§2.4). Otherwise **needs
  review**, with every reason that applies, in this order: `grid_too_dense`, `image_changed`,
  `image_replaced`, `grid_changed`, `not_confirmed`. (Confirmation already requires a fitting grid,
  so `grid_too_dense` appears on a stamped question only after a replace; it is still part of the
  status so no path can make an unplayable grid playable.) COMPLETENESS questions need confirmation
  too: it guarantees `config.width/height` (the phones' proportions) match the image at start.
- **Writes (create/update, under the game lock, after all locking reads):**
  1. `meta = image_service.get_image_meta(db, game_id, config.image_id, locking=True)`.
  2. `config.width/height := meta.width/height` (T8: reconfirming refreshes them; T7 refreshes on
     every write, so "confirmed ⇒ dimensions current" holds by construction).
  3. A stamp **sent in this request** (create body; update body's `answer_data`) is an attempted
     confirmation. It must equal `{config.image_id, meta.version, grid.cols, grid.rows}`, otherwise
     **409** "The image or grid changed after the accepted cells were checked (checked: image 12 v2,
     6×4; now: v3, 6×4). Review the cells and save again." Then the grid must fit
     `max_grid(meta.width, meta.height)`, otherwise **422** "Can't confirm: a 10×6 grid is too dense
     for this image on phones (at most 6×10). Change the grid, re-check the accepted cells, and
     confirm again."
  4. Without a sent stamp, density is not checked: an unconfirmed question saves with any
     structurally valid grid and needs review.
  5. A stamp carried only by the update merge is not re-validated; if stale, the question simply
     needs review (e.g. a PUT that changes only the grid).
- **Editor:** a checkbox "Accepted cells checked against this image and grid". Initially ticked iff
  the server reports the question confirmed; unticked by changing the image or grid, or when the
  library reports a newer version of the form's image; **disabled** while the grid doesn't fit the
  image, with the limits shown ("This image allows up to 6×10"). Save sends the stamp
  `{image_id, image_version: libraryItem.version, cols, rows}` when ticked, `null` when not, so
  drafts (including too-dense ones) can be saved unconfirmed. Changing the grid never moves the
  accepted cells; cells outside a smaller grid must be removed by the author (the form lists them)
  before saving, since out-of-bounds cells are a structural 422.
- **API:** `QuestionResponse` gains `needs_review: bool` and `review_reasons: list[str]` (empty
  when confirmed), computed in the service for list/create/update responses.
- **Room gates (T8 §2.16):** `create_room` (right after its course-match check, under the lock) and
  `start_game` (after its fresh question read) refuse with **409** "These Spot It questions need
  review before this game can run: Q3 (grid too dense for the image, image replaced), Q7 (grid
  changed). Open the game in the editor, fix and check each one, and confirm it." Questions are read `fresh(...)` (shared) and
  sorted in Python (T8's no-SQL-sort rule); images via `get_image_meta(..., locking=True)`. The
  start check is a backstop that the live guard (§2.6) makes unreachable through normal writes.

### 2.6 Live sessions: freezing T7 questions (T7 decision)

Question edits during a live session are unblocked for existing types (existing limitation). For
the two new types they are not: while `image_service.live_sessions(db, redis, game_id,
locking=True)` is non-empty, **a T7 question is frozen except for its prompt text, its time limit
and (Sort It) its prompt image.** A create/update is refused with 409 (naming each live session in
T8's format) if, **compared as stored**, it would change on a stored T7 question any of:
- `type` (to anything), `grading_type`, `points_value`;
- the answer key and confirmation: the whole `answer_data` (`correct_boxes`; `accepted_cells`,
  `confirmed_for`);
- the answer surface: Sort It `config.boxes`/`config.cards`; Spot It `config.image_id`/`config.grid`
  (`config.width/height` can't change while live: images can't be replaced then, T8).

and also if it would change another question's `type` **to** a T7 type, or create a Spot It
question that isn't confirmed (a LOBBY session would include it at start). Creating a confirmed
Spot It or any valid Sort It question stays allowed (a LOBBY session includes it at start; an
IN_PROGRESS session's list is already fixed).

- **Why:** every player in a session must be scored and revealed against one key and one shape.
  The gateway re-reads the row on each submit and at results, so a mid-session change would mix
  keys across players, reject in-flight answers (fatal error screens), or re-map cells.
- **How it is compared:** the fresh locked read of the question is captured as plain values before
  any assignment; after T8's assignment and `_as_stored`, the frozen fields are compared on the
  stored forms (exact despite `FLOAT` rounding and MySQL JSON normalization). A refusal raises and
  `get_db` rolls back, so a refused change leaves the question exactly as it was.
- **Scope:** the live read happens only when the stored or resulting type is a T7 type; other
  types' writes are unchanged. `create_question`/`update_question` gain a `redis` parameter; both
  routers pass `Depends(get_redis)`.
- **Consequence:** fixing a wrong answer key on a T7 question requires ending (or deleting) the
  session first; the 409 says so.
- Unchanged (existing): deleting an unanswered question mid-session still errors at advance;
  reorders are unaffected; existing types keep today's mid-session behaviour.

### 2.7 Realtime contract, reveal phases, reconnects

**Payloads**

| Event | T7 change |
|---|---|
| `new_question`, late-join push, host `sync_state` question (`_question_payload`) | `config` as stored (Spot It: `image_id`, `grid`, `width`, `height` — ids, never links); T8's `images` is `{}` for T7 types. Sort It adds `cardOrder` |
| `submit_answer` | T7 answers validated in the gateway before `record_answer` (after the existing phase/lock/idempotency checks): `answer_data` must be a dict of the exact shape, else `error` and no `SessionScore`. The stored `answer_data` is the normalized answer |
| `answer_received` | unchanged |
| host `question_results` | `answerReveal` (T7 variants) + bounded `answerDistribution` |
| player `question_results` | **T7 questions only (other types unchanged):** `questionType`, `gradingType`, `pointsValue` (maximum points), `answered: bool`, `yourAnswer` (their stored normalized answer, or null), `yourIsCorrect: bool` — from the gateway's existing per-question score query (adds the `answer_data` and `is_correct` columns) |
| `game_over` | shape unchanged; player items already carry `type`, `gradingType`, `maxPoints`, `playerAnswer`, `answerReveal`; host items carry rebuilt distributions |

**Self-contained T7 results.** The phone's T7 result screens read **only** the
`question_results` payload (never `currentQuestion` or `lastAnswerData`): type and grading mode
from `questionType`/`gradingType`; full/partial/none from `yourPoints` vs `pointsValue` with
`yourIsCorrect` for "full"; Spot It labels from `yourAnswer` and `acceptedCells`; Sort It counts
from `yourAnswer.placements` and `correctBoxes` (n = its length). This is what makes results
correct after a **reconnect before results** (a phone that answered, reloaded and rejoined gets
`sync_state` and no question content, then the full results payload). It does **not** change the
existing limitation that a phone or projector **refreshed during RESULTS** gets no results until
the next question: `sync_state` carries none, and T7 doesn't re-send them.

**Reveal phases**

| Phase | Projector | Phone |
|---|---|---|
| QUESTION open | prompt + Sort It board / Spot It image+grid; answered count only | Sort board / grid canvas |
| QUESTION locked (host, auto-lock, host disconnect) | prompt text hides (existing); board/canvas stay | inputs disabled |
| answer phase ended | unchanged; host-enabled auto-advance emits advance after 1.5 s | feedback or still on question |
| RESULTS (Show Results or enabled auto-advance only) | Sort bars / Spot heatmap, counts, legend, Correct outlines | text results |
| GAME OVER | per-question cards | text recap |

Nothing answer-revealing is sent before `QUESTION_RESULTS`. Existing: `answer_received` already
carries correctness to the answering phone (hidden on screen, visible in dev tools).

**Reconnects**
- Phone, not yet answered: late-join `new_question` (remaining time ≥ 5 s) → board/canvas redraw;
  local selection lost.
- Phone, answered then reloaded before results: no question content (existing); results render
  fully from the payload (above).
- Phone or projector refreshed during RESULTS: no results until the next question (existing).
- Projector refreshed mid-question: `sync_state` restores the question and canvas; answered count
  restarts (existing).
- Host disconnect: auto-lock disables phone inputs; rejoin auto-unlocks (existing).

### 2.8 Results, summaries, reports, CSV

- Host game-over summary: reveal + distribution rebuilt from `session_scores.answer_data` with the
  **same** parse/key functions as live play; stored answers that don't fit the current shape are
  skipped.
- **HTML report**: badges "Sort It" (`badge-sort`) / "Spot It" (`badge-spot`) with CSS; reveals via
  the shared functions; distributions computed per type from the session's scores
  (`_extract_answer_key` keeps its single-key contract).
  - Sort It table: card | correct box | % correct | most common wrong box | % left out
    (COMPLETENESS: card | most common box | %).
  - Spot It: inline SVG in the stored proportions — the **small copy** as `<image>` via
    `data_uri(image, "small")` when `include_images`, else T8's withheld text over a neutral
    background — heat fills, counts, Correct outlines/labels, legend; T8's "replaced after this
    session" note when applicable.
  - Answers that no longer fit the current question (edited after the session): "N answers were
    given to an earlier version of this question and aren't shown."
- CSV exports are points-only: no change.

### 2.9 Export / import

**One portable representation, used by both export and measurement.** `bundle_service.build_bundle`
is the only producer; `bundle_size` calls it (`include_data=False`), and every export-rule check
(question writes as stored, image writes, import as stored) goes through `bundle_size`. So the
bytes exported and the bytes measured always come from the same transform.

- **Sort It:** no transform (v1 when the library is empty, else v2 per T8).
- **Spot It** (always v2; T8 maps `config.image_id` ↔ key):
  - `config`: **`width`/`height` omitted** (server-derived; rebuilt on import). Exported config =
    `{"image_id": key, "grid": {...}}`.
  - `answer_data`: `{"accepted_cells": […]}` (when present) plus `"confirmed": true` **only when**
    the §2.5 status is confirmed against the exported image's current version; the key is omitted
    otherwise. `confirmed_for` is never exported. Key order fixed by the transform, independent of
    MySQL's JSON key order.
  - Needs `ImageRecord.version` (read by `export_inputs`); T8's `replace_image` substitutes
    `version + 1` in its export-rule check, because a replace can drop `"confirmed": true`.
- **Import** (within T8's §2.15 order):
  - step 5, `find_in_image`: `answer_data.confirmed` must be a boolean if present (absent = false);
    `answer_data.confirmed_for` → 422 "Question N: bundles record confirmation as
    answer_data.confirmed"; `config.width/height` in a bundle are ignored; `confirmed` is set aside
    before `QuestionCreate`. A v1 bundle can't carry Spot It (T8's v1 image-reference refusal, or
    "config.image_id is required").
  - Grid and accepted cells are **preserved exactly** (structural rules only: integer bounds,
    accepted cells within the grid, no duplicates, half cap); nothing is resized or remapped.
  - after step 6, before any write: only a question marked `confirmed: true` is an attempted
    confirmation, and its grid must fit its processed image (§2.4), otherwise 422 "Question N is
    marked confirmed, but its 10×6 grid is too dense for its image (at most 6×10); remove
    "confirmed" or fix the grid". Unconfirmed questions are not density-checked. (The app never
    exports such a bundle: confirmed status includes the fit.)
  - step 8: `config.width/height` from the processed image; if confirmed,
    `confirmed_for = {real image id, image_version: 1, cols, rows}`; otherwise none → needs review
    (with `grid_too_dense` too when the grid doesn't fit). The export rule is measured on the game
    as stored (T8).
- **Replaced before reconfirmation** (e.g. a 10×6 landscape question whose image became portrait):
  stored `width/height` still describe the old image, but they aren't exported; the question
  exports with its 10×6 grid and cells and without `confirmed`, imports unchanged as needing review
  (`grid_too_dense`, `not_confirmed`) with dimensions rebuilt from the portrait image, re-exports
  identically, and can't be played until the author fixes the grid/cells and reconfirms.
- **Round trips:** export → import → export gives identical bytes for both types and both
  confirmation states.

Example bundle questions (v2):
```json
{"type": "find_in_image", "grading_type": "ACCURACY", "prompt": "Find the lighthouse",
 "config": {"image_id": "img-1", "grid": {"cols": 6, "rows": 4}},
 "answer_data": {"accepted_cells": [{"col": 4, "row": 1}, {"col": 4, "row": 2}], "confirmed": true},
 "time_limit_seconds": 30, "points_value": 1000}
```
```json
{"type": "sort_into_boxes", "grading_type": "ACCURACY", "prompt": "Sort by class",
 "config": {"boxes": ["Mammal", "Reptile", "Amphibian"],
            "cards": ["Whale", "Gecko", "Frog", "Bat", "Newt", "Tortoise"]},
 "answer_data": {"correct_boxes": [0, 1, 2, 0, 2, 1]},
 "time_limit_seconds": 45, "points_value": 1000}
```

### 2.10 T8 contract mapping (nothing in T8 is weakened)

| T8 supplies (§2.16) | T7 use |
|---|---|
| `get_image_meta(..., locking=True)` | density check, width/height, stamp validation, room/start gates |
| canvas refs in `image_refs`, library check, `used_by`, delete refusal, bundles | Spot It `image_id` (T7 only opens it in `validate_structure`) |
| full links only in the host session link map | projector canvas; phones get none (`player_images` is option-only) |
| `data_uri(image, "small")` | report heatmap |
| replace at any aspect ratio; live guard | reconfirmation + density recheck; stable versions during sessions |
| export rule on stored rows | T7 fills server fields before `_as_stored`; one bundle transform for export and size |
| lock in `create_room`/`start_game`, fresh reads | T7 gates right after the lock (after the course check) |

### 2.11 Existing limitations (recorded, not fixed by T7)

Mid-session edits for existing types; refresh during RESULTS loses results (both apps); phones
can't see game over after refresh; any socket `error` is fatal to the client (T7's malformed-answer
rejections are reachable only by non-standard clients, plus the existing "Question is locked" race);
`answer_received` leaks correctness in dev tools; reports rebuilt from current data; the player
app's `yourPoints > 0` rule for existing types; game-over summaries still `ORDER BY` in SQL (T8
§5); no unique constraint on `session_scores`; `maxPossibleScore` `int()` truncation (fractional
Sort It points make it visible); `FLOAT` precision of points (~7 significant digits); non-dict
`answer_data` or non-numeric `selectedIndex` can crash existing branches; late joiners get ≥ 5 s;
game over and max score include questions created after start; `user-scalable=no` on the player.

## 3. Alternatives (revised)

| Alternative | Why it lost |
|---|---|
| Exact tap point + distance scoring with margin/falloff | Superseded: whole-cell selection is settled |
| Author-drawn target rectangles | Superseded: authors select accepted cells directly |
| Partial credit for neighbouring cells | All-or-nothing is settled |
| The image on phones (any phase) | T8 host-only delivery; bandwidth at question start; keeps eyes on the projector |
| Pinch-zoom / scrollable phone canvas | Gesture conflicts with cell selection; the density rule plus nudge buttons keep every grid answerable |
| Exporting stored `width/height` | Stale after a replace before reconfirmation; import would rewrite them, breaking byte-identical round trips |
| A density rule on columns only | A wide image with many rows gives unusably short cells |
| Confirmation by image `sha256` | T8's contract is version-based; stored bytes differ from hand-authored files |
| Server stamps the current version on "confirm" | Races a replace between viewing and saving; a client-sent version is checked instead |
| Always import Spot It unconfirmed | Every round trip and sample game would need re-review before running |
| Freeze only the shape while live | Players in one session could be scored against different keys |
| Block all question edits while live | Changes existing types' behaviour; only T7 is in scope |
| Snapshot questions into Redis at start | More machinery than a write-time freeze |
| Results fields for every type | Widens an existing payload beyond T7 |
| Per-phone card shuffle | Projector would show author order and leak the grouping |
| No floor on Sort It | Would subtract from totals; inconsistent with `multi_select` |
| Accept all-null Sort It placements | Free COMPLETENESS points for an empty board |
| Sort It plain fraction-right / all-or-nothing / on a canvas | Rewards guessing / no partial information / worse accessibility |
| Subject-specific canvases, Match Up, Put In Order, Label the Image, Room Poll, Drawing, speed bonus | As in the draft |

## 4. Detailed implementation

No migration: new fields live in `config`/`answer_data`; `questions.type` is a plain string.

### 4.1 Backend (`backend/app/`)
- **`services/sort_it.py`** (new, pure): `validate`, `parse_answer`, `score`, `dist_keys`,
  `reveal`, `card_order`.
- **`services/spot_it.py`** (new, pure; imports only `image_refs` keys): `validate`, `max_grid`,
  `parse_answer`, `score`, `dist_key`, `reveal`, `cell_label`, `review_status`, `stamp_for`,
  `export_fields` (the portable config/answer_data transform).
- **`schemas/admin.py`**: both types in the `QuestionCreate` and `QuestionUpdate` regexes;
  `validate_structure` delegates; `_validate_image_refs` requires `image_id` on `find_in_image` and
  refuses a non-null `prompt_image_id` there (still refused on other types); `QuestionResponse`
  gains `needs_review`, `review_reasons`. (Density is not a schema rule: it needs the image.)
- **`services/game_service.py`** (adds `image_service`, as T8 anticipated): `calculate_score` T7
  branch before the COMPLETENESS shortcut; `record_answer` dist keys; both summaries' reveals and
  dist rebuild; `spot_it_review_blockers(db, game_id)`; gates in `create_room` and `start_game`.
- **`services/game_management_service.py`**: `create_question`/`update_question(…, redis)`: after
  `assert_in_library`, the T7 locking reads (`get_image_meta(locking=True)`;
  `live_sessions(locking=True)` when a T7 type is involved) and the plain-value snapshot of the
  stored question, then `export_inputs(locking=True)`; then on plain values: for a sent stamp only,
  the stamp match (409) then the density fit (422); creation/type-to-T7 live checks (409);
  width/height into the written config;
  then T8's assignment and `_as_stored`, the frozen-field comparison (409), and the export rule.
  `list_questions` and write responses attach review status. Import hooks (§2.9).
- **`services/bundle_service.py`**: `ImageRecord.version`; `build_bundle` applies
  `spot_it.export_fields`.
- **`services/image_service.py`**: `export_inputs` fills `ImageRecord.version`; `replace_image`
  substitutes `version + 1` in `check_export_change`.
- **`websocket/gateway.py`**: `_answer_reveal` delegation; `on_submit_answer` T7 shape checks
  (dict first), normalized storage; `_question_payload` adds `cardOrder`; per-player
  `question_results` T7 fields.
- **`services/report_service.py`**: badges + CSS, reveals, `_images.for_question` returns the canvas
  image, Sort It table, Spot It SVG heatmap, stale-answer note.
- **`routers/host.py`, `routers/admin.py`**: pass `redis` to question create/update; responses with
  review status.

### 4.2 Host app (`frontend/host/src/`)
- `types/game.ts`: unions, explicit `AnswerReveal` variants, config fields, `cardOrder`, summary
  fields.
- `lib/questionImages.ts`: `referencedImageIds` includes the canvas `image_id`. New
  `lib/spotGrid.ts` (`maxGrid`, fit, cell-from-point, labels, heat steps, default grid) and
  `lib/canvasColors.ts`.
- New presentational `components/question/SpotItCanvas.tsx` (question, results-heatmap and
  author-toggle modes; keyboard; data attributes) and `components/question/SortItResults.tsx`.
- `pages/QuestionEditorPage.tsx`: both types; form state; `questionToForm`/`formToPayload` (points
  typed in both modes; derivation table gains two rows); Sort It editor (2–4 boxes, 4–10 cards,
  correct-box select; removing box j unassigns its cards and shifts later indices; client checks
  mirror the server); Spot It editor (T8 `ImagePicker` for the canvas image, prompt-image picker
  hidden, grid inputs with limits, default and phone cell size, author canvas, "3 of max 12"
  accepted cells, confirmation checkbox, status and reason); list lines "Sort It · 3 boxes · 8
  cards" / "Spot It · 6×4 · 2 cells" and a **Needs review** badge; questions reload after an image
  replace; the live-freeze 409 shown in the form's error line.
- `components/images/ImageLibraryPanel.tsx`: Replace warns when `used_by` has a canvas use ("Q3
  (Spot It) will need review").
- `pages/game/QuestionPage.tsx`, `ResultsPage.tsx`, `GameOverPage.tsx`: T7 branches.

### 4.3 Player app (`frontend/player/src/`)
- `types/game.ts`: unions, `PlayerAnswerReveal` variants, `cardOrder`, the T7 results fields.
- `lib/spotGrid.ts` (copy), new `components/SortBoard.tsx`, `components/SpotCanvas.tsx` (with the
  nudge-button row; presentational; answer via callback).
- `pages/game/QuestionPage.tsx` branches; `ResultsPage.tsx` T7 branches reading only the payload,
  with explicit "No answer"; `GameOverPage.tsx` branches (no answer = null `playerAnswer`;
  correctness from answer + reveal, identical to the server rule).

### 4.4 Tests — unit (`tests/unit/`)
- `test_sort_it.py`: every data rule (booleans, untrimmed, casefold duplicates, lengths,
  COMPLETENESS optional key); answer parsing (non-dict, extra keys, wrong length, bools, floats, out
  of range, all-null); scoring k = 2/3/4, floor, unplaced, full = exactly `points_value`,
  `is_correct`, malformed under COMPLETENESS → 0; dist keys; reveal; `card_order` determinism and
  non-identity; the random-guess table enumerated and asserted.
- `test_spot_it.py`: grid bounds and bools; `max_grid` for every row of the §2.4 table plus the
  5.5:1 and 1:5.9 edges and refusals beyond; accepted-cell bounds/duplicates/half cap; COMPLETENESS;
  `image_id` required; `prompt_image_id` refused; stamp shape; answer parsing; scoring both modes;
  dist key; reveal; `review_status` for each reason alone and combined (including
  `grid_too_dense` on a stamp that otherwise matches), in the defined order; labels;
  `export_fields` (no width/height;
  `confirmed` only when confirmed; fixed key order).
- `test_bundle_service.py` additions: `bundle_size` equals the export length for games with Spot It
  questions confirmed, unconfirmed, and replaced-before-reconfirmation with different stored
  dimensions; replace substitution with `version + 1`.

### 4.5 Tests — integration (`tests/integration/`, live stack)
In a new self-contained `test_t7_question_types.py` (env like `test_t8_images.py`; images generated
with Pillow, landscape 1600×900 and portrait 900×1600; reuses T8's `conftest` helpers, MySQL lock
harness and padding-question headroom helper):
- **Validation:** 422 on create **and** merged update for each structural rule (a removed box that
  `correct_boxes` uses; a grid shrunk under an accepted cell; duplicate cells; bool entries; grid
  1 or 11); Spot It with another game's image / no image / a prompt image.
- **Density gates confirmation, not saving:** for the landscape and the portrait image, an
  unconfirmed grid beyond `max_grid` saves (200, `needs_review`, `grid_too_dense`) with grid and
  cells unchanged, and an attempted confirmation of it → 422 naming the limits; a question on a 6:1
  image saves unconfirmed, and every confirmation attempt → 422; `create_room` refuses both.
- **Confirmation:** stale sent stamp → 409; confirmed create → `needs_review` false; bogus
  `width/height` overwritten; grid-only PUT → `grid_changed`.
- **Regression — 10×6 landscape replaced with portrait before reconfirmation:** a confirmed
  ACCURACY question on a 1600×900 image with a 10×6 grid and accepted cells including column 9
  (J) → replace with a 900×1600 image → `review_reasons` = [`grid_too_dense`, `image_replaced`],
  grid, cells and stamp unchanged → `create_room` 409 naming "Q n" → export (grid 10×6, cells,
  no `width/height`, no `confirmed`) → import succeeds with grid and cells unchanged, `width/height`
  = 900×1600, `review_reasons` = [`grid_too_dense`, `not_confirmed`] → re-export byte-identical to
  the first export → `create_room` on the imported game 409 → confirming 10×6 → 422 → the author
  changes the grid to 6×10 and replaces out-of-bounds cells (an attempt that leaves column-9 cells
  → structural 422) → confirms → 200 → `create_room` 201 and the game starts. The same repair is
  checked on the original game.
- **Start backstop:** a stale stamp planted in MySQL after room creation → host advance gets the
  error and the session stays LOBBY.
- **Live freeze** (with a LOBBY room, and with an IN_PROGRESS session): each of `type`,
  `grading_type`, `points_value`, `correct_boxes`, `accepted_cells`, un-confirming, `boxes`,
  `cards`, `image_id`, `grid`, and a type change *to* a T7 type → 409; after each, GET returns the
  question unchanged in every field, the export meter is unchanged, and (IN_PROGRESS) the next answer
  is scored against the original key. Prompt and time-limit edits → 200; unconfirmed Spot It
  create → 409; confirmed create → 201; the same edits succeed once the session has ended.
- **Lock test** (T8 harness; both requests waiting before release): `create_room` vs an
  un-confirming update when no session exists yet → exactly one consistent outcome (update 200 then
  room 409, or room 201 then update 409).
- **Gameplay over sockets:** Sort It full/partial/zero (k = 2 and 3; approx compare for `FLOAT`),
  Spot It in/out, both COMPLETENESS; malformed answers (bool col, extra key, out of bounds, list
  `answer_data`, all-null placements) → `error`, no `SessionScore`, another player's next valid
  answer still scored; distribution keys ⊆ the bounded set; reveals; `cardOrder` identical for host
  and players; host game-over distribution equals the live one.
- **Self-contained results:** for both types × both grading modes × {answered, unanswered,
  answered-then-reconnected (new socket, `join_room`) before results}: the player's
  `question_results` carries `questionType`, `gradingType`, `pointsValue`, `answered`,
  `yourAnswer`, `yourIsCorrect` with correct values, and the reconnected player received no
  `new_question`; other types' `question_results` are unchanged.
- **Image isolation:** every event a phone receives for a Spot It question contains no
  `/api/images/` string and `images == {}`; the host session link map contains the canvas image.
- **Export/import:** Sort It v1 and Spot It v2 round trips byte-identical; a Spot It question
  replaced (landscape → portrait) and **not** reconfirmed exports without `width/height` and
  without `confirmed`, imports needing review with `width/height` = 900×1600, and re-exports
  byte-identical; `confirmed_for` in a bundle, non-bool `confirmed`, `confirmed: true` with a grid
  too dense for the bundled image, Spot It in v1 → 422 (an unconfirmed too-dense question imports:
  see the regression test).
- **Export-size boundary** (padding question at exact headroom, meter equal to the downloaded
  export length at every step; the Spot It question uses a 6×6 grid, valid for both image shapes,
  so reconfirming changes only the exported flag): with the unconfirmed post-replacement question at headroom 0 the
  game exports to exactly 41,943,040 bytes, re-imports, and re-exports identically; reconfirming
  (adds `"confirmed": true`, c bytes) is refused at headroom c − 1 with the question unchanged and
  accepted at headroom c, landing exactly on the limit; a replace at headroom 0 is measured with
  `version + 1` (the dropped flag included) and the meter then equals the actual export length.
- **Report:** Sort It table; Spot It SVG with counts and "Correct"; small-copy data URI; withheld
  text for a host without game access.
- **Access:** a host without the game grant and a guest → 403 creating either type.

Also: `engine/scoring.py` (`compute_question_score`, `_build_reveal`) and
`scenarios/all_question_types.py` gain an independent Sort It implementation and scenarios (Spot It
gameplay stays in the new module, which uploads and confirms an image); `scripts/simulate_players.py`
gains Sort It (random with ~20% left out; correct with `--game-json`) and Spot It (random cell; an
accepted cell with `--game-json`).

### 4.6 Browser checks (Playwright through nginx, `npm run build`, `:8080`)
Projector 1440×900; phones 390×844, 360×640 and 640×360 landscape at DPR 1/2/3; a landscape
(16:9) and a portrait (9:16) image:
- both types end to end; drag and tap-tap; keyboard on the phone canvas and the editor canvas;
- **coordinates:** tapping the centre of each corner cell and one interior cell on the phone
  submits that `{col, row}`; the phone's and projector's grid rectangles (data attributes) have the
  same aspect as `width/height` within 1 px; the nudge buttons reach every cell in landscape;
- rotation keeps the selection; the editor's shown limits match the server's confirmation limits,
  its checkbox is disabled while the grid doesn't fit, and a too-dense draft still saves;
- reveal only after Show Results and after enabled auto-advance; heatmap, counts, legend, Correct
  outlines; COMPLETENESS variant;
- **results without local state:** reload the phone after answering, before results → full T7
  results text; reload during RESULTS → "Loading results…" (existing limitation, expected);
- projector image-failure placeholder + Retry; editor checkbox, Needs review after replace, live
  freeze 409 in the form, 409 room message on Play; no horizontal scroll at 360 px.

### 4.7 Physical-phone checklist
LAN setup per T8 §4.12 (host at `http://<LAN>:8080/host/`, temporary `CORS_ORIGINS` entry, recreate
the backend, remove afterwards). Record device model, OS and browser versions and pass/fail per
item:
1. Join by QR.
2. Sort It: drag with a finger without the page scrolling; tap-tap placing; move a placed card;
   return to pool; 10 cards + 4 boxes fit the width; submitting with a card left out confirms.
3. Spot It with a landscape and a portrait image: corner and edge taps select the intended cell;
   drag moves the selection; nudge buttons; Lock in; no pinch zoom; rotate mid-question (canvas
   resizes, selection kept); background the browser and return.
4. The phone never shows the Spot It image (question, results, game over).
5. Results text: selected cell, correct/incorrect, correct cells, points; "No answer" when skipped;
   correct after reloading between answering and results.
6. Host lock / disconnect disables inputs; unlock re-enables.
7. The projector heatmap is readable from the back of the room.

### 4.8 Docs and context files
`README.md` (bundle schema for both types; `confirmed`; no `width/height` in bundles);
`docs/realtime.md` (`cardOrder`, T7 answer shapes, T7 results fields); `CLAUDE.md` updates only
where stale (context-sync rule): `backend/app` (checklist entries, new modules, module dependencies,
the live-freeze and confirmation invariants), `services`, `schemas`, `websocket`, `frontend` (grid
geometry copies), host `pages`/`components`/`lib`, player `pages`/`components`/`lib`.

### 4.9 Order of work (after the Goldfish review and authorization)
0. Rebuild (`docker compose up --build`), `alembic upgrade head`, baseline unit + integration runs.
1. Pure modules + unit tests.
2. Schema validation; service writes (width/height, stamp and density on confirmation, live
   freeze, statuses); gates; routers.
3. Scoring, distributions, summaries, gateway checks and payload fields.
4. Export/import (`bundle_service`, `image_service` tweaks); report.
5. Host editor.
6. Projector screens.
7. Phone screens.
8. Integration tests; `simulate_players.py`; browser checks; physical phone; docs/context files;
   `mean-review`.

### 4.10 Verification

- CI checks, run locally: `ruff check backend/ scripts/`, `ruff format --check backend/ scripts/`,
  and `tsc --noEmit` in all three apps.
- `cd tests/unit && pytest -v`; `cd tests/integration && pytest -v` with the stack up. The lock
  tests must run, not skip; T8's suites stay green; existing tests are not deleted or weakened.
- `python scripts/simulate_players.py --room <CODE> --game-json <exported T7 bundle>` for both types.
- The §4.6 browser checks and the §4.7 physical-phone checklist, with results recorded in this
  document.

## 5. Risks

- Phone space for 10 cards + 4 boxes at 360 px; the density limits' feel on real devices.
- Spot It difficulty/ties depend on the image, grid and time limit (no speed element).
- Canvas redraw cost on low-end phones (redraw on change only).
- The live freeze blocks mid-session key fixes; its 409 names the sessions and the fix.
- Two copies of the grid geometry (host, player) plus the backend's `max_grid` can drift; the
  browser coordinate checks and the editor-vs-server limit check guard it.
- T8 may change during its review → re-merge and re-run the baseline.

**Trade-offs accepted in this design:**
- **Live freeze** blocks legitimate mid-session key corrections on T7 questions; the host must end
  the session. Existing types keep the mixed-key hazard.
- **Trusting `confirmed: true` in bundles:** a hand-written bundle can claim confirmation for cells
  that don't match its image — the same trust as accepting its `accepted_cells`; the gate protects
  against later changes, not authoring mistakes.
- **Reference phone:** 360×640 portrait is an assumption; smaller or landscape screens rely on the
  nudge buttons rather than comfortable cells.
- **Unplayable drafts are storable:** a game can hold (and import) Spot It questions that can never
  be confirmed until edited, e.g. on a 6:1 image; the editor and the room 409 must make the fix
  obvious.
- **Confirmation for COMPLETENESS** adds an author step with no answer key, only to keep phone
  proportions fresh.
- **Sent-stamp 409 for API clients:** re-sending a stored stale `confirmed_for` is refused; an API
  client must send `null` or a current stamp.
- **Half-grid cap** limits a large object on a coarse grid; the author can increase density within
  the limits.
- **Strict unknown-key rejection** on T7 configs relies on T8 never omitting a canvas `image_id` on
  export (true while delete is refused for referenced images).
- **Start-time backstop** surfaces as the existing fatal projector error; acceptable only because
  normal writes can't reach it.

## 6. Out of scope

T6 sample games (`sample_games/` unchanged; T7 uses in-test fixtures), T9 theming, and every
existing limitation in §2.11 (each would get its own branch).

## 7. Changes from the pre-T8 draft

- Spot It rewritten to whole cells: grid config, directly selected accepted cells, all-or-nothing
  ACCURACY, unchanged COMPLETENESS, precise validation; all point/rectangle/distance/margin/falloff
  content and tests removed.
- Phones never receive or show the image; the projector draws image + transparent grid on a canvas;
  the phone canvas shows only grid, labels, proportions.
- Results: projector heatmap, counts, legend, "Correct" outlines only on entering RESULTS; phone text
  results with explicit "No answer", **self-contained** in the payload (T7 only), tested across
  grading modes, answered/unanswered and reconnect-before-results; the refresh-during-RESULTS
  limitation stated as unchanged.
- Grid geometry uses **both cell width and height** against a reference phone, with explicit limits
  per image shape, a nudge-button fallback, and matching-coordinate tests for landscape and portrait
  images. **Density gates confirmation and play only:** unconfirmed drafts (including imports)
  keep any structurally valid grid and cells unchanged; an attempted confirmation must fit the
  current image; a non-fitting grid needs review and blocks room creation/start. Regression test:
  10×6 landscape replaced with portrait, round trip identical, play blocked until fixed and
  reconfirmed.
- Server-validated confirmation, "Needs review" with all applicable reasons, room/start gates,
  width/height server-derived.
- Export/import: **one portable representation** (no `width/height`, `confirmed` only when true)
  used by both export and exact measurement; tested for a replaced-but-unconfirmed image, round
  trips and the export-size boundary.
- **Live freeze** of T7 questions (type, grading, points, answer key, answer surface), compared as
  stored, with tests that refused edits leave the question unchanged.
- Sort It kept; guessing explanation corrected with an exact table; shared `cardOrder`; all-null
  placements rejected; malformed answers score 0 before the COMPLETENESS shortcut.
