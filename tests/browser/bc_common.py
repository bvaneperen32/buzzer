"""Shared helpers for the T7 browser checks (docs/plans/t7-question-types.md §4.6).

Everything goes through the production builds served by nginx (`npm run build`, then the
compose stack): the UI and the API alike. Every browser context pins `locale: 'en-US'`, so the
expected number strings are deterministic; the apps themselves keep the viewer's locale.

Configuration comes from the environment (see README.md); no credentials are stored here.
Setup uses the REST API as the admin; the checks use the real UI.

Results are printed and appended to `<output>/results.jsonl`:
- PASS / FAIL — a T7 behaviour;
- XFAIL — a known pre-existing bug reproduced (recorded in the spec's §11, not T7's to fix);
- XPASS — a known bug did *not* reproduce: it may have been fixed, so its entry, and the
  `rejoined()` synchronization below, need revisiting.
A run exits non-zero on any FAIL.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import pathlib
import re
import sys
import time
import uuid

import httpx
from PIL import Image

TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
from integration.conftest import read_repo_env  # noqa: E402

BASE = os.environ.get("BUZZER_BROWSER_BASE_URL", "http://localhost:8080").rstrip("/")
CHANNEL = os.environ.get("BUZZER_BROWSER_CHANNEL", "chrome") or None
HEADED = os.environ.get("BUZZER_BROWSER_HEADED", "") not in ("", "0", "false")
OUTPUT = pathlib.Path(os.environ.get("BUZZER_BROWSER_OUTPUT", pathlib.Path(__file__).parent / "output"))
SHOTS = OUTPUT / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
RESULTS = OUTPUT / "results.jsonl"

PHONES = {
    "390x844@3": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3},
    "360x640@2": {"viewport": {"width": 360, "height": 640}, "device_scale_factor": 2},
    "640x360@1": {"viewport": {"width": 640, "height": 360}, "device_scale_factor": 1},
}


def admin_credentials() -> tuple[str, str]:
    """BUZZER_ADMIN_USERNAME / BUZZER_ADMIN_PASSWORD, else ADMIN_USERNAME / ADMIN_PASSWORD from
    the environment, else from the repo's .env (as the integration suite reads them)."""
    env = read_repo_env()
    username = (
        os.environ.get("BUZZER_ADMIN_USERNAME")
        or os.environ.get("ADMIN_USERNAME")
        or env.get("ADMIN_USERNAME")
    )
    password = (
        os.environ.get("BUZZER_ADMIN_PASSWORD")
        or os.environ.get("ADMIN_PASSWORD")
        or env.get("ADMIN_PASSWORD")
    )
    if not username or not password:
        sys.exit("Set BUZZER_ADMIN_USERNAME and BUZZER_ADMIN_PASSWORD (or ADMIN_* in .env).")
    return username, password


async def launch(playwright):
    """Chrome by default (BUZZER_BROWSER_CHANNEL=chrome); an empty channel uses Playwright's
    bundled Chromium (`python -m playwright install chromium`)."""
    return await playwright.chromium.launch(channel=CHANNEL, headless=not HEADED)


class Checks:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.items: list[tuple[str, str, str]] = []

    def _record(self, name: str, status: str, detail: object) -> None:
        self.items.append((name, status, str(detail)))
        shown = f" — {detail}" if detail and status in ("FAIL", "XPASS", "XFAIL") else ""
        print(f"{status:5} [{self.scenario}] {name}{shown}", flush=True)
        with RESULTS.open("a") as f:
            f.write(json.dumps({"scenario": self.scenario, "check": name, "status": status,
                                "detail": str(detail)}) + "\n")

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        self._record(name, "PASS" if ok else "FAIL", detail)
        return bool(ok)

    def known_issue(self, bug: str, name: str, reproduced: bool, detail: object = "") -> None:
        """A pre-existing bug (spec §11) checked directly: XFAIL when it reproduces."""
        self._record(f"[{bug}] {name}", "XFAIL" if reproduced else "XPASS", detail)

    def summary(self) -> int:
        count = {s: sum(1 for _, st, _ in self.items if st == s) for s in ("PASS", "FAIL", "XFAIL", "XPASS")}
        print(f"\n[{self.scenario}] {count['PASS']} passed, {count['FAIL']} failed, "
              f"{count['XFAIL']} known issues reproduced, {count['XPASS']} known issues not reproduced")
        for name, status, _ in self.items:
            if status in ("FAIL", "XPASS"):
                print(f"  {status}: {name}")
        return 1 if count["FAIL"] else 0


