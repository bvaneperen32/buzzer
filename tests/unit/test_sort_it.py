"""
Unit tests for Sort It (`sort_into_boxes`, T7): app/services/sort_it.py.

Pure functions only — no database, Redis or running stack. The spec is
docs/plans/t7-question-types.md (§2.2 text rules, §2.3 data, scoring and results).

Run with:
    cd tests/unit && PYTHONPATH=../../backend pytest test_sort_it.py -v
"""

from __future__ import annotations

import random
from math import comb

import pytest
from pydantic import ValidationError

from app.schemas.admin import QuestionCreate
from app.services import sort_it

BOXES = ["Mammal", "Reptile", "Amphibian"]
CARDS = ["Whale", "Gecko", "Frog", "Bat"]
KEY = [0, 1, 2, 0]


def config(boxes=None, cards=None, **extra) -> dict:
    return {
        "boxes": list(BOXES if boxes is None else boxes),
        "cards": list(CARDS if cards is None else cards),
        **extra,
    }


def refused(cfg, answer_data=None, grading="ACCURACY", points=1000.0) -> str:
    answer_data = {"correct_boxes": KEY} if answer_data is None else answer_data
    with pytest.raises(ValueError) as caught:
        sort_it.validate(cfg, answer_data, grading, points)
    return str(caught.value)


def accepted(cfg, answer_data=None, grading="ACCURACY", points=1000.0) -> None:
    answer_data = {"correct_boxes": KEY} if answer_data is None else answer_data
    sort_it.validate(cfg, answer_data, grading, points)


def body(**overrides) -> dict:
    question = {
        "type": "sort_into_boxes",
        "grading_type": "ACCURACY",
        "prompt": "Sort by class",
        "config": config(),
        "answer_data": {"correct_boxes": KEY},
        "points_value": 1000,
    }
    question.update(overrides)
    return question


# ---------------------------------------------------------------------------
# Data rules (spec §2.3 "Data")
# ---------------------------------------------------------------------------


class TestShape:
    def test_valid_question(self):
        accepted(config())

    @pytest.mark.parametrize("count", [1, 5])
    def test_box_count(self, count):
        boxes = [f"Box {i}" for i in range(count)]
        answer = {"correct_boxes": [0, 0, 0, 0]}
        assert "2–4 box labels" in refused(config(boxes=boxes), answer)

    @pytest.mark.parametrize("count", [3, 11])
    def test_card_count(self, count):
        cards = [f"Card {i}" for i in range(count)]
        answer = {"correct_boxes": [0] * count}
        assert "4–10 card labels" in refused(config(cards=cards), answer)

    def test_limits_are_inclusive(self):
        accepted(config(boxes=["A", "B"]), {"correct_boxes": [0, 1, 0, 1]})
        cards = [f"Card {i}" for i in range(10)]
        boxes = ["A", "B", "C", "D"]
        accepted(config(boxes=boxes, cards=cards), {"correct_boxes": [3] * 10})

    @pytest.mark.parametrize("value", [None, "Mammal", {"0": "Mammal"}])
    def test_labels_must_be_a_list(self, value):
        cfg = {"boxes": value, "cards": CARDS}
        assert "config.boxes must be a list" in refused(cfg)
        assert "config.boxes must be a list" in refused({"cards": CARDS})

    @pytest.mark.parametrize("label", [7, None, True, ["Mammal"]])
    def test_labels_must_be_text(self, label):
        assert "Box 2 must be text" in refused(config(boxes=["A", label, "C"]))

    def test_unknown_keys(self):
        assert "Unknown Sort It config key(s): image_id" in refused(config(image_id=3))
        assert "Unknown Sort It answer_data key(s): extra" in refused(
            config(), {"correct_boxes": KEY, "extra": 1}
        )

    def test_prompt_image_is_allowed(self):
        accepted(config(prompt_image_id=5))

    @pytest.mark.parametrize("value", [[], "x", None])
    def test_config_and_answer_data_must_be_objects(self, value):
        assert "config must be an object" in refused(value)
        with pytest.raises(ValueError, match="answer_data must be an object"):
            sort_it.validate(config(), value, "ACCURACY", 1000.0)


