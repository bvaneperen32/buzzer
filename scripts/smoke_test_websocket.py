#!/usr/bin/env python3
"""
WebSocket gateway smoke test — Phase 5 verification.

Run against the live Docker stack:
    docker compose up -d
    pip install httpx "python-socketio[asyncio_client]"
    python scripts/smoke_test_websocket.py

Optional env vars:
    ADMIN_USERNAME  (default: admin)
    ADMIN_PASSWORD  (default: changeme)
    BASE_URL        (default: http://localhost:8000)
"""

import asyncio
import os
import random
import string
import sys

import httpx
import socketio

BASE = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "changeme")

G = "\033[92m✓\033[0m"
B = "\033[91m✗\033[0m"
AR = "\033[94m→\033[0m"

_failures = 0


def ok(label: str) -> None:
    print(f"  {G} {label}")


def fail(label: str, detail: str = "") -> None:
    global _failures
    _failures += 1
    print(f"  {B} {label}" + (f"\n      {detail}" if detail else ""))


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        ok(label)
    else:
        fail(label, detail)


async def api(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    token: str = "",
    expect_204: bool = False,
    **kwargs,
):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await getattr(client, method)(f"{BASE}/api{path}", headers=headers, **kwargs)
    if not r.is_success:
        fail(f"HTTP {r.status_code} {method.upper()} {path}", r.text[:200])
        sys.exit(1)
    if expect_204:
        return None
    return r.json() if r.text else None


