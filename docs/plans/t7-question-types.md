# T7 — Two New Question Types: Sort It and Spot It

**Destination:** `docs/plans/t7-question-types.md`
**Status:** Spec drafted from the design discussion. **Not yet Goldfish-tested.** Commit before
any implementation (T3), run `goldfish-test` in a fresh session, and revise this file first if
implementation reveals a flaw.
**Read first:** `backend/app/CLAUDE.md` (architecture and the question-type checklist),
`backend/app/websocket/CLAUDE.md`, `backend/app/services/CLAUDE.md`, `frontend/CLAUDE.md`, and the
`pages/` context files for the host and player apps.

---

## 1. Problem

Buzzer has four question types: `multiple_choice`, `true_false`, `fill_in_the_blank` and
`multi_select`. Task T7 adds two more, each working end to end:
- authoring in the host app's question editor (used by hosts and admins)
- validation
- storage
- player interaction
- the host's live screens
- scoring
- results, game-over summaries and the HTML session report
- JSON export/import
- tests (T5) and sample games (T6)

Constraints:
- One type suits a **classroom** and one a **party**.
- At least one uses an **HTML `<canvas>` as the player's interaction surface**.
- Each has a **deliberate scoring approach**.
- Rubric R7 also grades clean integration with the existing architecture.

## 2. Technical Plan

### 2.1 The two types

| | **Sort It** | **Spot It** |
|---|---|---|
| `type` value | `sort_into_boxes` | `find_in_image` |
| Audience | Classroom | Party |
| Player does | Drags cards (or taps a card, then a box) into 2–4 labeled boxes, then submits | Taps where an object is in an image, adjusts the marker, then presses **Lock in** |
| Interaction surface | Normal page elements | **`<canvas>`** (meets the canvas requirement) |
| Scoring | Per card, corrected for guessing | Distance to the target, with partial credit for near misses |
| Depends on T8 (image storage) | No | **Yes** |

**Rationale.**
- **Sort It** matches card sorting, a way teachers check understanding in any subject: can the
  student tell examples from non-examples? Examples: mammal / reptile / amphibian; metal /
  nonmetal; O(n) / O(n log n) / O(n²); fact / opinion. The subject comes from the game's content,
  not the type. The reveal shows which cards the class misclassified, so the teacher can address
  the misconception immediately.