# ---------------------------------------------------------------------------
# Socket observation
# ---------------------------------------------------------------------------


class SocketLog:
    """The socket.io WebSockets a page has opened: when each opened, whether it has re-joined
    (received `sync_state`), and the events it received."""

    def __init__(self):
        self.sockets: list[dict] = []

    def _new(self) -> dict:
        record = {"opened": time.monotonic(), "synced": False, "events": []}
        self.sockets.append(record)
        return record

    def _frame(self, record: dict, payload) -> None:
        text = payload if isinstance(payload, str) else ""
        match = re.match(r'^42\["([a-z_]+)",?(.*)\]$', text, re.S)
        if not match:
            return
        event, raw = match.groups()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        record["events"].append((event, data))
        if event == "sync_state":
            record["synced"] = True

    def events(self, name: str) -> list:
        return [data for s in self.sockets for event, data in s["events"] if event == name]

    async def rejoined(self, page, since: float, grace: float = 3.0) -> None:
        """Wait until a socket opened after `since` has re-joined its room.

        Needed only because of the pre-existing socket-recreation bug (spec §11, B1): each
        navigation of the host or player app opens a new socket, and anything emitted before it
        has re-joined is dropped by the server. This waits for that event rather than a fixed
        delay; if no new socket opens within `grace` seconds (the bug fixed), it returns — and
        the B1 known-issue check then reports XPASS.
        """
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            fresh = [s for s in self.sockets if s["opened"] >= since]
            if any(s["synced"] for s in fresh):
                return
            if not fresh and time.monotonic() - since > grace:
                return
            await page.wait_for_timeout(20)
        raise AssertionError("A new socket opened but never re-joined its room")


class SocketWatch(SocketLog):
    """Observes a page's sockets (page.on('websocket')); register before the first navigation."""

    def __init__(self, page):
        super().__init__()
        page.on("websocket", self._on_socket)

    def _on_socket(self, ws) -> None:
        if "/socket.io/" not in ws.url:
            return
        record = self._new()
        ws.on("framereceived", lambda payload: self._frame(record, payload))


class SocketProxy(SocketLog):
    """Routes a page's socket.io WebSockets through Playwright, so a check can drop the live
    connection — as a network loss would — without reloading the page. socket.io then
    reconnects on its own and re-sends `join_room`."""

    def __init__(self):
        super().__init__()
        self.routes: list[tuple] = []

    async def attach(self, page) -> None:
        await page.route_web_socket(re.compile(r"/socket\.io/"), self._handle)

    def _handle(self, ws) -> None:
        server = ws.connect_to_server()
        record = self._new()

        def from_server(message):
            self._frame(record, message)
            ws.send(message)

        server.on_message(from_server)  # page → server keeps flowing on its own
        self.routes.append((ws, server))

    async def drop(self) -> float:
        """Close the current connection on both sides; returns when that happened."""
        ws, server = self.routes[-1]
        dropped = time.monotonic()
        await ws.close(code=4000, reason="dropped by the browser check")
        closing = server.close()  # the server-side route's close isn't awaitable in every version
        if inspect.isawaitable(closing):
            await closing
        return dropped


# ---------------------------------------------------------------------------
# Images and API setup
# ---------------------------------------------------------------------------


