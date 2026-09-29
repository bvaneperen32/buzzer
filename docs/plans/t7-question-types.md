# T7 — Two New Question Types: Sort It and Spot It

**Destination:** `docs/plans/t7-question-types.md`
**Status:** **Ready for implementation** (2026-09-29). The comprehension, criticism and
implementation-readiness reviews are complete, every required correction from them is applied, and
no required issue remains.
- The pre-T8 Elephant draft is preserved in commit `a3a0f50`; §7 lists what changed from it. §8,
  §9 and §10 record how each review's findings were resolved.
- This branch has T8 merged (merge `07c0bf8`, T8 tip `7fbd2b2`, implementation `990fed5`), so the
  T8 code and `docs/plans/t8-image-support.md` are the baseline.
- Implementation begins at §4.9 step 0 when it is authorized. If implementation reveals a flaw,
  revise this document first, then the code.

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
- **Text rules (server-authoritative)** for Sort It box and card labels:
  - a label is a `str`, and its length is counted in Unicode code points (Python `len`);
  - it must be non-empty and equal `s.strip()`, where whitespace is exactly the characters for
    which `str.isspace()` is true: this includes U+001C–U+001F, U+0085, U+00A0, U+2000–U+200A and
    U+3000, and excludes U+200B and U+FEFF;
  - uniqueness compares `s.casefold()`, with no Unicode normalization, so "Straße" and "STRASSE"
    collide while a precomposed "é" and "e" + U+0301 don't.

  The editor only approximates these rules (§4.2); the server's 422 is authoritative. The
  editor also preserves every label it didn't edit, exactly as loaded (§2.3).
- **Validated answer** = the answer exactly as received, once it passes its type's exact-shape
  check. T7 stores it unchanged in `session_scores.answer_data`; nothing is rewritten.
- **Review status** (§2.5) applies only to Spot It. Every other type, Sort It included, reports
  `needs_review: false` and `review_reasons: []`.
- **Points display** applies to two places only: the **phone's** T7 results and game-over screens
  (awarded and maximum points), and the **maximum-points label in each T7 question's report
  `q-timing` header**. The projector shows no per-player points and gains no new points display;
  its existing labels keep their current formatting, and the report stays aggregate-only (no
  individual scores). The rule: at most one decimal place, rounding half up (half away from
  zero), with a trailing ".0" dropped.
  - Apps: `Intl.NumberFormat(undefined, {maximumFractionDigits: 1})` (its default rounding mode
    is half-expand).
  - Report: `Decimal(repr(x)).quantize(Decimal("0.1"), ROUND_HALF_UP)`, then `format(d, "f")`
    with a trailing ".0" dropped. Never `normalize()`, which prints 1000 as "1E+3".
  - Both round the value's **shortest decimal representation** half up, so they agree: verified
    0.15 → "0.2", 2.55 → "2.6", 0.35 → "0.4" in both. `Decimal(x)` would instead round the exact
    binary value (0.1499…) and give "0.1" and "2.5", disagreeing with the apps (§9, D5).
  - Examples: 687.5 → "687.5"; 708.333… → "708.3"; 1.25 → "1.3"; 1000 → "1,000" on the phone
    (en-US), "1000" in the report.
  - The phone formats in the viewer's locale (`undefined`), keeping the locale's digit grouping,
    so separators follow it (de-DE shows "0,2" and "1.000"); the rounding is identical. The report
    is locale-independent. Browser checks pin the test browser's locale instead of changing the
    app (§4.6). **Every phone text example in this document is its en-US rendering** (e.g.
    "687.5 of 1,000 points").
  - Stored and scored values are never rounded. Existing screens keep their current formatting.

### 2.3 Sort It (`sort_into_boxes`) — n = cards, k = boxes

**Data** (enforced in `validate_structure`; unknown keys → 422):

| Field | Rule |
|---|---|
| `config.boxes` | list of 2–4 strings; each 1–30 code points, trimmed, unique (§2.2 text rules) |
| `config.cards` | list of 4–10 strings; each 1–60 code points, trimmed, unique (§2.2 text rules) |
| `config.prompt_image_id` | optional (T8: every type may have a prompt image) |
| `answer_data.correct_boxes` | ACCURACY: required, length n, each an integer in `[0, k)`; a box may be empty. COMPLETENESS: optional; validated with the same rules if present, and its presence (or absence) is kept as stored |
| `points_value` | ACCURACY: > 0, typed by the author (not derived) |
| Player answer | exactly `{"placements": [...]}`; length n; each `null` or an integer in `[0, k)`; **at least one non-null** |

The server rejects untrimmed text rather than normalizing it, because `update_question` writes
the raw merged changes, not the validated model.

