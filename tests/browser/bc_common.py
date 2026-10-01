"""Shared helpers for the T7 browser checks (docs/plans/t7-question-types.md §4.6).

Everything goes through the production builds served by nginx (`npm run build`, then the
compose stack): the UI and the API alike. Every browser context pins `locale: 'en-US'`, so the
expected number strings are deterministic; the apps themselves keep the viewer's locale.

Configuration comes from the environment (see README.md); no credentials are stored here.
Setup uses the REST API as the admin; the checks use the real UI. Everything setup creates is
recorded in the run's ownership ledger (tests/ledger.py) and deleted when the run ends.

Results are printed and appended to `<output>/results.jsonl`, one PASS or FAIL per check. A run
exits non-zero on any FAIL.
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

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))  # the `tests` package: its ledger and the integration helpers
from tests.ledger import (  # noqa: E402
    Ledger, admin_login, ledger_dir, teardown, token_subject,
)

BASE = os.environ.get("BUZZER_BROWSER_BASE_URL", "http://localhost:8080").rstrip("/")
CHANNEL = os.environ.get("BUZZER_BROWSER_CHANNEL", "chrome") or None
HEADED = os.environ.get("BUZZER_BROWSER_HEADED", "") not in ("", "0", "false")
OUTPUT = pathlib.Path(os.environ.get("BUZZER_BROWSER_OUTPUT", pathlib.Path(__file__).parent / "output"))
SHOTS = OUTPUT / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
RESULTS = OUTPUT / "results.jsonl"
LEDGERS = ledger_dir()

# The semester of every course the checks create ("BC <title> <tag>").
SEMESTER = "T7 checks"

PHONES = {
    "390x844@3": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3},
    "360x640@2": {"viewport": {"width": 360, "height": 640}, "device_scale_factor": 2},
    "640x360@1": {"viewport": {"width": 640, "height": 360}, "device_scale_factor": 1},
}


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
        shown = f" — {detail}" if detail and status == "FAIL" else ""
        print(f"{status:5} [{self.scenario}] {name}{shown}", flush=True)
        with RESULTS.open("a") as f:
            f.write(json.dumps({"scenario": self.scenario, "check": name, "status": status,
                                "detail": str(detail)}) + "\n")

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        self._record(name, "PASS" if ok else "FAIL", detail)
        return bool(ok)

    def summary(self) -> int:
        count = {s: sum(1 for _, st, _ in self.items if st == s) for s in ("PASS", "FAIL")}
        print(f"\n[{self.scenario}] {count['PASS']} passed, {count['FAIL']} failed")
        for name, status, _ in self.items:
            if status == "FAIL":
                print(f"  {status}: {name}")
        return 1 if count["FAIL"] else 0


# ---------------------------------------------------------------------------
# Socket observation
# ---------------------------------------------------------------------------


class SocketLog:
    """The socket.io WebSockets a page has opened: when each opened, whether it has re-joined
    (received `sync_state`), the events it received and when, and — through a SocketProxy —
    the events the page sent and when."""

    def __init__(self):
        self.sockets: list[dict] = []

    def _new(self) -> dict:
        record = {"opened": time.monotonic(), "synced": False, "events": [], "arrived": [],
                  "sent": []}
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
        record["arrived"].append((time.monotonic(), event))
        if event == "sync_state":
            record["synced"] = True

    def _sent(self, record: dict, payload) -> None:
        match = re.match(r'^42\["([a-z_]+)"', payload) if isinstance(payload, str) else None
        if match:
            record["sent"].append((time.monotonic(), match.group(1)))

    def events(self, name: str) -> list:
        return [data for s in self.sockets for event, data in s["events"] if event == name]

    def arrived(self, name: str) -> list[float]:
        """When each `name` event reached the page, over all its sockets."""
        return sorted(at for s in self.sockets for at, event in s["arrived"] if event == name)

    def sent(self, name: str) -> list[float]:
        """When the page sent each `name` event, over all its sockets (SocketProxy only)."""
        return sorted(at for s in self.sockets for at, event in s["sent"] if event == name)

    async def rejoined(self, page, since: float, timeout: float = 15.0) -> None:
        """Wait until a socket opened after `since` — a real reconnect — has re-joined its room
        (received `sync_state`). Navigation never opens one (B1 is fixed), so this is for
        dropped connections only."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(s["synced"] for s in self.sockets if s["opened"] >= since):
                return
            await page.wait_for_timeout(20)
        raise AssertionError(f"No socket re-joined its room within {timeout}s")


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
    reconnects on its own and re-sends `join_room`. A drop can also keep the page offline
    (the browser's own offline mode, so every reconnection attempt fails at once) until
    `release()`. `withhold()` holds back chosen server events from the page — as if they were
    still on their way — until `deliver_withheld()`. Every event the page sends is recorded
    (`sent()`) on its way through."""

    def __init__(self):
        super().__init__()
        self.routes: list[tuple] = []
        self.page = None
        self.withheld_events: set[str] = set()
        self.withheld: list[tuple] = []

    async def attach(self, page) -> None:
        self.page = page
        await page.route_web_socket(re.compile(r"/socket\.io/"), self._handle)

    def _handle(self, ws) -> None:
        server = ws.connect_to_server()
        record = self._new()

        def from_server(message):
            event = re.match(r'^42\["([a-z_]+)"', message) if isinstance(message, str) else None
            if event and event.group(1) in self.withheld_events:
                self.withheld.append((ws, record, message))
                return
            self._frame(record, message)
            ws.send(message)

        def from_page(message):  # with a handler set, forwarding is ours to do
            self._sent(record, message)
            server.send(message)

        server.on_message(from_server)
        ws.on_message(from_page)
        self.routes.append((ws, server))

    async def drop(self, *, hold: bool = False) -> float:
        """Close the current connection on both sides; returns when that happened. With
        `hold`, the page stays offline until `release()`."""
        if hold:
            await self.page.context.set_offline(True)
        ws, server = self.routes[-1]
        dropped = time.monotonic()
        await ws.close(code=4000, reason="dropped by the browser check")
        closing = server.close()  # the server-side route's close isn't awaitable in every version
        if inspect.isawaitable(closing):
            await closing
        return dropped

    async def release(self) -> float:
        """Back online: the next reconnection attempt goes through. Returns when."""
        await self.page.context.set_offline(False)
        return time.monotonic()

    def withhold(self, *events: str) -> None:
        self.withheld_events = set(events)

    def deliver_withheld(self) -> None:
        """Stop withholding, and deliver what was held back, in order."""
        self.withheld_events = set()
        for ws, record, message in self.withheld:
            self._frame(record, message)
            ws.send(message)
        self.withheld.clear()

    def attempts_since(self, since: float) -> int:
        """Connection attempts the page made after `since` (failed ones included)."""
        return sum(1 for s in self.sockets if s["opened"] >= since)

    def send_as_page(self, event: str, data) -> None:
        """Send an event to the server on the page's current connection, as the page's own
        code would (the server can't tell the difference)."""
        _ws, server = self.routes[-1]
        server.send("42" + json.dumps([event, data]))


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
    """REST setup through nginx, as the admin. Everything it creates goes into the run's
    ownership ledger the moment it exists, and cleanup() deletes exactly that."""

    def __init__(self, ledger: Ledger | None = None):
        self.http = httpx.Client(base_url=BASE, timeout=120.0)
        self.token = admin_login(self.http)
        self.tag = uuid.uuid4().hex[:6]
        self.ledger = ledger or Ledger.begin(LEDGERS, pathlib.Path(sys.argv[0]).stem, base_url=BASE)

    def req(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, path, headers={"Authorization": f"Bearer {self.token}"}, **kw)

    def ok(self, method: str, path: str, status: int = 200, **kw) -> dict:
        r = self.req(method, path, **kw)
        assert r.status_code == status, f"{method} {path}: {r.status_code} {r.text}"
        return r.json() if r.content else {}

    def course(self, name: str, semester: str = SEMESTER) -> int:
        course = self.ok("POST", "/api/admin/courses", 201, json={"name": name, "semester": semester})
        self.ledger.record("courses", id=course["id"], name=course["name"], semester=course["semester"])
        return course["id"]

    def game(self, course_id: int, title: str, **extra) -> int:
        game = self.ok("POST", "/api/admin/games", 201, json={"title": title, "course_id": course_id, **extra})
        self.ledger.record("games", id=game["id"], title=game["title"])
        return game["id"]

    def setup_game(self, title: str) -> tuple[int, int]:
        course_id = self.course(f"BC {title} {self.tag}")
        return course_id, self.game(course_id, f"{title} {self.tag}")

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
        self.ledger.record("sessions", id=data["session_id"], room_code=data["room_code"])
        return data["room_code"], data["session_id"]

    def guest(self, room_code: str, name: str) -> str:
        # A minted email (tests/ledger.py): no account can already have it, so the guest the
        # server returns is this run's own, and only that makes it deletable.
        email = self.ledger.mint_guest_email(name)
        r = self.http.post("/api/auth/guest", json={"display_name": name, "email": email, "room_code": room_code})
        r.raise_for_status()
        token = r.json()["access_token"]
        self.ledger.record("guests", id=token_subject(token), email=email, owned=True)
        return token

    def report(self, session_id: str) -> str:
        r = self.req("GET", f"/api/host/sessions/{session_id}/report")
        assert r.status_code == 200, r.text
        return r.text

    def cleanup(self) -> list[str]:
        """Delete everything this run recorded. Anything that couldn't be deleted stays in the
        ledger, for `tests/cleanup.py`, and is printed."""
        problems = teardown(self.ledger, self.http, self.token)
        if problems:
            print(f"\nCleanup kept {len(problems)} record(s); ledger: {self.ledger.path}")
            for problem in problems:
                print(f"  {problem}")
        return problems


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
    """The projector page, with its sockets observed. A click never waits for a re-join: the
    projector keeps one socket across its screens."""

    def __init__(self, page):
        self.page = page
        self.sockets = SocketWatch(page)

    async def click(self, name: str) -> None:
        await self.page.get_by_role("button", name=name).click()
