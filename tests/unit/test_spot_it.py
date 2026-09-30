"""
Unit tests for Spot It (`find_in_image`, T7): app/services/spot_it.py.

Pure functions only — no database, Redis or running stack. The spec is
docs/plans/t7-question-types.md (§2.4 data and geometry, §2.5 review status, §2.9 export).

Run with:
    cd tests/unit && PYTHONPATH=../../backend pytest test_spot_it.py -v
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.admin import QuestionCreate
from app.services import spot_it
from app.services.spot_it import (
    GRID_CHANGED,
    GRID_TOO_DENSE,
    IMAGE_CHANGED,
    IMAGE_MISSING,
    IMAGE_REPLACED,
    NOT_CONFIRMED,
    ImageInfo,
)

LANDSCAPE = ImageInfo(id=7, version=1, width=1600, height=900)
PORTRAIT = ImageInfo(id=7, version=2, width=900, height=1600)


def cell(col: int, row: int) -> dict:
    return {"col": col, "row": row}


def config(cols=6, rows=4, image_id=7, **extra) -> dict:
    return {"image_id": image_id, "grid": {"cols": cols, "rows": rows}, **extra}


def refused(cfg, answer_data, grading="ACCURACY", points=1000.0) -> str:
    with pytest.raises(ValueError) as caught:
        spot_it.validate(cfg, answer_data, grading, points)
    return str(caught.value)


def accepted(cfg, answer_data, grading="ACCURACY", points=1000.0) -> None:
    spot_it.validate(cfg, answer_data, grading, points)


CELLS = {"accepted_cells": [cell(4, 1), cell(4, 2)]}


def body(**overrides) -> dict:
    question = {
        "type": "find_in_image",
        "grading_type": "ACCURACY",
        "prompt": "Find the lighthouse",
        "config": config(),
        "answer_data": dict(CELLS),
        "points_value": 1000,
    }
    question.update(overrides)
    return question


def schema_error(question: dict) -> str:
    with pytest.raises(ValidationError) as caught:
        QuestionCreate(**question)
    return str(caught.value)


# ---------------------------------------------------------------------------
# Data rules (spec §2.4 "Data") — structural only
# ---------------------------------------------------------------------------


class TestGrid:
    def test_valid(self):
        accepted(config(), CELLS)
        accepted(config(2, 2), {"accepted_cells": [cell(0, 0)]})
        accepted(config(10, 10), CELLS)

    @pytest.mark.parametrize(
        "grid",
        [
            None,
            [6, 4],
            {"cols": 6},
            {"cols": 6, "rows": 4, "extra": 1},
            {"cols": 1, "rows": 4},
            {"cols": 6, "rows": 11},
            {"cols": True, "rows": 4},
            {"cols": 6.0, "rows": 4},
            {"cols": "6", "rows": 4},
        ],
    )
    def test_malformed(self, grid):
        cfg = {"image_id": 7, "grid": grid}
        assert "config.grid" in refused(cfg, CELLS)

    def test_unknown_keys(self):
        assert "Unknown Spot It config key(s): options" in refused(
            config(options=["a"]), CELLS
        )
        assert "Unknown Spot It answer_data key(s): confirmed" in refused(
            config(), {**CELLS, "confirmed": True}
        )

    @pytest.mark.parametrize(
        ("width", "height"), [("abc", -5), (None, None), (1.5, [1]), (0, 0)]
    )
    def test_width_and_height_are_never_checked(self, width, height):
        accepted(config(width=width, height=height), CELLS)
        accepted(config(width=width), CELLS)
        accepted(config(), CELLS)


class TestAcceptedCells:
    def test_required_under_accuracy(self):
        assert "need answer_data.accepted_cells" in refused(config(), {})
        assert "Choose from 1 to 12 accepted cells" in refused(
            config(), {"accepted_cells": []}
        )

    def test_half_the_grid_at_most(self):
        cells = [cell(c, r) for r in range(2) for c in range(6)]  # 12 of 24
        accepted(config(), {"accepted_cells": cells})
        assert "Choose from 1 to 12 accepted cells" in refused(
            config(), {"accepted_cells": [*cells, cell(0, 2)]}
        )

    @pytest.mark.parametrize(
        ("bad", "message"),
        [
            (cell(6, 0), "outside the 6×4 grid"),
            (cell(0, 4), "outside the 6×4 grid"),
            (cell(-1, 0), "outside the 6×4 grid"),
            ({"col": True, "row": 0}, "whole numbers"),
            ({"col": 1.0, "row": 0}, "whole numbers"),
            ({"col": 1}, "exactly"),
            ({"col": 1, "row": 0, "x": 0}, "exactly"),
            ([1, 0], "exactly"),
        ],
    )
    def test_malformed_cells(self, bad, message):
        assert message in refused(config(), {"accepted_cells": [cell(0, 0), bad]})

    def test_duplicates(self):
        assert "Accepted cell E2 is listed twice" in refused(
            config(), {"accepted_cells": [cell(4, 1), cell(0, 0), cell(4, 1)]}
        )

    def test_not_a_list(self):
        assert "must be a list" in refused(config(), {"accepted_cells": cell(0, 0)})

    def test_accuracy_needs_positive_points(self):
        assert "points value above 0" in refused(config(), CELLS, points=0)

    def test_completeness_cells_are_optional(self):
        accepted(config(), {}, grading="COMPLETENESS", points=0)
        accepted(config(), {"accepted_cells": []}, grading="COMPLETENESS")
        accepted(config(), CELLS, grading="COMPLETENESS")
        too_many = [cell(c, r) for r in range(3) for c in range(5)]  # 15 of 24
        assert "at most 12 accepted cells" in refused(
            config(), {"accepted_cells": too_many}, grading="COMPLETENESS"
        )


class TestStamp:
    STAMP = {"image_id": 7, "image_version": 1, "cols": 6, "rows": 4}

    def test_valid_and_null(self):
        accepted(config(), {**CELLS, "confirmed_for": self.STAMP})
        accepted(config(), {**CELLS, "confirmed_for": None})

    @pytest.mark.parametrize(
        "stamp",
        [
            {"image_id": 7, "image_version": 1, "cols": 6},
            {**STAMP, "extra": 1},
            {**STAMP, "image_version": 0},
            {**STAMP, "cols": -6},
            {**STAMP, "image_id": True},
            {**STAMP, "rows": "4"},
            {**STAMP, "rows": 4.0},
            [7, 1, 6, 4],
            True,
        ],
    )
    def test_malformed(self, stamp):
        assert "confirmed_for" in refused(config(), {**CELLS, "confirmed_for": stamp})


class TestSchema:
    def test_accepted(self):
        QuestionCreate(**body())

    @pytest.mark.parametrize("image_id", [None, 0, -1, "7", True, 7.0])
    def test_image_id_is_required(self, image_id):
        assert "config.image_id is required" in schema_error(
            body(config=config(image_id=image_id))
        )

    def test_image_id_absent(self):
        cfg = {"grid": {"cols": 6, "rows": 4}}
        assert "config.image_id is required" in schema_error(body(config=cfg))

    def test_prompt_image_is_refused(self):
        assert "config.prompt_image_id must be absent or null" in schema_error(
            body(config=config(prompt_image_id=5))
        )
        QuestionCreate(**body(config=config(prompt_image_id=None)))

    def test_image_id_still_refused_on_other_types(self):
        error = schema_error(
            {
                "type": "true_false",
                "grading_type": "ACCURACY",
                "prompt": "Is it?",
                "config": {"image_id": 7},
                "answer_data": {"answer_points": {"true": 1, "false": 0}},
            }
        )
        assert "only allowed on canvas question types" in error


# ---------------------------------------------------------------------------
# Grid geometry (spec §2.4) — integer arithmetic
# ---------------------------------------------------------------------------


class TestMaxGrid:
    @pytest.mark.parametrize(
        ("width", "height", "expected"),
        [
            (1600, 900, (10, 6)),  # 16:9 landscape
            (1600, 1200, (10, 8)),  # 4:3
            (1000, 1000, (10, 10)),  # 1:1
            (1200, 1600, (8, 10)),  # 3:4 portrait
            (900, 1600, (6, 10)),  # 9:16 portrait
            (1500, 500, (10, 3)),  # 3:1 panorama
            (500, 1500, (3, 10)),  # 1:3 tall
            (1200, 200, (10, 1)),  # 6:1 — fits no grid
            (200, 1200, (1, 10)),  # 1:6 — fits no grid
        ],
    )
    def test_table(self, width, height, expected):
        assert spot_it.max_grid(width, height) == expected

    @pytest.mark.parametrize(
        ("width", "height", "expected"),
        [
            (1100, 200, (10, 2)),
            (1101, 200, (10, 1)),
            (140, 830, (2, 10)),
            (140, 831, (1, 10)),
            (308, 332, (10, 10)),  # the branch tie
        ],
    )
    def test_exact_edges(self, width, height, expected):
        assert spot_it.max_grid(width, height) == expected

    @pytest.mark.parametrize(("width", "height"), [(0, 900), (1600, -1), (True, 5), (1.5, 2)])
    def test_unusable_dimensions_fit_nothing(self, width, height):
        assert spot_it.max_grid(width, height) == (0, 0)
        assert not spot_it.grid_fits(2, 2, width, height)

    def test_fits(self):
        assert spot_it.grid_fits(10, 6, 1600, 900)
        assert not spot_it.grid_fits(10, 7, 1600, 900)
        assert not spot_it.grid_fits(10, 6, 900, 1600)  # the landscape grid on portrait
        assert spot_it.grid_fits(6, 6, 900, 1600) and spot_it.grid_fits(6, 6, 1600, 900)


class TestDefaultGrid:
    @pytest.mark.parametrize(
        ("width", "height", "expected"),
        [
            (1600, 1200, (6, 5)),  # 4.5 rows rounds half up
            (1600, 900, (6, 3)),
            (900, 1600, (6, 10)),
            (500, 1500, (3, 9)),
            (1000, 1000, (6, 6)),
            (1200, 1600, (6, 8)),
            (1500, 500, (6, 2)),
        ],
    )
    def test_half_up(self, width, height, expected):
        assert spot_it.default_grid(width, height) == expected

    @pytest.mark.parametrize(("width", "height"), [(1200, 200), (200, 1200), (1101, 200)])
    def test_none_when_nothing_fits(self, width, height):
        assert spot_it.default_grid(width, height) is None


# ---------------------------------------------------------------------------
# Review status (spec §2.5): each reason, the allowed combinations, the fixed order
# ---------------------------------------------------------------------------


def stamp(image_id=7, version=1, cols=6, rows=4) -> dict:
    return {"confirmed_for": spot_it.stamp_for(image_id, version, cols, rows)}


class TestReviewStatus:
    def test_confirmed(self):
        assert spot_it.review_status(config(), {**CELLS, **stamp()}, LANDSCAPE) == []

    @pytest.mark.parametrize(
        ("cfg", "answer_data", "image", "expected"),
        [
            # each reason alone
            (config(), stamp(), None, [IMAGE_MISSING]),
            (config(10, 6), stamp(version=2, cols=10, rows=6), PORTRAIT, [GRID_TOO_DENSE]),
            (config(), stamp(image_id=8), LANDSCAPE, [IMAGE_CHANGED]),
            (config(), stamp(version=2), LANDSCAPE, [IMAGE_REPLACED]),
            (config(), stamp(cols=5), LANDSCAPE, [GRID_CHANGED]),
            (config(), {}, LANDSCAPE, [NOT_CONFIRMED]),
            # every allowed combination, always in the fixed order
            (config(10, 6), stamp(image_id=8, cols=10, rows=6), PORTRAIT,
             [GRID_TOO_DENSE, IMAGE_CHANGED]),
            (config(10, 6), stamp(cols=10, rows=6), PORTRAIT,
             [GRID_TOO_DENSE, IMAGE_REPLACED]),
            (config(10, 7), stamp(version=2, cols=10, rows=6), PORTRAIT,
             [GRID_TOO_DENSE, GRID_CHANGED]),
            (config(), stamp(image_id=8, rows=3), LANDSCAPE, [IMAGE_CHANGED, GRID_CHANGED]),
            (config(), stamp(version=5, rows=3), LANDSCAPE, [IMAGE_REPLACED, GRID_CHANGED]),
            (config(10, 7), stamp(image_id=8, cols=10, rows=6), PORTRAIT,
             [GRID_TOO_DENSE, IMAGE_CHANGED, GRID_CHANGED]),
            (config(10, 7), stamp(cols=10, rows=6), PORTRAIT,
             [GRID_TOO_DENSE, IMAGE_REPLACED, GRID_CHANGED]),
            (config(10, 6), {}, PORTRAIT, [GRID_TOO_DENSE, NOT_CONFIRMED]),
        ],
    )  # fmt: skip
    def test_reasons(self, cfg, answer_data, image, expected):
        assert spot_it.review_status(cfg, answer_data, image) == expected

    def test_image_missing_is_always_alone(self):
        for answer_data in ({}, stamp(image_id=8, cols=10, rows=10)):
            assert spot_it.review_status(config(10, 10), answer_data, None) == [
                IMAGE_MISSING
            ]

    @pytest.mark.parametrize(
        ("cfg", "answer_data"),
        [
            ("junk", stamp()),
            (config(), {"confirmed_for": {"image_id": 7}}),
            (config(), {"confirmed_for": None}),
            (config(), None),
            (config(), []),
            ({"image_id": 7, "grid": {"cols": 99, "rows": 4}}, stamp()),
            ({"image_id": 7}, stamp()),
        ],
    )
    def test_malformed_stored_data_reads_as_not_confirmed(self, cfg, answer_data):
        assert spot_it.review_status(cfg, answer_data, LANDSCAPE) == [NOT_CONFIRMED]

    def test_texts(self):
        cfg = config(10, 6)
        reasons = [
            IMAGE_MISSING,
            GRID_TOO_DENSE,
            IMAGE_CHANGED,
            IMAGE_REPLACED,
            GRID_CHANGED,
            NOT_CONFIRMED,
        ]
        assert spot_it.reason_texts(reasons, cfg, PORTRAIT) == [
            "its image is missing",
            "the 10×6 grid is too dense for its image (at most 6×10)",
            "a different image was chosen after the cells were checked",
            "its image was replaced after the cells were checked",
            "its grid changed after the cells were checked",
            "its accepted cells haven't been checked",
        ]

    def test_too_wide_and_too_tall_texts(self):
        wide = ImageInfo(7, 1, 1200, 200)
        tall = ImageInfo(7, 1, 200, 1200)
        assert spot_it.reason_texts([GRID_TOO_DENSE], config(2, 2), wide) == [
            "its image is too wide for any grid (crop it to at most 11:2)"
        ]
        assert spot_it.reason_texts([GRID_TOO_DENSE], config(2, 2), tall) == [
            "its image is too tall for any grid (crop it to at most 14:83)"
        ]


# ---------------------------------------------------------------------------
# Answers, scoring, reveal, labels
# ---------------------------------------------------------------------------


class TestAnswers:
    @pytest.mark.parametrize(
        "answer",
        [
            None,
            [1, 2],
            {},
            {"col": 1},
            {"col": 1, "row": 2, "extra": 0},
            {"col": True, "row": 2},
            {"col": 1, "row": 2.0},
            {"col": "1", "row": 2},
            {"col": 6, "row": 0},
            {"col": 0, "row": 4},
            {"col": -1, "row": 0},
        ],
    )
    def test_malformed(self, answer):
        assert spot_it.parse_answer(config(), answer) is None

    def test_valid(self):
        assert spot_it.parse_answer(config(), cell(5, 3)) == (5, 3)

    def test_unusable_grid(self):
        assert spot_it.parse_answer({"grid": None}, cell(0, 0)) is None

    @pytest.mark.parametrize("grading", ["ACCURACY", "COMPLETENESS"])
    def test_malformed_scores_nothing(self, grading):
        assert spot_it.score(grading, 1000.0, config(), CELLS, {"col": True, "row": 0}) == (
            0.0,
            False,
        )

    def test_accuracy_all_or_nothing(self):
        assert spot_it.score("ACCURACY", 1000.0, config(), CELLS, cell(4, 2)) == (1000.0, True)
        assert spot_it.score("ACCURACY", 1000.0, config(), CELLS, cell(3, 2)) == (0.0, False)

    def test_completeness(self):
        assert spot_it.score("COMPLETENESS", 700.0, config(), {}, cell(0, 0)) == (700.0, True)

    def test_cell_order_never_matters(self):
        forward = {"accepted_cells": [cell(1, 0), cell(4, 1), cell(0, 2)]}
        backward = {"accepted_cells": [cell(0, 2), cell(4, 1), cell(1, 0)]}
        for answer in (cell(4, 1), cell(0, 2), cell(1, 0), cell(3, 3)):
            assert spot_it.score("ACCURACY", 10.0, config(), forward, answer) == spot_it.score(
                "ACCURACY", 10.0, config(), backward, answer
            )
        expected = {
            "type": "find_in_image",
            "acceptedCells": [cell(1, 0), cell(4, 1), cell(0, 2)],  # row-major
        }
        assert spot_it.reveal("ACCURACY", config(), forward) == expected
        assert spot_it.reveal("ACCURACY", config(), backward) == expected
        assert spot_it.reveal("COMPLETENESS", config(), forward) == {"type": "completeness"}

    def test_reveal_of_malformed_data_never_raises(self):
        assert spot_it.reveal("ACCURACY", {"grid": "x"}, CELLS)["acceptedCells"] == []
        assert spot_it.reveal("ACCURACY", config(), None)["acceptedCells"] == []

    def test_dist_key_and_labels(self):
        assert spot_it.dist_key((3, 2)) == "3,2"
        assert spot_it.cell_label(0, 0) == "A1"
        assert spot_it.cell_label(1, 2) == "B3"
        assert spot_it.cell_label(9, 9) == "J10"


# ---------------------------------------------------------------------------
# Export (spec §2.9)
# ---------------------------------------------------------------------------


class TestExportFields:
    CONFIG = {**config(), "width": 1600, "height": 900}

    def test_confirmed(self):
        answer_data = {"confirmed_for": stamp()["confirmed_for"], **CELLS}
        exported_config, exported_answer = spot_it.export_fields(
            self.CONFIG, answer_data, LANDSCAPE
        )
        assert exported_config == config()
        assert "width" not in exported_config and "height" not in exported_config
        # Fixed key order: accepted_cells, then confirmed — never the stamp.
        assert json.dumps(exported_answer) == json.dumps(
            {"accepted_cells": CELLS["accepted_cells"], "confirmed": True}
        )

    @pytest.mark.parametrize(
        ("answer_data", "image"),
        [
            ({**CELLS}, LANDSCAPE),  # never confirmed
            ({**CELLS, **stamp()}, ImageInfo(7, 2, 1600, 900)),  # replaced
            ({**CELLS, **stamp()}, None),  # image missing
        ],
    )
    def test_unconfirmed_has_no_flag(self, answer_data, image):
        _, exported_answer = spot_it.export_fields(self.CONFIG, answer_data, image)
        assert exported_answer == {"accepted_cells": CELLS["accepted_cells"]}

    def test_too_dense_is_never_exported_as_confirmed(self):
        cfg = {**config(10, 6), "width": 1600, "height": 900}
        answer_data = {**CELLS, **stamp(version=2, cols=10, rows=6)}
        exported_config, exported_answer = spot_it.export_fields(cfg, answer_data, PORTRAIT)
        assert exported_config == config(10, 6)
        assert "confirmed" not in exported_answer

    def test_order_and_presence_are_kept(self):
        backward = [cell(4, 2), cell(0, 0), cell(4, 1)]
        _, exported = spot_it.export_fields(
            self.CONFIG, {"accepted_cells": backward}, LANDSCAPE
        )
        assert exported["accepted_cells"] == backward
        _, absent = spot_it.export_fields(self.CONFIG, {**stamp()}, LANDSCAPE)
        assert absent == {"confirmed": True}  # COMPLETENESS without cells, confirmed
        _, empty = spot_it.export_fields(self.CONFIG, {"accepted_cells": []}, LANDSCAPE)
        assert empty == {"accepted_cells": []}

    def test_malformed_stored_data_never_raises(self):
        assert spot_it.export_fields("junk", {"x": 1}, LANDSCAPE) == ("junk", {"x": 1})
        assert spot_it.export_fields(self.CONFIG, None, LANDSCAPE) == (config(), {})
