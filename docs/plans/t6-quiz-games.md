# T6 — Quiz/Game JSON Files (Dexter)

Two new games in `sample_games/`, one for class and one for a party, both generated with their
images by `scripts/generate_t6_games.py`. This file records what was delivered and how it was
verified. Branch: `feature/dexter-t6-quiz-games`.

## 1. The games

| File | Title | Purpose | Questions | Images | Max score |
|---|---|---|---|---|---|
| `read_the_graph_data_literacy.json` | Read the Graph: Data Literacy Essentials | Classroom: intro statistics and reading charts, professional tone, made-up example data | 11 | 7 | 14 pts |
| `game_night_gauntlet.json` | Game Night Gauntlet | Party: dice, cards, tiles and hot takes, no experts needed | 11 | 6 | 11,000 pts |

**Read the Graph** (classroom)

| # | Type | Grading | Points | Topic | Image |
|---|---|---|---|---|---|
| 1 | Multiple choice | Completeness | 1 | Warm-up: where you meet charts | — |
| 2 | Fill in the blank | Accuracy | 1 | Median of a small data set | — |
| 3 | Multi-select | Accuracy | 3 | Measures of spread (wrong picks cost a point) | — |
| 4 | Sort It | Accuracy | 2 | Categorical vs quantitative variables (8 cards) | — |
| 5 | Multiple choice | Accuracy | 1 | Strongest negative correlation | 4 scatter plots as options |
| 6 | True/false | Accuracy | 1 | Mean vs median in a right-skewed histogram | Prompt |
| 7 | True/false | Accuracy | 1 | Correlation strength (r = −0.8 vs 0.5) | — |
| 8 | Multiple choice | Accuracy | 1 | Actual increase behind a truncated axis | Prompt |
| 9 | Spot It | Accuracy | 1 | Find the outlier in a scatter plot | Canvas, 6×4 grid |
| 10 | Fill in the blank | Accuracy | 1 | The mode (one typo allowed) | — |
| 11 | Multi-select | Completeness | 1 | Exit ticket: what to review next | — |

**Game Night Gauntlet** (party)

| # | Type | Grading | Points | Topic | Image |
|---|---|---|---|---|---|
| 1 | True/false | Completeness | 1,000 | Confession: quit a game of Monopoly | — |
| 2 | Multiple choice | Accuracy | 1,000 | Opposite faces of a die | — |
| 3 | Fill in the blank | Accuracy | 1,000 | Scrabble score for QUIZ | Prompt (letter tiles) |
| 4 | Multiple choice | Accuracy | 1,000 | Which poker hand is a straight | 4 hands as options |
| 5 | Multi-select | Accuracy | 1,000 | Games that use dice (wrong picks cost points) | — |
| 6 | Sort It | Accuracy | 1,000 | Board, card or dice game (9 cards, 3 boxes) | — |
| 7 | Spot It | Accuracy | 1,000 | Find the one die showing a single pip | Canvas, 8×5 grid |
| 8 | True/false | Accuracy | 1,000 | The knight jumps over pieces | — |
| 9 | Fill in the blank | Accuracy | 1,000 | Castling (one typo allowed) | — |
| 10 | Multiple choice | Completeness | 1,000 | Best game-night snack | — |
| 11 | Sort It | Completeness | 1,000 | Love it or leave it (no answer key) | — |

## 2. Requirement coverage