def png(color: str, size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def scene(size: tuple[int, int], seed: int = 1) -> bytes:
    """An image with visible structure (a checkerboard), so screenshots are readable."""
    w, h = size
    img = Image.new("RGB", size, (30, 60, 90))
    px = img.load()
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            if ((x // 80) + (y // 80) + seed) % 2 == 0:
                for dy in range(4):
                    for dx in range(4):
                        if x + dx < w and y + dy < h:
                            px[x + dx, y + dy] = (200, 160, 60)
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def file_payload(name: str, data: bytes) -> dict:
    """A file for set_input_files / FileChooser.set_files, straight from memory."""
    return {"name": name, "mimeType": "image/png", "buffer": data}


class Api:
    """REST setup through nginx, as the admin. Everything it creates is deleted by cleanup()."""

    def __init__(self):
        username, password = admin_credentials()
        self.http = httpx.Client(base_url=BASE, timeout=120.0)
        r = self.http.post("/api/auth/login", json={"username": username, "password": password})
        r.raise_for_status()
        self.token = r.json()["access_token"]
        self.tag = uuid.uuid4().hex[:6]
        self.game_ids: list[int] = []
        self.session_ids: list[str] = []

    def req(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, path, headers={"Authorization": f"Bearer {self.token}"}, **kw)

    def ok(self, method: str, path: str, status: int = 200, **kw) -> dict:
        r = self.req(method, path, **kw)
        assert r.status_code == status, f"{method} {path}: {r.status_code} {r.text}"
        return r.json() if r.content else {}

    def setup_game(self, title: str) -> tuple[int, int]:
        course = self.ok("POST", "/api/admin/courses", 201, json={"name": f"BC {title} {self.tag}", "semester": "T7 checks"})
        game = self.ok("POST", "/api/admin/games", 201, json={"title": f"{title} {self.tag}", "course_id": course["id"]})
        self.game_ids.append(game["id"])
        return course["id"], game["id"]

    def upload(self, game_id: int, data: bytes, description: str, filename: str = "image.png") -> dict:
        r = self.req("POST", f"/api/host/games/{game_id}/images",
                     files={"file": (filename, data, "image/png")}, data={"description": description})
        assert r.status_code == 201, r.text
        return r.json()

    def replace(self, game_id: int, image_id: int, data: bytes, filename: str = "new.png") -> dict:
        r = self.req("PUT", f"/api/host/games/{game_id}/images/{image_id}", files={"file": (filename, data, "image/png")})
        assert r.status_code == 200, r.text
        return r.json()

    def question(self, game_id: int, body: dict) -> dict:
        return self.ok("POST", f"/api/host/games/{game_id}/questions", 201, json=body)

    def questions(self, game_id: int) -> list[dict]:
        return self.ok("GET", f"/api/host/games/{game_id}/questions")

    def get_question(self, game_id: int, qid: int) -> dict:
        return next(q for q in self.questions(game_id) if q["id"] == qid)

    def room(self, course_id: int, game_id: int) -> tuple[str, str]:
        data = self.ok("POST", "/api/game/rooms", 201, json={"course_id": course_id, "game_id": game_id})
        self.session_ids.append(data["session_id"])
        return data["room_code"], data["session_id"]

    def guest(self, room_code: str, name: str) -> str:
        r = self.http.post("/api/auth/guest", json={
            "display_name": name, "email": f"{name.lower()}.{uuid.uuid4().hex[:6]}@example.com", "room_code": room_code})
        r.raise_for_status()
        return r.json()["access_token"]

    def report(self, session_id: str) -> str:
        r = self.req("GET", f"/api/host/sessions/{session_id}/report")
        assert r.status_code == 200, r.text
        return r.text

    def cleanup(self) -> None:
        for session_id in self.session_ids:
            self.req("DELETE", f"/api/game/sessions/{session_id}")
        for game_id in self.game_ids:
            self.req("DELETE", f"/api/admin/games/{game_id}")


def stamp(image_id: int, version: int, cols: int, rows: int) -> dict:
    return {"image_id": image_id, "image_version": version, "cols": cols, "rows": rows}


def cell(col: int, row: int) -> dict:
    return {"col": col, "row": row}


def spot_body(image_id: int, cols: int, rows: int, cells=None, *, grading="ACCURACY", points=1000,
              confirm=None, prompt="Find the lighthouse", time_limit=120) -> dict:
    answer_data: dict = {}
    if cells is not None:
        answer_data["accepted_cells"] = cells
    if confirm is not None:
        answer_data["confirmed_for"] = confirm
    return {
        "type": "find_in_image", "grading_type": grading, "prompt": prompt,
        "config": {"image_id": image_id, "grid": {"cols": cols, "rows": rows}},
        "answer_data": answer_data, "points_value": points, "time_limit_seconds": time_limit,
    }


def sort_body(boxes, cards, key, *, grading="ACCURACY", points=1000, prompt="Sort these", time_limit=120) -> dict:
    return {
        "type": "sort_into_boxes", "grading_type": grading, "prompt": prompt,
        "config": {"boxes": boxes, "cards": cards},
        "answer_data": {} if key is None else {"correct_boxes": key},
        "points_value": points, "time_limit_seconds": time_limit,
    }


# ---------------------------------------------------------------------------
# Browser contexts and canvases
# ---------------------------------------------------------------------------

# Counts canvas redraws: every draw of a Spot It canvas starts with one clearRect.
COUNT_DRAWS = """
window.__canvasDraws = 0;
const __clearRect = CanvasRenderingContext2D.prototype.clearRect;
CanvasRenderingContext2D.prototype.clearRect = function (...args) {
  window.__canvasDraws += 1;
  return __clearRect.apply(this, args);
};
"""


async def new_context(browser, token: str, *, phone: str | None = None, desktop=(1440, 900), count_draws=False):
    """A browser context signed in with `token`. The apps share one origin and one localStorage
    key, so each signed-in identity needs its own context."""
    if phone:
        options = dict(PHONES[phone], is_mobile=True, has_touch=True)
    else:
        options = {"viewport": {"width": desktop[0], "height": desktop[1]}}
    context = await browser.new_context(locale="en-US", **options)
    await context.add_init_script(f"window.localStorage.setItem('token', {json.dumps(token)});")
    if count_draws:
        await context.add_init_script(COUNT_DRAWS)
    return context


async def grid_rect(locator) -> tuple[float, float, float, float]:
    raw = await locator.get_attribute("data-grid-rect")
    x, y, w, h = (float(v) for v in raw.split(","))
    return x, y, w, h


async def cell_point(locator, col: int, row: int) -> tuple[float, float]:
    """The page coordinates of a cell's centre on a canvas that exposes data-grid-*."""
    box = await locator.bounding_box()
    x, y, w, h = await grid_rect(locator)
    cols = int(await locator.get_attribute("data-grid-cols"))
    rows = int(await locator.get_attribute("data-grid-rows"))
    return box["x"] + x + (col + 0.5) * w / cols, box["y"] + y + (row + 0.5) * h / rows


def aspect_ok(w: float, h: float, width: int, height: int) -> bool:
    """The grid rectangle has the image's aspect within 1 px."""
    return abs(w - h * width / height) <= 1.0 and abs(h - w * height / width) <= 1.0


def text_squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def now() -> float:
    return time.monotonic()


async def until(page, check, *, timeout: float = 10.0, what: str = "the condition"):
    """Poll `check()` (sync or async) until it is truthy, and return that value."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out after {timeout}s waiting for {what}")
        await page.wait_for_timeout(50)


class Host:
    """The projector page. Each click on a control that navigates (Start, Show Results, Next,
    Show Final Results) re-creates the host socket (pre-existing bug B1), so the next click
    waits until that socket has re-joined — an event, not a fixed delay."""

    def __init__(self, page):
        self.page = page
        self.sockets = SocketWatch(page)
        self.last_navigation: float | None = None

    async def click(self, name: str, *, navigates: bool = True) -> float:
        if self.last_navigation is not None:
            await self.sockets.rejoined(self.page, self.last_navigation)
        since = now()
        await self.page.get_by_role("button", name=name).click()
        if navigates:
            self.last_navigation = since
        return since