class TestTextRules:
    """Spec §2.2: code points, `isspace` trimming, uniqueness by `casefold()`."""

    @pytest.mark.parametrize("label", ["", "   ", "\t\n"])
    def test_empty(self, label):
        assert "Box 1 is empty" in refused(config(boxes=[label, "B"]), {"correct_boxes": [0] * 4})

    @pytest.mark.parametrize("label", [" Frog", "Frog ", "\u0085Frog", "Frog　"])
    def test_untrimmed_is_rejected_not_normalized(self, label):
        cards = ["Whale", "Gecko", label, "Bat"]
        assert "Card 3 has leading or trailing spaces" in refused(config(cards=cards))

    def test_leading_bom_is_kept(self):
        # U+FEFF isn't `isspace`, so the server accepts it; the editor preserves it unless the
        # label is edited (§2.3 editor label rule).
        cards = ["Whale", "Gecko", "﻿Frog", "Bat"]
        accepted(config(cards=cards))

    def test_lengths_count_code_points(self):
        accepted(config(boxes=["😀" * 30, "B", "C"]))
        assert "Box 1 is longer than 30 characters" in refused(
            config(boxes=["😀" * 31, "B", "C"])
        )
        accepted(config(cards=["x" * 60, "Gecko", "Frog", "Bat"]))
        assert "Card 1 is longer than 60 characters" in refused(
            config(cards=["x" * 61, "Gecko", "Frog", "Bat"])
        )

    def test_duplicates_by_casefold(self):
        assert "Boxes 1 and 3 are the same (ignoring case)" in refused(
            config(boxes=["Straße", "Reptile", "STRASSE"])
        )
        assert "Cards 2 and 4 are the same (ignoring case)" in refused(
            config(cards=["Whale", "frog", "Gecko", "FROG"])
        )

    def test_no_unicode_normalization(self):
        # A precomposed "é" and "e" + U+0301 are different strings.
        accepted(config(boxes=["café", "café", "C"]))


class TestAnswerKey:
    def test_required_under_accuracy(self):
        assert "need answer_data.correct_boxes" in refused(config(), {})

    @pytest.mark.parametrize(
        "key",
        [
            [0, 1, 2],  # too short
            [0, 1, 2, 0, 1],  # too long
            [0, 1, 3, 0],  # out of range (k = 3)
            [0, -1, 2, 0],
            [0, True, 2, 0],  # a bool isn't a box index
            [0, 1.0, 2, 0],
            [0, "1", 2, 0],
            [0, None, 2, 0],
            "0120",
        ],
    )
    def test_malformed_key(self, key):
        refused(config(), {"correct_boxes": key})

    def test_a_box_may_be_empty(self):
        accepted(config(), {"correct_boxes": [0, 0, 0, 0]})

    def test_accuracy_needs_positive_points(self):
        assert "points value above 0" in refused(config(), points=0)

    def test_completeness_key_is_optional_but_never_malformed(self):
        accepted(config(), {}, grading="COMPLETENESS", points=0)
        accepted(config(), {"correct_boxes": KEY}, grading="COMPLETENESS")
        refused(config(), {"correct_boxes": [0, 1]}, grading="COMPLETENESS")


class TestSchema:
    def test_create_accepts_the_type(self):
        QuestionCreate(**body())

    def test_create_refuses_a_canvas_image(self):
        with pytest.raises(ValidationError) as caught:
            QuestionCreate(**body(config=config(image_id=4)))
        assert "Unknown Sort It config key(s): image_id" in str(caught.value)

    def test_create_reports_the_rule(self):
        with pytest.raises(ValidationError) as caught:
            QuestionCreate(**body(config=config(boxes=["A", "a"])))
        assert "Boxes 1 and 2 are the same (ignoring case)" in str(caught.value)


# ---------------------------------------------------------------------------
# Answers and scoring (spec §2.3 "Scoring")
# ---------------------------------------------------------------------------

