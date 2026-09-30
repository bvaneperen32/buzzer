"""Spot It (`find_in_image`, T7): the question type's pure rules.

The projector shows an image under a labelled grid; players pick the matching cell on a phone
that shows only the grid (never the image). ACCURACY scoring is all or nothing on an accepted
cell (docs/plans/t7-question-types.md §2.4–§2.5, §2.9).

Pure: no database and no I/O; imports only the image-reference keys. The schema, the scorer,
the gateway, the summaries, the report, the room gates and the bundle builder all share it.

Two kinds of function:
- `validate` raises `ValueError` (a 422) and runs on every write path;
- everything that reads **stored** data never raises: malformed values (only reachable by
  direct database edits) read as "no usable value", and `review_status` reports the question
  as needing review.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from .image_refs import CANVAS_KEY

TYPE = "find_in_image"

MIN_GRID, MAX_GRID = 2, 10

# `width`/`height` are server-owned: accepted here without any check, and replaced with the
# trusted image dimensions before every write (spec §2.4).
CONFIG_KEYS = frozenset({CANVAS_KEY, "grid", "width", "height", "prompt_image_id"})
SERVER_CONFIG_KEYS = ("width", "height")
ANSWER_DATA_KEYS = frozenset({"accepted_cells", "confirmed_for"})
GRID_KEYS = frozenset({"cols", "rows"})
CELL_KEYS = frozenset({"col", "row"})
STAMP_KEYS = frozenset({"image_id", "image_version", "cols", "rows"})

# Review reasons, in the order they are always listed (spec §2.5).
IMAGE_MISSING = "image_missing"
GRID_TOO_DENSE = "grid_too_dense"
IMAGE_CHANGED = "image_changed"
IMAGE_REPLACED = "image_replaced"
GRID_CHANGED = "grid_changed"
NOT_CONFIRMED = "not_confirmed"


class ImageInfo(NamedTuple):
    """The current image a question is checked against."""

    id: int
    version: int
    width: int
    height: int


def is_int(value: Any) -> bool:
    """An integer, and not a bool (`True` is an int in Python)."""
    return type(value) is int


# ── Grid geometry (spec §2.4, integer arithmetic only) ───────────────────────


def max_grid(width: int, height: int) -> tuple[int, int]:
    """(max columns, max rows) for an image, so every cell is at least 28 px wide and tall on
    the reference phone's 308×332 grid box. 308 / 28 = 11 and 332 / 28 = 83 / 7, so this is
    exact in integers — the same in the host app's TypeScript copy (`lib/spotGrid.ts`)."""
    if not (is_int(width) and is_int(height)) or width <= 0 or height <= 0:
        return 0, 0
    if 308 * height <= 332 * width:  # the width limits the grid area
        return 10, min(10, (11 * height) // width)
    return min(10, (83 * width) // (7 * height)), 10


def grid_fits(cols: int, rows: int, width: int, height: int) -> bool:
    max_cols, max_rows = max_grid(width, height)
    return cols <= max_cols and rows <= max_rows


def default_grid(width: int, height: int) -> tuple[int, int] | None:
    """The editor's default grid for an image, rounding half up in integers; None when the
    image fits no grid."""
    max_cols, max_rows = max_grid(width, height)
    if max_cols < MIN_GRID or max_rows < MIN_GRID:
        return None
    cols = min(6, max_cols)
    rows = max(MIN_GRID, min(max_rows, (2 * cols * height + width) // (2 * width)))
    return cols, rows


def cell_label(col: int, row: int) -> str:
    """ "B3": the column letter (A–J), then the 1-based row number."""
    return f"{chr(ord('A') + col)}{row + 1}"


# ── Validation (every write path: create, merged update, import) ────────────


def _check_grid(grid: Any) -> tuple[int, int]:
    if not isinstance(grid, dict) or set(grid) != GRID_KEYS:
        raise ValueError('config.grid must be exactly {"cols": …, "rows": …}')
    cols, rows = grid["cols"], grid["rows"]
    for name, value in (("cols", cols), ("rows", rows)):
        if not is_int(value) or not MIN_GRID <= value <= MAX_GRID:
            raise ValueError(f"config.grid.{name} must be a whole number from 2 to 10")
    return cols, rows


def _check_cells(cells: Any, cols: int, rows: int) -> list[tuple[int, int]]:
    if not isinstance(cells, list):
        raise ValueError("answer_data.accepted_cells must be a list of cells")
    seen: set[tuple[int, int]] = set()
    for number, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict) or set(cell) != CELL_KEYS:
            raise ValueError(
                f'Accepted cell {number} must be exactly {{"col": …, "row": …}}'
            )
        col, row = cell["col"], cell["row"]
        if not (is_int(col) and is_int(row)):
            raise ValueError(f"Accepted cell {number} must use whole numbers")
        if not (0 <= col < cols and 0 <= row < rows):
            raise ValueError(
                f"Accepted cell {number} is outside the {cols}×{rows} grid"
            )
        if (col, row) in seen:
            raise ValueError(f"Accepted cell {cell_label(col, row)} is listed twice")
        seen.add((col, row))
    return list(seen)


def _check_stamp(stamp: Any) -> None:
    if stamp is None:
        return  # accepted in a request and removed before writing (spec §2.5 case 2)
    if not isinstance(stamp, dict) or set(stamp) != STAMP_KEYS:
        raise ValueError(
            "answer_data.confirmed_for must be exactly "
            '{"image_id", "image_version", "cols", "rows"} or null'
        )
    if not all(is_int(value) and value > 0 for value in stamp.values()):
        raise ValueError(
            "answer_data.confirmed_for values must be positive whole numbers"
        )


def validate(
    config: Any, answer_data: Any, grading_type: str, points_value: float
) -> None:
    """Raise `ValueError` unless the question is a well-formed Spot It (spec §2.4 "Data").

    Structural rules only. The image-dependent density limits gate confirmation and play, not
    saving (spec §2.4); `config.image_id` and `prompt_image_id` are checked with the other
    image references in the schema.
    """
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        raise ValueError(f"Unknown Spot It config key(s): {', '.join(unknown)}")
    cols, rows = _check_grid(config.get("grid"))

    if not isinstance(answer_data, dict):
        raise ValueError("answer_data must be an object")
    unknown = sorted(set(answer_data) - ANSWER_DATA_KEYS)
    if unknown:
        raise ValueError(f"Unknown Spot It answer_data key(s): {', '.join(unknown)}")
    half = (cols * rows) // 2
    if grading_type == "ACCURACY":
        if "accepted_cells" not in answer_data:
            raise ValueError(
                "ACCURACY Spot It questions need answer_data.accepted_cells"
            )
        cells = _check_cells(answer_data["accepted_cells"], cols, rows)
        if not 1 <= len(cells) <= half:
            raise ValueError(
                f"Choose from 1 to {half} accepted cells (at most half of the grid)"
            )
        if not points_value > 0:
            raise ValueError("ACCURACY Spot It questions need a points value above 0")
    elif "accepted_cells" in answer_data:
        cells = _check_cells(answer_data["accepted_cells"], cols, rows)
        if len(cells) > half:
            raise ValueError(f"Choose at most {half} accepted cells (half of the grid)")
    _check_stamp(answer_data.get("confirmed_for"))


# ── Reading stored questions (never raises) ─────────────────────────────────


def grid(config: Any) -> tuple[int, int] | None:
    """(cols, rows) from a stored config, or None when it isn't a valid grid."""
    if not isinstance(config, Mapping):
        return None
    value = config.get("grid")
    if not isinstance(value, Mapping) or set(value) != GRID_KEYS:
        return None
    cols, rows = value["cols"], value["rows"]
    if not all(is_int(v) and MIN_GRID <= v <= MAX_GRID for v in (cols, rows)):
        return None
    return cols, rows


def accepted_cells(answer_data: Any, cols: int, rows: int) -> set[tuple[int, int]]:
    """The stored accepted cells that are well formed and inside the grid."""
    if not isinstance(answer_data, Mapping):
        return set()
    value = answer_data.get("accepted_cells")
    if not isinstance(value, list):
        return set()
    cells = set()
    for cell in value:
        if not isinstance(cell, Mapping) or set(cell) != CELL_KEYS:
            continue
        col, row = cell["col"], cell["row"]
        if is_int(col) and is_int(row) and 0 <= col < cols and 0 <= row < rows:
            cells.add((col, row))
    return cells


def stamp(answer_data: Any) -> dict | None:
    """The stored confirmation stamp when it is well formed, else None (no stamp)."""
    if not isinstance(answer_data, Mapping):
        return None
    value = answer_data.get("confirmed_for")
    if not isinstance(value, Mapping) or set(value) != STAMP_KEYS:
        return None
    if not all(is_int(v) and v > 0 for v in value.values()):
        return None
    return dict(value)


def stamp_for(image_id: int, version: int, cols: int, rows: int) -> dict:
    return {"image_id": image_id, "image_version": version, "cols": cols, "rows": rows}


# ── Answers (the gateway's check, scoring, answer counts, reveal) ───────────


def parse_answer(config: Any, answer: Any) -> tuple[int, int] | None:
    """A player's cell, or None when the answer is malformed for this question: exactly
    `{"col": int, "row": int}` (ints, not bools) inside the question's grid."""
    dims = grid(config)
    if dims is None or not isinstance(answer, Mapping) or set(answer) != CELL_KEYS:
        return None
    col, row = answer["col"], answer["row"]
    if not (is_int(col) and is_int(row)):
        return None
    cols, rows = dims
    if not (0 <= col < cols and 0 <= row < rows):
        return None
    return col, row


def score(
    grading_type: str, points_value: float, config: Any, answer_data: Any, answer: Any
) -> tuple[float, bool]:
    """(points, is_correct): all or nothing on an accepted cell (set membership, so the
    stored order of the cells never matters). A malformed answer scores (0, False) in either
    grading mode — before the COMPLETENESS shortcut."""
    cell = parse_answer(config, answer)
    if cell is None:
        return 0.0, False
    if grading_type == "COMPLETENESS":
        return points_value, True
    if cell in accepted_cells(answer_data, *grid(config)):
        return points_value, True
    return 0.0, False


def dist_key(cell: tuple[int, int]) -> str:
    """One answer-count key per answer: "{col},{row}" (at most cols·rows ≤ 100 keys)."""
    return f"{cell[0]},{cell[1]}"


def sort_cells(cells: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Row-major order: row, then column — the order reveals and displays use."""
    return sorted(cells, key=lambda cell: (cell[1], cell[0]))


def reveal(grading_type: str, config: Any, answer_data: Any) -> dict:
    """What results reveal: the accepted cells in row-major order, whatever their stored
    order. COMPLETENESS keeps the existing `{type: "completeness"}`."""
    if grading_type == "COMPLETENESS":
        return {"type": "completeness"}
    dims = grid(config)
    cells = sort_cells(accepted_cells(answer_data, *dims)) if dims else []
    return {"type": TYPE, "acceptedCells": [{"col": c, "row": r} for c, r in cells]}


# ── Confirmation (spec §2.5): the one shared rule ────────────────────────────


def review_status(config: Any, answer_data: Any, image: ImageInfo | None) -> list[str]:
    """Every review reason that applies, in the fixed order; empty means confirmed.

    `image` is the current image of `config.image_id` in the question's game, or None when it
    isn't in the library. Stored data that fails the shape check reads as `not_confirmed`, so
    a malformed row can't be played and never raises here.
    """
    if image is None:
        return [IMAGE_MISSING]
    dims = grid(config)
    if dims is None:
        return [NOT_CONFIRMED]
    cols, rows = dims
    reasons = []
    if not grid_fits(cols, rows, image.width, image.height):
        reasons.append(GRID_TOO_DENSE)
    stored = stamp(answer_data)
    if stored is None:
        reasons.append(NOT_CONFIRMED)
        return reasons
    image_id = config.get(CANVAS_KEY) if isinstance(config, Mapping) else None
    if stored["image_id"] != image_id:
        reasons.append(IMAGE_CHANGED)
    elif stored["image_version"] != image.version:
        reasons.append(IMAGE_REPLACED)
    if (stored["cols"], stored["rows"]) != (cols, rows):
        reasons.append(GRID_CHANGED)
    return reasons


def too_dense_text(cols: int, rows: int, width: int, height: int) -> str:
    max_cols, max_rows = max_grid(width, height)
    if max_rows < MIN_GRID:
        return "its image is too wide for any grid (crop it to at most 11:2)"
    if max_cols < MIN_GRID:
        return "its image is too tall for any grid (crop it to at most 14:83)"
    return f"the {cols}×{rows} grid is too dense for its image (at most {max_cols}×{max_rows})"


def reason_texts(
    reasons: Iterable[str], config: Any, image: ImageInfo | None
) -> list[str]:
    """The display text of each reason (spec §2.5 table), lower-case, in the given order."""
    texts = []
    for reason in reasons:
        if reason == IMAGE_MISSING:
            texts.append("its image is missing")
        elif reason == GRID_TOO_DENSE and image is not None and grid(config):
            texts.append(too_dense_text(*grid(config), image.width, image.height))
        elif reason == IMAGE_CHANGED:
            texts.append("a different image was chosen after the cells were checked")
        elif reason == IMAGE_REPLACED:
            texts.append("its image was replaced after the cells were checked")
        elif reason == GRID_CHANGED:
            texts.append("its grid changed after the cells were checked")
        elif reason == NOT_CONFIRMED:
            texts.append("its accepted cells haven't been checked")
    return texts


# ── Export (spec §2.9): the one portable representation ─────────────────────


def export_fields(
    config: Any, answer_data: Any, image: ImageInfo | None
) -> tuple[Any, Any]:
    """(config, answer_data) as a bundle carries them. Server-owned `width`/`height` are
    dropped (import rebuilds them); the stamp becomes `"confirmed": true`, written only when
    the question is confirmed against the exported image. `accepted_cells` keeps its stored
    order and presence. The key order is fixed here, independent of MySQL's JSON key order."""
    if not isinstance(config, Mapping):
        return config, answer_data
    confirmed = review_status(config, answer_data, image) == []
    exported_config = {
        key: value for key, value in config.items() if key not in SERVER_CONFIG_KEYS
    }
    exported_answer: dict[str, Any] = {}
    if isinstance(answer_data, Mapping) and "accepted_cells" in answer_data:
        exported_answer["accepted_cells"] = answer_data["accepted_cells"]
    if confirmed:
        exported_answer["confirmed"] = True
    return exported_config, exported_answer
