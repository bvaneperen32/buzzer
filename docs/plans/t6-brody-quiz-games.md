# T6 — Quiz/Game JSON Files (Brody): two Wisconsin games

Brody's two T6 games: a classroom game, **All About Wisconsin**, and a party game, **Wisconsin
Sports**. Both are written by hand as version 2 game bundles with real downloaded photos, which a
small new script embeds. Dexter's two T6 games are separate (`docs/plans/t6-quiz-games.md`).
Branch: `feature/create-two-games`.

Status: designed in an Elephant session (2026-10-04); Goldfish-tested the same day and revised with
its findings; built the same day (`pack_game.py`, both bundles, the README and `CLAUDE.md` lines).
§5.1–5.4 pass. The UI play-test (§5.5–5.6) is Brody's.

## 1. Problem

T6 (`instructions.md`, graded by rubric R6) requires each team member to commit two game JSON files
under `sample_games/` that import cleanly into the running app. One must be a classroom game and
one a party game. Every game must use all four classic question types and both grading modes, and
at least one game per member must use the team's two T7 types. Brody wants his games built "as
code", like Dexter's, but using **real photos he downloads** rather than images drawn by a script.

The constraint that shapes the design is that a bundle can't point to an image file. Each image's
bytes have to sit inside the JSON as base64. Spot It (`find_in_image`) needs an image, so at
least one image is unavoidable.

## 2. Technical plan

- **Each committed bundle is the only source of its text.** Both games are written directly as
  version 2 bundles: `sample_games/wisconsin_all_about.json` and `sample_games/wisconsin_sports.json`.
  The question text exists only there. There's no template or generator holding a second copy.
- **Images are files beside the bundle.** Each bundle has a folder named after the file's stem,
  `sample_games/images/<stem>/`. Each image entry's `filename` names a file in that folder. The
  images are committed too, so the embedded data can always be rebuilt and reviewed.
- **New `scripts/pack_game.py`** is small (~150 lines) and generic, so teammates can reuse it. For each
  bundle it reads every image file named in the bundle and copies its bytes, base64-encoded and
  otherwise unchanged, into `data`. It validates what the server can't tell the author in advance
  and rewrites the file. It also has `--check` (is the file up to date?) and `--preview DIR` (draws
  the grid on each Spot It image, so accepted cells are chosen by looking, not by arithmetic).
- **Importing** goes through the existing Host UI (Games → Import JSON). The backend processes and
  stores each image in MySQL (`images` table, one library per game) and remaps image keys to ids.
  The committed JSON is the deliverable; the database copy is just Brody's local import.
- **No backend, frontend or test-code changes.** Dexter's integration test
  `test_version2_samples_import_and_round_trip` (`tests/integration/test_t8_images.py`) already
  runs once for every version 2 file at the top level of `sample_games/`, so both new games are
  covered automatically.

## 3. Alternatives considered

| Alternative | Why it lost |
|---|---|
| Build both games in the Host UI, then Export JSON | Workable and the least code. Brody chose the code route: Claude writes the content faster, the content is reviewable as text in the spec and MR, and the spec → Goldfish → implement flow has something concrete to check. Also, the game would live only in the local database until exported. |
| Dexter-style generator that draws the images (Pillow) | Brody wants real photos. Dexter's script is 1,160 lines, about half of it drawing code. |
| Source template (bundle minus `data`) + generator writing a separate output | Two copies of every question's text that can drift apart; `--check` isn't run in CI to catch it. |
| Paste base64 by hand once, no script | Can't be reproduced, nothing checks it, and swapping an image means hand-editing a huge line. |
| Script resizes or re-encodes images | The output bytes would then depend on the Pillow build, which is exactly Dexter's `--check` caveat. Instead the script enforces a size cap and Brody downloads right-sized images. |
| Party Spot It as a grid-aligned collage of single-player photos | Fairer cell boundaries, but needs ~30 lines of compositing code. Brody chose one real photo of 4–6 players instead. |
| Sort It and Spot It in only one game (the letter of T6) | Both games carry both types: safer for grading, at the cost of one extra image. |
| Questions about current officeholders or recent records | They go stale (the governor changes in January 2027; the Brewers are in the 2026 postseason now). Only settled history is used. |

