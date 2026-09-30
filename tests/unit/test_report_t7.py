"""
Unit tests for the report's T7 renderers (docs/plans/t7-question-types.md §2.8): the Sort It
table, the Spot It heatmap, and escaping of every author-controlled string they add.

The renderers are pure; the section builders are exercised with unsaved ORM objects, so no
database, Redis or running stack is needed.

Escaping is checked the way a browser would see it: the escaped text is present exactly, and
parsing the output finds no injected elements and no `on…` attributes. The absence of
substrings such as `onerror=` is deliberately not asserted — properly escaped label text
legitimately contains them.

Run with:
    cd tests/unit && PYTHONPATH=../../backend pytest test_report_t7.py -v
"""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser

import pytest

from app.models.game import Question
from app.models.image import Image
from app.models.session import SessionScore
from app.services import report_service as rs
from app.services import sort_it

from .test_sort_it import (
    CROSS_CHECK_ACCURACY,
    CROSS_CHECK_ANSWERS,
    CROSS_CHECK_COMPLETENESS,
)

SCRIPT_CARD = "<script>alert(1)</script>"
IMG_BOX = '"><img src=x onerror=alert(1)>'
DESCRIPTION = 'The "harbour" at <dusk> & dawn'

SCRIPT_CARD_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"
IMG_BOX_ESCAPED = "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"
DESCRIPTION_ESCAPED = "The &quot;harbour&quot; at &lt;dusk&gt; &amp; dawn"


