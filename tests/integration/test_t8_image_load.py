"""
T8 opt-in load and burst checks (docs/plans/t8-image-support.md §4.11). Runs only with
--image-load; print the measurements with -s:

    cd tests/integration && pytest test_t8_image_load.py --image-load -s

Load: 150 guest players in one room. The host starts a text-only baseline question, then a
multiple-choice question with 4 option images. On each `new_question` every player fetches the
question's small links concurrently (none for the baseline), then submits. Measured, for both:
image-fetch latency from `new_question` and its errors (non-200 or timeout), and answer-ack
latency (`submit_answer` → `answer_received`) and its errors (an `error` event, or no ack within
10 s). Acceptance: zero errors of either kind, and p95 ≤ 2 s for both.

Limits: everything runs from one machine against the development stack, so rate limits are
off and the socket manager is in-memory. It measures the backend and the database pool, not
phone networks. The image fetches use aiohttp: from one process httpx tops out near 100
requests/s at this concurrency and would measure itself (spec §4.11).

Burst: 6 concurrent 30 MP non-conforming uploads. Records status codes (201 or 503),
latencies, and the backend's peak memory sampled from `docker stats`. Observations only.
"""

from __future__ import annotations

import asyncio
import io
import statistics
import subprocess
import threading
import time
import uuid

import aiohttp
import httpx
import pytest
from PIL import Image

from .conftest import _REPO_ROOT, create_guest_tokens
from .engine.socket_client import TestSocketClient as SocketClient

PLAYERS = 150
ACK_TIMEOUT = 10.0
FETCH_TIMEOUT = 10.0
P95_CEILING = 2.0


@pytest.fixture(scope="module", autouse=True)
def image_load_enabled(request):
    if not request.config.getoption("--image-load"):
        pytest.skip("Opt-in: run with --image-load")


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def summary(label: str, values: list[float], errors: int) -> str:
    if not values:
        return f"{label}: no samples, {errors} errors"
    return (
        f"{label}: n={len(values)} p50={statistics.median(values):.3f}s "
        f"p95={percentile(values, 95):.3f}s max={max(values):.3f}s errors={errors}"
    )


def png(color: str) -> bytes:
    buffer = io.BytesIO()
    # 400 × 300 photos-worth of pixels: a realistic small copy, not a trivially tiny one.
    img = Image.effect_mandelbrot((400, 300), (-2.0, -1.2, 1.0, 1.2), 60 + len(color)).convert("RGB")
    img.save(buffer, "PNG")
    return buffer.getvalue()


def admin_client(base_url: str, admin_token: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url, timeout=60.0, headers={"Authorization": f"Bearer {admin_token}"}
    )


async def run_question(
    host: SocketClient,
    players: list[SocketClient],
    http: aiohttp.ClientSession,
    base_url: str,
) -> dict:
    """Advance to the next question; every player fetches its images, then answers."""
    fetch_times: list[float] = []
    fetch_errors = 0
    ack_times: list[float] = []
    ack_errors = 0

    async def play(client: SocketClient) -> None:
        nonlocal fetch_errors, ack_errors
        payload = await client.wait_for("new_question", timeout=30.0)
        received = time.perf_counter()
        urls = [entry["url"] for entry in payload.get("images", {}).values()]
        if urls:

            async def fetch(url: str) -> int:
                async with http.get(base_url + url, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)) as r:
                    await r.read()
                    return r.status

            try:
                statuses = await asyncio.gather(*(fetch(url) for url in urls))
                fetch_errors += sum(1 for status in statuses if status != 200)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                fetch_errors += len(urls)
            fetch_times.append(time.perf_counter() - received)
        sent = time.perf_counter()
        await client.emit(
            "submit_answer",
            {
                "question_id": payload["questionId"],
                "answer_data": {"selectedIndex": 0},
                "answer_time_ms": int((sent - received) * 1000),
            },
        )
        try:
            ack = await client.wait_for("answer_received", timeout=ACK_TIMEOUT)
            ack_times.append(time.perf_counter() - sent)
            if "questionId" not in ack:
                ack_errors += 1
        except asyncio.TimeoutError:
            ack_errors += 1
        if client._queues["error"].qsize():
            ack_errors += 1

    tasks = [asyncio.create_task(play(p)) for p in players]
    await host.emit("host_advance", {})
    await asyncio.gather(*tasks)
    return {
        "fetch": fetch_times,
        "fetch_errors": fetch_errors,
        "ack": ack_times,
        "ack_errors": ack_errors,
    }


