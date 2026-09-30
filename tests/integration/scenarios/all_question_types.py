"""
Core coverage scenarios: every question type × grading combination, with
players that answer correctly, incorrectly, and not at all.

Adding a new question type:
  1. Add a QuestionSpec for it here.
  2. Add a corresponding case to engine/scoring.py:compute_question_score().
  3. The test_game_flow parametrize picks it up automatically.
"""
from __future__ import annotations

from ..engine.scoring import GameScenario, PlayerScript, QuestionSpec

# ---------------------------------------------------------------------------
# Reusable question specs — multiple_choice and true_false
# ---------------------------------------------------------------------------

# Multiple-choice ACCURACY — correct answer is index 1 (Mercury, 1000 pts)
MC_ACCURACY = QuestionSpec(
    type="multiple_choice",
    grading_type="ACCURACY",
    prompt="Which planet is closest to the Sun?",
    config={"options": ["Venus", "Mercury", "Mars", "Earth"]},
    answer_data={"answer_points": [0, 1000, 0, 0]},
    points_value=1000,
)

# Multiple-choice COMPLETENESS — any answer earns full points
MC_COMPLETENESS = QuestionSpec(
    type="multiple_choice",
    grading_type="COMPLETENESS",
    prompt="Which of these is your favourite colour?",
    config={"options": ["Red", "Blue", "Green"]},
    answer_data={"answer_points": [1000, 1000, 1000]},
    points_value=1000,
)

# True/False ACCURACY — correct answer is True (1000 pts)
TF_ACCURACY_TRUE = QuestionSpec(
    type="true_false",
    grading_type="ACCURACY",
    prompt="Water boils at 100 °C at sea level.",
    config={},
    answer_data={"answer_points": {"true": 1000, "false": 0}},
    points_value=1000,
)

# True/False ACCURACY — correct answer is False (1000 pts)
TF_ACCURACY_FALSE = QuestionSpec(
    type="true_false",
    grading_type="ACCURACY",
    prompt="The Great Wall of China is visible from space with the naked eye.",
    config={},
    answer_data={"answer_points": {"true": 0, "false": 1000}},
    points_value=1000,
)

# True/False COMPLETENESS — any answer earns full points
TF_COMPLETENESS = QuestionSpec(
    type="true_false",
    grading_type="COMPLETENESS",
    prompt="Would you visit Mars if given the chance?",
    config={},
    answer_data={"answer_points": {"true": 1000, "false": 1000}},
    points_value=1000,
)

# MC ACCURACY with many options — tests 10-option questions
MC_MANY_OPTIONS = QuestionSpec(
    type="multiple_choice",
    grading_type="ACCURACY",
    prompt="Which country has the largest land area?",
    config={"options": [
        "Canada", "Russia", "China", "USA",
        "Brazil", "Australia", "India", "Argentina",
        "Kazakhstan", "Algeria",
    ]},
    # Russia (index 1) is correct
    answer_data={"answer_points": [0, 1000, 0, 0, 0, 0, 0, 0, 0, 0]},
    points_value=1000,
)

# ---------------------------------------------------------------------------
# Reusable question specs — fill_in_the_blank
# ---------------------------------------------------------------------------

# FITB ACCURACY, editDistance=0: only exact (case-insensitive, whitespace-normalised)
# matches are accepted.
FITB_ACCURACY_EXACT = QuestionSpec(
    type="fill_in_the_blank",
    grading_type="ACCURACY",
    prompt="What chess piece can only move diagonally?",
    config={"maxLength": 30},
    answer_data={"acceptedAnswers": ["bishop"], "answerPoints": [1000], "editDistance": 0},
    points_value=1000,
)

# FITB ACCURACY, editDistance=1: one-character typos are forgiven.
# "Nill" (edit distance 1 from "nile") should earn full points.
FITB_ACCURACY_FUZZY = QuestionSpec(
    type="fill_in_the_blank",
    grading_type="ACCURACY",
    prompt="What is the name of the longest river in Africa?",
    config={"maxLength": 30},
    answer_data={"acceptedAnswers": ["Nile", "the Nile"], "answerPoints": [1000, 1000], "editDistance": 1},
    points_value=1000,
)

# FITB COMPLETENESS: any non-empty answer earns full points; no correct answer.
FITB_COMPLETENESS = QuestionSpec(
    type="fill_in_the_blank",
    grading_type="COMPLETENESS",
    prompt="Name your favourite season.",
    config={"maxLength": 30},
    answer_data={},
    points_value=1000,
)

# ---------------------------------------------------------------------------
# Player answer helpers
# ---------------------------------------------------------------------------

def _correct(q: QuestionSpec) -> dict:
    """Return the first correct answer for a question."""
    if q.type == "multiple_choice":
        pts = q.answer_data["answer_points"]
        idx = next(i for i, p in enumerate(pts) if p == q.points_value)
        return {"selectedIndex": idx}
    if q.type == "true_false":
        pts = q.answer_data["answer_points"]
        correct_val = pts.get("true", 0) >= q.points_value
        return {"selectedValue": correct_val}
    if q.type == "fill_in_the_blank":
        return {"text": q.answer_data["acceptedAnswers"][0]}
    raise NotImplementedError(f"No _correct helper for type {q.type!r}")


