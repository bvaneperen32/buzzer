"""
Parametrized game-flow integration tests.

Each registered GameScenario becomes one test case.  The test:
  1. Creates a game with exactly the questions declared in the scenario.
  2. Registers one guest player per PlayerScript.
  3. Runs the full game via real socket.io connections.
  4. Asserts every player's game_over payload against expectations
     derived deterministically from the scenario definition.

Adding new coverage:
  - New question type / grading combo → add to scenarios/all_question_types.py
  - New edge case → add to scenarios/edge_cases.py
  - Both files are picked up automatically via collect_all_scenarios().
"""
from __future__ import annotations

import httpx
import pytest

from .conftest import create_guest_tokens, create_questions, create_room
from .engine.orchestrator import run_game, run_game_with_timer_expiry
from .engine.scoring import compute_expected_summary, compute_player_score
from .scenarios import collect_all_scenarios
from .scenarios.edge_cases import EdgeScenario


# ---------------------------------------------------------------------------
# Parametrize over every registered scenario
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "scenario",
    collect_all_scenarios(),
    ids=[s.name for s in collect_all_scenarios()],
)
async def test_game_scenario(scenario, game_setup, base_url, admin_token, fast_mode):
    """
    Run one complete game scenario and verify all player outcomes.
    """
    # Skip timer-expiry scenarios unless --fast is active
    if isinstance(scenario, EdgeScenario) and scenario.requires_fast and not fast_mode:
        pytest.skip(f"Scenario '{scenario.name}' requires --fast (short timers)")

    course_id = game_setup["course_id"]
    game_id = game_setup["game_id"]

    # ── Create questions for this scenario ──────────────────────────────────
    create_questions(base_url, admin_token, game_id, scenario, fast_mode)

    # ── Create room and guest tokens ─────────────────────────────────────────
    room_code = create_room(base_url, admin_token, course_id, game_id)
    player_tokens = create_guest_tokens(base_url, room_code, len(scenario.player_scripts))

    # ── Run the game ─────────────────────────────────────────────────────────
    timer_expiry = isinstance(scenario, EdgeScenario) and scenario.uses_timer_expiry
    if timer_expiry:
        payloads = await run_game_with_timer_expiry(
            base_url=base_url,
            host_token=admin_token,
            player_tokens=player_tokens,
            room_code=room_code,
            scenario=scenario,
            timer_seconds=2,
        )
    else:
        payloads = await run_game(
            base_url=base_url,
            host_token=admin_token,
            player_tokens=player_tokens,
            room_code=room_code,
            scenario=scenario,
        )

    # ── Assert outcomes ───────────────────────────────────────────────────────
    assert len(payloads) == len(scenario.player_scripts), (
        f"Expected {len(scenario.player_scripts)} game_over payloads, "
        f"got {len(payloads)}"
    )

    for player_idx, payload in enumerate(payloads):
        _assert_player_outcome(scenario, player_idx, payload)

    # ── Verify relative ranking order across players ──────────────────────────
    if len(payloads) > 1:
        _assert_ranking_order(scenario, payloads)

    # ── Verify session is marked COMPLETED in MySQL ───────────────────────────
    r = httpx.get(
        f"{base_url}/api/game/rooms/{room_code}",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10.0,
    )
    assert r.status_code == 200, (
        f"GET /api/game/rooms/{room_code} returned {r.status_code}: {r.text}"
    )
    assert r.json().get("status") == "COMPLETED", (
        f"Expected session status COMPLETED after game end, "
        f"got {r.json().get('status')!r}"
    )


# ---------------------------------------------------------------------------
# Per-player assertion helpers
# ---------------------------------------------------------------------------

def _assert_player_outcome(scenario, player_idx: int, payload: dict):
    label = f"Player {player_idx} in scenario '{scenario.name}'"

    # ── Final score ───────────────────────────────────────────────────────────
    expected_score = compute_player_score(scenario, player_idx)
    actual_score = payload.get("yourFinalScore", -1)
    assert actual_score == expected_score, (
        f"{label}: expected final score {expected_score}, got {actual_score}"
    )

    # ── Question summary present and complete ─────────────────────────────────
    summary = payload.get("questionSummary", [])
    assert len(summary) == len(scenario.questions), (
        f"{label}: expected {len(scenario.questions)} questions in summary, "
        f"got {len(summary)}"
    )

    # ── Per-question assertions ───────────────────────────────────────────────
    expected_summary = compute_expected_summary(scenario, player_idx)
    script = scenario.player_scripts[player_idx]

    for q_idx, (q_spec, expected, actual_q) in enumerate(
        zip(scenario.questions, expected_summary, summary)
    ):
        q_label = f"{label}, Q{q_idx + 1} ({q_spec.type}/{q_spec.grading_type})"
        response = script.responses[q_idx]

        # Points awarded
        assert actual_q["pointsAwarded"] == expected["pointsAwarded"], (
            f"{q_label}: expected {expected['pointsAwarded']} pts, "
            f"got {actual_q['pointsAwarded']}"
        )

        # Player's submitted answer (or null for skips)
        if response is None:
            assert actual_q.get("playerAnswer") is None, (
                f"{q_label}: expected null playerAnswer (not submitted), "
                f"got {actual_q.get('playerAnswer')}"
            )
        else:
            assert actual_q.get("playerAnswer") is not None, (
                f"{q_label}: expected non-null playerAnswer, got null"
            )
            _assert_answer_matches(q_label, response, actual_q["playerAnswer"])

        # Answer reveal type and content
        reveal = actual_q.get("answerReveal", {})
        expected_reveal = expected["answerReveal"]
        assert reveal.get("type") == expected_reveal.get("type"), (
            f"{q_label}: expected reveal type {expected_reveal.get('type')!r}, "
            f"got {reveal.get('type')!r}"
        )
        _assert_reveal_content(q_label, q_spec, reveal, expected_reveal)

    # ── Rank is a positive integer within plausible range ─────────────────────
    rank = payload.get("yourFinalRank", 0)
    n_players = payload.get("playerCount", 0)
    assert 1 <= rank <= max(n_players, 1), (
        f"{label}: rank {rank} out of range [1, {n_players}]"
    )


