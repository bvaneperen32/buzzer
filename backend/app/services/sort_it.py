"""Sort It (`sort_into_boxes`, T7): the question type's pure rules.

Players place cards into 2–4 labelled boxes. Scoring is per card, corrected for guessing and
floored at 0 (docs/plans/t7-question-types.md §2.3):

    right  = cards placed in their correct box
    wrong  = cards placed in another box (a card left out is neither)
    credit = max(0, right − wrong/(k−1)) / n        points = points_value × credit

Pure: no database, no I/O, and no imports from the application, so the schema, the scorer, the
gateway, the summaries, the report and the bundle code can all share one definition.

Nothing here raises on malformed *stored* data: readers get `None` / an empty result and treat
the question as having no usable key. Only `validate` raises (`ValueError`, which Pydantic turns
into a 422) — it runs on every write path.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

TYPE = "sort_into_boxes"

MIN_BOXES, MAX_BOXES = 2, 4
MIN_CARDS, MAX_CARDS = 4, 10
BOX_MAX_LENGTH, CARD_MAX_LENGTH = 30, 60

CONFIG_KEYS = frozenset({"boxes", "cards", "prompt_image_id"})
ANSWER_DATA_KEYS = frozenset({"correct_boxes"})
ANSWER_KEYS = frozenset({"placements"})


def is_int(value: Any) -> bool:
    """An integer, and not a bool (`True` is an int in Python)."""
    return type(value) is int


# ── Validation (every write path: create, merged update, import) ────────────


def check_labels(
    labels: Any, *, noun: str, key: str, min_count: int, max_count: int, max_length: int
) -> None:
    """The server's text rules (spec §2.2): each label a non-empty `str` of at most
    `max_length` code points, equal to its own `strip()` (whitespace is `str.isspace`), and
    unique by `casefold()` with no Unicode normalization."""
    if not isinstance(labels, list) or not min_count <= len(labels) <= max_count:
        raise ValueError(
            f"config.{key} must be a list of {min_count}–{max_count} {noun.lower()} labels"
        )
    seen: dict[str, int] = {}
    for number, label in enumerate(labels, start=1):
        if not isinstance(label, str):
            raise ValueError(f"{noun} {number} must be text")
        if not label.strip():
            raise ValueError(f"{noun} {number} is empty")
        if label != label.strip():
            raise ValueError(f"{noun} {number} has leading or trailing spaces")
        if len(label) > max_length:
            raise ValueError(f"{noun} {number} is longer than {max_length} characters")
        folded = label.casefold()
        if folded in seen:
            # `key` is the plural ("boxes", "cards"): "Boxes 1 and 3 are the same …".
            raise ValueError(
                f"{key.capitalize()} {seen[folded]} and {number} are the same "
                "(ignoring case)"
            )
        seen[folded] = number


def check_correct_boxes(value: Any, n: int, k: int) -> None:
    if not isinstance(value, list) or len(value) != n:
        raise ValueError(
            f"answer_data.correct_boxes must list one box index per card ({n})"
        )
    for number, box in enumerate(value, start=1):
        if not is_int(box) or not 0 <= box < k:
            raise ValueError(
                f"Card {number}'s correct box must be a box index from 0 to {k - 1}"
            )


def validate(
    config: Any, answer_data: Any, grading_type: str, points_value: float
) -> None:
    """Raise `ValueError` unless the question is a well-formed Sort It (spec §2.3 "Data")."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        raise ValueError(f"Unknown Sort It config key(s): {', '.join(unknown)}")
    check_labels(
        config.get("boxes"),
        noun="Box",
        key="boxes",
        min_count=MIN_BOXES,
        max_count=MAX_BOXES,
        max_length=BOX_MAX_LENGTH,
    )
    check_labels(
        config.get("cards"),
        noun="Card",
        key="cards",
        min_count=MIN_CARDS,
        max_count=MAX_CARDS,
        max_length=CARD_MAX_LENGTH,
    )
    n, k = len(config["cards"]), len(config["boxes"])

    if not isinstance(answer_data, dict):
        raise ValueError("answer_data must be an object")
    unknown = sorted(set(answer_data) - ANSWER_DATA_KEYS)
    if unknown:
        raise ValueError(f"Unknown Sort It answer_data key(s): {', '.join(unknown)}")
    if grading_type == "ACCURACY":
        if "correct_boxes" not in answer_data:
            raise ValueError(
                "ACCURACY Sort It questions need answer_data.correct_boxes"
            )
        check_correct_boxes(answer_data["correct_boxes"], n, k)
        if not points_value > 0:
            raise ValueError("ACCURACY Sort It questions need a points value above 0")
    elif "correct_boxes" in answer_data:
        # Optional under COMPLETENESS, but never stored malformed.
        check_correct_boxes(answer_data["correct_boxes"], n, k)


# ── Reading stored questions (never raises) ─────────────────────────────────


def shape(config: Any) -> tuple[int, int] | None:
    """(n cards, k boxes) from a stored config, or None when it isn't usable."""
    if not isinstance(config, Mapping):
        return None
    cards, boxes = config.get("cards"), config.get("boxes")
    if not isinstance(cards, list) or not isinstance(boxes, list):
        return None
    if not cards or len(boxes) < 2:
        return None
    return len(cards), len(boxes)