EIGHT = [f"Card {i}" for i in range(8)]


def cfg8(k: int) -> dict:
    return config(boxes=[f"Box {b}" for b in range(k)], cards=EIGHT)


class TestParseAnswer:
    @pytest.mark.parametrize(
        "answer",
        [
            None,
            [0, 1, 2, 0],
            "placements",
            {},
            {"placements": [0, 1, 2, 0], "extra": 1},
            {"Placements": [0, 1, 2, 0]},
            {"placements": [0, 1, 2]},  # wrong length
            {"placements": [0, 1, 2, 0, 1]},
            {"placements": [0, True, 2, 0]},  # bool
            {"placements": [0, 1.0, 2, 0]},  # float
            {"placements": [0, 3, 2, 0]},  # out of range
            {"placements": [0, -1, 2, 0]},
            {"placements": [None, None, None, None]},  # nothing placed
            {"placements": "0120"},
        ],
    )
    def test_malformed(self, answer):
        assert sort_it.parse_answer(config(), answer) is None

    def test_valid_with_cards_left_out(self):
        answer = {"placements": [None, 1, None, 0]}
        assert sort_it.parse_answer(config(), answer) == [None, 1, None, 0]

    @pytest.mark.parametrize("cfg", [None, [], {"boxes": ["A"], "cards": ["x"]}, {}])
    def test_unusable_config(self, cfg):
        assert sort_it.parse_answer(cfg, {"placements": [0]}) is None


def placements(right: int, wrong: int, left_out: int, k: int) -> tuple[list, list]:
    """(placements, key) for 8 cards whose key is all box 0."""
    key = [0] * (right + wrong + left_out)
    answer = [0] * right + [1 % k] * wrong + [None] * left_out
    return answer, key


class TestScore:
    @pytest.mark.parametrize(
        ("k", "expected"), [(2, 625.0), (3, 687.5), (4, 708.3333333333334)]
    )
    def test_partial_example(self, k, expected):
        # 8 cards: 6 right, 1 wrong, 1 left out (spec §2.3, §4.5).
        answer, key = placements(6, 1, 1, k)
        points, is_correct = sort_it.score(
            "ACCURACY", 1000.0, cfg8(k), {"correct_boxes": key}, {"placements": answer}
        )
        assert points == pytest.approx(expected)
        assert is_correct is False

    @pytest.mark.parametrize(
        ("right", "wrong", "left_out", "k", "credit"),
        [
            (8, 0, 0, 3, 1.0),
            (6, 2, 0, 3, 0.625),
            (6, 0, 2, 3, 0.75),
            (4, 4, 0, 3, 0.25),
            (2, 6, 0, 3, 0.0),  # the floor
            (0, 8, 0, 3, 0.0),
            (6, 2, 0, 2, 0.5),
            (1, 0, 7, 4, 0.125),
        ],
    )
    def test_spec_examples(self, right, wrong, left_out, k, credit):
        answer, key = placements(right, wrong, left_out, k)
        assert sort_it.credit(answer, key, k) == pytest.approx(credit)
        points, is_correct = sort_it.score(
            "ACCURACY", 1000.0, cfg8(k), {"correct_boxes": key}, {"placements": answer}
        )
        assert points == pytest.approx(1000.0 * credit)
        assert is_correct is (right == 8)

    def test_full_marks_are_exactly_the_points_value(self):
        answer = {"placements": KEY}
        for points_value in (1000.0, 708.333, 0.1, 1e-40):
            points, is_correct = sort_it.score(
                "ACCURACY", points_value, config(), {"correct_boxes": KEY}, answer
            )
            assert points == points_value
            assert is_correct is True

    def test_all_right_but_one_left_out_is_not_correct(self):
        points, is_correct = sort_it.score(
            "ACCURACY",
            1000.0,
            config(),
            {"correct_boxes": KEY},
            {"placements": [0, 1, 2, None]},
        )
        assert points == 750.0
        assert is_correct is False

    @pytest.mark.parametrize("grading", ["ACCURACY", "COMPLETENESS"])
    def test_malformed_answers_score_nothing(self, grading):
        for answer in ({"placements": [None] * 4}, {"placements": [0]}, [0, 1, 2, 0]):
            assert sort_it.score(
                grading, 1000.0, config(), {"correct_boxes": KEY}, answer
            ) == (0.0, False)

    def test_completeness_gives_full_points_for_any_valid_answer(self):
        assert sort_it.score(
            "COMPLETENESS", 500.0, config(), {}, {"placements": [None, 2, None, None]}
        ) == (500.0, True)

    def test_accuracy_with_an_unusable_stored_key_scores_nothing(self):
        answer = {"placements": KEY}
        for answer_data in ({}, {"correct_boxes": [0, 1]}, None, []):
            assert sort_it.score("ACCURACY", 1000.0, config(), answer_data, answer) == (
                0.0,
                False,
            )