def _assert_answer_matches(label: str, submitted: dict, actual: dict):
    """Check the stored playerAnswer matches what the player submitted."""
    if "selectedIndex" in submitted:
        assert actual.get("selectedIndex") == submitted["selectedIndex"], (
            f"{label}: stored selectedIndex {actual.get('selectedIndex')} "
            f"≠ submitted {submitted['selectedIndex']}"
        )
    if "selectedValue" in submitted:
        assert actual.get("selectedValue") == submitted["selectedValue"], (
            f"{label}: stored selectedValue {actual.get('selectedValue')} "
            f"≠ submitted {submitted['selectedValue']}"
        )
    if "text" in submitted:
        assert actual.get("text") == submitted["text"], (
            f"{label}: stored text {actual.get('text')!r} "
            f"≠ submitted {submitted['text']!r}"
        )
    if "placements" in submitted:
        # T7: the validated answer is stored unchanged.
        assert actual == submitted, f"{label}: stored {actual} ≠ submitted {submitted}"


def _assert_ranking_order(scenario, payloads: list[dict]):
    """
    For every pair of players with strictly different expected scores, assert
    that the higher-scoring player received a strictly lower (better) rank number.
    Tied scores carry no rank constraint — SQL ORDER BY is stable but the
    tiebreak order is unspecified.
    """
    expected_scores = [compute_player_score(scenario, i) for i in range(len(payloads))]
    ranks = [p.get("yourFinalRank", 0) for p in payloads]

    for i in range(len(payloads)):
        for j in range(i + 1, len(payloads)):
            score_i, score_j = expected_scores[i], expected_scores[j]
            rank_i, rank_j = ranks[i], ranks[j]
            if score_i > score_j:
                assert rank_i < rank_j, (
                    f"Player {i} (score={score_i}) should rank better than "
                    f"Player {j} (score={score_j}), "
                    f"but got ranks {rank_i} vs {rank_j}"
                )
            elif score_i < score_j:
                assert rank_i > rank_j, (
                    f"Player {i} (score={score_i}) should rank worse than "
                    f"Player {j} (score={score_j}), "
                    f"but got ranks {rank_i} vs {rank_j}"
                )
            # equal scores: no constraint


def _assert_reveal_content(label: str, q_spec, actual_reveal: dict, expected_reveal: dict):
    """Check reveal payload content matches our locally-computed expectation."""
    kind = expected_reveal.get("type")
    if kind == "completeness":
        return  # No further content to check

    if kind == "multiple_choice":
        assert set(actual_reveal.get("correctIndices", [])) == set(
            expected_reveal.get("correctIndices", [])
        ), (
            f"{label}: correctIndices {actual_reveal.get('correctIndices')} "
            f"≠ expected {expected_reveal.get('correctIndices')}"
        )

    if kind == "true_false":
        assert actual_reveal.get("correctValue") == expected_reveal.get("correctValue"), (
            f"{label}: correctValue {actual_reveal.get('correctValue')} "
            f"≠ expected {expected_reveal.get('correctValue')}"
        )

    if kind == "fill_in_the_blank":
        assert set(actual_reveal.get("acceptedAnswers", [])) == set(
            expected_reveal.get("acceptedAnswers", [])
        ), (
            f"{label}: acceptedAnswers {actual_reveal.get('acceptedAnswers')} "
            f"≠ expected {expected_reveal.get('acceptedAnswers')}"
        )
        assert actual_reveal.get("editDistance") == expected_reveal.get("editDistance"), (
            f"{label}: editDistance {actual_reveal.get('editDistance')} "
            f"≠ expected {expected_reveal.get('editDistance')}"
        )

    if kind == "sort_into_boxes":
        # Order matters: one correct box per card, in the author's card order.
        assert actual_reveal.get("correctBoxes") == expected_reveal.get("correctBoxes"), (
            f"{label}: correctBoxes {actual_reveal.get('correctBoxes')} "
            f"≠ expected {expected_reveal.get('correctBoxes')}"
        )