async def test_load_150_players_fetch_option_images(base_url, admin_token):
    tag = uuid.uuid4().hex[:6]
    with admin_client(base_url, admin_token) as api:
        course = api.post(
            "/api/admin/courses", json={"name": f"T8 load {tag}", "semester": "T8"}
        ).json()["id"]
        game = api.post(
            "/api/admin/games",
            json={"title": f"T8 load {tag}", "course_id": course, "max_players": 500},
        ).json()["id"]
        image_ids = [
            api.post(
                f"/api/host/games/{game}/images",
                files={"file": (f"{c}.png", png(c), "image/png")},
                data={"description": f"Option {c}"},
            ).json()["id"]
            for c in ("red", "green", "blue", "gold")
        ]
        base = {"grading_type": "ACCURACY", "answer_data": {"answer_points": [1, 0, 0, 0]},
                "points_value": 1, "time_limit_seconds": 60, "type": "multiple_choice"}
        api.post(f"/api/host/games/{game}/questions",
                 json={**base, "prompt": "Baseline", "config": {"options": ["A", "B", "C", "D"]}})
        api.post(f"/api/host/games/{game}/questions",
                 json={**base, "prompt": "Images",
                       "config": {"options": ["", "", "", ""], "option_images": image_ids}})
        room = api.post("/api/game/rooms", json={"course_id": course, "game_id": game}).json()
        room_code, session_id = room["room_code"], room["session_id"]

    tokens = create_guest_tokens(base_url, room_code, PLAYERS)
    players = [SocketClient(base_url, t, f"p{i}") for i, t in enumerate(tokens)]
    host = SocketClient(base_url, admin_token, "host")
    try:
        await host.connect()
        await host.emit("join_room", {"room_code": room_code, "role": "HOST"})
        await host.wait_for("sync_state")
        for batch in range(0, PLAYERS, 25):
            group = players[batch : batch + 25]
            await asyncio.gather(*(p.connect() for p in group))
            for p in group:
                await p.emit("join_room", {"room_code": room_code, "role": "PLAYER"})
            await asyncio.gather(*(p.wait_for("sync_state", timeout=30.0) for p in group))

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as http:
            baseline = await run_question(host, players, http, base_url)
            await host.emit("host_advance", {})
            await host.wait_for("question_results", timeout=30.0)
            # Pace the next advance like a person. The gateway sends the host its results before
            # its loop of per-player emits and records the RESULTS phase only after that loop, so
            # an advance within those milliseconds would re-run results (an existing gateway
            # race, not T8's). Every phone having its results marks the loop's end.
            await asyncio.gather(*(p.wait_for("question_results", timeout=30.0) for p in players))
            await asyncio.sleep(0.5)
            images = await run_question(host, players, http, base_url)
    finally:
        for client in (host, *players):
            try:
                await client.disconnect()
            except Exception:
                pass
        with admin_client(base_url, admin_token) as api:
            api.delete(f"/api/game/sessions/{session_id}")
            api.delete(f"/api/admin/games/{game}")

    print()
    print(f"[T8 load] {PLAYERS} players, 4 option images of {len(png('red'))} bytes each")
    print("[T8 load] " + summary("baseline answer-ack", baseline["ack"], baseline["ack_errors"]))
    print("[T8 load] " + summary("image fetch (4 links)", images["fetch"], images["fetch_errors"]))
    print("[T8 load] " + summary("image-question answer-ack", images["ack"], images["ack_errors"]))

    assert baseline["ack_errors"] == 0
    assert images["fetch_errors"] == 0
    assert images["ack_errors"] == 0
    assert len(images["fetch"]) == PLAYERS
    assert percentile(images["fetch"], 95) <= P95_CEILING
    assert percentile(images["ack"], 95) <= P95_CEILING
    assert percentile(baseline["ack"], 95) <= P95_CEILING