def _wrong(q: QuestionSpec) -> dict:
    """Return a wrong answer for a question."""
    if q.type == "multiple_choice":
        pts = q.answer_data["answer_points"]
        idx = next(i for i, p in enumerate(pts) if p < q.points_value)
        return {"selectedIndex": idx}
    if q.type == "true_false":
        pts = q.answer_data["answer_points"]
        correct_val = pts.get("true", 0) >= q.points_value
        return {"selectedValue": not correct_val}
    if q.type == "fill_in_the_blank":
        # "xxxxxxxx" has high edit distance from any realistic answer
        return {"text": "xxxxxxxx"}
    raise NotImplementedError(f"No _wrong helper for type {q.type!r}")


def _any(q: QuestionSpec) -> dict:
    """Return any valid answer (used for COMPLETENESS questions)."""
    if q.type == "multiple_choice":
        return {"selectedIndex": 0}
    if q.type == "true_false":
        return {"selectedValue": True}
    if q.type == "fill_in_the_blank":
        return {"text": "anything"}
    raise NotImplementedError(f"No _any helper for type {q.type!r}")


# ---------------------------------------------------------------------------
# Scenarios — multiple_choice / true_false (existing)
# ---------------------------------------------------------------------------

ALL_TYPES = GameScenario(
    name="all_types_and_grading",
    description=(
        "One question of every type×grading combination. "
        "Four players: all-correct, all-wrong, all-skip, mixed."
    ),
    questions=[
        MC_ACCURACY,
        MC_COMPLETENESS,
        TF_ACCURACY_TRUE,
        TF_ACCURACY_FALSE,
        TF_COMPLETENESS,
        MC_MANY_OPTIONS,
    ],
    player_scripts=[
        # Player 0 — answers every question correctly / with any valid answer
        PlayerScript(responses=[
            _correct(MC_ACCURACY),
            _any(MC_COMPLETENESS),
            _correct(TF_ACCURACY_TRUE),
            _correct(TF_ACCURACY_FALSE),
            _any(TF_COMPLETENESS),
            _correct(MC_MANY_OPTIONS),
        ]),
        # Player 1 — answers every question wrong (COMPLETENESS still earns pts)
        PlayerScript(responses=[
            _wrong(MC_ACCURACY),
            _any(MC_COMPLETENESS),    # wrong answer still earns on COMPLETENESS
            _wrong(TF_ACCURACY_TRUE),
            _wrong(TF_ACCURACY_FALSE),
            _any(TF_COMPLETENESS),
            _wrong(MC_MANY_OPTIONS),
        ]),
        # Player 2 — skips every question
        PlayerScript(responses=[None, None, None, None, None, None]),
        # Player 3 — mixed: some correct, some wrong, some skipped
        PlayerScript(responses=[
            _correct(MC_ACCURACY),      # correct
            None,                        # skip COMPLETENESS
            _wrong(TF_ACCURACY_TRUE),   # wrong
            None,                        # skip ACCURACY
            _any(TF_COMPLETENESS),      # answered
            _correct(MC_MANY_OPTIONS),  # correct
        ]),
    ],
)


SINGLE_PLAYER = GameScenario(
    name="single_player_all_correct",
    description="One player answers every question correctly. Simplest smoke test.",
    questions=[MC_ACCURACY, TF_ACCURACY_TRUE, TF_ACCURACY_FALSE, MC_COMPLETENESS],
    player_scripts=[
        PlayerScript(responses=[
            _correct(MC_ACCURACY),
            _correct(TF_ACCURACY_TRUE),
            _correct(TF_ACCURACY_FALSE),
            _any(MC_COMPLETENESS),
        ]),
    ],
)


# ---------------------------------------------------------------------------
# Scenario — fill_in_the_blank (all variations)
# ---------------------------------------------------------------------------

FITB_TYPES = GameScenario(
    name="fill_in_the_blank_variations",
    description=(
        "Three FITB questions (exact-match ACCURACY, fuzzy ACCURACY, COMPLETENESS). "
        "Four players exercise: exact match, case-insensitive match, whitespace "
        "normalisation, within-edit-distance match, wrong answer, and skip."
    ),
    questions=[FITB_ACCURACY_EXACT, FITB_ACCURACY_FUZZY, FITB_COMPLETENESS],
    player_scripts=[
        # Player 0 — exact correct answers on every question
        #   Q1: "bishop"  → exact match → 1000 pts
        #   Q2: "Nile"    → exact match → 1000 pts
        #   Q3: "summer"  → COMPLETENESS → 1000 pts
        PlayerScript(responses=[
            {"text": "bishop"},
            {"text": "Nile"},
            {"text": "summer"},
        ]),

        # Player 1 — case-insensitive and within-edit-distance variations
        #   Q1: "BISHOP"  → case-normalised to "bishop" → exact match → 1000 pts
        #   Q2: "Nill"    → normalised "nill"; levenshtein("nill","nile")=1 ≤ 1 → 1000 pts
        #   Q3: "WINTER"  → COMPLETENESS, any text → 1000 pts
        PlayerScript(responses=[
            {"text": "BISHOP"},
            {"text": "Nill"},
            {"text": "WINTER"},
        ]),

        # Player 2 — whitespace-normalised correct, wrong on fuzzy, correct on completeness
        #   Q1: "  bishop  " → stripped to "bishop" → exact match → 1000 pts
        #   Q2: "Amazon"     → "amazon"; levenshtein("amazon","nile")=5 > 1 → 0 pts
        #   Q3: "pizza"      → COMPLETENESS → 1000 pts
        PlayerScript(responses=[
            {"text": "  bishop  "},
            {"text": "Amazon"},
            {"text": "pizza"},
        ]),

        # Player 3 — wrong answer on exact, skip on fuzzy, skip on completeness
        #   Q1: "rook"  → "rook"; levenshtein("rook","bishop")=5 > 0 → 0 pts
        #   Q2: None    → skip → 0 pts
        #   Q3: None    → skip → 0 pts (even COMPLETENESS scores 0 for no answer)
        PlayerScript(responses=[
            {"text": "rook"},
            None,
            None,
        ]),
    ],
)