## 4. Detailed implementation

### 4.1 Files

| File | Change |
|---|---|
| `sample_games/wisconsin_all_about.json` | New: classroom bundle (§4.3) |
| `sample_games/wisconsin_sports.json` | New: party bundle (§4.4) |
| `sample_games/images/wisconsin_all_about/` | New: `ripon_schoolhouse.jpg`, `wisconsin_map.jpg` |
| `sample_games/images/wisconsin_sports/` | New: `lambeau_leap.jpg`, `team_photo.jpg` |
| `scripts/pack_game.py` | New (§4.6) |
| `README.md` | "Sample games with images": a paragraph on these two games and `pack_game.py`; the scripts tree under "Project structure" gets a `pack_game.py` line |
| `CLAUDE.md` (root) | "Scripts" block: `python scripts/pack_game.py sample_games/<game>.json   # embed a hand-written game's images (--check, --preview DIR)` |
| this file | The map's row (§4.5) and the Spot It grids (§4.7) are filled in during implementation |

Nothing reads `sample_games/` recursively. Both test suites glob `sample_games/*.json` only,
which is why the images folder can live inside it. **Never put a non-bundle `.json` file at the
top level of `sample_games/`**, because both test suites would try to import it.

### 4.2 The bundle format (what the backend's import accepts)

Top level: `{"format": "buzzer/game", "version": 2, "game": {...}, "images": [...],
"questions": [...]}`.