- **Spot It** is a "Where's Waldo"-style search:
  - it needs no background knowledge
  - the room searches together on the projector
  - the reveal (everyone's tap plotted on the image) is a shared moment

### 2.2 Terms and existing rules this spec relies on

- **Question fields** (`questions` table):
  - `type`: a plain string
  - `grading_type`: `ACCURACY` or `COMPLETENESS`
  - `prompt`
  - `config`: JSON, **sent to phones as-is** in the `new_question` event
  - `answer_data`: JSON, the answer key, **never sent before results**
  - `time_limit_seconds`
  - `points_value`: a float, the maximum points available
- **Scoring is per answer, when it's submitted.**
  - `game_service.calculate_score(question, answer)` runs inside the socket handler for
    `submit_answer` (`websocket/gateway.py`).
  - The player immediately receives `answer_received` with their points, and a `SessionScore`
    row is written.
  - A score may depend only on the question and that one answer: no comparison with other
    players, and no client-reported timing.
- **COMPLETENESS:** `calculate_score` returns full `points_value` for any non-empty answer,
  **before** any type-specific code. Both new types keep this behavior unchanged.
- **Correctness convention:** `is_correct = points >= points_value`. Both types are designed so a
  full score is exactly `points_value × 1.0`.
- **Reveal:** a summary of the answer key sent only with results, built by `_answer_reveal` in
  `websocket/gateway.py`, and separately by the two game-over summary builders in
  `services/game_service.py` and by `services/report_service.py`.
- **Answer counts:** a Redis hash for each question, counting how many answers fell under each
  key. It feeds the host's live results chart and must use a bounded set of keys.
- **Validation:** `QuestionCreate.validate_structure` (`schemas/admin.py`) runs on:
  - **create**
  - **update**: `game_management_service.update_question` merges the sent fields onto the stored
    question and re-validates the result with `QuestionCreate`
  - **import**

  New rules therefore apply the same way to all three.
- **Naming:**
  - `config` and `answer_data` keys are **snake_case**, like the existing `config.options` and
    `answer_points`. Clients read `config` keys as-is.
  - Socket payload keys built by the server (reveals, summaries) are **camelCase**.
- **Projector** = the host app's game screens (`frontend/host/src/pages/game/`). **Phone** = the
  player app. The phone does not render the prompt; the projector does.

### 2.3 Sort It (`sort_into_boxes`)

Notation: `n` = number of cards, `k` = number of boxes.

#### Data

| Field | Shape | Rules (enforced in `validate_structure`) |
|---|---|---|
| `config.boxes` | `string[]` | 2–4 labels; each trimmed, non-empty, ≤ 30 characters; unique ignoring case |
| `config.cards` | `string[]` | 4–10 items; each trimmed, non-empty, ≤ 60 characters; unique ignoring case |
| `answer_data.correct_boxes` | `int[]` | Required for ACCURACY: same length as `cards`, each an int in `[0, k)`. Every card has exactly one correct box; a box may have no cards. Not required for COMPLETENESS |
| `points_value` | float | ACCURACY: **must be > 0** (otherwise every answer counts as correct). Typed by the author, not calculated |
| Player answer | `{placements: (int \| null)[]}` | Length `n`; entry `i` is the box index chosen for card `i`, or `null` if left unplaced |

#### Scoring (ACCURACY)

```
right  = number of i where placements[i] == correct_boxes[i]
wrong  = number of i where placements[i] is not null and != correct_boxes[i]
credit = max(0, (right − wrong / (k − 1)) / n)
points = points_value × credit
is_correct = (right == n)          # credit is exactly 1.0 only in this case
```

**Why this rule:** it is the standard correction for guessing. A card placed at random adds zero
on average, whatever `k` is. Under "fraction right," a random guesser earns `1/k`: 50% with 2
boxes. An unplaced card scores zero and costs nothing: it is the honest "I don't know," and the
teacher can see it.

| Example | Credit |
|---|---|
| n = 8, k = 3: 8 right | 1.0 |
| 6 right, 2 wrong | (6 − 1)/8 = 0.625 |
| 6 right, 2 unplaced | 0.75 |
| 4 right, 4 wrong | 0.25 |
| 2 right, 6 wrong | 0 (floor) |
| n = 8, k = 2: 6 right, 2 wrong | 0.5 |

**COMPLETENESS:** unchanged existing behavior (full points for any submitted answer). The phone
does not allow submitting with zero cards placed. A client that bypasses the phone could submit
all `null` and still get full points, the same as for existing types today.

#### Checks, answer counts, reveal
- **Gateway** (`on_submit_answer`, next to the existing `multi_select` check): reject with an
  `error` event unless `placements` is a list of length `n` whose entries are each `null` or an
  `int` that is **not a `bool`**, in `[0, k)`.
- **`calculate_score`** must also return `(0, False)` for any malformed answer rather than raising
  an error. It should never rely on the gateway having checked.
- **Answer counts** (`record_answer`): for each card `i`, increment key `"{i}:{placements[i]}"`,
  or `"{i}:none"` if unplaced. That's at most `n × (k + 1)` keys, several per answer (the same
  loop pattern as `multi_select`).
- **Reveal:** `{type: "sort_into_boxes", correctBoxes: int[]}`. COMPLETENESS keeps the existing
  `{type: "completeness"}`.