class TestRandomGuessing:
    """Spec §2.3's table: the exact expected credit when every card is placed at random."""

    TABLE = {
        (4, 2): 18.8, (4, 3): 14.8, (4, 4): 10.5,
        (6, 2): 15.6, (6, 3): 11.0, (6, 4): 9.9,
        (8, 2): 13.7, (8, 3): 10.2, (8, 4): 7.8,
        (10, 2): 12.3, (10, 3): 9.1, (10, 4): 7.5,
    }  # fmt: skip

    @pytest.mark.parametrize(("n", "k"), sorted(TABLE))
    def test_expected_credit(self, n, k):
        key = [0] * n
        expected = 0.0
        for right in range(n + 1):
            answer = [0] * right + [1] * (n - right)
            probability = comb(n, right) * (1 / k) ** right * ((k - 1) / k) ** (n - right)
            expected += probability * sort_it.credit(answer, key, k)
        assert round(expected * 100, 1) == self.TABLE[(n, k)]
        assert expected > 0  # the floor: guessing doesn't average zero


# ---------------------------------------------------------------------------
# Answer counts, reveal, card order
# ---------------------------------------------------------------------------


class TestDistAndReveal:
    def test_dist_keys(self):
        assert sort_it.dist_keys([0, None, 2, 0]) == ["0:0", "1:none", "2:2", "3:0"]

    def test_reveal(self):
        assert sort_it.reveal("ACCURACY", config(), {"correct_boxes": KEY}) == {
            "type": "sort_into_boxes",
            "correctBoxes": KEY,
        }
        assert sort_it.reveal("COMPLETENESS", config(), {"correct_boxes": KEY}) == {
            "type": "completeness"
        }

    @pytest.mark.parametrize(
        ("cfg", "answer_data"),
        [(config(), {}), (config(), {"correct_boxes": [9, 9, 9, 9]}), (None, None)],
    )
    def test_reveal_of_malformed_data_never_raises(self, cfg, answer_data):
        assert sort_it.reveal("ACCURACY", cfg, answer_data) == {
            "type": "sort_into_boxes",
            "correctBoxes": [],
        }


class TestCardOrder:
    @pytest.mark.parametrize("n", range(4, 11))
    def test_a_deterministic_permutation_never_the_authors_order(self, n):
        for question_id in range(1, 300):
            order = sort_it.card_order(question_id, n)
            assert order == sort_it.card_order(question_id, n)
            assert sorted(order) == list(range(n))
            assert order != list(range(n))

    def test_the_identity_shuffle_is_rotated(self):
        def shuffled(seed: int, n: int) -> list[int]:
            order = list(range(n))
            random.Random(seed).shuffle(order)
            return order

        question_id = next(
            qid for qid in range(1, 10_000) if shuffled(qid, 4) == [0, 1, 2, 3]
        )
        assert sort_it.card_order(question_id, 4) == [1, 2, 3, 0]


# ---------------------------------------------------------------------------
# Results percentages (spec §2.3) — also the expected data of the §4.6 cross-check
# ---------------------------------------------------------------------------