**Editor label rule.** Each editor label row (box or card) remembers the text it was loaded with;
new rows have none. At save, a row whose current text equals its loaded text sends the **loaded
text unchanged**; any other row sends its text trimmed with JavaScript's `trim()`. Unedited labels
therefore round-trip byte for byte — including one with a U+FEFF at its edge from an import or an
API write, which the server accepts (U+FEFF isn't `isspace`) but `trim()` would strip — and the
editor never rewrites text the author didn't touch. No server rule changes for this. The editor's
checks remain approximations (§4.2); the server's 422 is authoritative.

**Editor answer-key rule (`correct_boxes`).** An answer key whose current value equals the loaded
one (including its presence) is sent exactly as loaded. When the author edits it, the editor
**never emits a partial or invalid key**:
- **ACCURACY:** every card has a correct-box selector, and Save is blocked ("Assign every card a
  box") until every card has one; the full list is sent.
- **COMPLETENESS:** an "Include an answer key (not scored)" toggle, on when the key was loaded
  present and off when it was loaded absent. On: Save is blocked until every card has a box ("Assign
  every card a box, or turn off the answer key"), then the full list is sent. Off: `correct_boxes`
  is omitted.
- **Card and box edits:** a new card starts unassigned, and removing box j unassigns its cards and
  shifts later indices, so an existing key becomes incomplete and Save stays blocked until the
  author assigns the cards (or, in COMPLETENESS, turns the key off).
- **Switching the grading mode never silently discards an authored key.** The form keeps the key
  (and any partial assignments) across switches. ACCURACY → COMPLETENESS turns the toggle on with
  that key; COMPLETENESS → ACCURACY makes the key required, showing the kept assignments. Switching
  back without other edits leaves the key equal to the loaded one, so it is sent exactly as loaded.
  (While a session is live, `grading_type` is frozen anyway, §2.6.)

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
  unplaced cards it asks "2 cards are left out and score 0. Submit anyway?". Inputs disabled
  after submit and while locked.
- **Lock and time-out — no automatic submission.** When the question locks (the time-out
  auto-lock, the host's Lock, or a host disconnect), unsubmitted placements stay on screen but
  are disabled. If the host unlocks the same question, they are editable again and Submit works.
  Only a new question or a reload clears them. A board that was never submitted shows "No answer"
  at results. (Lock and unlock only toggle a `disabled` prop; the page stays mounted, keyed by
  `questionId`.)
- **Projector (question):** prompt, box labels, cards in `cardOrder`. Nothing revealed.
- **Projector (results):** ACCURACY — per card: its correct box, bar of % correct, % left out,
  most common wrong box. COMPLETENESS — each card's most common box and its %. Cards are listed in
  `cardOrder`, the order the room saw during the question (the game-over card and the report use
  the author's order).
- **Results percentages.** Two implementations follow these definitions exactly:
  - Python `sort_it.results_breakdown` serves **the report** (and its unit tests);
  - **one** host TypeScript port, `lib/sortResults.ts` (a deliberate copy, like `spotGrid.ts`,
    cross-checked in §4.6), serves **both** host results pages: ResultsPage from the live
    `question_results`, and GameOverPage from the host game-over summary. That summary keeps its
    existing shape and all its existing fields; it sends the rebuilt `answerDistribution` and
    `totalAnswered`, and no precomputed breakdown.

  Both take the same inputs: the `"{i}:{box}"` / `"{i}:none"` distribution counts, the answered
  count, `correctBoxes` (ACCURACY), n and k.
  - the denominator is the question's **answered count**: the valid answers stored for it in
    this session (not the number of players);
  - "% correct" = answers placing the card in its correct box; "% left out" = answers with
    `null` for it;
  - "most common wrong box" = the most frequent box for the card other than its correct box
    (and never "left out"), a tie going to the lowest box index, "—" when nobody placed it
    wrongly;
  - COMPLETENESS "most common box" = the most frequent box among answers that placed the card
    (same tie rule; "—" when nobody placed it), with its % of the answered count;
  - a **displayed percentage is an integer, rounded half up in integer arithmetic**:
    `⌊(200·count + answered) / (2·answered)⌋` — never `round` or `Math.round` (1 of 8 = 12.5 % →
    13 %). Bar lengths use the exact fraction;
  - with no answers, every percentage and "most common" value shows "—".
- **Phone (results):** from the results payload alone (§2.7): "6 of 8 correct, 1 left out" +
  "687.5 of 1,000 points" (k = 3: `(6 − 1/2) / 8 = 0.6875`); COMPLETENESS "Answer recorded — you
  placed 7 of 8 cards"; "No answer — 0 of 1,000 points" when unanswered. Points use the §2.2
  display rule (en-US renderings shown).
- **Game over.**
  - **Projector:** a Sort It `QuestionCard` shows one row per card in the author's order (the
    answer is already revealed). ACCURACY rows: card, correct box, % correct, % left out.
    COMPLETENESS rows: card, most common box, its %. Computed by `lib/sortResults.ts` from the host
    summary's rebuilt `answerDistribution` and `totalAnswered`.
  - **Phone:** one line per question, e.g. "Sort It — 6 of 8 correct, 1 left out · 687.5 / 1,000
    pts", "Sort It — No answer · 0 / 1,000 pts", or for COMPLETENESS "Sort It — Placed 7 of 8
    cards · 1,000 / 1,000 pts". Counts come from the summary item's `playerAnswer` (null when
    unanswered) and `answerReveal`.

### 2.4 Spot It (`find_in_image`)

**Data** (shape in `validate_structure`; image-dependent rules in the service; unknown keys → 422):

| Field | Rule |
|---|---|
| `config.image_id` | **required**, positive integer; in the same game's library (T8 `assert_in_library`, canvas role) |
| `config.grid` | exactly `{"cols", "rows"}`; integers, each 2–10 (structural; always enforced). The image-dependent **density limits** (§2.4 geometry) gate **confirmation and play**, not saving or importing an unconfirmed question |
| `config.width`, `config.height` | **Server-owned, stored, never exported.** `validate_structure` never checks them: any value, or none, is accepted, and they don't count as unknown keys. The service discards whatever arrived and writes the trusted image dimensions — on create/update after the locked `get_image_meta` read, just before the write, **always persisting the merged `config` even when the request omitted `config`** (§2.5 Writes, step 2); on import at step 8, from the processed image. Phones use them for proportions |
| `config.prompt_image_id` | absent or null (the canvas image is the question's image) |
| `answer_data.accepted_cells` | list of exactly-`{"col","row"}` objects; integers in grid bounds; no duplicates; ACCURACY 1…⌊cols·rows/2⌋; COMPLETENESS optional, 0…⌊cols·rows/2⌋, and its presence (absent vs `[]`) is kept as stored. Stored in the order sent: order matters only for storage (and so for the live freeze, §2.6); scoring uses set membership, and reveals and displays sort row-major (row, then column) |
| `answer_data.confirmed_for` | absent, or exactly `{"image_id","image_version","cols","rows"}`, positive integers. A `null` in a request is accepted and removed before writing (§2.5), so stored data never holds `confirmed_for: null` |
| `points_value` | ACCURACY: > 0 |
| Player answer | exactly `{"col": int, "row": int}`; `0 ≤ col < cols`, `0 ≤ row < rows` of the question's grid |

**Scoring:** ACCURACY — answer in `accepted_cells` → `(points_value, True)`, else `(0, False)`.
COMPLETENESS — any valid answer → `(points_value, True)` (unchanged behaviour; only valid answers
reach scoring). Malformed → `(0, False)` under either mode (T7 branch before the shortcut).

**Answer counts / reveal:** one key `"{col},{row}"` per answer, ≤ cols·rows ≤ 100 keys. Reveal:
ACCURACY `{type: "find_in_image", acceptedCells: [{col,row}…]}`, with the cells sorted row-major
whatever their stored order; COMPLETENESS `{type: "completeness"}`.

**Labels:** columns A–J (index 0 = A), rows 1–10; cell "B3"; accessible "column B, row 3".
Identical on projector, phone, editor, results and report.

**Grid geometry and density (both cell width and height matter).** One pure function,
`spot_it.max_grid(width, height)`, with a copy in each app's `lib/spotGrid.ts` (a deliberate copy,
like T8's `SafeImage`), defines the limits from a **reference phone**: a 360×640 CSS px portrait
viewport → canvas box 328×352 (16 px gutters; 55% of the height), minus 20 px header bands (column
letters on top, row numbers on the left) → grid box 308×332. The image's grid area is the
largest rectangle of its aspect ratio inside that box, and every cell must be at least 28 px in
both directions. Because 308 / 28 = 11 and 332 / 28 = 83 / 7, this is computed **in integers
only** (identical in Python and both TypeScript copies; `width`, `height` are the image's pixel
dimensions):

```
if 308·height ≤ 332·width:      # the width limits the grid area
    max_cols = 10
    max_rows = min(10, ⌊11·height / width⌋)
else:                           # the height limits it
    max_rows = 10
    max_cols = min(10, ⌊83·width / (7·height)⌋)
```

A grid **fits** the image only if `cols ≤ max_cols` and `rows ≤ max_rows`, so every cell is
**≥ 28 px wide and ≥ 28 px tall** on the reference phone (above WCAG 2.2's 24 px minimum). An
image with `max_rows < 2` (wider than 11:2, i.e. 5.5:1) or `max_cols < 2` (taller than 14:83,
about 1:5.93) fits no grid: a question using it can be saved but never confirmed ("its image is
too wide for any grid (crop it to at most 11:2)"). Exact edges: 1100×200 fits (10×2) and 1101×200
fits no grid; 140×830 fits (2×10) and 140×831 fits no grid; at the branch tie (308×332) both
branches give 10×10.

| Image | Grid box (≈) | Max grid | Editor default | Default cell (≈) |
|---|---|---|---|---|
| 16:9 landscape (1600×900) | 308×173 | 10×6 | 6×3 | 51×58 |
| 4:3 (1600×1200) | 308×231 | 10×8 | 6×5 | 51×46 |
| 1:1 | 308×308 | 10×10 | 6×6 | 51×51 |
| 3:4 portrait (1200×1600) | 249×332 | 8×10 | 6×8 | 42×42 |
| 9:16 portrait (900×1600) | 187×332 | 6×10 | 6×10 | 31×33 |
| 3:1 panorama (1500×500) | 308×103 | 10×3 | 6×2 | 51×51 |
| 1:3 tall (500×1500) | 111×332 | 3×10 | 3×9 | 37×37 |
| 6:1 / 1:6 | — | fits no grid | — | — |

- **When the editor applies a default grid.** The form tracks where its grid came from: loaded,
  edited by the author, or defaulted. A default is applied only when the form has **no grid yet**
  (a new Spot It question, or a question switched to Spot It in the form) and an image is chosen,
  and it is recomputed on a later image change only while the grid is still a default the author
  hasn't touched. The editor **never resets a loaded question's grid** (its accepted cells depend
  on it) **and never resets a grid the author has edited**. Choosing a different image then keeps
  the grid, unticks the checkbox (§2.5) and shows the new image's limits; if the grid doesn't fit,
  confirmation stays disabled until the author changes it.
- **Editor default** (computed as above), in integers with rounding half up:
  `cols = min(6, max_cols)`;
  `rows = clamp(⌊(2·cols·height + width) / (2·width)⌋, 2, max_rows)` — never `Math.round` or
  Python `round`, which disagree at .5 (4:3 gives 4.5, so 5 rows). No default exists when the
  image fits no grid. The editor shows the limits ("up to 10×6 for this image") and the resulting
  phone cell size, and disables confirmation while the grid doesn't fit (the draft still saves);
  the server's confirmation check (422 naming the limits) is authoritative.
- **Where density applies — confirmation and playability only:**
  - saving or importing an **unconfirmed** question never checks density; its grid and accepted
    cells are stored exactly as sent (structural rules still apply) and are never resized or
    remapped;
  - an **attempted confirmation** (a sent stamp, or `confirmed: true` in a bundle) whose grid
    doesn't fit the current image is refused with 422 naming the limits, or, for an image that fits
    no grid, saying the image is too wide or too tall (§2.5 Writes, step 3; §2.9);
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
- **Two sources of dimensions, deliberately different:**
  - **Gameplay dimensions** — `config.width/height`, written by the server at each save. The
    projector, phone, results, game-over and report grids place the grid area with this aspect
    (equal to the image while the question is confirmed).
  - **Authoring dimensions** — the selected image's **current library `width`/`height`** (T8's
    `ImageItem`). The editor's author canvas, its density limits, its default grid and its
    phone-cell preview use these, **never** `config.width/height`. That covers new questions
    (nothing stored yet), a different image chosen in the form, and an image replaced since the
    last save (whose stored dimensions are stale until the next write). They are exactly the values
    the next save will store, so authors check cells against the geometry play will use. When the
    library reloads with new dimensions for the form's image, the author canvas redraws and the
    checkbox unticks (§2.5).
  - Both index cells by `(col, row)` from the top-left; only the scale differs. Canvases expose
    `data-grid-cols`, `data-grid-rows` and `data-grid-rect="x,y,w,h"` — the grid area in CSS px
    relative to the canvas element's border box, each number with at most two decimals — for
    automated checks.

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
- **Lock and time-out — no automatic submission.** A selected cell that isn't locked in is never
  submitted. When the question locks (the time-out auto-lock, the host's Lock, or a host
  disconnect), the selection stays visible but inactive: the canvas, the nudge buttons and Lock in
  are disabled. If the host unlocks the same question, the selection and Lock in work again. Only
  a new question or a reload clears it. A question that was never locked in shows "No answer" at
  results. (Lock and unlock only toggle a `disabled` prop; the page stays mounted, keyed by
  `questionId`.)
- **Projector results (only on entering RESULTS):** heatmap — each cell with ≥ 1 answer filled with
  one colour from a 5-step colour-blind-safe sequential ramp (viridis stops, 55% opacity; step =
  count when max ≤ 5, else `ceil(5·count/max)`); the count in every non-empty cell; a legend
  (swatches with count ranges, "blank = no answers", "N of M answered"); for ACCURACY, every
  accepted cell outlined (white inner, black outer) with the text **"Correct"**. COMPLETENESS:
  heatmap, counts and legend only. Colours in one constants module per app (T9 themes it later).
- **Phone results (text, from the results payload alone, §2.7):** "You picked B3 — Correct" /
  "Incorrect", "Correct cells: B3, C3" (row-major), "1,000 of 1,000 points"; "No answer" +
  correct cells + "0 of 1,000 points" when unanswered; COMPLETENESS "You picked B3 — answer
  recorded". Points use the §2.2 display rule (en-US renderings shown).
- **Game over:** projector cards show a small heatmap canvas (small link) with counts and Correct
  outlines. Phones show one line per question: "Spot It — You picked B3 — Correct · Correct
  cells: B3, C3 · 1,000 / 1,000 pts", "Spot It — No answer · Correct cells: B3, C3 · 0 / 1,000
  pts", or for COMPLETENESS "Spot It — You picked B3 · 1,000 / 1,000 pts".

**Editor answer-key rule (`accepted_cells`).** Cells the author doesn't touch are sent exactly as
loaded (order and presence included). When the author edits them:
- **ACCURACY:** required, 1…⌊cols·rows/2⌋ cells; sent in stored order with new cells appended and
  removed cells deleted in place; Save is blocked outside that range.
- **COMPLETENESS:** optional; `accepted_cells` is sent **iff it was loaded present or it is
  currently non-empty**, in stored order with new cells appended. So a key loaded absent that the
  author ticks and then unticks stays absent; a loaded `[]` stays `[]`; a loaded non-empty key
  that the author empties is sent as `[]`.
- **Switching the grading mode never silently discards the cells.** The form keeps them across
  switches and emits them under the rule for the current mode; COMPLETENESS → ACCURACY makes at
  least one cell required. Switching back without other edits leaves them equal to the loaded
  value, so they are sent exactly as loaded.

**Accessibility statement (required by T8 §2.12):** Sort It is fully keyboard and screen-reader
operable. Spot It's phone input is keyboard operable, but finding the object requires seeing the
projected image: Spot It is visual-only. The editor's Spot It help text says so.

### 2.5 Confirmation and reconfirmation (server-validated)

- **Stamp:** `answer_data.confirmed_for = {image_id, image_version, cols, rows}` records the image
  version and grid the accepted cells were checked against (T8 §2.16).
- **One shared rule.** `spot_it.ImageInfo(id, version, width, height)` describes the current
  image, and the pure function
  `spot_it.review_status(config, answer_data, image: ImageInfo | None) -> list[str]` returns the
  question's review reasons. The question is **confirmed** iff the list is empty. `image` is
  `None` when `config.image_id` isn't in the game's library. Every caller uses this one function:
  - question writes and the room/start gates: `ImageInfo` from
    `get_image_meta(..., locking=True)`;
  - question list responses: from a plain `images_for_refs` read (write responses reuse the
    write's locked meta);
  - export and the exact size measurement: from the image's `ImageRecord`, which carries
    `version`, `width` and `height` (§2.9).

  COMPLETENESS questions need confirmation too: it guarantees `config.width/height` (the phones'
  proportions) match the image at start.

  `review_status` and `export_fields` **never raise**, matching T8's "nothing that reads
  references may raise on a malformed one": stored Spot It data that fails the shape check (only
  reachable by direct database edits) is reported as needing review (`not_confirmed`) and exported
  without `confirmed`. Nothing repairs or migrates it; like T8's other invalid stored data it is
  outside the re-import guarantee, and its next edit must make it valid.

- **Reasons,** always listed in this order ("stamp" = the stored `confirmed_for`; "grid" =
  `config.grid`; `max_cols`/`max_rows` from §2.4 for the image's width and height):

  | # | Code | Condition | Can combine with | Display text |
  |---|---|---|---|---|
  | 1 | `image_missing` | `image` is `None` | nothing — it is then the only reason | "its image is missing" |
  | 2 | `grid_too_dense` | image present, and `cols > max_cols` or `rows > max_rows` | 3, 4, 5, 6 | "the C×R grid is too dense for its image (at most C′×R′)"; when `max_rows < 2`: "its image is too wide for any grid (crop it to at most 11:2)"; when `max_cols < 2`: "its image is too tall for any grid (crop it to at most 14:83)" |
  | 3 | `image_changed` | stamp present and `stamp.image_id ≠ config.image_id` | 2, 5 | "a different image was chosen after the cells were checked" |
  | 4 | `image_replaced` | stamp present, `stamp.image_id = config.image_id`, and `stamp.image_version ≠ image.version` | 2, 5 | "its image was replaced after the cells were checked" |
  | 5 | `grid_changed` | stamp present and `(stamp.cols, stamp.rows) ≠ (grid.cols, grid.rows)` | 2, and 3 or 4 | "its grid changed after the cells were checked" |
  | 6 | `not_confirmed` | no stamp | 2 | "its accepted cells haven't been checked" |

  `image_changed` and `image_replaced` are mutually exclusive, and `not_confirmed` excludes both
  and `grid_changed`. Reasons 3–5 compare the stamp's own values, so one stamp can carry
  `image_changed` and `grid_changed` together. The editor capitalises each display text and joins
  them with "; ".

- **Missing image (reason 1).** No application write can produce it: every question write runs
  T8's library check, and an image can't be deleted while a question references it. If one is
  planted anyway (direct database edits): the editor shows "Needs review — its image is missing;
  choose an image"; the room gates refuse; the report shows "[image missing]" where the heatmap
  image would be; and T8's export repair omits the unresolved `image_id`, so that bundle fails
  re-import with 422 "config.image_id is required". That places it outside the re-import
  guarantee, as T8 §2.7 does for other stored questions that break today's rules.

- **When `grid_too_dense` appears.** Without a stamp, whenever the grid doesn't fit. With a stamp:
  after a replace with a new shape (together with `image_replaced`); after a grid change saved
  with a carried stamp (together with `grid_changed`, e.g. a partial PUT of `config`); or if a
  later release tightens the limits. A new confirmation never produces it, because confirmation
  requires a fitting grid. It is part of the status so that no path can make an unplayable grid
  playable.

- **Stamp cases in a create or update request:**
  1. **`answer_data` not sent** (update only; a top-level `"answer_data": null` counts the same,
     since the merge ignores explicit nulls): the stored `answer_data`, stamp included, is carried
     unchanged and not stamp-validated (the merged question is still validated structurally, as
     every update is). If that stamp is stale, the question simply needs review.
  2. **`answer_data` sent without `confirmed_for`, or with `"confirmed_for": null`:** the written
     `answer_data` has no stamp. The service removes a null key before writing (Pydantic's
     `exclude_none` doesn't reach inside the `answer_data` dict), so stored data never holds
     `confirmed_for: null` and the two forms store identically. The question needs review
     (`not_confirmed`, plus `grid_too_dense` if applicable).
  3. **`answer_data` sent with a stamp object:** an attempted confirmation, on create or update,
     even if it equals the stored stamp.

- **Writes (create/update, under the game lock, after all locking reads):**
  1. `meta = image_service.get_image_meta(db, game_id, config.image_id, locking=True)`.
  2. **Every Spot It create and update persists `config`**: the merged config (the stored config
     overlaid with any sent one) with `width`/`height` replaced by `meta.width/height`, **even
     when the request omits `config`** — the service adds `config` to the fields it writes. An
     `answer_data`-only reconfirmation therefore also refreshes the stored dimensions. (T8:
     reconfirming refreshes them; T7 refreshes on every write, so "confirmed ⇒ dimensions
     current" holds by construction. While a session is live the rewritten config equals the
     stored one, because images can't be replaced then, so the §2.6 freeze is unaffected.)
  3. For stamp case 3 only: the stamp must equal `{config.image_id, meta.version, grid.cols,
     grid.rows}`, otherwise **409** "The image or grid changed after the accepted cells were
     checked (checked: image 12 v2, 6×4; now: v3, 6×4). Review the cells and save again." Then the
     grid must fit `max_grid(meta.width, meta.height)`, otherwise **422** with the matching
     `grid_too_dense` wording from the reason table:
     - "Can't confirm: the 10×6 grid is too dense for this image (at most 6×10). Change the grid,
       re-check the accepted cells, and confirm again.";
     - for an image that fits no grid: "Can't confirm: this image is too wide for any grid (crop it
       to at most 11:2). Choose or replace the image, then confirm again." (or "too tall … (crop it
       to at most 14:83)").
  4. Cases 1 and 2 never check density: an unconfirmed question saves with any structurally
     valid grid and needs review.
  5. For case 2, the null `confirmed_for` key is removed from the written `answer_data`.

- **Editor:** a checkbox "Accepted cells checked against this image and grid".
  - It starts ticked iff the server reports `needs_review: false` **and** the loaded stamp's
    `image_id` and `image_version` equal the form's image and that image's current library
    version. Otherwise it starts unticked. This guards against a question list that is momentarily
    stale relative to the library (writes commit after responding), which would otherwise tick the
    box and resend a stale stamp for an unexplained 409.
  - It is unticked by changing the image or the grid, or when the library reports a newer version
    of the form's image. It is **disabled** while the grid doesn't fit the image, with the limits
    shown ("This image allows up to 6×10").
  - At save, the stamp depends on the checkbox's history since the form loaded:
    - **ticked continuously since load**, with no change to the image, the grid or that image's
      displayed library version: the **loaded stamp object exactly**. Its values equal the current
      ones, because the box can start ticked only when the question is confirmed;
    - **any transition from unticked to ticked**, including a box that started unticked (a
      `not_confirmed` question, or one flagged `image_replaced` or `grid_changed`) and is ticked
      with nothing else changed: a **fresh stamp** `{image_id: the form's image, image_version:
      the version of that image currently displayed from the library, cols, rows}`. So
      reconfirming after a replacement is a single tick;
    - **unticked:** `"confirmed_for": null`, so drafts (including too-dense ones) save
      unconfirmed.

    The server rules are unchanged: a fresh stamp is validated like any supplied stamp (§2.5
    Writes, step 3).
  - After a save, the editor applies the response's `needs_review` and `review_reasons` to that
    question immediately instead of waiting for the list reload, because `get_db` commits after
    responding and a reload can briefly return the old state.
  - Changing the grid never moves the accepted cells; cells outside a smaller grid must be removed
    by the author (the form lists them) before saving, since out-of-bounds cells are a structural
    422.
  - A "Needs review" badge shows the display texts.
- **API:** `QuestionResponse` gains `needs_review: bool` and `review_reasons: list[str]` (codes, in
  the order above; empty when confirmed), computed in the service for list/create/update responses.
- **Room gates (T8 §2.16):** `create_room` (right after its course-match check, under the lock) and
  `start_game` (after its fresh question read) refuse with **409**, one sentence per question in
  question-number order, e.g. "These Spot It questions need review before this game can run: Q3 —
  the 10×6 grid is too dense for its image (at most 6×10); its image was replaced after the cells
  were checked. Q7 — its grid changed after the cells were checked. Open the game in the editor,
  fix each question, check its accepted cells, and confirm it." Questions are read `fresh(...)`
  (shared) and sorted in Python (T8's no-SQL-sort rule); images via
  `get_image_meta(..., locking=True)`. The start check is a backstop that the live freeze (§2.6)
  makes unreachable through normal writes.

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
- **The 409's text.** T8's `_live_block_message` is image-specific ("Images can't be …"), so T7
  doesn't reuse its sentence. It reuses only T8's **room list** — `room ABC123 (IN_PROGRESS,
  hosted by …, created …)`, joined with "; " — factored out of `_live_block_message` into a helper
  both use, so T8's own message is unchanged. The T7 message is `{lead-in} Live: {room list}.
  {fix}`:
  - lead-in for a frozen-field change: "Q3 can't be changed while a session of this game is live;
    only its prompt, time limit and (Sort It) prompt image can change until the session ends.";
  - lead-in for a type change to a T7 type: "Q3 can't become a Sort It or Spot It question while a
    session of this game is live.";
  - lead-in for an unconfirmed Spot It create: "A Spot It question can't be added unconfirmed while
    a session of this game is live; check its accepted cells and confirm it first.";
  - fix: "To change it now, the session's host can finish the game or delete the session from
    Play (deleting discards its partial scores); admins can delete sessions from Sessions."
- **How it is compared:** the fresh locked read of the question is captured as plain values before
  any assignment; after T8's assignment and `_as_stored`, the frozen fields are compared on the
  stored forms (exact despite `FLOAT` rounding and MySQL JSON normalization). Both sides are in
  canonical stored form: no `confirmed_for: null` (§2.5), `accepted_cells` in stored order, and
  MySQL's own JSON key order. A refusal raises and `get_db` rolls back, so a refused change leaves
  the question exactly as it was.
- **Editor round trips must preserve frozen fields.** Loading a question into the editor and
  saving it must send every frozen field back exactly as loaded unless the author changed it:
  - `accepted_cells` in stored order (new cells appended, removed cells deleted in place);
  - `correct_boxes` and COMPLETENESS `accepted_cells` present or absent exactly as loaded (absent
    stays absent, `[]` stays `[]`); edited keys follow the per-type answer-key rules (§2.3, §2.4);
  - `points_value` as the exact number received (display rounding never feeds back);
  - `type` and `grading_type` unchanged;
  - the stamp chosen by the §2.5 editor rule — the loaded stamp while the box stays ticked since
    load (every Spot It question in a live session is confirmed, so its box starts ticked);
  - `boxes` and `cards`: unedited labels exactly as loaded (§2.3 editor label rule); `image_id`
    and `grid` unchanged.

  A prompt-only or time-limit edit during a live session therefore leaves every frozen field
  identical as stored and succeeds. §4.5 tests it through the API, and §4.6 through the real
  editor with a matrix covering every T7 variant.
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
| `submit_answer` | T7 answers validated in the gateway before `record_answer` (after the existing phase/lock/idempotency checks): `answer_data` must be a dict of the exact shape, else `error` and no `SessionScore`. The stored `answer_data` is the validated answer (§2.2), unchanged |
| `answer_received` | unchanged |
| host `question_results` | `answerReveal` (T7 variants) + bounded `answerDistribution` |
| player `question_results` | **Existing fields, sent for every type today** (verified in `gateway._on_host_advance_impl`): `questionId`, `answerReveal` (the same reveal the host gets), `yourPoints`, `yourScore`, `yourRank`, `playerCount`. For T7 questions `answerReveal` is `{type: "sort_into_boxes", correctBoxes}`, `{type: "find_in_image", acceptedCells}` (row-major) or `{type: "completeness"}`. **New, for T7 questions only (other types unchanged):** `questionType`, `gradingType`, `pointsValue` (maximum points), `answered: bool`, `yourAnswer` (their validated answer, or null), `yourIsCorrect: bool` — from the gateway's existing per-question score query (adds the `answer_data` and `is_correct` columns). T8 §2.17's "unchanged" described T8; T7 adds only these six fields |
| `game_over` | shape unchanged, with all existing fields kept; player items already carry `type`, `gradingType`, `maxPoints`, `playerAnswer`, `answerReveal`; host items carry the rebuilt `answerDistribution` and `totalAnswered`, from which `lib/sortResults.ts` computes the Sort It card (no precomputed breakdown is added) |

**Self-contained T7 results.** The phone's T7 result screens read **only** the
`question_results` payload (never `currentQuestion` or `lastAnswerData`): type and grading mode
from `questionType`/`gradingType`; full/partial/none from `yourPoints` vs `pointsValue` with
`yourIsCorrect` for "full"; Spot It labels from `yourAnswer` and `answerReveal.acceptedCells`;
Sort It counts from `yourAnswer.placements` and `answerReveal.correctBoxes` (n = its length). This is what makes results
correct after a **reconnect before results** (a phone that answered, reloaded and rejoined gets
`sync_state` and no question content, then the full results payload). It does **not** change the
existing limitation that a phone or projector **refreshed during RESULTS** gets no results until
the next question: `sync_state` carries none, and T7 doesn't re-send them.

**Reveal phases**

| Phase | Projector | Phone |
|---|---|---|
| QUESTION open | prompt + Sort It board / Spot It image+grid; answered count only | Sort board / grid canvas |
| QUESTION locked (host, auto-lock, host disconnect) | prompt text hides (existing); board/canvas stay | inputs disabled; placements and selections kept; unlock restores input; nothing is auto-submitted |
| answer phase ended | unchanged; host-enabled auto-advance emits advance after 1.5 s | feedback or still on question |
| RESULTS (Show Results or enabled auto-advance only) | Sort bars / Spot heatmap, counts, legend, Correct outlines | text results |
| GAME OVER | per-question cards | text recap |

T7 sends nothing answer-revealing before `QUESTION_RESULTS`: no reveal, distribution or answer key
during a question. The existing `answer_received` still sends the submitting phone its own
correctness and points immediately (hidden on screen, visible in dev tools). That is an
acknowledged limitation (§2.11), unchanged by T7.

**Reconnects**
- Phone, not yet answered: late-join `new_question` (remaining time ≥ 5 s) → board/canvas redraw;
  local selection lost.
- Phone, answered then reloaded before results: no question content (existing); results render
  fully from the payload (above).
- Phone or projector refreshed during RESULTS: no results until the next question (existing).
- Projector refreshed mid-question: `sync_state` restores the question and canvas; answered count
  restarts (existing).
- Host disconnect: auto-lock disables phone inputs; rejoin auto-unlocks (existing).
- Lock and unlock never clear a player's placements or selection; only a new question or a reload
  does.
- **Socket reconnect vs page reload.** On a socket reconnect the page stays loaded; T7 components
  are keyed by `questionId`, so placements and selection are kept, and a late-join `new_question`
  for the same question doesn't remount them. A page reload loses them (local state).
- **Phone reloaded or joining while the question is locked:** no question content, even after the
  unlock. The late-join push is sent only while the question is unlocked, and `question_unlocked`
  carries no content, so the phone waits for the next question (existing; §2.11).

### 2.8 Results, summaries, reports, CSV

- Host game-over summary: reveal + distribution rebuilt from `session_scores.answer_data` with the
  **same** parse/key functions as live play; stored answers that don't fit the current shape are
  skipped.
- **HTML report**: badges "Sort It" (`badge-sort`) / "Spot It" (`badge-spot`) with CSS; reveals via
  the shared functions; distributions computed per type from the session's scores
  (`_extract_answer_key` keeps its single-key contract).
  - Sort It table: card | correct box | % correct | most common wrong box | % left out
    (COMPLETENESS: card | most common box | %), in the author's card order, with the §2.3
    percentage definitions computed by `sort_it.results_breakdown`.
  - The report's existing per-question stats line ("N% correct", from `is_correct` with Python
    `round`) measures whole boards for Sort It, so for Sort It it reads "N% all cards correct" to
    avoid confusion with the per-card percentages beside it.
  - **Points:** the report stays aggregate-only (no individual scores). The one T7 change is each
    T7 question's `q-timing` header: its maximum-points label is produced by `_t7_points_label`,
    using the §2.2 report formatter ("708.3 pts", "2.6 pts", "1000 pts"; "pt" only when the
    formatted value is exactly "1"). Existing types keep today's `f"{points_value:g}"` label.
  - Spot It: inline SVG in the stored proportions — the **small copy** as `<image>` via
    `data_uri(image, "small")` when `include_images`, else T8's withheld text over a neutral
    background — heat fills, counts, Correct outlines/labels, legend; T8's "replaced after this
    session" note when applicable.
  - Answers that no longer fit the current question (edited after the session), i.e. stored
    answers that fail the current shape check: "N answers were given to an earlier version of
    this question and aren't shown."
  - A Spot It question whose image is missing (§2.5 reason 1) shows "[image missing]" in place of
    the image.
  - **Escaping.** Every author-controlled string T7 adds to the report goes through `_esc`
    (`html.escape`, quotes included), in element text and attribute values alike, in HTML and
    inline SVG: Sort It box and card labels, the Spot It image description (`alt`, `aria-label`,
    `<title>`), and any text composed from them. Only the prompt stays raw HTML, as today, because
    it is bleach-sanitized on write; labels and descriptions are not sanitized, so they must never
    copy that pattern. Generated text (cell labels, counts, reason texts) goes through `_esc` too.
    The T7 renderers (`_render_sort_table`, `_render_spot_svg`) are pure functions so they can be
    unit-tested. (The app screens render labels as React text or with canvas `fillText`, never as
    HTML.)
- CSV exports are points-only: no change.

### 2.9 Export / import

**One portable representation, used by both export and measurement.** `bundle_service.build_bundle`
is the only producer; `bundle_size` calls it (`include_data=False`), and every export-rule check
(question writes as stored, image writes, import as stored) goes through `bundle_size`. So the
bytes exported and the bytes measured always come from the same transform.

- **Sort It:** no transform (v1 when the library is empty, else v2 per T8).
- **Spot It** (always v2; T8 maps `config.image_id` ↔ key). `build_bundle` calls
  `spot_it.export_fields(config, answer_data, image: ImageInfo | None)` for each `find_in_image`
  question, in this order:
  1. look up the question's `ImageRecord` by `config.image_id` and turn it into
     `ImageInfo(id, version, width, height)`, or `None` if it isn't in the library;
  2. `confirmed = review_status(config, answer_data, image) == []` — the same rule as §2.5;
  3. `config`: **`width`/`height` dropped** (server-owned; rebuilt on import), so the exported
     config is `{"image_id": …, "grid": {...}}`;
  4. `answer_data`: `{"accepted_cells": […]}` in stored order, only if present, plus
     `"confirmed": true` only if confirmed; `confirmed_for` is never exported. The key order is
     fixed by the transform, independent of MySQL's JSON key order;
  5. T8's reference mapping and export repairs then apply to the config (`image_id` → key).
- **`ImageRecord` carries `version`, `width` and `height`.** They are new **required** fields
  (no defaults, so a missed constructor fails loudly), placed before `data`:
  - `export_inputs` selects `Image.version`, `Image.width` and `Image.height`;
  - `add_image`'s pending record uses version 1 and the processed image's dimensions;
  - `replace_image`'s substituted record takes `version + 1` and the processed width and height,
    along with the byte size and filename it already substitutes (a replace can drop
    `"confirmed": true`);
  - `set_description`'s substitution keeps them;
  - T8's unit-test helper in `tests/unit/test_bundle_service.py` passes them.

  Because `bundle_size` calls `build_bundle` with the same records, the bytes measured always
  equal the bytes exported, for stored, pending and replacement records alike.
- **Import** (within T8's §2.15 order):
  - step 5, `find_in_image`: `answer_data.confirmed` must be a boolean if present (absent = false);
    `answer_data.confirmed_for` → 422 "Question N: bundles record confirmation as
    answer_data.confirmed"; `config.width/height` in a bundle are never checked (as in §2.4) and are
    discarded at step 8; `confirmed` is set aside before `QuestionCreate`. A v1 bundle can't carry
    Spot It (T8's v1 image-reference refusal, or "config.image_id is required").
  - Grid and accepted cells are **preserved exactly** (structural rules only: integer bounds,
    accepted cells within the grid, no duplicates, half cap); nothing is resized or remapped.
    `accepted_cells` keeps its order and its presence (absent vs `[]`).
  - after step 6, before any write: only a question marked `confirmed: true` is an attempted
    confirmation, and its grid must fit its processed image (§2.4), otherwise 422 "Question N is
    marked confirmed, but the 10×6 grid is too dense for its image (at most 6×10); remove
    "confirmed" or fix the grid", or, for an image that fits no grid, "Question N is marked
    confirmed, but its image is too wide for any grid (crop it to at most 11:2); remove
    "confirmed" or change the image" (or "too tall … (crop it to at most 14:83)"). Unconfirmed
    questions are not density-checked. (The app never exports such a bundle: confirmed status
    includes the fit.)
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
game over and max score include questions created after start; `user-scalable=no` on the player;
a phone reloaded or joining while the question is locked gets no question content until the next
question, even after an unlock.

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
  `reveal`, `card_order`, `results_breakdown` (the §2.3 percentages, used by the report; the host
  pages use the TypeScript port).
- **`services/spot_it.py`** (new, pure; imports only `image_refs` keys): `ImageInfo(id, version,
  width, height)`, `validate`, `max_grid(width, height)` and `default_grid(width, height)` (integer
  arithmetic, §2.4), `parse_answer`, `score` (set membership), `dist_key`,
  `reveal(question_like)` (cells row-major), `cell_label`,
  `review_status(config, answer_data, image)` (§2.5), `stamp_for(image_id, version, cols, rows)`,
  `export_fields(config, answer_data, image)` (§2.9).
- **Module dependencies** (each arrow means "imports"; T8's graph otherwise unchanged):
  ```
  sort_it                 → (nothing)
  spot_it                 → image_refs
  schemas.admin           → image_refs, sort_it, spot_it
  bundle_service          → image_refs, spot_it, common.exceptions
  game_service            → game_lock, image_service, sort_it, spot_it
  game_management_service → image_refs, bundle_service, image_service, game_lock, game_service,
                            sort_it, spot_it
  report_service          → image_service, image_refs, sort_it, spot_it
  websocket/gateway       → image_service, sort_it, spot_it
  ```
  `sort_it` and `spot_it` import nothing else from the application, and `image_service` and
  `bundle_service` still never import `game_service` or `game_management_service`, so the graph
  stays cycle-free.
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
  the stamp match (409) then the density fit (422); creation/type-to-T7 live checks (409); the
  merged `config` with trusted `width`/`height` added to the written fields **even when the request
  omitted `config`** (§2.5 Writes, step 2); a null `confirmed_for` removed; then T8's assignment
  and `_as_stored`, the frozen-field comparison (409, §2.6 text), and the export rule.
  `list_questions` and write responses attach review status. Import hooks (§2.9).
- **`services/bundle_service.py`**: `ImageRecord` gains required `version`, `width`, `height`;
  `build_bundle` applies `spot_it.export_fields` with each question's `ImageInfo` (§2.9).
- **`services/image_service.py`**: `export_inputs` selects the three new fields; `add_image`'s
  pending record and `replace_image`'s substituted record carry them (§2.9); `set_description`'s
  substitution keeps them. The room-list part of `_live_block_message` is factored into a helper
  that T7's freeze message also uses (§2.6); T8's message text is unchanged.
- **`websocket/gateway.py`**: `_answer_reveal` delegation; `on_submit_answer` T7 shape checks
  (dict first), storing the validated answer; `_question_payload` adds `cardOrder`; per-player
  `question_results` T7 fields.
- **`services/report_service.py`**: badges + CSS, reveals, `_images.for_question` returns the canvas
  image, Sort It table and Spot It SVG heatmap as pure renderers (`_render_sort_table`,
  `_render_spot_svg`) with every author string through `_esc` (§2.8), stale-answer note, the
  `q-timing` maximum-points label for T7 questions through `_t7_points_label` (§2.8), and "N% all
  cards correct" on Sort It's stats line.
- **`routers/host.py`, `routers/admin.py`**: pass `redis` to question create/update; responses with
  review status.

### 4.2 Host app (`frontend/host/src/`)
- `types/game.ts`: unions; explicit `AnswerReveal` variants
  `{type: 'sort_into_boxes'; correctBoxes: number[]}` and
  `{type: 'find_in_image'; acceptedCells: {col: number; row: number}[]}`; config fields;
  `cardOrder`; summary fields; `QuestionResponse`'s `needs_review` and `review_reasons`.
- `lib/questionImages.ts`: `referencedImageIds` includes the canvas `image_id`. New
  `lib/spotGrid.ts` (`maxGrid` and `defaultGrid` in the §2.4 integer arithmetic, fit,
  cell-from-point, labels, heat steps), `lib/canvasColors.ts` and `lib/sortResults.ts` (the one
  TypeScript port of `sort_it.results_breakdown`, §2.3, used by both `ResultsPage` and
  `GameOverPage`). The host app gets no `formatPoints`: the projector shows no per-player points
  (§2.2).
- New presentational `components/question/SpotItCanvas.tsx` (question, results-heatmap and
  author-toggle modes; keyboard; data attributes; in author mode it takes the library item's
  `width`/`height`, never `config.width/height`, §2.4) and `components/question/SortItResults.tsx`.
- `pages/QuestionEditorPage.tsx`: both types; form state; `questionToForm`/`formToPayload` (points
  typed in both modes; derivation table gains two rows); Sort It editor (2–4 boxes, 4–10 cards,
  correct-box select; removing box j unassigns its cards and shifts later indices; client checks
  mirror the server); Spot It editor (T8 `ImagePicker` for the canvas image, prompt-image picker
  hidden, grid inputs with limits, default and phone cell size, author canvas, "3 of max 12"
  accepted cells, confirmation checkbox, status and reasons); list lines "Sort It · 3 boxes · 8
  cards" / "Spot It · 6×4 · 2 cells" and a **Needs review** badge; questions reload after an image
  replace; the live-freeze 409 shown in the form's error line.
  - **Round-trip preservation** (§2.6) and the stamp rules (§2.5): form state keeps the loaded
    `accepted_cells` order, optional-field presence, exact `points_value` (the input is seeded
    with the number received, never a rounded display value), each label row's loaded text, and
    the loaded stamp together with the checkbox's history since load. `formToPayload` emits them
    unchanged unless the author edited them; any unticked → ticked transition builds a fresh stamp
    from the displayed image version and grid.
  - **Author canvas dimensions:** the selected image's library `width`/`height` (§2.4), for new
    questions, a newly chosen image and a replaced image alike.
  - **Text checks are approximations** of the §2.2 server rules, for early feedback only:
    `[...s].length` counts code points (matches the server); `trim()` differs from `isspace`
    (JavaScript doesn't strip U+001C–U+001F or U+0085, and does strip U+FEFF); `toLowerCase()`
    differs from `casefold` (e.g. "ß", ligatures such as "ﬁ"). Only edited label rows are trimmed
    (§2.3 editor label rule). The server's 422 message is shown in the form and is authoritative.
  - After each save, the response's `needs_review`/`review_reasons` update the question's badge at
    once (§2.5).
  - **Checkbox initial state:** ticked only if `needs_review` is false and the loaded stamp matches
    the form image's current library version (§2.5).
  - **Grid source** tracked as loaded / edited / defaulted; defaults apply only when the form has
    no grid yet and are never applied over a loaded or edited grid (§2.4).
  - **Answer keys:** the per-type rules for `correct_boxes` (§2.3: never a partial key; the
    COMPLETENESS toggle) and `accepted_cells` (§2.4: sent iff loaded present or non-empty),
    including grading-mode switches that keep the authored key.
- `components/images/ImageLibraryPanel.tsx`: Replace warns when `used_by` has a canvas use ("Q3
  (Spot It) will need review").
- `pages/game/QuestionPage.tsx`, `ResultsPage.tsx`, `GameOverPage.tsx`: T7 branches (the two
  results pages compute Sort It numbers only through `lib/sortResults.ts`).

### 4.3 Player app (`frontend/player/src/`)
- `types/game.ts`: unions; `PlayerAnswerReveal` variants `{type: 'sort_into_boxes'; correctBoxes:
  number[]}` and `{type: 'find_in_image'; acceptedCells: {col: number; row: number}[]}`;
  `cardOrder`; the six T7 results fields (optional in the type, since other types omit them).
- `lib/spotGrid.ts` (copy), `lib/formatPoints.ts` (the §2.2 display rule), new
  `components/SortBoard.tsx`, `components/SpotCanvas.tsx` (with the nudge-button row;
  presentational; answer via callback; a `disabled` prop that never clears state).
- `pages/game/QuestionPage.tsx` branches (lock and unlock only toggle `disabled`); `ResultsPage.tsx`
  T7 branches reading only the payload, with explicit "No answer"; `GameOverPage.tsx` branches with
  the §2.3/§2.4 texts (no answer = null `playerAnswer`; correctness from answer + reveal, identical
  to the server rule).

### 4.4 Tests — unit (`tests/unit/`)
- `test_sort_it.py`:
  - every data rule: booleans, lengths, COMPLETENESS optional key;
  - the §2.2 text rules: "Straße"/"STRASSE" are duplicates; "😀" counts as one code point; a
    leading U+0085 is rejected; a leading U+FEFF is accepted — kept by the server, and preserved
    by the editor unless that label is edited (§2.3); a precomposed "é" and "e" + U+0301 are
    distinct;
  - answer parsing: non-dict, extra keys, wrong length, bools, floats, out of range, all-null;
  - scoring k = 2/3/4, the floor, unplaced cards, full = exactly `points_value`, `is_correct`,
    malformed under COMPLETENESS → 0, and the §2.3 example (6 right, 1 wrong, 1 left out → 625 /
    687.5 / 708.33… of 1000 for k = 2 / 3 / 4);
  - dist keys; reveal; `results_breakdown`: the denominator; a designed tie between two wrong
    boxes (lowest index wins); the correct box and "left out" never counted as a wrong box; a card
    left out by some answers; a .5 percentage (1 of 8 → 13 %) using the integer half-up formula;
    zero answers → "—". The same case table is the expected data of the §4.6 cross-check;
  - `card_order` determinism and non-identity; the random-guess table enumerated and asserted.
- `test_spot_it.py`:
  - grid bounds and bools; accepted-cell bounds, duplicates and half cap; COMPLETENESS;
    `image_id` required; `prompt_image_id` refused; `width`/`height` never checked (any value
    accepted); stamp shape (null accepted);
  - `max_grid`: every row of the §2.4 table; exact edges 1100×200 → 10×2, 1101×200 → 10×1,
    140×830 → 2×10, 140×831 → 1×10; the branch tie 308×332 → 10×10;
  - `default_grid`: half up (4:3 → 6×5, 16:9 → 6×3, 9:16 → 6×10, 1:3 → 3×9); none for 6:1 and 1:6;
  - `review_status`: each reason alone; every allowed combination ({2,3}, {2,4}, {2,5}, {3,5},
    {4,5}, {2,3,5}, {2,4,5}, {2,6}); `image_missing` alone even when other conditions hold; the
    exact order; every display text, including the too-wide and too-tall variants;
  - answer parsing; scoring both modes; **cell order**: the same cells in different stored orders
    give identical scores and identical (row-major) reveals; dist key; labels;
  - `export_fields` with an `ImageInfo` that confirms, one that doesn't, a too-dense grid and a
    missing image: no width/height; `confirmed` only when confirmed; `accepted_cells` order and
    presence kept; fixed key order.
- `test_bundle_service.py` additions:
  - `bundle_size` equals the export length for games with Spot It questions in every review
    status, including replaced-before-reconfirmation with different stored dimensions;
  - the same for an `add_image` pending record and a `replace_image` substituted record carrying
    `version`, `width` and `height`;
  - T8's existing helper updated to pass the three new required fields.
- `test_report_points.py` (or inside the report tests) — the report formatter **as rendered**
  through `_t7_points_label`, the T7 `q-timing` maximum-points label (§2.8): 687.5 → "687.5 pts",
  708.333 → "708.3 pts", 1.25 → "1.3 pts", 1000 → "1000 pts" (not "1E+3"), 1 → "1 pt", and the
  **regression cases** 0.15 → "0.2 pts", 2.55 → "2.6 pts", 0.35 → "0.4 pts", which pin
  `Decimal(repr(x))` against a switch to `Decimal(x)` (§9, D5). An existing-type question keeps
  its `:g` label. (The player app has no test runner; its `formatPoints` is checked on the phone
  in §4.6.)
- `test_report_t7.py` — escaping in the pure renderers (§2.8):
  - inputs: a card `<script>alert(1)</script>`, a box `"><img src=x onerror=alert(1)>` (exactly
    30 code points, so a valid label), and an image description containing `"`, `<` and `&`;
  - the escaped text is preserved exactly (e.g. `&lt;script&gt;alert(1)&lt;/script&gt;` and
    `&quot;&gt;&lt;img src=x onerror=alert(1)&gt;`);
  - parsing the output with an HTML parser finds **no injected elements and no event-handler
    attributes** from those strings: no `script` or `img` element created from a label, and no
    attribute whose name starts with `on`;
  - it does **not** assert the absence of substrings such as `onerror=`, which properly escaped
    label text legitimately contains.
- Stamp cases (§2.5) are exercised in integration (§4.5), where the service's locked reads run.

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
- **Confirmation:** confirmed create → `needs_review` false; bogus `width/height` (strings,
  negatives, absent) accepted and overwritten with the image's dimensions; grid-only PUT →
  `grid_changed`.
- **Stamp cases (§2.5):** an update without `answer_data` carries the stored stamp, and a stale
  carried stamp is not stamp-validated (200, still needs review); `answer_data` with `confirmed_for`
  omitted and with `confirmed_for: null` both store no `confirmed_for` key (read back from MySQL)
  and report `not_confirmed`, checked on **create and on update** (the null survives both paths
  until the service strips it); a supplied stale stamp → 409; a supplied current stamp on a
  too-dense grid → 422; a supplied stamp equal to a stale stored stamp → 409.
- **Reconfirmation after a replace (API counterpart of the editor's tick-only path):** after a
  same-shape replace, a PUT of the loaded fields with a fresh stamp (the new version) → 200,
  `needs_review: false`, and the stored stamp carries the new version.
- **Regression — stamp-only reconfirmation refreshes the stored dimensions (§2.5 Writes, step
  2):** a confirmed question with a 6×6 grid (fits both shapes) on a 1600×900 image → replace the
  image with a 900×1600 one → a PUT whose body contains **only `answer_data`** (the same accepted
  cells plus a fresh stamp for version 2; no `config`) → 200, `needs_review: false`, and the
  stored `config.width/height` read back from MySQL are **900×1600**, with `image_id` and `grid`
  unchanged. A room then starts, and the phones' `new_question` `config` carries 900×1600. This
  fails if dimensions are only rewritten inside a sent `config`.
- **Review reasons end to end:** a partial PUT changing `config.image_id` and `config.grid`
  together with a carried stamp → `["image_changed", "grid_changed"]`; a missing image planted in
  MySQL → `["image_missing"]`, room 409 with "its image is missing", report "[image missing]"; the
  room 409's exact wording for a multi-reason question.
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
  is scored against the original key. Unconfirmed Spot It create → 409; confirmed create → 201; the
  same edits succeed once the session has ended. The three 409 texts match §2.6 exactly (lead-in,
  T8's room list, fix), and T8's own image live-guard message is unchanged.
- **Prompt-only live edits** (LOBBY and IN_PROGRESS), using an editor-shaped round trip — GET the
  question, then PUT every loaded field with only the prompt (or only the time limit) changed:
  - → 200 for Sort It ACCURACY; Sort It COMPLETENESS with and without `correct_boxes`; Spot It
    ACCURACY whose `accepted_cells` are stored out of row-major order; Spot It COMPLETENESS with
    `accepted_cells` absent and with `[]`; Sort It with a card stored as `"﻿Frog"` (bytes
    unchanged afterwards); with `points_value` 687.5, 708.333 and 0.1;
  - the stored question afterwards differs only in the prompt (or time limit);
  - the same round trip with the cells reordered, a `null` stamp, or an optional key added or
    removed → 409, and the question is unchanged in every field.
- **Lock tests with forced orderings.** Each order is forced by a row lock the test holds, so no
  outcome depends on InnoDB's grant order and no test can pass through the room-first path alone.
  They rely on today's lock order: `replace_image` takes the game lock, then `FOR UPDATE` on the
  image row; `update_question` takes the game lock, then share-locks the question row
  (`_get_question`); `create_room` sets its REPEATABLE READ snapshot in its access checks, then
  takes the game lock, then its gate share-locks the game's question rows and the image row. If
  implementation changes that order, these tests change with it.
  - **Harness extension (required).** T8's `LockHarness` (`tests/integration/test_t8_images.py`)
    locks only a `games` row, and its `waiting(game_id)` matches only
    `FROM games … WHERE games.id = N … FOR UPDATE`. T7 adds a subclass, `RowLockHarness(LockHarness)`,
    in `test_t7_question_types.py`, importing `LockHarness` from `.test_t8_images`
    (`tests/integration` is a package). T8's class and methods are not modified, so T8's tests
    behave exactly as before. The subclass adds:
    - `lock_row(table, row_id)`: `SELECT id FROM <table> WHERE id = %s FOR UPDATE`, with `table`
      restricted to `games`, `images` or `questions`;
    - `waiting_on(pattern)`: the number of other connections of this account whose PROCESSLIST
      `INFO` matches a compiled regex (same query as `waiting`);
    - `wait_for(pattern, count=1, timeout=10.0)`: polls every 50 ms like `wait_for_waiters`, and on
      timeout fails with the same "set BUZZER_TEST_DB_USER to the backend's MySQL account" hint.
      It never passes on a timeout.

    Each wait pattern names the **table, the row predicate and the lock clause**, never a bare
    table match. Ids are interpolated literally, as T8's existing pattern already assumes of the
    rendered SQL; `{G}`, `{I}` and `{Q}` are the game, image and question ids:

    | Waiting statement | Pattern (case-insensitive) |
    |---|---|
    | `lock_game` (T8's, unchanged) | `FROM games\s+WHERE games\.id = {G}\s+FOR UPDATE` |
    | the replace's `_locked_image` | `FROM images\s+WHERE images\.id = {I} AND images\.game_id = {G}\s+FOR UPDATE` |
    | the gate's `get_image_meta(locking=True)` | `FROM images\s+WHERE images\.id = {I} AND images\.game_id = {G}\s+LOCK IN SHARE MODE` |
    | the update's `_get_question(locking=True)` | `FROM questions\s+WHERE questions\.id = {Q} AND questions\.game_id = {G}\s+LOCK IN SHARE MODE` |
    | the gate's question read | `FROM questions\s+WHERE questions\.game_id = {G}\s+LOCK IN SHARE MODE` |

    - The gate's question read is specified as exactly
      `fresh(select(Question.id, Question.order_index, Question.type, Question.config,
      Question.answer_data).where(Question.game_id == game_id))`, so its WHERE clause is the game
      predicate alone. `export_inputs` issues the same clause, but in the orders below the update
      is still blocked on `lock_game` (which the test also waits for), so only `create_room` can be
      the matching connection.
    - When implementing, confirm each pattern once against the live PROCESSLIST text. A pattern
      that matches nothing makes `wait_for` fail with the hint; it is never loosened.
  - **Setup:** a confirmed ACCURACY Spot It question, 10×6 grid, on a 1600×900 image.
  - **Replace first — proves the room gate's fresh image read:**
    1. `lock_row("images", I)`;
    2. start the portrait (900×1600) replace; `wait_for` the `_locked_image` pattern (the replace
       already holds the game lock);
    3. start `create_room`; `wait_for` the `lock_game` pattern (its snapshot was taken at its
       access check, before the replace commits);
    4. assert neither request has finished, then release;
    5. expected, exactly: replace 200 (version 2), then room 409 naming "Q n — the 10×6 grid is too
       dense for its image (at most 6×10); its image was replaced after the cells were checked",
       with no session row and no Redis room.

    A plain image read in the gate would see version 1 and return 201, so this fails exactly when
    the gate doesn't read fresh.
  - **Room first:** `lock_row("images", I)`; start `create_room` and `wait_for` the gate's
    `get_image_meta` pattern (it holds the game lock); start the replace and `wait_for` the
    `lock_game` pattern; release. Expected: room 201 (the stamp still matches version 1), then
    replace 409 from T8's live guard naming the room; the image stays version 1.
  - **Un-confirming update, both orders**, forced by `lock_row("questions", Q)`:
    - update first: start the update (`answer_data` with `confirmed_for: null`) and `wait_for` the
      `_get_question` pattern; start `create_room` and `wait_for` the `lock_game` pattern; release →
      update 200, then room 409 (`not_confirmed`);
    - room first: start `create_room` and `wait_for` the gate's question-read pattern; start the
      update and `wait_for` the `lock_game` pattern; release → room 201, then update 409 with the
      §2.6 freeze text.
- **Gameplay over sockets:** Sort It full/partial/zero, with the partial case for **k = 2, 3 and
  4** (8 cards; 6 right, 1 wrong, 1 left out → 625 / 687.5 / 708.33… of 1000; approx compare for
  `FLOAT`), Spot It in/out (and the same answers against a
  question whose cells are stored in a different order), both COMPLETENESS; malformed answers
  (bool col, extra key, out of bounds, list
  `answer_data`, all-null placements) → `error`, no `SessionScore`, another player's next valid
  answer still scored; distribution keys ⊆ the bounded set; reveals; `cardOrder` identical for host
  and players; host game-over distribution equals the live one.
- **Self-contained results:** for both types × both grading modes × {answered, unanswered,
  answered-then-reconnected (new socket, `join_room`) before results}: the player's
  `question_results` carries the existing fields (`questionId`, `yourPoints`, `yourScore`,
  `yourRank`, `playerCount`); `answerReveal` equal to the exact T7 variant for ACCURACY (Sort It
  `correctBoxes`; Spot It `acceptedCells` row-major) or `{type: "completeness"}`, identical for
  every player and equal to the host's; and `questionType`, `gradingType`, `pointsValue`,
  `answered`, `yourAnswer`, `yourIsCorrect` with correct values. The reconnected player received
  no `new_question`. Other types' `question_results` are unchanged.
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
  so reconfirming changes only the exported flag):
  - with the unconfirmed post-replacement question at headroom 0, the game exports to exactly
    41,943,040 bytes, re-imports, and re-exports identically;
  - reconfirming (adds `"confirmed": true`, `c` bytes) is refused at headroom `c − 1` with the
    question unchanged, and accepted at headroom `c`, landing exactly on the limit;
  - **replacement size boundary (pinned).** `c` is measured as the meter with the question
    confirmed minus the meter unconfirmed. The replacement is the **same image bytes** (conforming
    bytes are stored unchanged, so `byte_size` and the base64 length don't change) under an ASCII
    filename `d` bytes longer than `a.png`, so the replace adds `+d` (filename) and `−c` (the
    version increases, so `confirmed` drops). **Each case gets its own fresh confirmed setup at
    headroom 0:**
    - `d = c` → 200; the meter is exactly the limit and equals the downloaded export;
    - `0 < d < c` → 200;
    - `d = c + 1` → 409 naming the export limit, with the image's version and filename unchanged.

    This fails exactly when `replace_image`'s substituted record lacks `version + 1`: `d = c`
    would then be measured as `+c` and refused.
  - **Follow-on from the `d = c` case:** the export is now exactly at its limit and the question
    reports `image_replaced`. Reconfirming would add `c` bytes, so a tick-only reconfirmation (a
    fresh stamp) is refused with 409 naming the export limit, and the question is unchanged. After
    freeing at least `c` bytes (shrinking the padding question), the same reconfirmation succeeds
    (200, `needs_review: false`). The spec doesn't promise immediate reconfirmation at zero
    headroom.
- **Report:** Sort It table; Spot It SVG with counts and "Correct"; small-copy data URI; withheld
  text for a host without game access; Sort It's stats line reads "N% all cards correct"; the
  rendered `q-timing` header of T7 questions worth 708.333 and 2.55 reads "708.3 pts" and "2.6 pts"
  (an existing-type question keeps its `:g` label); no individual scores appear anywhere in the
  report.
- **Report escaping:** a downloaded report for a session whose Sort It labels and Spot It
  description are the §4.4 escaping inputs contains their escaped text exactly, and parsing it
  with an HTML parser finds no elements and no `on…` event-handler attributes injected by those
  strings. (Substrings such as `onerror=` may legitimately appear inside escaped text, so their
  mere presence isn't a failure.)
- **Access:** a host without the game grant and a guest → 403 creating either type.

Also: `tests/integration/engine/scoring.py` (`compute_question_score`, `_build_reveal`) and
`tests/integration/scenarios/all_question_types.py` gain an independent Sort It implementation and
scenarios (Spot It
gameplay stays in the new module, which uploads and confirms an image); `scripts/simulate_players.py`
gains Sort It (random with ~20% left out; correct with `--game-json`) and Spot It (random cell; an
accepted cell with `--game-json`).

### 4.6 Browser checks (Playwright through nginx, `npm run build`, `:8080`)
Projector 1440×900; phones 390×844, 360×640 and 640×360 landscape at DPR 1/2/3; a landscape
(16:9) and a portrait (9:16) image. Every Playwright browser context sets `locale: 'en-US'`, so the
expected number strings are deterministic; the apps themselves keep formatting in the viewer's
locale (§2.2), and no user preference is changed.
- both types end to end; drag and tap-tap; keyboard on the phone canvas and the editor canvas;
- **coordinates:** tapping the centre of each corner cell and one interior cell on the phone
  submits that `{col, row}`; the phone's and projector's grid rectangles (data attributes) have the
  same aspect as `width/height` within 1 px; the nudge buttons reach every cell in landscape;
- rotation keeps the selection; the editor's shown limits match the server's confirmation limits
  for the 16:9 and 9:16 images **and** for edge-size images 1100×200, 1101×200, 140×830 and
  140×831 (catching an off-by-one in the TypeScript `maxGrid`); its checkbox is disabled while the
  grid doesn't fit, and a too-dense draft still saves;
- **authoring dimensions:** after a landscape → portrait replace and before re-saving, the editor
  canvas's `data-grid-rect` has aspect 900:1600 (±1 px) while the stored `config` still says
  1600×900; after fixing and confirming, the projector's `data-grid-rect` is 900:1600 too;
- **tick-only reconfirmation after a replacement:** replace with the same shape → open the question
  (box unticked, "its image was replaced …") → tick only → save → 200, the badge clears at once
  (from the save response) and stays clear after reload, and the stored stamp has the new version;
  landscape → portrait with a 6×6 grid (fits both) → tick only → 200; landscape → portrait with a
  10×6 grid → the box stays disabled until the grid fits;
- reveal only after Show Results and after enabled auto-advance; heatmap, counts, legend, Correct
  outlines; COMPLETENESS variant;
- **results without local state:** reload the phone after answering, before results → full T7
  results text; reload during RESULTS → "Loading results…" (existing limitation, expected);
- projector image-failure placeholder + Retry; editor checkbox, Needs review after replace, live
  freeze 409 in the form, 409 room message on Play; no horizontal scroll at 360 px;
- **real-editor round trips during a live lobby** — each question set up via the API, then opened
  in the editor, only the prompt changed, and saved → 200 with the "Needs review" state unchanged;
  a GET afterwards differs only in the prompt:

  | Variant | Setup detail |
  |---|---|
  | Sort It ACCURACY | `points_value` 708.333 (a `FLOAT` reads back 708.333; displayed "708.3") |
  | Sort It COMPLETENESS | with `correct_boxes` (points 0.1), and without |
  | Sort It, U+FEFF label | a card stored as `"﻿Frog"` (§2.3 label rule) |
  | Spot It ACCURACY | confirmed; cells stored out of row-major order; points 708.333 |
  | Spot It COMPLETENESS | confirmed; `accepted_cells` absent, and `[]` |

  An editor that seeded its points input from the rounded display would send 708.3 and get the
  freeze's 409, and one that trimmed unedited labels would strip the U+FEFF, so this matrix
  catches both;
- **lock and time-out:** host Lock then Unlock keeps the Sort It placements and the Spot It
  selection, and Submit / Lock in work again; letting the time run out with a selected but
  unlocked cell (and with an unsubmitted board) submits nothing and shows "No answer";
- **game over:** the Sort It projector card rows and the phone lines for both types, in both
  grading modes;
- **points display (phone only; individual points appear nowhere else):** on the phone's T7
  results screen and game-over lines, "687.5 of 1,000 points" and "708.3 of 1,000 points" for the
  §4.5 partial scores; **rounding regressions** (§2.2, D5): a Sort It question worth 0.3 with a
  half-credit answer shows "0.2 of 0.3", and one worth 5.1 shows "2.6 of 5.1". The projector
  shows no per-player points. The report's T7 maximum-points label is checked in §4.4 and §4.5;
- **checkbox initial state (§2.5):** a confirmed question opened right after a replace in another
  tab (library shows the new version) starts **unticked**, and a prompt-only save succeeds
  instead of resending a stale stamp;
- **default grid (§2.4):** a new Spot It question gets the default for the first image and a
  recomputed default when the image changes before the grid is touched; after the author edits
  the grid, or on a loaded question, choosing another image keeps the grid (the checkbox unticks
  and the new limits show);
- **answer keys (§2.3, §2.4):**
  - Sort It: in ACCURACY, Save stays blocked until every card has a box; in COMPLETENESS, turning
    the key on with a card unassigned blocks Save, and turning it off saves with no
    `correct_boxes`; adding a card or removing a box never sends a partial key;
  - Spot It COMPLETENESS: a key loaded absent, ticked and unticked, saves absent; a loaded
    non-empty key emptied saves `[]`;
  - switching ACCURACY → COMPLETENESS → ACCURACY keeps the authored key and, with no other edits,
    saves it exactly as loaded (checked with a GET);
- **Sort It results cross-check** (§2.3): one scripted session with a designed tie (two wrong
  boxes equally common for a card), a card left out by some players, and a count giving a .5
  percentage (1 of 8 → 13 %). The projector's ResultsPage and GameOverPage numbers (read from the
  DOM) equal the downloaded report's table and the §4.4 expected values;
- **text rules:** the editor accepts "Straße" and "STRASSE" as boxes, and the form then shows the
  server's duplicate 422.

### 4.7 Physical-phone checklist
LAN setup per T8 §4.12 (host at `http://<LAN>:8080/host/`, temporary `CORS_ORIGINS` entry, recreate
the backend, remove afterwards). Record device model, OS, browser versions and locale, and
pass/fail per item (number checks accept the phone locale's decimal separator):
1. Join by QR.
2. Sort It: drag with a finger without the page scrolling; tap-tap placing; move a placed card;
   return to pool; 10 cards + 4 boxes fit the width; submitting with a card left out confirms.
3. Spot It with a landscape and a portrait image: corner and edge taps select the intended cell;
   drag moves the selection; nudge buttons; Lock in; no pinch zoom; rotate mid-question (canvas
   resizes, selection kept); background the browser and return.
4. The phone never shows the Spot It image (question, results, game over).
5. Results text: selected cell, correct/incorrect, correct cells, points; "No answer" when skipped;
   correct after reloading between answering and results.
6. Host lock / disconnect disables inputs; unlock re-enables them with the placements and the
   selection kept; time running out with a selected but unlocked cell submits nothing.
7. The projector heatmap is readable from the back of the room.

### 4.8 Docs and context files
`README.md` (bundle schema for both types; `confirmed`; no `width/height` in bundles);
`docs/realtime.md` (`cardOrder`, T7 answer shapes, T7 results fields); `CLAUDE.md` updates only
where stale (context-sync rule): `backend/app` (checklist entries, new modules, the §4.1 module
dependency lines added to T8's graph, the live-freeze and confirmation invariants), `services`,
`schemas`, `websocket`, `frontend` (the grid-geometry copies, the player's `formatPoints`, the
host's `sortResults.ts` port, the editor's text-rule approximations), host
`pages`/`components`/`lib`, player `pages`/`components`/`lib`.

### 4.9 Order of work (once implementation is authorized)
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
- `lib/sortResults.ts` is a second implementation of `sort_it.results_breakdown`; the §4.6
  cross-check covers the tie, left-out, .5 and no-answer cases, not every possible input.
- The forced-order lock tests (§4.5) depend on today's lock order in `replace_image` and
  `create_room`; a change there must update them.
- **Consideration (not adopted):** the phone canvas's 55vh cap versus the visible height behind a
  phone browser's toolbars. Evaluate `svh` (small viewport height) during implementation and on the
  physical phone (§4.7) before choosing.

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
- **The editor's text checks only approximate the server's Unicode rules** (§2.2, §4.2): some
  duplicates and whitespace cases are caught only by the server's 422.
- **Editor round-trip preservation** (§2.6) is extra editor state — loaded optional-field
  presence, exact points, each label row's loaded text, and the checkbox's history since load; a
  missed case shows up as a live-session 409, not as corrupt data, and §4.5/§4.6 test the round
  trip for every T7 variant.
- **Removing a null `confirmed_for` before writing** is a small server-side change to the sent
  data, needed so null and omitted store identically for the live freeze's comparison.
- **A planted missing image** makes that question's export fail re-import — the same boundary T8
  draws for invalid stored data, and unreachable through application writes.

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

## 8. Decisions from the comprehension review (2026-09-29)

A fresh Goldfish session read this document and its "Read first" files, restated the design
correctly, and found the gaps below. Each is resolved in the section named.

| # | Finding | Resolution |
|---|---|---|
| 1 | Review reasons had no defined conditions, combinations, order or text | §2.5: reason table (conditions, exclusions, fixed order, display texts); `image_missing` defined with its behaviour; the three stamp cases (not sent, omitted/null, supplied) |
| 2 | Unclear whether phones receive the correct answer in `question_results` | §2.7: verified that phones already receive `answerReveal`; listed with the existing and six new fields; reveal variants in both apps' types (§4.2, §4.3) and tests (§4.5) |
| 3 | `export_fields` couldn't evaluate "confirmed" (grid fit needs the image's size) | §2.5, §2.9: one `review_status(config, answer_data, ImageInfo)`; `ImageRecord` carries `version`, `width`, `height` through `export_inputs`, `add_image`, `replace_image` and `set_description` |
| 4 | Accepted-cell order could make a prompt-only live edit fail the freeze | §2.4, §2.6: order stored as sent and ignored by scoring and reveals; editor round trips preserve every frozen field; §4.5/§4.6 prompt-only live edit checks |
| 5 | The editor's default-grid rounding was unstated (4.5 rounds differently in Python and JavaScript) | §2.4: integer round-half-up formula; 4:3 row corrected to 6×5 |
| 6 | Floating-point `max_grid` could disagree between copies; the tall limit was misquoted | §2.4: integer formula; limits 11:2 and 14:83; §4.4 exact edge sizes |
| 7 | Time-out, Sort It game over, lock/unlock and fractional points unspecified | §2.2 points display; §2.3 and §2.4 lock/time-out (no automatic submission) and game-over texts; §2.7 phases |
| 8 | Client `width`/`height` conflicted with strict validation | §2.4, §2.9: never checked; discarded and replaced with trusted image dimensions at defined points |
| 9 | The editor can't mirror Python's text rules | §2.2: server rules in code points, `isspace`, `casefold`; §4.2: editor approximations and their known gaps |
| 10 | Wrong score example ("640 of 1000") | §2.3: 687.5 of 1000 (k = 3) |
| 11 | "`grid_too_dense` only after a replace" was false | §2.5: every way it appears |
| 12 | Test harness paths abbreviated | §4.5: full `tests/integration/…` paths |
| 13 | Grid rectangle attribute unnamed | §2.4: `data-grid-rect="x,y,w,h"` |
| 14 | New import dependencies unrecorded | §4.1: dependency graph; §4.8: context-file update |

Also corrected: the claim that nothing answer-revealing is sent early (§2.7 now names the existing
`answer_received` limitation).

**Inferences the review asked to confirm:**
1. `needs_review` is `false` (with `review_reasons: []`) for Sort It and every existing type —
   confirmed (§2.2).
2. The stored answer is simply the validated answer — confirmed; "normalized" is no longer used
   (§2.2).
3. Sort It percentages divide by the question's answered count — confirmed, with tie-breaks
   (§2.3).
4. `engine/` and `scenarios/` are under `tests/integration/` — confirmed (§4.5).
5. The admin app is untouched — confirmed: it has no question-type code, and admins author in the
   host app. The admin API's question routes call the same service, so they get the same
   validation, freeze and `redis` parameter.

## 9. Decisions from the criticism review (2026-09-29)

A fresh Goldfish session reviewed this document critically against its "Read first" files and the
source it hooks into. It confirmed every §8 comprehension correction is applied consistently (one
wording nit, fixed as O6), found no settled product choice that needed reopening, and raised the
findings below.

| # | Finding | Disposition |
|---|---|---|
| D1 | Ticking the confirmation box with nothing else changed had no defined stamp, so reconfirming after a replace would send the stale stamp and get a 409 | **Accepted.** Any unticked → ticked transition builds a fresh stamp from the displayed image version and grid (§2.5); tick-only reconfirmation tested via the API (§4.5) and the real editor (§4.6) |
| D2 | The editor was told to use `config.width/height`, impossible for new questions and wrong after a replace | **Accepted.** Authoring uses the selected image's current library dimensions; gameplay keeps `config.width/height` (§2.4, §4.2); editor aspect check after a replace (§4.6) |
| D3 | The editor's `trim()` strips a stored U+FEFF, breaking the round trip | **Accepted with the preferred alternative:** unedited labels are sent exactly as loaded (§2.3 label rule, §2.6, §4.2); no new server rejection of U+FEFF; U+FEFF round trips tested (§4.5, §4.6) |
| D4 | Host pages claimed to call a Python function | **Accepted.** One TypeScript port, `lib/sortResults.ts`, shared by ResultsPage and GameOverPage; integer half-up percentages and an explicit tie rule in both implementations (§2.3, §4.2); cross-checked against the report (§4.4, §4.6) |
| D5 | Claimed the apps and the report round differently, and proposed `Decimal(x)` | **Rejected.** Read-only checks show `Intl.NumberFormat("en-US", {maximumFractionDigits: 1})` formats 0.15 as "0.2" and 2.55 as "2.6", matching `Decimal(repr(x))` with `ROUND_HALF_UP`; the proposed `Decimal(x)` gives "0.1" and "2.5" and would itself create the mismatch. The existing approach stays; regression cases 0.15, 2.55 and 0.35 added (§2.2, §4.4, §4.6), plus a rule never to use `normalize()` (1000 → "1E+3") |
| D6 | Escaping of new author text in the report wasn't stated | **Accepted.** Every author-controlled string goes through `_esc` in HTML and SVG, text and attributes (§2.8); tests assert the escaped text is preserved and that parsing finds no injected elements or event-handler attributes, without banning harmless substrings (§4.4, §4.5) |
| T1 | No test proved the room gate reads the image fresh after a concurrent replace | **Accepted, strengthened.** Both orders forced by a row lock the test holds — replace first (the case that proves the fresh read) and room first — plus the un-confirming update in both orders (§4.5) |
| T2 | The replace-at-headroom step couldn't detect a missing `version + 1` in the substituted record | **Accepted.** Pinned boundary with a fresh confirmed setup per case: `d = c` accepted, `0 < d < c` accepted, `d = c + 1` refused; at the limit, reconfirmation is refused until at least `c` bytes are freed, then succeeds (§4.5) |
| T3 | The real-editor round trip used 687.5, which can't show rounding feedback, and missed variants | **Accepted.** Real-editor matrix with 708.333, 0.1, both COMPLETENESS optional-field states, cells out of order and a U+FEFF label (§4.6) |
| O1 | Stale badge after save (commit after response) | **Adopted:** the editor applies the save response's review status immediately (§2.5, §4.2, §4.6) |
| O2 | Locale-dependent number strings in tests | **Adopted:** browser checks pin Playwright's `locale: 'en-US'`; the apps keep the viewer's locale; the phone checklist records the locale (§2.2, §4.6, §4.7) |
| O3 | Limit check only used two image shapes | **Adopted:** edge sizes 1100×200, 1101×200, 140×830, 140×831 (§4.6) |
| O4 | Reconnect and locked-reload behaviour | **Adopted:** socket reconnect keeps state, reload loses it, and a phone reloaded while locked gets no content until the next question (§2.7, §2.11) |
| O5 | Null-stamp stripping on both write paths | **Adopted:** checked on create and update (§4.5) |
| O6 | "crop to" vs "crop it to" | **Adopted:** §2.4 now quotes §2.5 exactly |
| O7 | 55vh vs visible height behind browser bars | **Documented as a consideration** (§5), to settle during implementation and on the physical phone |

## 10. Implementation-readiness review (2026-09-29)

A fresh session reviewed this document, including its uncommitted corrections, against its "Read
first" files and the current source it hooks into. It recomputed the numbers independently and
checked the apps' rounding with Node 20's ICU.

**Verdict: "Ready with small corrections."** The design was implementable as written, no product
decision needed reopening, and no T8 guarantee was weakened. It found six required corrections
and five clarifications, all applied below, plus four optional improvements. With them applied,
**no required issue remains, and the document is marked Ready for implementation.**

**Required corrections**

| # | Finding | Resolution |
|---|---|---|
| R1 | Phone example strings said "1000" while §2.2's locale-aware formatter (with grouping) renders "1,000" in en-US | Kept the formatter and its grouping; every phone example is now its en-US rendering ("687.5 of 1,000 points", "1,000 / 1,000 pts"), stated in §2.2 (§2.3, §2.4) |
| R2 | §4.6 expected per-player points on the projector and in the report, which show none, and §2.8's report rule had no element to apply to | Individual awarded points are tested **on the phone only** (§4.6). The report formatter is wired to the existing maximum-points label in each T7 question's `q-timing` header (`_t7_points_label`) and that rendered output is tested (§2.8, §4.4, §4.5). No individual scores are added to the projector or the report; the host app gets no `formatPoints` (§2.2, §4.2) |
| R3 | Contradiction over who computes the game-over Sort It numbers | Python `results_breakdown` serves the report; **both** host results pages use `lib/sortResults.ts`, and GameOverPage computes from the host summary's rebuilt `answerDistribution` and `totalAnswered`. The `game_over` shape and all its other fields are unchanged (§2.3, §2.7, §2.8, §4.1) |
| R4 | §4.5 expected 708.33… from k = 2 or 3, which can't produce it | Gameplay covers k = 2, 3 and 4 with 8 cards, 6 right, 1 wrong, 1 left out → 625 / 687.5 / 708.33… (§4.5) |
| R5 | Refreshing `config.width/height` when a request omits `config` was implied but not required or tested | Every Spot It create/update persists the merged `config` with trusted current dimensions, even when the request omits `config` (§2.4, §2.5 Writes step 2, §4.1). New regression: a stamp-only (`answer_data`-only) reconfirmation after a landscape → portrait replace stores 900×1600 (§4.5) |
| R6 | T8's lock harness can't lock or detect waits on `images` or `questions` rows | Specified `RowLockHarness(LockHarness)` with `lock_row`, `waiting_on` and `wait_for`, and five anchored wait patterns naming table, row predicate and lock clause (including the question-id and game-id predicates); T8's harness and tests unchanged (§4.5) |

**Clarifications**

| # | Finding | Resolution |
|---|---|---|
| C1 | The checkbox could start ticked from a momentarily stale question list | Starts ticked only if `needs_review` is false **and** the loaded stamp matches the form image's current library version (§2.5, §4.2, §4.6) |
| C2 | "Grid untouched" was undefined | The form tracks its grid's source; a default applies only when there is no grid yet, and never over a loaded question's grid or a grid the author edited (§2.4, §4.2, §4.6) |
| C3 | Behaviour of optional answer keys after edits and grading switches | Untouched keys are sent exactly as loaded. Sort It never emits a partial or invalid key (ACCURACY requires every card; COMPLETENESS has an explicit include toggle); Spot It COMPLETENESS cells are sent iff loaded present or non-empty; grading switches keep the authored key (§2.3, §2.4, §2.6, §4.2, §4.6) |
| C4 | Confirmation and import 422s printed "at most 10×1" for images that fit no grid | Both reuse the reason table's too-wide and too-tall wording (§2.4, §2.5, §2.9) |
| C5 | The freeze 409's text was unspecified, and T8's message is image-specific | Defined three lead-ins, T8's room list (factored into a shared helper so T8's message is unchanged) and the fix sentence; tested verbatim (§2.6, §4.1, §4.5) |

**Optional improvements** (not readiness requirements; adopted as narrow wording or behaviour
only — no malformed-data recovery redesign)

| # | Suggestion | Disposition |
|---|---|---|
| O1 | The report's whole-board "N% correct" sits beside per-card percentages | Adopted: Sort It's stats line reads "N% all cards correct" (§2.8, §4.5) |
| O2 | `review_status`/`export_fields` must not raise on malformed stored data | Adopted narrowly: they never raise; such data reports `not_confirmed` and exports without `confirmed`; no repair or migration (§2.5) |
| O3 | "not re-validated" overstated case 1 | Adopted: "not stamp-validated; still structurally validated" (§2.5) |
| O4 | Card order on the projector's Sort It results | Adopted: `cardOrder` on ResultsPage; the author's order at game over and in the report (§2.3, §2.8) |