#### Screens
- **Phone** (`QuestionPage`):
  - The boxes are drop zones and the cards form a pool.
  - Cards are shown in an order **shuffled once when the question mounts** and kept stable for
    the whole question, so the author's order doesn't give away the grouping. Answers always use
    the original card indices.
  - **Placing a card:** drag it onto a box (built with **pointer events**; the HTML5
    drag-and-drop API doesn't work on touch screens), or tap it to select and then tap a box.
    Tapping a placed card lets the player move it or return it to the pool.
  - The rule is shown on screen: "Each wrong placement costs 1/(k−1) of a card. Leave a card out
    if you're unsure."
  - **Submit** is enabled once at least one card is placed. If any cards are unplaced, it asks
    for confirmation.
  - Inputs are disabled after submitting or while the question is locked.
- **Projector (question):** the prompt, the box labels and the cards.
- **Projector (results):** one bar per card showing the % of answering players who put it in its
  correct box, labeled with the correct box.
- **Phone (results):** "6 of 8 correct, 1 left out" plus the points. Full / partial / none is
  decided by comparing `yourPoints` with `pointsValue`, **not** by the app's existing
  `yourPoints > 0` rule.
- **Game over (both apps):** the same information per question.

### 2.4 Spot It (`find_in_image`)

**Coordinate system:** positions are measured in units where **image width = 1**. The image
spans `x ∈ [0, 1]` and `y ∈ [0, H]`, where `H = config.height / config.width`. Using one unit on
both axes means distances mean the same thing horizontally and vertically.

#### Data

| Field | Shape | Rules |
|---|---|---|
| `config.image_id` | T8 image reference | Must refer to an existing image. Checked in `game_management_service`, since the schema can't query the database |
| `config.width`, `config.height` | int (pixels) | **Filled in by the server** from T8's image record on create, update and import. Values sent by the author are overwritten |
| `answer_data.targets` | `[{x, y, w, h}]` | ACCURACY: 1–3 rectangles (top-left corner plus size), each lying inside the image, with `w ≥ 0.05` and `h ≥ 0.05` |
| `answer_data.margin` | float | `0 ≤ margin ≤ 0.05`; the editor's default is 0.02 |
| `answer_data.falloff` | float | `0 ≤ falloff ≤ 0.15`; the editor's default is 0.08 |
| `points_value` | float | ACCURACY: **must be > 0**. Typed by the author |
| Player answer | `{x, y}` | Finite numbers (not bools), `0 ≤ x ≤ 1`, `0 ≤ y ≤ H` |

Because `validate_structure` can't see the image's dimensions until the service fills them in,
**the service checks the targets against `H`** after filling in `width`/`height`. The schema
checks only what it can see without them.

#### Scoring (ACCURACY)

```
for each target t:  dx = max(t.x − x, 0, x − (t.x + t.w))
                    dy = max(t.y − y, 0, y − (t.y + t.h))
                    d_t = sqrt(dx² + dy²)                 # 0 when inside t
d = min over targets of d_t
credit = 1                              if d ≤ margin
       = 1 − (d − margin) / falloff     if falloff > 0 and d < margin + falloff
       = 0                              otherwise
points = points_value × credit
is_correct = (d ≤ margin)
```

- **`margin`** absorbs thumb imprecision. There is no zoom, so players often find the object on
  the projector and then tap slightly off on the smaller phone image.
- **`falloff`** gives partial credit for near misses.
- **Examples** (defaults): inside or within 0.02 of a target → 1.0; 0.06 outside → 0.5; 0.10 or
  more outside → 0.
- **Cap on `falloff`:** a player who never found the object shouldn't earn much by tapping at
  random. With the defaults and a 0.06 × 0.06 target, a random tap averages roughly 3–4% of the
  points (an estimate from the area of the partial-credit band). The cap keeps it that low.
- **COMPLETENESS:** unchanged existing behavior (full points for any tap).

#### Checks, answer counts, reveal
- **Gateway:** reject with an `error` event unless `x` and `y` are finite `int`/`float` (not
  `bool`) inside the bounds above.
- **`calculate_score`** also returns `(0, False)` for any malformed answer.
- **Answer counts:** key `"{gx},{gy}"`, where `gx = min(99, ⌊100x⌋)` and
  `gy = min(⌈100H⌉ − 1, ⌊100y⌋)`. That's one key per answer, at most one per player per question.
- **Reveal:** `{type: "find_in_image", targets, margin, falloff}`.

#### Screens
- **Phone:** a `<canvas>` that draws the image scaled to fit the available width, keeping its
  aspect ratio. The canvas's internal size is scaled by `devicePixelRatio`, and pointer positions
  are converted to image units using the drawn image's rectangle.
  - **Tapping** places a marker; **dragging** while pressed moves it.
  - With keyboard focus on the canvas, **arrow keys** move the marker by 0.01, and a **visible
    focus ring** is drawn.
  - **Lock in** sends `{x, y}`.
  - Shows a loading state until the image loads, and an error message if it fails.
  - **No zoom:** the player app sets `user-scalable=no`, and this spec does not build zooming
    into the canvas.
- **Projector (question):** the prompt and the image, large.
- **Projector (results):** the image with every tap plotted, colored by credit (full / partial /
  none), the target rectangles outlined, and the margin and falloff bands shaded.
- **Phone (results):** its own canvas showing the player's tap and the targets, labeled "Found
  it" / "Close" / "Missed", with points.
- **Game over (both apps):** the same visuals at a smaller size.

### 2.5 What T7 needs from T8 (T8 has its own spec and is built before Spot It)

1. **An image ID** that can be stored in `config.image_id`, with stored pixel **width** and
   **height**.
2. **An image URL** that the host and player apps can load during a live session, **including
   guest players**. An `<img>` load (and so the canvas image) cannot send the app's bearer token,
   so T8 must decide how image loads are authorized.
3. **A service-level way to check that an image exists and read its size,** for create, update
   and import.
4. **Images in exported games.** A version-1 bundle can't carry images, and an image ID from one
   database means nothing in another. T8 defines the format (e.g., version 2, embedding the image
   data) and how import re-creates images and rewrites `image_id`. Version-1 bundles must keep
   importing. Sort It needs none of this.
5. **Image bytes available to `report_service`,** so the HTML report can embed the image in its
   tap plot.
6. **Deleting or replacing an image used by a question.**
   - Deleting an image referenced by any `find_in_image` question must be refused (409).
   - Replacing such an image must either keep the same aspect ratio or be refused, since the
     targets are stored relative to the image.

   *(Proposed; T8's spec decides.)*

## 3. Alternatives considered

| Alternative | Why it lost |
|---|---|
| Subject-specific canvas types: a pin on a statistics chart, labeling a biology diagram, tracing a CS algorithm | Too narrow; the team wants mechanics that work in any subject |
| Match Up (one card per box), Put In Order (numbered slots) | Narrower cases of sorting. Order needs a different scoring rule (Kendall τ) for little extra value |
| Label the Image (drag labels onto an image) | A second type depending on T8, overlapping Spot It |
| Rank It, Ballpark estimate (party) | Both compatible with the architecture; the team preferred a canvas party type |
| Room Poll (guess what the majority picks) | Scored against other players, which requires rescoring when results are shown. That bypasses per-answer scoring |
| Drawing / Pictionary | Can't be scored automatically per answer |
| Spot It speed bonus | Image download time varies by phone and network, which makes speed unfair, and scores become hard to test deterministically |
| Spot It pinch-zoom on the phone | Significant canvas gesture work. The margin plus partial credit absorbs imprecise taps instead |
| Sort It scored as plain fraction right | Rewards guessing (50% for a random guess with 2 boxes) |
| Sort It scored all-or-nothing | Gives the teacher no partial information |
| Sort It drawn on a canvas | Unnecessary (Spot It meets the requirement) and worse for accessibility than normal page elements |

## 4. Detailed Implementation

No database migration for T7: the new fields live inside the existing `config` / `answer_data`
JSON columns, and `questions.type` is a plain string. (T8 brings its own migration.)

### 4.1 Backend (`backend/app/`)

- **`schemas/admin.py`**
  - Add both type values to the `type` regex in **both** `QuestionCreate` and `QuestionUpdate`.
  - Add `validate_structure` branches enforcing every rule in the Data tables of 2.3 and 2.4,
    including `points_value > 0` for ACCURACY.
- **`services/game_management_service.py`**
  - For `find_in_image` in `create_question`, `update_question` and `import_game`: check that
    `config.image_id` exists, fill in `config.width`/`config.height` from the T8 image record,
    then check the targets against `H`.
  - Refuse with `UnprocessableError` (422) if either check fails.
  - Every question write already goes through this service, so no other write path exists.
- **`services/game_service.py`**
  - `calculate_score`: add ACCURACY branches for both types, exactly as in 2.3 and 2.4, returning
    `(0, False)` on malformed input.
  - `record_answer`: add the answer-count key branches.
  - `get_player_question_summary`: add the reveal branches.
  - `get_host_question_summary`: add the reveal branches **and** answer-count rebuild branches
    (rebuilt from `session_scores.answer_data`, using the same keys as `record_answer`).
- **`websocket/gateway.py`**
  - `_answer_reveal`: add both reveals.
  - `on_submit_answer`: add both answer-shape checks, next to the `multi_select` check.
  - `_question_payload` needs **no change**: `config` passes through as-is.
- **`services/report_service.py`**
  - Change `_extract_answer_key` to return a **list** of keys, and update its caller in
    `build_session_report` to increment each key.
  - Add both types to `_answer_reveal`, the type-badge map (with new CSS classes) and the chart
    dispatch:
    - a per-card table for Sort It: card, correct box, % correct, most common wrong box
    - an embedded image with an SVG tap plot for Spot It, using image bytes from T8 embedded in
      the file

### 4.2 Host app (`frontend/host/src/`)

- **`types/game.ts`:** add both values to the question-type unions, `AnswerReveal` variants for
  both reveals, and the summary item fields they need.
- **`pages/QuestionEditorPage.tsx`:**
  - Add both types to `QuestionType` and the type dropdown, plus new form state:
    - Sort It: box labels, and cards each with text and a correct-box index
    - Spot It: image ID, targets, margin, falloff
  - Add `questionToForm` and `formToPayload` branches.
  - For both new types, `points_value` is the form's points input in **both** grading modes (not
    calculated).
  - **Sort It editor:** add/remove boxes (2–4) and cards (4–10); a correct-box selector per card.
  - **Spot It editor:** T8's image picker; a canvas that shows the image and lets the author
    **draw, move and delete target rectangles**; `margin` and `falloff` sliders with the bands
    previewed live on the canvas.
  - Add the question-list type label and preview for both.
- **`pages/game/QuestionPage.tsx`:** show Sort It's boxes and cards, and Spot It's image.
- **`pages/game/ResultsPage.tsx`:** the per-card bar chart (the existing `AnswerBarChart` can be
  reused if its bar shape fits) and the Spot It tap plot.
- **`pages/game/GameOverPage.tsx`:** `QuestionCard` branches for both.
- **New display-only components** under `components/question/`: the target editor canvas and the
  tap plot. Per `components/CLAUDE.md`, they receive props and report changes through callbacks;
  they don't read `useGame()` or call `api`.

### 4.3 Player app (`frontend/player/src/`)

- **`types/game.ts`:** add both values to the question-type unions, and `PlayerAnswerReveal`
  variants.
- **`pages/game/QuestionPage.tsx`:** a branch for each type that renders new display-only
  components, `components/SortBoard.tsx` and `components/SpotCanvas.tsx`. They hand the answer
  back through a callback; the page calls `emitAnswer`.
- **`pages/game/ResultsPage.tsx`:** `describeAnswer` branches. For the new types, full / partial
  / none comes from `yourPoints` compared with `currentQuestion.pointsValue`.
- **`pages/game/GameOverPage.tsx`:** `describePlayerAnswer` and `describeCorrectAnswer` branches.

### 4.4 Tests, scripts, content, docs

- **`tests/unit/test_question_type_scoring.py`** (new), importing from `app.*` as `test_auth.py`
  does:
  - `calculate_score` for Sort It: k = 2, 3 and 4; the floor at 0; unplaced cards; all correct →
    exactly `points_value`; malformed answers → 0
  - `calculate_score` for Spot It: inside, at the margin, mid-falloff, beyond, a non-square image,
    multiple targets (nearest wins), malformed answers → 0
  - `QuestionCreate` acceptance and rejection for every rule in the Data tables
- **`tests/integration/`:**
  - `scenarios/all_question_types.py`: new question specs and players' answers producing full,
    partial and zero scores for both types in ACCURACY and COMPLETENESS
  - `engine/scoring.py`: `compute_question_score` and `_build_reveal` branches that exactly copy
    2.3 and 2.4
  - `_correct` / `_wrong` helpers
  - Spot It scenarios upload an image through T8's API in their setup
- **`tests/integration/test_t7_question_types.py`** (new):
  - 422 on create **and** on update for structural violations (e.g., an update removing a box
    that `correct_boxes` refers to)
  - a Spot It question referring to a nonexistent image → 422
  - export → import round trips: Sort It in version 1; Spot It in T8's format
  - a host without a grant for the game → 403 when creating either type
  - a malformed socket answer → an `error` event to that player and no `SessionScore` row, while
    the next valid answer from another player is still scored
- **`scripts/simulate_players.py`:** answer strategies for both types (random; or correct or near
  the target when given `--game-json`).
- **`sample_games/`:** each team member's T6 games include both types. Files with Spot It
  questions use T8's bundle format.
- **Context files:** update the `CLAUDE.md` of every directory touched, and extend the
  question-type checklist in `backend/app/CLAUDE.md` with these types and the test/script sites.

### 4.5 Order of work

1. Sort It backend: schema, scoring, gateway, summaries, report. Then its unit tests.
2. Sort It frontend: host editor, projector screens, player board, types.
3. Sort It integration tests and `simulate_players.py`.
4. T8, on its own spec and branch, delivering everything in 2.5.
5. Spot It backend (including the image check in the service). Then its unit tests.
6. Spot It frontend: editor canvas, player canvas, tap plots.
7. Spot It integration tests, sample games, context file updates.

### 4.6 Verification

- Unit and integration suites pass against the live stack (`cd tests/unit && pytest`,
  `cd tests/integration && pytest`), with the existing tests intact.
- CI checks pass: `ruff check`, `ruff format --check`, `tsc --noEmit` for all three apps.
- Play a room using both types with `simulate_players.py`.
- Play through by hand on real phones under the nginx build (`npm run build`, `:8080`), checking:
  - Sort It dragging and tap-then-tap on touch screens
  - Spot It tap accuracy on high-density screens
  - the projector results in both themes (T9)

## 5. Risks and open questions

- **Everything depends on T8's timing.** Spot It is the canvas type R7 requires. If T8 slips,
  Spot It slips with it.
- **Guessing correction may feel harsh,** especially with 2 boxes, where a wrong card cancels a
  right one. The on-screen rule is essential. (The SAT dropped its guessing penalty in 2016 partly
  because it disadvantaged cautious test-takers.)
- **Spot It ties:** with no speed element, easy images give everyone full points. Difficulty
  depends on the author's choice of image and time limit.
- **Phone screen space:** 10 cards plus 4 boxes on a ~360px-wide phone. Check on real devices
  before finalizing the limits.
- **Open:**
  - who owns T8, and when its spec settles 2.5
  - whether the Sort It rule text can be shorter
  - whether the default `margin` (0.02) and `falloff` (0.08) feel right in real play

## 6. Out of scope

These are existing defects found during design. Each gets its own fix branch (T1):
- The test suite's copy of fill-in-the-blank scoring doesn't match the backend.
- A non-numeric `multiple_choice` `selectedIndex` crashes the answer handler.
- `report_service` has no `multi_select` branches.
- The player app judges correctness for existing types with `yourPoints > 0`.
