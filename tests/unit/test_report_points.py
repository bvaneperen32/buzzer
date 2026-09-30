"""
Unit tests for the report's T7 points label (docs/plans/t7-question-types.md §2.2, §2.8).

The report stays aggregate-only; the one T7 points change is each T7 question's
maximum-points label in its `q-timing` header, produced by `_t7_points_label` through
`format_points`. The regression cases pin `Decimal(repr(x))` (the apps' `Intl.NumberFormat`
rounds the shortest decimal representation half up) against a switch to `Decimal(x)`, which
rounds the binary value and would disagree with the apps (spec §9, D5).

Run with:
    cd tests/unit && PYTHONPATH=../../backend pytest test_report_points.py -v
"""

from __future__ import annotations

import pytest

from app.services import report_service as rs


@pytest.mark.parametrize(
    ("points_value", "label"),
    [
        (687.5, "687.5 pts"),
        (708.333, "708.3 pts"),
        (708.3333333333334, "708.3 pts"),
        (1.25, "1.3 pts"),
        (1000, "1000 pts"),  # not "1E+3": never normalize()
        (1000.0, "1000 pts"),
        (100000, "100000 pts"),
        (1, "1 pt"),
        (0.95, "1 pt"),  # "pt" only when the formatted value is exactly "1"
        (0.1, "0.1 pts"),
        (0, "0 pts"),
        # Regression cases: the shortest decimal representation, rounded half up.
        (0.15, "0.2 pts"),
        (2.55, "2.6 pts"),
        (0.35, "0.4 pts"),
    ],
)
def test_t7_label_as_rendered(points_value, label):
    assert rs._t7_points_label(points_value) == label
    assert rs._points_label("sort_into_boxes", points_value) == label
    assert rs._points_label("find_in_image", points_value) == label


@pytest.mark.parametrize(
    ("question_type", "points_value", "label"),
    [
        ("multiple_choice", 1000.0, "1000 pts"),
        ("true_false", 1.0, "1 pt"),
        ("fill_in_the_blank", 708.333, "708.333 pts"),
        ("multi_select", 2.55, "2.55 pts"),
        ("multiple_choice", 1e6, "1e+06 pts"),
    ],
)
def test_existing_types_keep_their_g_label(question_type, points_value, label):
    assert rs._points_label(question_type, points_value) == label