async def main() -> None:
    suffix = "".join(random.choices(string.ascii_lowercase, k=6))
    player_username = f"smoke_{suffix}"

    print("\n=== Buzzer — WebSocket Smoke Test ===")
    print(f"{AR} Target: {BASE}\n")

    # Track IDs for cleanup
    game_id = player_id = None

    async with httpx.AsyncClient(timeout=15) as http:
        # ── 1. Health check ──────────────────────────────────────────────────
        print("1. Health")
        health = await api(http, "get", "/health")
        check("API reachable", health is not None)
        check(
            "MySQL connected",
            health.get("services", {}).get("mysql") == "connected",
            str(health.get("services", {})),
        )
        check(
            "Redis connected",
            health.get("services", {}).get("redis") == "connected",
            str(health.get("services", {})),
        )

        # ── 2. Admin login ───────────────────────────────────────────────────
        print("\n2. Auth")
        try:
            tok = await api(
                http,
                "post",
                "/auth/login",
                json={"username": ADMIN_USER, "password": ADMIN_PASS},
            )
        except SystemExit:
            print("\n  Hint: set ADMIN_USERNAME / ADMIN_PASSWORD to match your .env")
            return
        admin_token = tok["access_token"]
        check("Admin login", bool(admin_token))

        # ── 3. Seed data ─────────────────────────────────────────────────────
        print("\n3. Seed data")

        course = await api(
            http,
            "post",
            "/admin/courses",
            admin_token,
            json={"name": "Smoke Test Course", "semester": "Test 2099"},
        )
        course_id = course["id"]
        check(f"Course created (id={course_id})", True)

        game = await api(
            http,
            "post",
            "/admin/games",
            admin_token,
            json={
                "title": "Smoke Test Game",
                "description": "",
                "max_players": 10,
                "course_id": course_id,
            },
        )
        game_id = game["id"]
        check(f"Game created (id={game_id})", True)

        q1 = await api(
            http,
            "post",
            f"/admin/games/{game_id}/questions",
            admin_token,
            json={
                "type": "multiple_choice",
                "grading_type": "ACCURACY",
                "prompt": "Which number is largest?",
                "config": {"options": ["1", "2", "3", "42"]},
                "answer_data": {"answer_points": [0, 0, 0, 1000]},
                "time_limit_seconds": 30,
                "points_value": 1000,
                "order_index": 0,
            },
        )
        q1_id = q1["id"]
        check(f"Question 1 created (multiple_choice, id={q1_id})", True)

        q2 = await api(
            http,
            "post",
            f"/admin/games/{game_id}/questions",
            admin_token,
            json={
                "type": "true_false",
                "grading_type": "ACCURACY",
                "prompt": "Is Python awesome?",
                "config": {},
                "answer_data": {"answer_points": {"true": 1000, "false": 0}},
                "time_limit_seconds": 20,
                "points_value": 1000,
                "order_index": 1,
            },
        )
        q2_id = q2["id"]
        check(f"Question 2 created (true_false, id={q2_id})", True)

        player_acct = await api(
            http,
            "post",
            "/admin/users",
            admin_token,
            json={
                "username": player_username,
                "display_name": "Smoke Player",
                "password": "SmokeTest123!",
            },
        )
        player_id = player_acct["id"]
        check(f"Player account created (username={player_username})", True)

        await api(
            http,
            "post",
            f"/admin/users/{player_id}/course-access",
            admin_token,
            json={"course_id": course_id, "role": "PLAYER"},
            expect_204=True,
        )
        check("Player granted PLAYER course access", True)

        player_tok = await api(
            http,
            "post",
            "/auth/login",
            json={"username": player_username, "password": "SmokeTest123!"},
        )
        player_token = player_tok["access_token"]
        check("Player login", bool(player_token))

        # ── 4. Create room ───────────────────────────────────────────────────
        print("\n4. Room creation")
        room = await api(
            http,
            "post",
            "/game/rooms",
            admin_token,
            json={"game_id": game_id, "course_id": course_id},
        )
        room_code = room["room_code"]
        check(f"Room created (code={room_code})", bool(room_code))

    # ── 5. WebSocket game loop ───────────────────────────────────────────────
    print("\n5. WebSocket game loop")

    host_ev: dict[str, dict] = {}
    player_ev: dict[str, dict] = {}

    host_sio = socketio.AsyncClient(logger=False, engineio_logger=False)
    player_sio = socketio.AsyncClient(logger=False, engineio_logger=False)

    @host_sio.on("*")
    async def on_host(event, data=None):
        host_ev[event] = data or {}
        print(f"  {AR} [HOST]   ← {event}")

    @player_sio.on("*")
    async def on_player(event, data=None):
        player_ev[event] = data or {}
        print(f"  {AR} [PLAYER] ← {event}")

    try:
        # Connection
        await host_sio.connect(BASE, auth={"token": admin_token})
        await player_sio.connect(BASE, auth={"token": player_token})
        await asyncio.sleep(0.3)
        check("Host connected", host_sio.connected)
        check("Player connected", player_sio.connected)

        # JOIN_ROOM
        await host_sio.emit("join_room", {"room_code": room_code, "role": "HOST"})
        await asyncio.sleep(0.5)
        check(
            "Host received sync_state", "sync_state" in host_ev, f"got: {list(host_ev)}"
        )
        check(
            "sync_state status=LOBBY",
            host_ev.get("sync_state", {}).get("status") == "LOBBY",
            str(host_ev.get("sync_state")),
        )

        await player_sio.emit("join_room", {"room_code": room_code, "role": "PLAYER"})
        await asyncio.sleep(0.5)
        check("Player received sync_state", "sync_state" in player_ev)
        check("Host received player_joined", "player_joined" in host_ev)

        # HOST_ADVANCE → start game, Q1 broadcast
        host_ev.clear()
        player_ev.clear()
        await host_sio.emit("host_advance", {})
        await asyncio.sleep(0.5)
        check(
            "Both received new_question",
            "new_question" in host_ev and "new_question" in player_ev,
        )
        nq = host_ev.get("new_question", {})
        check(f"Correct question id ({q1_id})", nq.get("questionId") == q1_id)
        check("answer_data not in payload", "answer_data" not in nq)
        check("grading_type not in payload", "grading_type" not in nq)
        check("questionNumber=1", nq.get("questionNumber") == 1)
        check("totalQuestions=2", nq.get("totalQuestions") == 2)

        # SUBMIT_ANSWER (correct — option index 3 = "42")
        host_ev.clear()
        player_ev.clear()
        await player_sio.emit(
            "submit_answer",
            {
                "question_id": q1_id,
                "answer_data": {"selectedIndex": 3},
                "answer_time_ms": 4200,
            },
        )
        await asyncio.sleep(0.5)
        check("Player received answer_received", "answer_received" in player_ev)
        ar = player_ev.get("answer_received", {})
        check("isCorrect=True", ar.get("isCorrect") is True, str(ar))
        check("pointsAwarded=1000", ar.get("pointsAwarded") == 1000, str(ar))
        check("Host received answer_status", "answer_status" in host_ev)
        check(
            "Host received answer_phase_ended (all answered)",
            "answer_phase_ended" in host_ev,
        )

        # Double-submit (idempotent)
        player_ev.clear()
        await player_sio.emit(
            "submit_answer",
            {
                "question_id": q1_id,
                "answer_data": {"selectedIndex": 0},
                "answer_time_ms": 5000,
            },
        )
        await asyncio.sleep(0.3)
        re_ar = player_ev.get("answer_received", {})
        check(
            "Double-submit returns alreadyAnswered=True",
            re_ar.get("alreadyAnswered") is True,
            str(re_ar),
        )

        # HOST_ADVANCE → Q1 results
        host_ev.clear()
        player_ev.clear()
        await host_sio.emit("host_advance", {})
        await asyncio.sleep(0.5)
        check("Host received question_results", "question_results" in host_ev)
        check("Player received question_results", "question_results" in player_ev)
        host_qr = host_ev.get("question_results", {})
        player_qr = player_ev.get("question_results", {})
        check("Host sees answerReveal", "answerReveal" in host_qr, str(host_qr))
        check(
            "Host sees answerDistribution",
            "answerDistribution" in host_qr,
            str(host_qr),
        )
        check(
            "Player does NOT see answerDistribution (privacy)",
            "answerDistribution" not in player_qr,
        )
        check("Player yourRank=1", player_qr.get("yourRank") == 1, str(player_qr))
        check("answer_data not in results", "answer_data" not in host_qr)

        # HOST_ADVANCE → Q2 broadcast
        host_ev.clear()
        player_ev.clear()
        await host_sio.emit("host_advance", {})
        await asyncio.sleep(0.5)
        check(
            "Q2 broadcast", host_ev.get("new_question", {}).get("questionId") == q2_id
        )
        check(
            "questionNumber=2",
            host_ev.get("new_question", {}).get("questionNumber") == 2,
        )

        # SUBMIT_ANSWER Q2 (true = correct)
        host_ev.clear()
        player_ev.clear()
        await player_sio.emit(
            "submit_answer",
            {
                "question_id": q2_id,
                "answer_data": {"selectedValue": True},
                "answer_time_ms": 1800,
            },
        )
        await asyncio.sleep(0.5)
        check(
            "Q2 answer correct",
            player_ev.get("answer_received", {}).get("isCorrect") is True,
        )
        check(
            "Q2 totalScore=2000",
            player_ev.get("answer_received", {}).get("totalScore") == 2000,
        )

        # HOST_ADVANCE → Q2 results
        host_ev.clear()
        player_ev.clear()
        await host_sio.emit("host_advance", {})
        await asyncio.sleep(0.5)
        check("Q2 results received", "question_results" in host_ev)

        # HOST_ADVANCE → GAME_OVER
        host_ev.clear()
        player_ev.clear()
        await host_sio.emit("host_advance", {})
        await asyncio.sleep(0.5)
        check("Host received game_over", "game_over" in host_ev)
        check("Player received game_over", "game_over" in player_ev)
        go_h = host_ev.get("game_over", {})
        go_p = player_ev.get("game_over", {})
        check("Host sees anonymous scores list", "scores" in go_h, str(go_h))
        check("Host sees maxPossibleScore", "maxPossibleScore" in go_h, str(go_h))
        check("Player does NOT see scores list (privacy)", "scores" not in go_p)
        check(
            "Player yourFinalScore=2000", go_p.get("yourFinalScore") == 2000, str(go_p)
        )
        check("Player yourFinalRank=1", go_p.get("yourFinalRank") == 1, str(go_p))
        check(
            "Player questionSummary has 2 entries",
            len(go_p.get("questionSummary", [])) == 2,
            str(go_p),
        )

        # Disconnect
        await host_sio.disconnect()
        await player_sio.disconnect()
        ok("Both sockets disconnected cleanly")

    finally:
        if host_sio.connected:
            await host_sio.disconnect()
        if player_sio.connected:
            await player_sio.disconnect()

    # ── 6. Verify session in DB ──────────────────────────────────────────────
    print("\n6. Post-game HTTP verification")
    async with httpx.AsyncClient(timeout=10) as http:
        room_info = await api(http, "get", f"/game/rooms/{room_code}", admin_token)
        check(
            "Session status=COMPLETED in DB",
            room_info.get("status") == "COMPLETED",
            str(room_info),
        )

        # ── Cleanup ──────────────────────────────────────────────────────────
        print("\n7. Cleanup")
        if game_id:
            await api(
                http, "delete", f"/admin/games/{game_id}", admin_token, expect_204=True
            )
            ok(f"Test game deleted (id={game_id})")
        if player_id:
            await api(
                http,
                "delete",
                f"/admin/users/{player_id}",
                admin_token,
                expect_204=True,
            )
            ok(f"Test player deleted (id={player_id})")
        print("  (test course left in DB — no delete endpoint)")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'=' * 44}")
    if _failures == 0:
        print(f"  {G} All checks passed!")
    else:
        print(f"  {B} {_failures} check(s) failed — see above")
    print(f"{'=' * 44}\n")
    sys.exit(0 if _failures == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