- `game`: `title`, `description`, `max_players` (150 for both).
- Each `images` entry has **exactly** four fields: `key` (matches `^[A-Za-z0-9_-]{1,64}$`, unique),
  `filename`, `description` (1–300 characters after trimming; it is the image's alt text), and
  `data` (standard base64 with no line breaks). Any extra field makes the import fail.
- Each question has `type`, `grading_type` (`ACCURACY` | `COMPLETENESS`), `prompt` (≤2000 chars),
  `config`, `answer_data`, `time_limit_seconds` (2–300) and `points_value` (0–100000). Use
  **whole numbers** for points: `multiple_choice` and `true_false` decide `is_correct` by exact
  float equality.
- Image references hold **keys**, not ids:
  - `config.prompt_image_id`: any type except Spot It
  - `config.option_images`: `multiple_choice` / `multi_select` only (not used here)
  - `config.image_id`: Spot It's canvas image, required

  On import the backend creates the images and swaps each key for the new id.

Per-type shapes (validators: `backend/app/schemas/admin.py` `validate_structure`,
`backend/app/services/sort_it.py`, `backend/app/services/spot_it.py`):

| Type | `config` | `answer_data` (ACCURACY) | Notes |
|---|---|---|---|
| `multiple_choice` | `options`: ≥2 strings | `answer_points`: one number ≥0 per option | Correct option's points = `points_value`, others 0 |
| `true_false` | `{}` | `answer_points`: `{"true": n, "false": n}` | |
| `fill_in_the_blank` | `{}` | `acceptedAnswers`, `answerPoints` (same length), `editDistance` (int ≥0) | Matching ignores case and collapses whitespace; Levenshtein ≤ `editDistance` against each accepted answer |
| `multi_select` | `options` | `answer_points`: one number per option, negatives allowed | `points_value` = sum of the positives; total floors at 0 |
| `sort_into_boxes` | `boxes`: 2–4 labels ≤30 chars; `cards`: 4–10 labels ≤60 chars; labels unique ignoring case, no leading/trailing spaces | `correct_boxes`: one box index per card | Cards are shown in an order shuffled by question id, never the author's order |
| `find_in_image` | `image_id`, `grid`: `{"cols", "rows"}` each 2–10 | `accepted_cells`: 1 to ⌊cols·rows/2⌋ distinct `{"col", "row"}` (0-based), plus `"confirmed": true` | The grid must fit the image (`spot_it.max_grid`); phones show **only the grid**, the image is on the projector |

`COMPLETENESS` questions use `answer_data: {}` and award full points for any answer, so they're
used only for polls and opinions. On import, a Spot It question's boolean `confirmed` is turned
into a `confirmed_for` stamp against the imported image. Without it the question shows "Needs
review" and the game can't start until a host confirms it in the editor.

### 4.3 Game 1 (classroom): All About Wisconsin

`game`: title **"All About Wisconsin"**; description **"A classroom tour of the Badger State: its
history, geography, government and traditions. Suitable for a Wisconsin history or social studies
class."**; `max_players` 150. Maximum score 14.

| # | Type | Grading | Pts | Time | Prompt | Options / cards | Answer key |
|---|---|---|---|---|---|---|---|
| 1 | multiple_choice | COMPLETENESS | 1 | 20 | Warm-up: which Wisconsin tradition is your favorite? | Friday fish fry · Supper clubs · Cheese curds · Summerfest | `{}` |
| 2 | multiple_choice + `prompt_image_id: "ripon-schoolhouse"` | ACCURACY | 1 | 30 | A March 1854 meeting in this Ripon schoolhouse is traditionally called the birthplace of which political party? | Democratic Party · Whig Party · Republican Party · Progressive Party | `answer_points` [0, 0, 1, 0] |
| 3 | true_false | ACCURACY | 1 | 20 | True or false: Wisconsin's largest city is also its state capital. | — | true 0, false 1 |
| 4 | fill_in_the_blank | ACCURACY | 1 | 25 | When Wisconsin joined the Union in 1848, it became the ___th state. (Answer with a number.) | — | accepted "30", "30th", "thirty", "thirtieth"; points 1 each; edit distance 0 |
| 5 | multi_select | ACCURACY | 3 | 30 | Which of these are official Wisconsin state symbols? Select all that apply. | Badger · Northern cardinal · American robin · Walleye · Muskellunge (muskie) · Monarch butterfly | `answer_points` [1, −1, 1, −1, 1, −1] |
| 6 | sort_into_boxes | ACCURACY | 3 | 60 | Sort each event into the period when it happened. | boxes: "Before statehood (pre-1848)" · "1848–1899" · "1900 and later"; cards below | `correct_boxes` below |
| 7 | find_in_image, `image_id: "wisconsin-map"` | ACCURACY | 1 | 30 | Find Madison, the state capital, on this map of Wisconsin. | grid per §4.7 | cells per §4.7, `"confirmed": true` |
| 8 | multiple_choice | ACCURACY | 1 | 25 | Which Wisconsin U.S. senator founded Earth Day in 1970? | Robert La Follette · Joseph McCarthy · William Proxmire · Gaylord Nelson | `answer_points` [0, 0, 0, 1] |
| 9 | true_false | ACCURACY | 1 | 20 | True or false: Wisconsin grows more cranberries than any other state. | — | true 1, false 0 |
| 10 | fill_in_the_blank | ACCURACY | 1 | 25 | Wisconsin's state motto is a single word: ___. | — | accepted "Forward"; points 1; edit distance 1 |

Q6 cards, in this order, with their box (0 = before statehood, 1 = 1848–1899, 2 = 1900 and
later): "Harley-Davidson is founded in Milwaukee" (1903) → 2; "Jean Nicolet lands on the shore of
Green Bay" (1634) → 0; "The Peshtigo Fire, deadliest wildfire in U.S. history" (1871) → 1;
"Wisconsin passes the first U.S. unemployment insurance law" (1932) → 2; "Marquette and Jolliet
travel the Wisconsin River" (1673) → 0; "The first U.S. kindergarten opens in Watertown" (1856)
→ 1; "The first Summerfest is held in Milwaukee" (1968) → 2; "The Black Hawk War ends at the
Battle of Bad Axe" (1832) → 0; "Wisconsin's Iron Brigade fights in the Civil War" (1861–65) → 1.
So `correct_boxes` = [2, 0, 1, 2, 0, 1, 2, 0, 1]. Every card is ≤60 characters; the longest is 58.

Content rules this game follows:
- The professional tone the classroom game requires.
- No item gives away a later question. Earth Day and the Ripon meeting are deliberately not Sort It
  cards.
- Elected officials are all historical.

### 4.4 Game 2 (party): Wisconsin Sports

`game`: title **"Wisconsin Sports"**; description **"Packers, Bucks, Brewers, Badgers and more: a
trivia mix for any crowd of Wisconsin sports fans."**; `max_players` 150. Every question is worth
1,000 points; maximum score 10,000.

| # | Type | Grading | Time | Prompt | Options / cards | Answer key |
|---|---|---|---|---|---|---|
| 1 | true_false | COMPLETENESS | 15 | Confession time: I have worn a cheesehead. | — | `{}` |
| 2 | multiple_choice | ACCURACY | 20 | In which year did the Milwaukee Bucks win their first NBA championship? | 1971 · 1974 · 1983 · 2021 | [1000, 0, 0, 0] |
| 3 | fill_in_the_blank + `prompt_image_id: "lambeau-leap"` | ACCURACY | 30 | This celebration is the Lambeau Leap. Which Packers safety did it first, in 1993? (Last name is enough.) | — | accepted "Butler", "LeRoy Butler"; 1000 each; edit distance 1 |
| 4 | multi_select | ACCURACY | 30 | Which of these players won their league's regular-season MVP award while playing for a Wisconsin team? Select all that apply. | Aaron Rodgers · Reggie White · Giannis Antetokounmpo · Ray Allen · Robin Yount · Donald Driver · Kareem Abdul-Jabbar | [250, −250, 250, −250, 250, −250, 250] |
| 5 | sort_into_boxes | ACCURACY | 50 | Has this athlete ever played for a Wisconsin team (a pro team based in Wisconsin, or the Wisconsin Badgers)? | boxes: "Played for a Wisconsin team" · "Never did"; cards below | `correct_boxes` below |
| 6 | find_in_image, `image_id: "team-photo"` | ACCURACY | 30 | Find Aaron Rodgers in this photo! | grid per §4.7 | cells per §4.7, `"confirmed": true` |
| 7 | multiple_choice | ACCURACY | 20 | In which year did the Milwaukee Braves win the World Series? | 1953 · 1957 · 1965 · 1982 | [0, 1000, 0, 0] |
| 8 | true_false | ACCURACY | 20 | True or false: the Green Bay Packers are older than the NFL itself. | — | true 1000, false 0 |
| 9 | multiple_choice | COMPLETENESS | 15 | Hot take: which Wisconsin team do you root for the hardest? | Packers · Bucks · Brewers · Badgers | `{}` |
| 10 | fill_in_the_blank | ACCURACY | 25 | Which Wisconsin Badgers running back won the 1999 Heisman Trophy? (Last name is enough.) | — | accepted "Dayne", "Ron Dayne"; 1000 each; edit distance 1 |

Q5 cards, in this order (0 = played, 1 = never did): Brett Favre 0, Tom Brady 1, Michael Jordan
1, Hank Aaron 0, Moses Malone 0, Peyton Manning 1, Derek Jeter 1, J.J. Watt 0, LeBron James 1,
Russell Wilson 0. So `correct_boxes` = [0, 1, 1, 0, 0, 1, 1, 0, 1, 0].

Notes on the answer keys:
- **Q4.** "MVP" means the league's regular-season award, not Finals, All-Star or Super Bowl MVP,
  which is why the prompt says so.
  - Correct: Rodgers (NFL MVP 2011, 2014, 2020, 2021), Giannis (NBA MVP 2019, 2020), Yount (AL MVP
    1982, 1989), Kareem (NBA MVP 1971, 1972, 1974, all with the Bucks).
  - Not correct: Reggie White's awards were Defensive Player of the Year. Ray Allen and Donald
    Driver never won a league MVP.
- **Q5.** The surprise "yes" answers are deliberate: Moses Malone (Bucks 1991–93), Hank Aaron
  (Milwaukee Braves and Brewers), J.J. Watt and Russell Wilson (Badgers).
- **Q8.** Packers founded August 1919; the NFL (as the APFA) in September 1920.

### 4.5 Images

Brody downloads each image into the game's images folder. When saving it, he renames it to the
filename below, keeping its real extension (`.jpg`, `.png` or `.webp`); the bundle's `filename`
must match exactly. Limits the script enforces:
- PNG, JPEG or WebP; not animated
- **≤1600 px on the long side** after EXIF orientation
- **≤2 MiB**

A few hundred KB is the target size, because each image is stored twice in the repo: once as the
file and once as base64 inside the bundle.

All four images are final and in their folders (all JPEG, no EXIF, so the server stores them
unchanged):

| Key | Game · use | File | Size (px) | What it shows | Description (alt text) |
|---|---|---|---|---|---|
| `ripon-schoolhouse` | classroom Q2 prompt | `ripon_schoolhouse.jpg` | 638 × 480 | The Little White Schoolhouse at dusk, with its sign and a historical marker | "Photo of the Little White Schoolhouse in Ripon, Wisconsin, a small white one-room wooden schoolhouse." |
| `wisconsin-map` | classroom Q7 Spot It | `wisconsin_map.jpg` | 425 × 550 | Blank outline map (replacing a first choice that marked Madison with a capital star): state borders, rivers and lakes, **Mendota and Monona included**; **no county lines, no city star, dot or name**; a title, a compass rose and a footer | "Outline map of Wisconsin showing its lakes and rivers, with no city names." |
| `lambeau-leap` | party Q3 prompt | `lambeau_leap.jpg` | 495 × 618 | A **drawing** (not a photo) of a Packers player leaping into the stands | "Illustration of a Green Bay Packers player leaping into the stands at Lambeau Field as cheering fans reach out." |
| `team-photo` | party Q6 Spot It | `team_photo.jpg` | 738 × 369 | Four Packers (#21, #12, #85, #50) walking side by side, numbers readable | "Four Green Bay Packers players in green and gold uniforms walking side by side across the field." |

Neither a description nor the picture itself may give away the answer: no "birthplace of the
Republican Party", no "LeRoy Butler", no target player's name. Known exceptions, accepted
knowingly:
- **Ripon photo.** The sign over the door and the roadside marker read "Birthplace of the
  Republican Party", and front rows can read them on the projector. Brody accepted this for now
  (2026-10-04). Cropping the sign out or swapping the photo later needs only a re-pack.
- **Lambeau drawing.** The jersey shows #36, LeRoy Butler's number. That's a hint, not the
  answer, but the description must never mention the number.

**Licences aren't tracked.** These games are coursework: graded, never published or reused.
Brody decided (2026-10-04) that image sources and licences are out of scope.

### 4.6 `scripts/pack_game.py`

Usage, from the repo root with the venv active:

```
python scripts/pack_game.py sample_games/wisconsin_all_about.json sample_games/wisconsin_sports.json
python scripts/pack_game.py --check <bundles...>        # exit 1 if any bundle would change
python scripts/pack_game.py --preview DIR <bundles...>  # also write Spot It grid overlays to DIR
```

For each bundle path `P` (all errors for a bundle are reported, and that bundle is not written):

1. Read `P` as UTF-8 JSON. Require `format == "buzzer/game"` and `version == 2`.
2. The images folder is `P.parent / "images" / P.stem`.
3. For each image entry:
   - It has exactly the four fields (`data` may be `""` before the first pack).
   - The key matches the pattern and is unique.
   - The description is 1–300 characters after trimming, and has no spaces at either end.
   - The filename is a plain file name: no path, no control characters, no spaces at either
     end, ≤255 characters.
   - `folder / filename` exists and is ≤2 MiB.

   The import trims the description and strips the filename to a bare name
   (`image_service.clean_description` / `clean_filename`), and the round-trip test compares
   both to the authored text, so anything it would change is an error here.
   - Pillow opens it as PNG, JPEG or WebP, and it isn't animated.
   - Its size **after `ImageOps.exif_transpose`** is ≤1600 px on the long side. The server
     applies the same orientation (`image_service._normalize`), so these are the dimensions
     players see.

   Then set `data` to the base64 of the file's bytes, unchanged.
4. References: collect every `config.prompt_image_id`, `config.image_id` and non-null
   `config.option_images` entry (with `image_refs.map_image_refs`, which visits exactly those
   slots). Each must be a known key, and each key must be referenced at
   least once (the import accepts unused images, but Dexter's round-trip test asserts every image
   is used).
5. Spot It questions:
   - `grid` must fit the image: `cols ≤ max_cols` and `rows ≤ max_rows` from
     `spot_it.max_grid(width, height)` on the oriented size.
   - `accepted_cells` must be 1 to ⌊cols·rows/2⌋ distinct cells, each inside the grid. This
     runs the import's own `spot_it.validate` (without the bundle-only `confirmed`), so unknown
     keys and a zero points value are caught too.
   - Warn if `answer_data.confirmed` isn't `true`, because the game can't start until a host
     confirms it.
6. Coverage: print the question types and grading modes present.
   - **Error** if any of `multiple_choice`, `true_false`, `fill_in_the_blank`, `multi_select`, or
     either grading mode is missing (every T6 game needs them).
   - **Warn** if `sort_into_boxes` or `find_in_image` is missing (only one game per member needs
     them).
7. Serialize as `json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"`. That's the exporter's
   format (`bundle_service.serialize`) plus a final newline, the same as Dexter's files.
   - Without `--check`: write the file only if the text changed; print `wrote` or `unchanged`.
   - With `--check`: write nothing, and report `out of date` if the text differs.
8. `--preview DIR`: for each Spot It question, save `DIR/<image key>_grid.png`. It is the
   oriented image with the grid lines drawn, each cell labelled with `spot_it.cell_label` ("A1" =
   column A, row 1), and the accepted cells tinted. Overlays are drawn **even when the bundle has
   other errors** (empty `data`, placeholder cells, a coverage gap), as long as the image opens and
   the grid has two whole numbers from 2 to 10. Choosing the grid is what the preview is for, so
   it can't wait for a clean bundle. Cells outside the grid are simply not tinted.

Further rules:
- Exit code 0 when everything passes, 1 on any error or stale file.
- No network, clock or system fonts (use `ImageFont.load_default`).
- Reuse, don't copy: import `spot_it` from the backend by putting `REPO_ROOT / "backend"` on
  `sys.path` *inside the function that needs it*. A module-level import after a `sys.path` edit
  trips ruff's default E402. `app/services/spot_it.py` imports only `image_refs`, so this pulls in
  no FastAPI and no database.
- Do **not** import `bundle_service`: it imports `common.exceptions`, which imports FastAPI.
- Must pass CI's `ruff check` and `ruff format --check` on `scripts/`.

The script never re-encodes or resizes. JPEGs carrying EXIF metadata are re-encoded **by the
server** on import (`image_service._nonconformance` → `"metadata"`), so the stored bytes can
differ from the bundle's. That's harmless: Dexter's round-trip test compares two exports, never
the authored file.

### 4.7 Spot It: choosing grids and accepted cells

Choose these after the images are in the folders, using `--preview`. Then record the result here.

- **Classroom Q7 (map → Madison).** Pick a grid from 5×5 to 8×8 (as far as the replacement
  map's `max_grid` allows) so Madison falls in 1–2 cells. Madison isn't marked, so locate it by
  its lakes. It sits on the isthmus between Lakes Mendota and Monona, in the south-central part of
  the state, where the Yahara River leaves the lakes heading south-east. Find that cluster on the
  preview before choosing the grid, and record its rough pixel position here. If it lies on a grid
  line, accept both cells.
- **Party Q6 (team photo).** Choose columns so each player occupies whole columns where possible.
  This photo has 4 players, so use 4 or 8 columns with 2 rows (it allows up to 10×5). At 4
  columns, #12 sits entirely inside column B. #85 and #50 each reach a few pixels into a
  neighbouring column. Accept every cell the target player occupies, up to half the grid. Never
  accept a cell where another player is the obvious subject. A referee and a photographer behind
  the players, and people cut off at both edges, aren't players, but they must not be the main
  subject of an accepted cell either. Decide the named player now, and put the name in the prompt.
- Set `"confirmed": true` only after Claude has viewed the `--preview` overlay (Claude can read
  PNGs) and agrees the target is inside the tinted cells. After import, Brody checks the question
  in the Host question editor: it must show no "Needs review".

Record:
- **Q7:** grid 7 × 7, cell D6 (`col` 3, `row` 5). Madison's lakes are at about x 216–227,
  y 407–422 of the 425 × 550 map, at least 14 px inside every edge of D6. 5 × 5 to 8 × 8 grids
  were weighed; all but 7 × 7 and 5 × 5 put a line within 4 px of the lakes, and 5 × 5's cells
  are too coarse. Milwaukee falls in column E or F. Confirmed from the preview, 2026-10-04.
- **Q6:** Aaron Rodgers (#12), grid 4 × 2, cells B1 and B2 (`col` 1, `row` 0 and 1). He fits
  entirely inside column B. #21, #85 and #50 are each the clear subject of their own column, and
  the referee behind him isn't a player. Confirmed from the preview, 2026-10-04.

### 4.8 Order of work

1. Brody downloads the four images into the two folders (§4.5). Done.
2. Write `scripts/pack_game.py` (§4.6); run `ruff check` / `ruff format` on it.
3. Write both bundles exactly as §4.3 and §4.4, with `"data": ""` and a placeholder for each
   Spot It answer:
   - `grid` is `spot_it.default_grid` for the image: team photo 6 × 3; for the map, whatever it
     gives for the replacement (any valid grid will do until it arrives).
   - `accepted_cells` is `[{"col": 0, "row": 0}]`.
   - `"confirmed": false`, which `pack_game.py` reports as a warning, not an error.
4. Run `pack_game.py --preview <scratch dir>`, choose grids and cells (§4.7), set `confirmed`,
   and record the choices in §4.7.
5. Run `pack_game.py` on both, then `--check`.
6. Import and play-test (§5).
7. Update `README.md` and the root `CLAUDE.md` (§4.1).
8. Stop and ask Brody whether and how to commit (he prefers a few large commits, and decides
   himself).

## 5. Verification

1. `python scripts/pack_game.py --check sample_games/wisconsin_all_about.json
   sample_games/wisconsin_sports.json` exits 0. The coverage line shows all 6 types and both
   modes for each.
2. `ruff check backend/ scripts/` and `ruff format --check backend/ scripts/` pass (CI).
3. Unit tests pass unchanged. Both new files are version 2, so the v1-only
   `test_sample_games_export_as_before` doesn't pick them up. On Windows PowerShell:
   `cd tests/unit; $env:PYTHONPATH = "..\..\backend"; python -m pytest -q`.
4. Integration (stack up): `cd tests/integration; python -m pytest -q -rs -k samples`.
   `test_version2_samples_import_and_round_trip` now runs 4 cases, Dexter's 2 plus these 2. It
   checks:
   - the import succeeds
   - every image is used and each reference lands on its image
   - Spot It arrives confirmed with the right stamp
   - export → import → export gives identical bytes
5. Import both files through Host → Games → Import JSON into a course Brody hosts. Then check:
   - Each library shows its 2 images with their descriptions.
   - All 10 questions have the right type, grading and points.
   - Both Spot It questions show no "Needs review".
6. Play each game end to end: host projector, one browser player answering everything correctly,
   and `python scripts/simulate_players.py --room <CODE>` for extra players.
   - The all-correct player scores **14** (classroom) and **10,000** (party).
   - Prompt images and the Spot It image show on the projector; the Spot It grid shows on the
     phone.
   - One deliberately wrong Spot It click scores 0.

## 6. Risks and known limitations

- **Spot It answers rest on a visual check.** The import trusts `"confirmed": true`, so a wrong
  cell imports silently and marks right answers wrong. The preview plus the editor check (§4.7)
  is the only guard.
- **The party Spot It is recognition from across a room.** A random guess among 5 players has a
  1-in-5 chance of being right. That's acceptable for a party game, and is why the photo must show
  readable faces or numbers.
- **Fill-in-the-blank tolerance can accept wrong answers.** With edit distance 1, "Cutler" matches
  "Butler" and "Wayne" matches "Dayne". This is accepted in exchange for tolerating misspellings
  such as "Butlar" and "Foward".
- **Facts.** Every answer was checked against the sources in §7. The Goldfish critic should
  re-check the keys. Nicolet is the traditional "first European in Wisconsin", but historians
  dispute that, so the Sort It card only says he landed at Green Bay.
- **Repo size.** Each image is stored twice (file + base64), so keep the images small (§4.5).
- **The images are low-resolution.** The three final ones are 618–738 px on the long side. The
  script enforces a maximum size but no minimum, so the projector scales them up (about 1.5–2× on
  a 1080p screen). Jersey numbers stay readable and photos look soft. Aim for a map replacement of
  at least ~600 px. A sharper
  replacement later needs only a re-pack, plus a new Spot It check if its grid image changes.
- **`--check` isn't in CI**, the same gap as Dexter's script. What can drift here is only the
  embedded image data versus the files, never the text, because the bundle is its only copy.
- **Out of scope:** two display bugs noted in `docs/plans/t6-quiz-games.md` §5. Phones show
  "Correct!" for partial credit (it affects this game's multi-select and Sort It), and the
  projector's results chart clips long option labels.

## 7. Fact sources (checked 2026-10-04)

- Republican Party / Ripon, 20 March 1854: [Wisconsin Historical Society](https://www.wisconsinhistory.org/Records/Article/CS17220), [Wikipedia: Little White Schoolhouse](https://en.wikipedia.org/wiki/Little_White_Schoolhouse)
- State symbols (badger 1957, American robin 1949, muskellunge 1955, honey bee): [Wikipedia: List of Wisconsin state symbols](https://en.wikipedia.org/wiki/List_of_Wisconsin_state_symbols)
- Cranberries (Wisconsin first for 31 straight years, about 60–65% of the U.S. crop): [The Farm WI](https://www.thefarmwi.com/wisconsin-atop-cranberry-production-for-31st-year-in-a-row/), [PolitiFact](https://politifact.com/factchecks/2023/feb/02/tony-evers/yes-wisconsin-does-produce-60-of-the-countrys-supp/)
- Nicolet, 1634: [Wikipedia: Jean Nicolet](https://en.wikipedia.org/wiki/Jean_Nicolet), [WHS: Was Nicolet really the first European?](https://wisconsinhistory.org/Records/Article/CS293)
- First kindergarten, Watertown 1856: [Wikipedia: First Kindergarten](https://en.wikipedia.org/wiki/First_Kindergarten)
- Unemployment insurance, 28 January 1932: [SSA historian](https://www.ssa.gov/history/wiscrole.html)
- First Summerfest, July 1968: [Wikipedia: Summerfest](https://en.wikipedia.org/wiki/Summerfest)
- Lambeau Leap, LeRoy Butler, 26 December 1993: [Packers.com](https://www.packers.com/news/leroy-butler-forever-part-of-lambeau-field), [Wikipedia: Lambeau Leap](https://en.wikipedia.org/wiki/Lambeau_Leap)
- Moses Malone with the Bucks, 1991–93: [Wikipedia: Moses Malone](https://en.wikipedia.org/wiki/Moses_Malone), [UPI, July 1991](https://www.upi.com/Archives/1991/07/10/Bucks-sign-Moses-Malone/1639679118400/)
- Not looked up (well established): statehood 1848 as 30th state; motto "Forward"; Milwaukee
  largest city, Madison capital; Gaylord Nelson and Earth Day; Bad Axe 1832; Marquette and Jolliet
  1673; Iron Brigade; Peshtigo 1871; Harley-Davidson 1903; Bucks 1971; Braves 1957; Packers 1919
  and APFA 1920; Dayne's 1999 Heisman; the MVP years in §4.4. The Goldfish should spot-check these.