class Parsed(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.chunks: list[str] = []
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.chunks.append(data)

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    def names(self) -> list[str]:
        return [tag for tag, _ in self.tags]


def assert_nothing_injected(markup: str) -> Parsed:
    parsed = Parsed(markup)
    assert "script" not in parsed.names()
    assert "img" not in parsed.names()
    for tag, attrs in parsed.tags:
        assert not [name for name in attrs if name.startswith("on")], (tag, attrs)
    return parsed


BOXES = [IMG_BOX, "Reptile", "Amphibian"]
CARDS = [SCRIPT_CARD, "Gecko", "Frog", "Bat"]
KEY = [0, 1, 2, 0]


def breakdown(key):
    counts: dict[str, int] = {}
    for answer in CROSS_CHECK_ANSWERS:
        for k in sort_it.dist_keys(answer):
            counts[k] = counts.get(k, 0) + 1
    return sort_it.results_breakdown(4, 3, counts, len(CROSS_CHECK_ANSWERS), key)


# ---------------------------------------------------------------------------
# Escaping (§2.8)
# ---------------------------------------------------------------------------


class TestEscaping:
    def test_the_inputs_are_valid_labels(self):
        assert len(IMG_BOX) == 30  # exactly the box limit, in code points
        sort_it.validate(
            {"boxes": BOXES, "cards": CARDS}, {"correct_boxes": KEY}, "ACCURACY", 1.0
        )

    @pytest.mark.parametrize("accuracy", [True, False])
    def test_sort_table(self, accuracy):
        markup = rs._render_sort_table(
            BOXES, CARDS, breakdown(KEY if accuracy else None), 8, accuracy=accuracy
        )
        assert SCRIPT_CARD_ESCAPED in markup
        assert IMG_BOX_ESCAPED in markup
        parsed = assert_nothing_injected(markup)
        assert SCRIPT_CARD in parsed.text  # the author's text survives exactly
        assert IMG_BOX in parsed.text

    def test_spot_svg_description(self):
        markup = rs._render_spot_svg(
            6,
            4,
            1600,
            900,
            {(4, 1): 3},
            [(4, 1)],
            image_href="data:image/webp;base64,UklGRg==",
            placeholder=None,
            description=DESCRIPTION,
            answered=3,
            total_players=4,
            replaced=False,
        )
        assert f"<title>Answer heatmap: {DESCRIPTION_ESCAPED}</title>" in markup
        assert f'aria-label="Answer heatmap: {DESCRIPTION_ESCAPED}"' in markup
        parsed = assert_nothing_injected(markup)
        [svg] = [attrs for tag, attrs in parsed.tags if tag == "svg"]
        assert svg["aria-label"] == f"Answer heatmap: {DESCRIPTION}"
        assert f"Answer heatmap: {DESCRIPTION}" in parsed.text

    def test_spot_svg_placeholder_is_escaped_text(self):
        markup = rs._render_spot_svg(
            2,
            2,
            100,
            100,
            {},
            [],
            image_href=None,
            placeholder='<b onclick="x()">no</b>',
            description="",
            answered=0,
            total_players=0,
            replaced=False,
        )
        parsed = assert_nothing_injected(markup)
        assert "b" not in parsed.names()
        assert '<b onclick="x()">no</b>' in parsed.text.replace("\n", " ")


# ---------------------------------------------------------------------------
# The Sort It table (§2.3 percentages, §2.8 layout)
# ---------------------------------------------------------------------------


def table_rows(markup: str) -> list[list[str]]:
    """The body rows' cell texts."""

    class Rows(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows: list[list[str]] = []
            self.in_body = self.in_cell = False

        def handle_starttag(self, tag, attrs):
            if tag == "tbody":
                self.in_body = True
            elif tag == "tr" and self.in_body:
                self.rows.append([])
            elif tag == "td" and self.in_body:
                self.in_cell = True
                self.rows[-1].append("")

        def handle_endtag(self, tag):
            if tag == "td":
                self.in_cell = False

        def handle_data(self, data):
            if self.in_cell:
                self.rows[-1][-1] += data

    parser = Rows()
    parser.feed(markup)
    return parser.rows


def pct(value):
    return "—" if value is None else f"{value}%"


def label(labels, index):
    return "—" if index is None else labels[index]


class TestSortTable:
    def test_accuracy_matches_the_cross_check(self):
        markup = rs._render_sort_table(BOXES, CARDS, breakdown(KEY), 8, accuracy=True)
        assert table_rows(markup) == [
            [CARDS[card], BOXES[box], pct(correct), label(BOXES, wrong), pct(left)]
            for card, box, correct, left, wrong in CROSS_CHECK_ACCURACY
        ]

    def test_completeness_matches_the_cross_check(self):
        markup = rs._render_sort_table(BOXES, CARDS, breakdown(None), 8, accuracy=False)
        assert table_rows(markup) == [
            [CARDS[card], label(BOXES, box), pct(value)]
            for card, box, value in CROSS_CHECK_COMPLETENESS
        ]

    def test_headers(self):
        accuracy = Parsed(rs._render_sort_table(BOXES, CARDS, breakdown(KEY), 8, accuracy=True))
        assert accuracy.chunks[:5] == [
            "Card",
            "Correct box",
            "% correct",
            "Most common wrong box",
            "% left out",
        ]
        completeness = Parsed(
            rs._render_sort_table(BOXES, CARDS, breakdown(None), 8, accuracy=False)
        )
        assert completeness.chunks[:3] == ["Card", "Most common box", "%"]

    def test_bar_uses_the_exact_fraction(self):
        markup = rs._render_sort_table(BOXES, CARDS, breakdown(KEY), 8, accuracy=True)
        assert 'style="width:62.50%"' in markup  # 5 of 8, while the number shows 63%

    def test_no_answers(self):
        rows = sort_it.results_breakdown(4, 3, {}, 0, KEY)
        markup = rs._render_sort_table(BOXES, CARDS, rows, 0, accuracy=True)
        for row in table_rows(markup):
            assert row[2:] == ["—", "—", "—"]


# ---------------------------------------------------------------------------
# Section builders: stored answers, stale answers, images
# ---------------------------------------------------------------------------


def sort_question(grading="ACCURACY") -> Question:
    return Question(
        id=11,
        type="sort_into_boxes",
        grading_type=grading,
        prompt="Sort",
        config={"boxes": BOXES, "cards": CARDS},
        answer_data={"correct_boxes": KEY},
        points_value=1000.0,
    )


def scores(answers) -> list[SessionScore]:
    return [SessionScore(answer_data=answer) for answer in answers]


class TestSortBlock:
    def test_table_from_stored_answers(self):
        q = sort_question()
        stored = scores([{"placements": a} for a in CROSS_CHECK_ANSWERS])
        markup = rs._sort_it_block(q, stored, rs._answer_reveal(q))
        assert table_rows(markup)[0] == [SCRIPT_CARD, IMG_BOX, "63%", "Reptile", "13%"]
        assert "t7-note" not in markup

    def test_stale_answers_are_counted_not_shown(self):
        q = sort_question()
        stored = scores(
            [{"placements": a} for a in CROSS_CHECK_ANSWERS]
            + [{"placements": [0, 1]}, {"selectedIndex": 1}, None]
        )
        markup = rs._sort_it_block(q, stored, rs._answer_reveal(q))
        # The denominators are the 8 valid answers, not the 11 stored.
        assert table_rows(markup)[0][2] == "63%"
        assert (
            "3 answers were given to an earlier version of this question and aren't shown."
            in Parsed(markup).text
        )

    def test_completeness_layout(self):
        q = sort_question("COMPLETENESS")
        stored = scores([{"placements": a} for a in CROSS_CHECK_ANSWERS])
        markup = rs._sort_it_block(q, stored, rs._answer_reveal(q))
        # Frog's most common box is box 0 (4 of 8), whose label is IMG_BOX.
        assert table_rows(markup)[2] == ["Frog", IMG_BOX, "50%"]


def spot_question(**config) -> Question:
    return Question(
        id=12,
        type="find_in_image",
        grading_type="ACCURACY",
        prompt="Find",
        config={"image_id": 7, "grid": {"cols": 6, "rows": 4}, **config},
        answer_data={"accepted_cells": [{"col": 4, "row": 1}, {"col": 1, "row": 0}]},
        points_value=1000.0,
    )


def library(include=True, replaced_at=None) -> rs._Images:
    image = Image(
        id=7,
        width=1600,
        height=900,
        version=1,
        description=DESCRIPTION,
        small_data=b"RIFF\x00\x00\x00\x00WEBP",
        replaced_at=replaced_at,
    )
    return rs._Images({7: image}, include, datetime(2026, 9, 29, 12, 0))


class TestSpotBlock:
    ANSWERS = [{"col": 4, "row": 1}] * 3 + [{"col": 0, "row": 0}]

    def render(self, q, images, answers=None):
        stored = scores(self.ANSWERS if answers is None else answers)
        return rs._spot_it_block(q, stored, rs._answer_reveal(q), images, total_players=5)

    def test_image_counts_and_correct(self):
        markup = self.render(spot_question(width=1600, height=900), library())
        parsed = assert_nothing_injected(markup)
        [image] = [attrs for tag, attrs in parsed.tags if tag == "image"]
        assert image["href"].startswith("data:image/webp;base64,")
        assert parsed.text.count("Correct") == 3  # two outlined cells + the legend key
        assert "4 of 5 answered" in parsed.text
        assert "blank = no answers" in parsed.text
        assert DESCRIPTION in parsed.text  # the SVG's <title>
        # Stored proportions: 640 × 360 grid area plus the 24-unit header bands.
        [svg] = [attrs for tag, attrs in parsed.tags if tag == "svg"]
        assert svg["viewbox"] == "0 0 664 384"

    def test_withheld(self):
        markup = self.render(spot_question(width=1600, height=900), library(include=False))
        parsed = assert_nothing_injected(markup)
        assert "image" not in parsed.names()
        assert rs.WITHHELD_TEXT in " ".join(parsed.text.split())
        assert DESCRIPTION not in parsed.text  # the image's description is withheld too
        assert "Correct" in parsed.text  # the heatmap itself still renders

    def test_missing(self):
        markup = self.render(spot_question(image_id=99, width=1600, height=900), library())
        parsed = Parsed(markup)
        assert rs.MISSING_IMAGE_TEXT in parsed.text
        assert "image" not in parsed.names()

    def test_replaced_note(self):
        markup = self.render(
            spot_question(width=1600, height=900),
            library(replaced_at=datetime(2026, 9, 29, 13, 0)),
        )
        assert rs.REPLACED_NOTE in Parsed(markup).text

    def test_unusable_stored_dimensions_fall_back_to_the_image(self):
        markup = self.render(spot_question(width="junk"), library())
        [svg] = [attrs for tag, attrs in Parsed(markup).tags if tag == "svg"]
        assert svg["viewbox"] == "0 0 664 384"

    def test_stale_answers(self):
        markup = self.render(
            spot_question(width=1600, height=900),
            library(),
            answers=[{"col": 4, "row": 1}, {"col": 9, "row": 9}, {"col": True, "row": 0}],
        )
        text = Parsed(markup).text
        assert "1 of 5 answered" in text
        assert (
            "2 answers were given to an earlier version of this question and aren't shown."
            in text
        )


# ---------------------------------------------------------------------------
# Heat ramp (§2.4)
# ---------------------------------------------------------------------------


class TestHeat:
    def test_steps_are_counts_up_to_five(self):
        assert [rs.heat_step(c, 5) for c in range(6)] == [0, 1, 2, 3, 4, 5]
        assert [rs.heat_step(c, 3) for c in range(4)] == [0, 1, 2, 3]

    def test_steps_scale_above_five(self):
        assert [rs.heat_step(c, 12) for c in range(13)] == [
            0, 1, 1, 2, 2, 3, 3, 3, 4, 4, 5, 5, 5,
        ]  # fmt: skip

    @pytest.mark.parametrize("max_count", range(1, 60))
    def test_legend_ranges_partition_the_counts(self, max_count):
        covered = []
        for step, text in rs.heat_legend(max_count):
            lo, _, hi = text.partition("–")
            values = range(int(lo), int(hi or lo) + 1)
            assert all(rs.heat_step(v, max_count) == step for v in values)
            covered.extend(values)
        assert covered == list(range(1, max_count + 1))

    def test_no_answers_no_swatches(self):
        assert rs.heat_legend(0) == []