# ---------------------------------------------------------------------------
# Scenario — Sort It (T7). Spot It gameplay stays in test_t7_question_types.py, which
# uploads and confirms an image first.
# ---------------------------------------------------------------------------

SORT_CARDS = ["Whale", "Gecko", "Frog", "Bat", "Newt", "Tortoise", "Dolphin", "Salamander"]

# k = 3: 6 right, 1 wrong, 1 left out → (6 − 1/2) / 8 = 0.6875 → 687.5 of 1000.
SORT_ACCURACY_K3 = QuestionSpec(
    type="sort_into_boxes",
    grading_type="ACCURACY",
    prompt="Sort these animals by class.",
    config={"boxes": ["Mammal", "Reptile", "Amphibian"], "cards": SORT_CARDS},
    answer_data={"correct_boxes": [0, 1, 2, 0, 2, 1, 0, 2]},
    points_value=1000,
)

# k = 2: 6 right, 1 wrong, 1 left out → (6 − 1) / 8 = 0.625 → 625 of 1000.
SORT_ACCURACY_K2 = QuestionSpec(
    type="sort_into_boxes",
    grading_type="ACCURACY",
    prompt="Land or sea?",
    config={"boxes": ["Land", "Sea"], "cards": SORT_CARDS},
    answer_data={"correct_boxes": [1, 0, 0, 0, 1, 0, 1, 0]},
    points_value=1000,
)

# COMPLETENESS: any valid board (at least one card placed) earns full points.
SORT_COMPLETENESS = QuestionSpec(
    type="sort_into_boxes",
    grading_type="COMPLETENESS",
    prompt="Which would you keep as a pet?",
    config={"boxes": ["Yes", "No"], "cards": ["Cat", "Snake", "Frog", "Goldfish"]},
    answer_data={},
    points_value=1000,
)


def _sorted(q: QuestionSpec, right: int, wrong: int) -> dict:
    """The first `right` cards in their box, the next `wrong` one box over, the rest out."""
    key = q.answer_data["correct_boxes"]
    k = len(q.config["boxes"])
    placements = [
        correct if i < right else (correct + 1) % k if i < right + wrong else None
        for i, correct in enumerate(key)
    ]
    return {"placements": placements}


SORT_IT_TYPES = GameScenario(
    name="sort_it_variations",
    description=(
        "Sort It at k = 3 and k = 2 (ACCURACY) and COMPLETENESS. Scores stay exact in binary "
        "(625, 687.5, 250), because the game-over assertions compare exactly."
    ),
    questions=[SORT_ACCURACY_K3, SORT_ACCURACY_K2, SORT_COMPLETENESS],
    player_scripts=[
        # Player 0 — every card right; any board on COMPLETENESS → 3000
        PlayerScript(responses=[
            _sorted(SORT_ACCURACY_K3, 8, 0),
            _sorted(SORT_ACCURACY_K2, 8, 0),
            {"placements": [0, 1, None, 0]},
        ]),
        # Player 1 — 6 right, 1 wrong, 1 left out → 687.5 + 625 + 1000 = 2312.5
        PlayerScript(responses=[
            _sorted(SORT_ACCURACY_K3, 6, 1),
            _sorted(SORT_ACCURACY_K2, 6, 1),
            {"placements": [None, None, 1, None]},
        ]),
        # Player 2 — skips everything → 0
        PlayerScript(responses=[None, None, None]),
        # Player 3 — k = 3: 4 right, 4 wrong → (4 − 2) / 8 = 0.25 → 250; k = 2: 2 right,
        # 6 wrong → floored at 0; skips COMPLETENESS → 250
        PlayerScript(responses=[
            _sorted(SORT_ACCURACY_K3, 4, 4),
            _sorted(SORT_ACCURACY_K2, 2, 6),
            None,
        ]),
    ],
)


SCENARIOS: list[GameScenario] = [ALL_TYPES, SINGLE_PLAYER, FITB_TYPES, SORT_IT_TYPES]