# ---------------------------------------------------------------------------
# Burst: concurrent large uploads against the two processing slots
# ---------------------------------------------------------------------------


class MemorySampler:
    """The backend container's memory usage every 50 ms, as `docker stats` computes it
    (cgroup memory.current minus inactive file cache), streamed from inside the container.
    `docker stats --no-stream` itself takes about a second per reading — too coarse for a
    burst that lasts about that long."""

    SCRIPT = (
        "while :; do c=$(cat /sys/fs/cgroup/memory.current); "
        "i=$(awk '/^inactive_file /{print $2}' /sys/fs/cgroup/memory.stat); "
        "echo $((c - i)); sleep 0.05; done"
    )

    def __init__(self):
        self.samples: list[int] = []
        self._proc = subprocess.Popen(
            ["docker", "compose", "exec", "-T", "backend", "sh", "-c", self.SCRIPT],
            cwd=_REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if line.strip().isdigit():
                self.samples.append(int(line))

    def wait_for_samples(self, count: int = 3, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.samples) < count and time.monotonic() < deadline:
            time.sleep(0.05)

    def stop(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._thread.join(timeout=5)

    @staticmethod
    def mib(value: int) -> float:
        return value / (1024 * 1024)


def thirty_megapixel_jpeg(seed: int) -> bytes:
    """6000 × 5000 = 30 MP, a smooth gradient so it stays under the 10 MiB input limit.
    Non-conforming (longer than 1600 px), so the full normalization runs."""
    gradient = Image.linear_gradient("L").resize((6000, 5000))
    img = Image.merge("RGB", (gradient, gradient.rotate(seed * 7), gradient.transpose(0)))
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


async def test_burst_of_large_uploads(base_url, admin_token):
    tag = uuid.uuid4().hex[:6]
    with admin_client(base_url, admin_token) as api:
        course = api.post(
            "/api/admin/courses", json={"name": f"T8 burst {tag}", "semester": "T8"}
        ).json()["id"]
        game = api.post(
            "/api/admin/games", json={"title": f"T8 burst {tag}", "course_id": course}
        ).json()["id"]
    uploads = [thirty_megapixel_jpeg(seed) for seed in range(6)]
    sampler = MemorySampler()
    sampler.wait_for_samples()
    before = list(sampler.samples)
    results: list[tuple[int, float]] = []
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as http:

            async def upload(n: int, data: bytes) -> None:
                started = time.perf_counter()
                r = await http.post(
                    f"/api/host/games/{game}/images",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    files={"file": (f"big{n}.jpg", data, "image/jpeg")},
                    data={"description": f"30 MP upload {n}"},
                )
                results.append((r.status_code, time.perf_counter() - started))

            await asyncio.gather(*(upload(n, data) for n, data in enumerate(uploads)))
            await asyncio.sleep(0.5)  # a few samples after the last response
    finally:
        sampler.stop()
        with admin_client(base_url, admin_token) as api:
            api.delete(f"/api/admin/games/{game}")

    print()
    print(
        f"[T8 burst] 6 × 30 MP JPEG uploads of {min(map(len, uploads))}–{max(map(len, uploads))} bytes"
    )
    for status, seconds in sorted(results, key=lambda r: r[1]):
        print(f"[T8 burst]   {status} in {seconds:.2f}s")
    if sampler.samples:
        print(
            f"[T8 burst] backend memory: {MemorySampler.mib(max(before or sampler.samples[:1])):.0f} MiB "
            f"before, peak {MemorySampler.mib(max(sampler.samples)):.0f} MiB "
            f"({len(sampler.samples)} samples at 50 ms)"
        )
    else:
        print("[T8 burst] backend memory: no samples (docker compose exec unavailable)")
    assert all(status in (201, 503) for status, _ in results), results