def correct_boxes(answer_data: Any, n: int, k: int) -> list[int] | None:
    """The stored answer key when it is well formed for this shape, else None."""
    if not isinstance(answer_data, Mapping):
        return None
    value = answer_data.get("correct_boxes")
    if not isinstance(value, list) or len(value) != n:
        return None
    if not all(is_int(box) and 0 <= box < k for box in value):
        return None
    return list(value)


# ── Answers (the gateway's check, scoring, answer counts) ───────────────────


def parse_answer(config: Any, answer: Any) -> list[int | None] | None:
    """A player's placements, or None when the answer is malformed for this question.

    Exactly `{"placements": [...]}`: one entry per card, each `null` or a box index (an int,
    not a bool), and at least one card placed (spec §2.3).
    """
    dims = shape(config)
    if dims is None or not isinstance(answer, Mapping) or set(answer) != ANSWER_KEYS:
        return None
    n, k = dims
    placements = answer["placements"]
    if not isinstance(placements, list) or len(placements) != n:
        return None
    for box in placements:
        if box is not None and not (is_int(box) and 0 <= box < k):
            return None
    if all(box is None for box in placements):
        return None
    return list(placements)


def credit(placements: Sequence[int | None], key: Sequence[int], k: int) -> float:
    """max(0, right − wrong/(k−1)) / n — exactly 1.0 only when every card is right."""
    right = sum(1 for box, correct in zip(placements, key) if box == correct)
    wrong = sum(
        1 for box, correct in zip(placements, key) if box is not None and box != correct
    )
    return max(0.0, (right - wrong / (k - 1)) / len(key))


def score(
    grading_type: str, points_value: float, config: Any, answer_data: Any, answer: Any
) -> tuple[float, bool]:
    """(points, is_correct) for one answer. A malformed answer scores (0, False) in either
    grading mode — this runs before the COMPLETENESS shortcut, so a malformed dict can't earn
    completeness points."""
    placements = parse_answer(config, answer)
    if placements is None:
        return 0.0, False
    if grading_type == "COMPLETENESS":
        return points_value, True
    n, k = shape(config)  # parse_answer succeeded, so the shape is usable
    key = correct_boxes(answer_data, n, k)
    if key is None:
        return 0.0, False
    all_right = all(box == correct for box, correct in zip(placements, key))
    return points_value * credit(placements, key, k), all_right


def dist_keys(placements: Sequence[int | None]) -> list[str]:
    """One answer-count key per card: "{card}:{box}", or "{card}:none" when left out."""
    return [
        f"{card}:{'none' if box is None else box}"
        for card, box in enumerate(placements)
    ]


def reveal(grading_type: str, config: Any, answer_data: Any) -> dict:
    """What results reveal. COMPLETENESS keeps the existing `{type: "completeness"}`."""
    if grading_type == "COMPLETENESS":
        return {"type": "completeness"}
    dims = shape(config)
    key = correct_boxes(answer_data, *dims) if dims else None
    return {"type": TYPE, "correctBoxes": key or []}


def card_order(question_id: int, n: int) -> list[int]:
    """The display order of the cards: a permutation seeded by the question id, identical for
    the projector and every phone, and never the author's order (which could leak the
    grouping). Answers always use original indices."""
    order = list(range(n))
    random.Random(question_id).shuffle(order)
    if n > 1 and order == sorted(order):
        order = order[1:] + order[:1]
    return order


# ── Results percentages (the report; the host pages use lib/sortResults.ts) ─


def percent(count: int, answered: int) -> int | None:
    """An integer percentage, rounded half up in integer arithmetic, or None with no answers.

    ⌊(200·count + answered) / (2·answered)⌋ — identical in the TypeScript port; never `round`
    or `Math.round` (1 of 8 = 12.5 % → 13 %).
    """
    if answered <= 0:
        return None
    return (200 * count + answered) // (2 * answered)


def _most_common(counts: Sequence[int], exclude: int | None = None) -> int | None:
    """The box with the highest count (ties to the lowest index), or None when every
    candidate count is 0."""
    best: int | None = None
    for box, count in enumerate(counts):
        if box == exclude or count <= 0:
            continue
        if best is None or count > counts[best]:
            best = box
    return best


def results_breakdown(
    n: int,
    k: int,
    distribution: Mapping[str, int],
    answered: int,
    key: Sequence[int] | None,
) -> list[dict]:
    """Per-card results from the answer counts (spec §2.3 "Results percentages").

    `key` is the answer key for ACCURACY and None for COMPLETENESS. Every percentage uses the
    answered count as its denominator; with no answers, percentages and "most common" values
    are None (shown as "—").
    """
    rows = []
    for card in range(n):
        counts = [int(distribution.get(f"{card}:{box}", 0) or 0) for box in range(k)]
        left_out = int(distribution.get(f"{card}:none", 0) or 0)
        if key is not None:
            correct = key[card]
            wrong_box = _most_common(counts, exclude=correct) if answered else None
            rows.append(
                {
                    "card": card,
                    "correct_box": correct,
                    "correct_count": counts[correct],
                    "pct_correct": percent(counts[correct], answered),
                    "pct_left_out": percent(left_out, answered),
                    "most_common_wrong_box": wrong_box,
                }
            )
        else:
            common = _most_common(counts) if answered else None
            rows.append(
                {
                    "card": card,
                    "most_common_box": common,
                    "most_common_count": counts[common] if common is not None else 0,
                    "pct_most_common": (
                        percent(counts[common], answered)
                        if common is not None
                        else None
                    ),
                }
            )
    return rows
