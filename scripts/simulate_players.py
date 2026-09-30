#!/usr/bin/env python3
"""
Multi-player answer simulator with realistic player behaviour profiles.

Players are distributed across five behaviour profiles:

  fast    — answers within 1–3 s, rarely skips
  normal  — answers within 3–8 s, occasionally skips
  slow    — takes 8–18 s, more likely to skip
  absent  — never submits an answer (simulates a disengaged student)
  late    — waits 25–70 s after connecting before joining the room,
            then answers at a normal pace (simulates arriving late)

All question types are handled automatically:

  multiple_choice   — random option; if --game-json supplied, fast/normal
                      players tend toward the correct answer
  true_false        — random True/False; same accuracy-weighting applies
  multi_select      — random subset of options; if --game-json supplied,
                      accurate players select exactly the positive-point options
  fill_in_the_blank — random word from --fitb-words; if --game-json supplied,
                      correct answers are drawn from acceptedAnswers and wrong
                      answers from --fitb-words
  sort_into_boxes   — (T7 Sort It) each card in a random box, about 20% left
                      out; if --game-json supplied, accurate players place every
                      card in its correct box
  find_in_image     — (T7 Spot It) a random grid cell; if --game-json supplied,
                      accurate players pick an accepted cell

Usage:
    # Basic — 20 players, default profile mix, default FITB word pool
    python scripts/simulate_players.py --room ABCDE1

    # 40 players with a custom profile mix (remainder becomes "normal")
    python scripts/simulate_players.py --room ABCDE1 --players 40 \\
        --fast-pct 20 --slow-pct 20 --absent-pct 10 --late-pct 10

    # Pass the game JSON so players give realistic per-question answers
    python scripts/simulate_players.py --room ABCDE1 --players 30 \\
        --game-json sample_games/cs_first_day.json

    # Custom FITB word pool — repeat a word to weight it higher in the cloud
    python scripts/simulate_players.py --room ABCDE1 --players 25 \\
        --fitb-words "spring,spring,summer,summer,summer,autumn,winter"

Prerequisites:
    pip install httpx "python-socketio[asyncio_client]"

Workflow:
    1. Create a room from the Host screen.
    2. Run this script with the room code shown on the lobby.
    3. Press Start / Next on the host screen to advance through questions.
       Players answer automatically according to their profile.
    4. Late players print a message when they eventually join mid-game.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import string
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import socketio

# ── Default FITB word pool ─────────────────────────────────────────────────────
# Duplicate entries raise that word's probability, giving a realistic-looking cloud.
DEFAULT_FITB_WORDS = [
    "Python",
    "Python",
    "Python",
    "Python",
    "Python",
    "JavaScript",
    "JavaScript",
    "JavaScript",
    "JavaScript",
    "Java",
    "Java",
    "Java",
    "TypeScript",
    "TypeScript",
    "Rust",
    "Rust",
    "Go",
    "Go",
    "C++",
    "C++",
    "Ruby",
    "Swift",
    "Kotlin",
    "C",
]

# ── ANSI ──────────────────────────────────────────────────────────────────────
_G = "\033[92m"
_B = "\033[94m"
_Y = "\033[93m"
_M = "\033[35m"
_DIM = "\033[2m"
_RST = "\033[0m"

OK = f"{_G}✓{_RST}"
ERR = f"\033[91m✗{_RST}"
INFO = f"{_B}→{_RST}"
WARN = f"{_Y}⚑{_RST}"
SKIP = f"{_DIM}·{_RST}"


# ── Player profile ─────────────────────────────────────────────────────────────
@dataclass
class Profile:
    label: str
    color: str
    min_delay: float  # seconds between question appearing and submitting
    max_delay: float
    skip_prob: float  # probability of skipping any given question (0–1)
    accuracy: float  # probability of choosing the correct answer when game
    # JSON is provided (ignored without --game-json)
    join_delay_min: float  # seconds to wait before joining room (0 = immediate)
    join_delay_max: float


PROFILES: dict[str, Profile] = {
    "fast": Profile("fast", _G, 0.5, 3.0, 0.05, 0.75, 0, 0),
    "normal": Profile("normal", _B, 3.0, 8.0, 0.15, 0.60, 0, 0),
    "slow": Profile("slow", _Y, 8.0, 18.0, 0.30, 0.50, 0, 0),
    "absent": Profile("absent", _DIM, 0, 0, 1.0, 0, 0, 0),
    "late": Profile("late", _M, 3.0, 8.0, 0.15, 0.60, 25.0, 70.0),
}

# Natural default split — all five sum to 100.
# Unspecified CLI flags are scaled proportionally from these values.
PROFILE_DEFAULTS = {"fast": 25, "normal": 35, "slow": 20, "absent": 10, "late": 10}


# ── HTTP helper ────────────────────────────────────────────────────────────────
async def api(
    http: httpx.AsyncClient,
    method: str,
    path: str,
    base: str,
    token: str = "",
    expect_204: bool = False,
    **kwargs,
):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await getattr(http, method)(f"{base}/api{path}", headers=headers, **kwargs)
    if not r.is_success:
        raise RuntimeError(
            f"HTTP {r.status_code} {method.upper()} {path}: {r.text[:300]}"
        )
    if expect_204:
        return None
    return r.json() if r.text else None


# ── Simulated player ───────────────────────────────────────────────────────────
class SimPlayer:
    def __init__(
        self,
        idx: int,
        display_name: str,
        token: str,
        base_url: str,
        profile: Profile,
        fitb_words: list[str],
        game_questions: list[dict] | None,
    ):
        self.idx = idx
        self.display_name = display_name
        self.token = token
        self.base_url = base_url
        self.profile = profile
        self.fitb_words = fitb_words
        self.game_questions = game_questions  # from --game-json, or None

        self.sio = socketio.AsyncClient(logger=False, engineio_logger=False)
        self._queues: dict[str, asyncio.Queue] = {
            e: asyncio.Queue()
            for e in [
                "sync_state",
                "new_question",
                "answer_received",
                "question_results",
                "game_over",
                "error",
            ]
        }
        for event in self._queues:

            @self.sio.on(event)
            async def _h(data=None, _e=event):
                await self._queues[_e].put(data or {})

    # ── helpers ──────────────────────────────────────────────────────────────

    def _tag(self) -> str:
        p = self.profile
        return f"{p.color}[{self.display_name:>18} | {p.label:<6}]{_RST}"

    async def _wait(self, event: str, timeout: float) -> dict:
        return await asyncio.wait_for(self._queues[event].get(), timeout=timeout)

    def _json_question(self, question_number: int) -> dict | None:
        """Return the game-JSON question for this 1-indexed question number."""
        if not self.game_questions:
            return None
        idx = question_number - 1
        if 0 <= idx < len(self.game_questions):
            return self.game_questions[idx]
        return None

    def _make_answer(self, q: dict) -> dict | None:
        p = self.profile
        q_type = q.get("type")
        q_num = q.get("questionNumber", 1)
        jq = self._json_question(q_num)

        if q_type == "fill_in_the_blank":
            accepted = (jq or {}).get("answer_data", {}).get("acceptedAnswers", [])
            if accepted and random.random() < p.accuracy:
                return {"text": random.choice(accepted)}
            # Wrong answer: use the --fitb-words pool (or fall back to accepted)
            pool = self.fitb_words or accepted
            return {"text": random.choice(pool)} if pool else None

        if q_type == "multiple_choice":
            if jq and random.random() < p.accuracy:
                pts = jq.get("answer_data", {}).get("answer_points", [])
                if pts:
                    max_pts = max(pts)
                    correct = [i for i, v in enumerate(pts) if v == max_pts]
                    if correct:
                        return {"selectedIndex": random.choice(correct)}
            n = len(q.get("config", {}).get("options", ["a"]))
            return {"selectedIndex": random.randint(0, n - 1)}

        if q_type == "true_false":
            if jq and random.random() < p.accuracy:
                pts = jq.get("answer_data", {}).get("answer_points", {})
                if pts:
                    correct = pts.get("true", 0) >= pts.get("false", 0)
                    return {"selectedValue": correct}
            return {"selectedValue": random.choice([True, False])}

        if q_type == "multi_select":
            if jq and random.random() < p.accuracy:
                pts = jq.get("answer_data", {}).get("answer_points", [])
                correct = [i for i, v in enumerate(pts) if v > 0]
                if correct:
                    return {"selectedIndices": correct}
            n = len(q.get("config", {}).get("options", []))
            if n == 0:
                return None
            k = random.randint(1, n)
            return {"selectedIndices": sorted(random.sample(range(n), k))}

        if q_type == "sort_into_boxes":
            config = q.get("config", {})
            n, k = len(config.get("cards", [])), len(config.get("boxes", []))
            if n == 0 or k < 2:
                return None
            key = (jq or {}).get("answer_data", {}).get("correct_boxes")
            if isinstance(key, list) and len(key) == n and random.random() < p.accuracy:
                return {"placements": list(key)}
            # Random boxes with about 20% of cards left out; at least one card is placed,
            # since an all-empty board is refused.
            placements = [
                None if random.random() < 0.2 else random.randrange(k) for _ in range(n)
            ]
            if all(box is None for box in placements):
                placements[random.randrange(n)] = random.randrange(k)
            return {"placements": placements}

        if q_type == "find_in_image":
            grid = q.get("config", {}).get("grid", {})
            cols, rows = grid.get("cols", 0), grid.get("rows", 0)
            if cols < 1 or rows < 1:
                return None
            cells = (jq or {}).get("answer_data", {}).get("accepted_cells")
            if cells and random.random() < p.accuracy:
                chosen = random.choice(cells)
                return {"col": chosen["col"], "row": chosen["row"]}
            return {"col": random.randrange(cols), "row": random.randrange(rows)}

        return None  # unknown type

    def _answer_str(self, answer: dict) -> str:
        if "text" in answer:
            return repr(answer["text"])
        if "selectedIndex" in answer:
            return f"option {answer['selectedIndex']}"
        if "selectedIndices" in answer:
            return f"options {answer['selectedIndices']}"
        if "selectedValue" in answer:
            return str(answer["selectedValue"])
        if "placements" in answer:
            placed = sum(1 for box in answer["placements"] if box is not None)
            return f"{placed}/{len(answer['placements'])} cards placed"
        if "col" in answer and "row" in answer:
            return f"cell {chr(ord('A') + answer['col'])}{answer['row'] + 1}"
        return repr(answer)

    # ── per-question handler ──────────────────────────────────────────────────

    async def _handle_question(self, q: dict, label: str = ""):
        p = self.profile
        q_type = q.get("type", "unknown")
        q_id = q.get("questionId")
        q_num = q.get("questionNumber", "?")
        t_lim = q.get("timeLimitSeconds", 30)
        ql = f"q{q_num}" + (f" {label}" if label else "")

        if p.skip_prob >= 1.0:
            print(f"  {SKIP}  {self._tag()}  {ql:<14}  —  (absent)")
            return

        if random.random() < p.skip_prob:
            print(f"  {SKIP}  {self._tag()}  {ql:<14}  —  (skipped)")
            return

        answer = self._make_answer(q)
        if answer is None:
            print(f"  {WARN}  {self._tag()}  {ql:<14}  unknown type {q_type!r}")
            return

        delay = random.uniform(p.min_delay, min(p.max_delay, t_lim * 0.8))
        await asyncio.sleep(delay)

        await self.sio.emit(
            "submit_answer",
            {
                "question_id": q_id,
                "answer_data": answer,
                "answer_time_ms": int(delay * 1000),
            },
        )

        try:
            ar = await self._wait("answer_received", timeout=10.0)
            pts = ar.get("pointsAwarded", 0)
            ans = self._answer_str(answer)
            tick = OK if ar.get("isCorrect") else SKIP
            print(f"  {tick}  {self._tag()}  {ql:<14}  {ans:<22}  {pts:>5} pts")
        except asyncio.TimeoutError:
            print(f"  {ERR}  {self._tag()}  {ql:<14}  no answer_received")

    # ── connection & main loop ────────────────────────────────────────────────

    async def connect(self):
        await self.sio.connect(
            self.base_url,
            socketio_path="/socket.io",
            auth={"token": self.token},
            transports=["websocket"],
        )

    async def disconnect(self):
        if self.sio.connected:
            await self.sio.disconnect()

    async def run(self, room_code: str):
        p = self.profile

        if p.join_delay_max > 0:
            delay = random.uniform(p.join_delay_min, p.join_delay_max)
            print(f"  {INFO}  {self._tag()}  will join in {delay:.0f}s…")
            await asyncio.sleep(delay)

        await self.sio.emit("join_room", {"room_code": room_code, "role": "PLAYER"})

        try:
            sync = await self._wait("sync_state", timeout=15.0)
        except asyncio.TimeoutError:
            print(f"  {ERR}  {self._tag()}  timed out waiting for sync_state")
            return

        # If we joined while a question is active (late joiner during QUESTION phase)
        current_q = sync.get("currentQuestion")
        if current_q:
            await self._handle_question(current_q, label="(joined late)")

        # Race between new_question and game_over for the rest of the game
        while True:
            q_task = asyncio.ensure_future(self._wait("new_question", timeout=300.0))
            go_task = asyncio.ensure_future(self._wait("game_over", timeout=300.0))

            done, pending = await asyncio.wait(
                {q_task, go_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

            if go_task in done:
                break
            if q_task in done:
                try:
                    await self._handle_question(q_task.result())
                except Exception:
                    pass


# ── Profile distribution ───────────────────────────────────────────────────────
def assign_profiles(n: int, pct: dict[str, int]) -> list[Profile]:
    used = sum(pct.values())
    pct["normal"] = pct.get("normal", 0) + max(0, 100 - used)

    counts: dict[str, int] = {}
    allocated = 0
    names = list(pct.keys())
    for name in names[:-1]:
        c = math.floor(n * pct[name] / 100)
        counts[name] = c
        allocated += c
    counts[names[-1]] = counts.get(names[-1], 0) + (n - allocated)

    profiles: list[Profile] = []
    for name, count in counts.items():
        profiles.extend([PROFILES[name]] * count)
    random.shuffle(profiles)
    return profiles


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    ap = argparse.ArgumentParser(
        description="Simulate multiple Buzzer players with realistic behaviour."
    )
    ap.add_argument(
        "--room",
        required=True,
        metavar="CODE",
        help="Room code shown on the host lobby screen",
    )
    ap.add_argument(
        "--players",
        type=int,
        default=20,
        metavar="N",
        help="Total number of simulated players (default: 20)",
    )

    # Profile mix — omitted flags are scaled proportionally from natural defaults
    # so the full five-way split always sums to 100%.
    ap.add_argument(
        "--fast-pct",
        type=int,
        default=None,
        metavar="PCT",
        help="Percentage of fast players (default: proportional from 25)",
    )
    ap.add_argument(
        "--slow-pct",
        type=int,
        default=None,
        metavar="PCT",
        help="Percentage of slow players (default: proportional from 20)",
    )
    ap.add_argument(
        "--absent-pct",
        type=int,
        default=None,
        metavar="PCT",
        help="Percentage of absent players (default: proportional from 10)",
    )
    ap.add_argument(
        "--late-pct",
        type=int,
        default=None,
        metavar="PCT",
        help="Percentage of late-joining players (default: proportional from 10)",
    )

    ap.add_argument(
        "--game-json",
        metavar="PATH",
        help="Path to a game JSON file. When provided, players give "
        "realistic per-question answers: fast/normal players lean "
        "toward the correct answer, slow players are less accurate.",
    )
    ap.add_argument(
        "--fitb-words",
        metavar="W1,W2,...",
        help="Comma-separated FITB answer pool used as 'wrong' answers "
        "when --game-json is given, or as the full pool without it. "
        "Repeat a word to weight it higher in the word cloud.",
    )
    ap.add_argument("--base-url", default="http://localhost:8000", metavar="URL")
    ap.add_argument(
        "--admin-user", default=os.getenv("ADMIN_USERNAME", "admin"), metavar="USER"
    )
    ap.add_argument(
        "--admin-pass",
        default=os.getenv("ADMIN_PASSWORD", "changeme123"),
        metavar="PASS",
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="Leave temporary player accounts in the DB after the run. "
        "Required if you want session scores to appear in admin reports "
        "(CSV export, HTML report) — deleting accounts also deletes their scores.",
    )
    args = ap.parse_args()

    if args.players < 1:
        print(f"{ERR} --players must be at least 1, got {args.players}")
        sys.exit(1)

    # Build the provided dict and validate
    cli_map = {
        "fast": args.fast_pct,
        "slow": args.slow_pct,
        "absent": args.absent_pct,
        "late": args.late_pct,
    }
    provided = {k: v for k, v in cli_map.items() if v is not None}

    for name, val in provided.items():
        if val < 0:
            print(f"{ERR} --{name}-pct must be ≥ 0, got {val}")
            sys.exit(1)

    provided_total = sum(provided.values())
    if provided_total > 100:
        parts = "  +  ".join(f"{n} {v}%" for n, v in provided.items())
        print(f"{ERR} Provided profile percentages exceed 100%:")
        print(f"       {parts}  =  {provided_total}%")
        sys.exit(1)

    # Scale unspecified profiles (including implicit "normal") proportionally
    # from their natural defaults so the full five-way split sums to 100.
    remaining = 100 - provided_total
    unspecified = [k for k in PROFILE_DEFAULTS if k not in provided]
    unspecified_default_sum = sum(PROFILE_DEFAULTS[k] for k in unspecified)

    if unspecified_default_sum > 0 and remaining > 0:
        scaled = {
            k: round(PROFILE_DEFAULTS[k] * remaining / unspecified_default_sum)
            for k in unspecified
        }
        # Absorb any rounding error into the largest unspecified category
        err = remaining - sum(scaled.values())
        if err:
            largest = max(unspecified, key=lambda k: scaled[k])
            scaled[largest] += err
    else:
        scaled = {k: 0 for k in unspecified}

    final_pct = {**provided, **scaled}  # all five profiles, sums to 100

    fitb_words = (
        [w.strip() for w in args.fitb_words.split(",") if w.strip()]
        if args.fitb_words
        else DEFAULT_FITB_WORDS
    )

    # Load game JSON if provided
    game_questions: list[dict] | None = None
    if args.game_json:
        path = Path(args.game_json)
        if not path.exists():
            print(f"{ERR} Game JSON not found: {path}")
            sys.exit(1)
        data = json.loads(path.read_text())
        game_questions = data.get("questions", [])
        print(f"{INFO} Loaded {len(game_questions)} question(s) from {path.name}")

    base = args.base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    room_code = args.room.upper().strip()

    normal_pct = final_pct["normal"]
    pct_mix = {k: final_pct[k] for k in ("fast", "slow", "absent", "late")}

    print(f"\n{'=' * 58}")
    print("  Buzzer Player Simulator")
    print(f"{'=' * 58}")
    print(f"  Room:      {room_code}")
    print(f"  Players:   {args.players}")
    print(
        f"  Mix:       fast {final_pct['fast']}%  normal {normal_pct}%  "
        f"slow {final_pct['slow']}%  absent {final_pct['absent']}%  late {final_pct['late']}%"
    )
    if game_questions:
        print(
            f"  Game JSON: {len(game_questions)} questions loaded "
            f"(accuracy-weighted answers)"
        )
    else:
        print(f"  FITB pool: {', '.join(sorted(set(fitb_words)))}")
    print(f"{'=' * 58}\n")

    created_ids: list[int] = []
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    profile_list = assign_profiles(args.players, pct_mix)

    async with httpx.AsyncClient(timeout=20) as http:
        # 1. Admin login
        print(f"{INFO} Authenticating as admin…")
        try:
            tok = await api(
                http,
                "post",
                "/auth/login",
                base,
                json={"username": args.admin_user, "password": args.admin_pass},
            )
        except RuntimeError as e:
            print(f"{ERR} Admin login failed: {e}")
            sys.exit(1)
        admin_token = tok["access_token"]
        print(f"{OK} Authenticated\n")

        # 2. Look up room
        print(f"{INFO} Looking up room {room_code}…")
        try:
            room = await api(http, "get", f"/game/rooms/{room_code}", base, admin_token)
        except RuntimeError as e:
            print(f"{ERR} Room lookup failed: {e}")
            sys.exit(1)

        status = room["status"]
        course_id = room["course_id"]
        n_questions = room["question_count"]
        print(
            f'{OK} "{room["game_title"]}"  —  {n_questions} question(s), status={status}'
        )

        if status not in ("LOBBY", "IN_PROGRESS"):
            print(f"{ERR} Room is {status!r}; must be LOBBY or IN_PROGRESS.")
            sys.exit(1)

        if game_questions and len(game_questions) != n_questions:
            print(
                f"{WARN} JSON has {len(game_questions)} questions but room has "
                f"{n_questions} — answers may not match if counts differ"
            )

        # 3. Create temporary player accounts
        print(f"\n{INFO} Creating {args.players} temporary player accounts…")
        player_tokens: list[tuple[str, str, Profile]] = []

        for i, profile in enumerate(profile_list):
            uname = f"sim_{suffix}_{i:03d}"
            display = f"Sim {suffix[:4].upper()} {i + 1:03d}"
            try:
                user = await api(
                    http,
                    "post",
                    "/admin/users",
                    base,
                    admin_token,
                    json={
                        "username": uname,
                        "display_name": display,
                        "password": "Sim1234!",
                    },
                )
                uid = user["id"]
                created_ids.append(uid)

                await api(
                    http,
                    "post",
                    f"/admin/users/{uid}/course-access",
                    base,
                    admin_token,
                    json={"course_id": course_id, "role": "PLAYER"},
                    expect_204=True,
                )

                ptok = await api(
                    http,
                    "post",
                    "/auth/login",
                    base,
                    json={"username": uname, "password": "Sim1234!"},
                )
                player_tokens.append((display, ptok["access_token"], profile))

            except RuntimeError as e:
                print(f"  {ERR} Failed on player {i}: {e}")

        print(f"{OK} {len(player_tokens)} players ready\n")
        if not player_tokens:
            print(f"{ERR} No players were created — exiting.")
            sys.exit(1)

        # 4. Connect all players via WebSocket
        print(f"{INFO} Connecting to {base}…")
        sims = [
            SimPlayer(i, name, token, base, profile, fitb_words, game_questions)
            for i, (name, token, profile) in enumerate(player_tokens)
        ]
        for sim in sims:
            await sim.connect()
            await asyncio.sleep(0.05)

        connected = sum(1 for s in sims if s.sio.connected)
        print(f"{OK} {connected} players connected\n")
        print("  Press Start / Next on the host screen to advance questions.")
        print("  Late players will join automatically after their delay.\n")
        print(
            f"  {'':2}  {'Player + profile':>28}  {'Question':<14}  {'Answer':<22}  {'Points':>5}"
        )
        print(f"  {'-' * 80}")

        # 5. Run all players concurrently
        try:
            await asyncio.gather(*(s.run(room_code) for s in sims))
        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n{WARN} Interrupted.")
        finally:
            for sim in sims:
                await sim.disconnect()
            print(f"\n{OK} All players disconnected")

        # 6. Cleanup
        if not args.keep and created_ids:
            print(
                f"\n{INFO} Deleting {len(created_ids)} temporary accounts (and their scores)…"
            )
            print(f"{WARN} Use --keep to preserve scores for admin CSV/HTML reports.")
            for uid in created_ids:
                try:
                    await api(
                        http,
                        "delete",
                        f"/admin/users/{uid}",
                        base,
                        admin_token,
                        expect_204=True,
                    )
                except RuntimeError:
                    pass
            print(f"{OK} Accounts deleted")
        elif args.keep:
            print(f"\n{WARN} --keep: temporary accounts left in DB")

    print(f"\n{'=' * 58}\n  Done.\n{'=' * 58}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