| T6 requirement | How it's met |
|---|---|
| 2 games per member | The two files above |
| Every supported type | Both games use multiple choice, true/false, fill in the blank and multi-select |
| Both grading modes | Both games mix ACCURACY and COMPLETENESS |
| Classroom and party | Read the Graph (class), Game Night Gauntlet (party) |
| The team's new types (T7) | Both games include Sort It and Spot It |
| Format, clean import | Imported through the Admin UI (§4.3) and by the integration tests (§4.2) |
| Format version | Version 2 (T8's image format), since both embed images. Unmodified version 1 files still import (§4.2) |
| Committed | Under `sample_games/` |

## 3. Images and the generator

The games are generated, not hand-edited. `scripts/generate_t6_games.py` draws all 13 images with
Pillow from fixed data and fixed seeds (no clock, network or system font), embeds them as base64,
and writes both bundles. Before writing anything it computes every picture-dependent answer from
the data it drew and refuses to write if one disagrees: the correlations, mean vs median, the
percentage increase, the outlier's and the one-pip die's cells, and which hand is a straight. It
also checks each bundle against T8's limits (image size and count, library budget, import size),
that every image is used, and that the games cover every type and both grading modes.

`--check` exits 1 if either file differs from what the script would write. Byte-identical PNGs
need the same Pillow build, so it runs from the repo's venv. To change a game, change the script
and regenerate (README, "Sample games with images").

## 4. Verification (2026-10-01)

All checks ran against this branch after merging origin/main at `92cd140`, the live-session fix
MR (`4664b14`) and T9 (`92cd140`) included, as merge `cbc0a95`. The stack was rebuilt from it
(`docker compose up --build`, `alembic upgrade head` at `005`, `npm run build`); the T6 files
weren't changed after that.

### 4.1 Offline checks

- `python scripts/generate_t6_games.py --check`: both games up to date.
- CI's `ruff check` and `ruff format --check` on `backend/` and `scripts/`: pass.
- `scripts/check_theme.py` (T9's token and contrast check): pass.
- Unit tests, `tests/unit/test_bundle_service.py`: 100 passed, including
  `test_sample_games_export_as_before` for each of the 12 version 1 sample games.

### 4.2 Integration tests (live stack)

`tests/integration/test_t8_images.py -k samples`: 3 passed.

- `test_version1_samples_still_import`: imports all 12 version 1 sample games, as before T8.
  It and the unit test above now select version 1 files explicitly, since the T6 games are
  version 2.
- `test_version2_samples_import_and_round_trip[game_night_gauntlet.json]` and
  `[read_the_graph_data_literacy.json]` (new): the whole image library arrives in bundle order
  with filenames and descriptions; each image reference lands on the image its key named; Spot It
  questions arrive confirmed, with `confirmed_for` matching the grid and image version; and
  export → import → export gives identical bytes.

Only the sample-game cases ran; the change touches nothing else.

### 4.3 Admin UI import

Both files were imported through Admin → Host tools → Games → Import JSON, into the course
"T6 Games - Dexter" (Fall 2026), and kept there.

- Libraries: 7 and 6 images, each with its filename, description and size (800×800 options,
  1600×900 and 1600×600 prompts and canvases), each used by the intended question. The images
  served back are byte-identical to the bundles'.
- Questions: all 22 have the bundle's type, grading and points.
- Spot It: both questions confirmed, no "Needs review".

### 4.4 Browser gameplay

Both games were played to the end through the production builds, with a projector page and two
phone pages (390×844 and 360×640), using only the UI, twice: 153 of 153 checks passed each time.

- Images loaded on the projector (prompts, options, Spot It canvas) and on the phones (option
  tiles); the Spot It grid matched on every screen.
- One player answered everything right; the other gave wrong, partial, typo and missing answers.
  Every stored score matched the expected value: 14 and 5.5 of 14 (classroom), 11,000 and 5,750
  of 11,000 (party). This covered multi-select negative points, Sort It partial credit, typo
  tolerance and "No answer".
- The rooms and guest accounts these runs created were deleted afterwards through the test
  ownership ledger (`tests/ledger.py`); the two imported games were never in it.

### 4.5 T9 compatibility

The classroom game ran in the light theme on a 1920×1080 projector, the party game in the dark
theme on 1280×720. Prompts and images are readable in both themes, images keep full brightness
on dark, and no phone screen scrolls sideways. A few 1280×720 results screens scroll slightly, but
only padding below the Next button.

### 4.6 Physical phone

The physical-phone checklist for both games (joining by QR over Wi-Fi, every question in order,
touch, the keyboard, rotation in Spot It, the theme toggle, results and game over) passed, on
Dexter's confirmation, 2026-10-01. For the test, the LAN address was temporarily added to
`CORS_ORIGINS` in `.env` (live sockets were checked from that address and from localhost); it was
removed afterwards, and `.env` is not committed.

## 5. Found during verification, not part of T6

Two display issues in the existing apps, tracked separately for their own fix branches:

- **Projector results clip option labels.** `AnswerBarChart` in
  `frontend/host/src/pages/game/ResultsPage.tsx` puts "letter + option text" in a 2rem column, so
  multi-word options wrap one word per line and long ones are cut off. From the starter template.
- **Phones say "Correct!" for partial credit.** The player's `ResultsPage` and `GameOverPage`
  decide from `points > 0`, not the server's `is_correct`. Already noted in
  `docs/plans/fix-live-session-test-cleanup.md`.