# Eight answers to BOXES × CARDS with KEY [0, 1, 2, 0], designed for the cross-check:
# Whale — a tie between two wrong boxes (1 each) and a .5 percentage (5 of 8 → 63 %);
# Gecko — all right; Frog — one right (12.5 % → 13 %); Bat — a tie between two wrong boxes
# (2 each), left out by two answers.
CROSS_CHECK_ANSWERS = [
    [0, 1, 2, 0],
    [0, 1, 0, 0],
    [0, 1, 0, 1],
    [0, 1, 0, 1],
    [0, 1, 0, 2],
    [1, 1, 1, 2],
    [2, 1, 1, None],
    [None, 1, None, None],
]
CROSS_CHECK_ACCURACY = [
    # (card, correct box, % correct, % left out, most common wrong box)
    (0, 0, 63, 13, 1),
    (1, 1, 100, 0, None),
    (2, 2, 13, 13, 0),
    (3, 0, 25, 25, 1),
]
CROSS_CHECK_COMPLETENESS = [
    # (card, most common box, its %)
    (0, 0, 63),
    (1, 1, 100),
    (2, 0, 50),
    (3, 0, 25),
]


def distribution(answers) -> dict[str, int]:
    counts: dict[str, int] = {}
    for answer in answers:
        for key in sort_it.dist_keys(answer):
            counts[key] = counts.get(key, 0) + 1
    return counts


class TestResultsBreakdown:
    def test_percent_is_integer_half_up(self):
        assert sort_it.percent(1, 8) == 13  # 12.5 → 13, never banker's rounding
        assert sort_it.percent(5, 8) == 63  # 62.5 → 63
        assert sort_it.percent(1, 3) == 33
        assert sort_it.percent(2, 3) == 67
        assert sort_it.percent(0, 8) == 0
        assert sort_it.percent(8, 8) == 100
        assert sort_it.percent(0, 0) is None

    def test_cross_check_accuracy(self):
        rows = sort_it.results_breakdown(
            4, 3, distribution(CROSS_CHECK_ANSWERS), len(CROSS_CHECK_ANSWERS), KEY
        )
        assert [
            (
                r["card"],
                r["correct_box"],
                r["pct_correct"],
                r["pct_left_out"],
                r["most_common_wrong_box"],
            )
            for r in rows
        ] == CROSS_CHECK_ACCURACY

    def test_cross_check_completeness(self):
        rows = sort_it.results_breakdown(
            4, 3, distribution(CROSS_CHECK_ANSWERS), len(CROSS_CHECK_ANSWERS), None
        )
        assert [
            (r["card"], r["most_common_box"], r["pct_most_common"]) for r in rows
        ] == CROSS_CHECK_COMPLETENESS

    def test_the_denominator_is_the_answered_count(self):
        # 2 of 5 answers, not 2 of however many players there were.
        rows = sort_it.results_breakdown(4, 3, {"0:0": 2, "0:none": 3}, 5, KEY)
        assert rows[0]["pct_correct"] == 40
        assert rows[0]["pct_left_out"] == 60

    def test_left_out_and_the_correct_box_are_never_the_wrong_box(self):
        rows = sort_it.results_breakdown(4, 3, {"0:0": 5, "0:none": 3}, 8, KEY)
        assert rows[0]["most_common_wrong_box"] is None

    def test_no_answers_shows_dashes(self):
        for row in sort_it.results_breakdown(4, 3, {}, 0, KEY):
            assert row["pct_correct"] is None
            assert row["pct_left_out"] is None
            assert row["most_common_wrong_box"] is None
        for row in sort_it.results_breakdown(4, 3, {}, 0, None):
            assert row["most_common_box"] is None
            assert row["pct_most_common"] is None

    def test_completeness_card_nobody_placed(self):
        rows = sort_it.results_breakdown(4, 3, {"0:none": 4, "1:2": 4}, 4, None)
        assert (rows[0]["most_common_box"], rows[0]["pct_most_common"]) == (None, None)
        assert (rows[1]["most_common_box"], rows[1]["pct_most_common"]) == (2, 100)
