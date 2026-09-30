"""Spot It browser checks (spec §4.6): play end to end on the projector and three phones —
coordinates, aspect, taps, nudges, rotation, keyboard, reveal, heatmap, phone texts, game over,
image isolation, the projector's image failure and Retry, auto-advance; a socket drop and
reconnect that must keep the selection; redraws; and the two known pre-existing bugs.

    cd tests/browser && python bc_spot.py
"""

from __future__ import annotations

import asyncio
import re
import sys

from playwright.async_api import async_playwright, expect

from bc_common import (
    BASE, SHOTS, Api, Checks, SocketProxy, SocketWatch, aspect_ok, cell, cell_point, grid_rect,
    launch, new_context, now, scene, spot_body, stamp, text_squash, until,
)

C = Checks("spot")


class Phone:
    """A player on a phone-sized context, with its sockets observed (or proxied)."""

    def __init__(self, page, sockets, image_requests):
        self.page, self.sockets, self.image_requests = page, sockets, image_requests

    async def ready(self, since: float) -> None:
        await self.sockets.rejoined(self.page, since)


async def join_phone(browser, api, code, name, shape, *, proxy=False, count_draws=False) -> Phone:
    context = await new_context(browser, api.guest(code, name), phone=shape, count_draws=count_draws)
    page = await context.new_page()
    if proxy:
        sockets = SocketProxy()
        await sockets.attach(page)
    else:
        sockets = SocketWatch(page)
    image_requests: list[str] = []
    page.on("request", lambda r: image_requests.append(r.url) if "/api/images/" in r.url else None)
    await page.goto(f"{BASE}/player/game/{code}/lobby")
    await expect(page.get_by_text(re.compile("Waiting for host to start"))).to_be_visible(timeout=15000)
    return Phone(page, sockets, image_requests)


class Projector:
    def __init__(self, page, sockets):
        self.page, self.sockets = page, sockets

    async def click(self, name: str) -> float:
        """Click a host control; returns when it was clicked (for the rejoin waits)."""
        since = now()
        await self.page.get_by_role("button", name=name).click()
        return since


async def open_projector(browser, api, code) -> Projector:
    context = await new_context(browser, api.token)
    page = await context.new_page()
    sockets = SocketWatch(page)
    await page.goto(f"{BASE}/host/game/{code}/lobby")
    await expect(page.get_by_text(code).first).to_be_visible(timeout=15000)
    return Projector(page, sockets)


async def no_hscroll(page) -> bool:
    return await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


async def selected_label(page) -> str:
    return await page.locator("[data-selected]").get_attribute("data-selected")


async def _changed(canvas, before):
    rect = await grid_rect(canvas)
    return rect if rect != before else None


async def draws(page) -> int:
    return await page.evaluate("window.__canvasDraws")


async def main_play(browser, api: Api):
    course_id, game_id = api.setup_game("Spot It play")
    L = api.upload(game_id, scene((1600, 900)), "Harbour at dusk")["id"]
    P = api.upload(game_id, scene((900, 1600), 2), "Lighthouse tower")["id"]
    api.question(game_id, spot_body(L, 6, 4, [cell(4, 1), cell(4, 2)], confirm=stamp(L, 1, 6, 4), prompt="Find the boat"))
    api.question(game_id, spot_body(L, 6, 4, None, grading="COMPLETENESS", confirm=stamp(L, 1, 6, 4), prompt="Where would you swim?"))
    api.question(game_id, spot_body(P, 6, 10, [cell(1, 1)], confirm=stamp(P, 1, 6, 10), prompt="Find the window"))
    code, _ = api.room(course_id, game_id)

    proj = await open_projector(browser, api, code)
    projector = proj.page
    A = await join_phone(browser, api, code, "Ada", "390x844@3", count_draws=True)
    B = await join_phone(browser, api, code, "Bo", "360x640@2")
    Cy = await join_phone(browser, api, code, "Cy", "640x360@1")
    a, b, c = A.page, B.page, Cy.page
    await expect(projector.get_by_text("3 players joined")).to_be_visible(timeout=15000)
    since = await proj.click("Start Game")

    # ---- Q1: landscape 6×4, ACCURACY ----
    canvas = projector.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    _, _, w, h = await grid_rect(canvas)
    C.check("projector grid rect has the image's aspect (1600×900, ±1 px)", aspect_ok(w, h, 1600, 900), (w, h))
    for name, phone in (("390x844@3", A), ("360x640@2", B), ("640x360@1", Cy)):
        phone_canvas = phone.page.locator("canvas[data-grid-rect]")
        await expect(phone_canvas).to_be_visible(timeout=15000)
        await phone.ready(since)
        _, _, pw, ph = await grid_rect(phone_canvas)
        C.check(f"phone {name} grid rect has the image's aspect (±1 px)", aspect_ok(pw, ph, 1600, 900), (pw, ph))
    C.check("no horizontal scroll at 360 px (question)", await no_hscroll(b))

    # Corner and interior taps select exactly that cell.
    a_canvas = a.locator("canvas[data-grid-rect]")
    for (col, row), label in {(0, 0): "A1", (5, 0): "F1", (0, 3): "A4", (5, 3): "F4", (2, 1): "C2"}.items():
        x, y = await cell_point(a_canvas, col, row)
        await a.touchscreen.tap(x, y)
        C.check(f"tap at the centre of {label} selects {label}", await selected_label(a) == label, await selected_label(a))
    box = await a_canvas.bounding_box()
    await a.touchscreen.tap(box["x"] + 5, box["y"] + 5)
    C.check("a header-band tap changes nothing", await selected_label(a) == "C2")

    # Nudge buttons reach every cell in landscape.
    reached = set()
    await c.get_by_role("button", name="Move right").click()  # from none → A1
    reached.add(await selected_label(c))
    for row in range(4):
        direction = "Move right" if row % 2 == 0 else "Move left"
        for _ in range(5):
            await c.get_by_role("button", name=direction).click()
            reached.add(await selected_label(c))
        if row < 3:
            await c.get_by_role("button", name="Move down").click()
            reached.add(await selected_label(c))
    C.check("nudge buttons reach every cell in landscape (640×360)", len(reached) == 24, sorted(reached))

    # Rotation keeps the selection.
    b_canvas = b.locator("canvas[data-grid-rect]")
    x, y = await cell_point(b_canvas, 1, 2)
    await b.touchscreen.tap(x, y)
    before = await grid_rect(b_canvas)
    await b.set_viewport_size({"width": 640, "height": 360})
    after = await until(b, lambda: _changed(b_canvas, before), what="the rotated layout")
    C.check("rotation keeps the selection (B3)", await selected_label(b) == "B3", await selected_label(b))
    C.check("rotation re-lays out the canvas", before != after, (before, after))
    await b.set_viewport_size({"width": 360, "height": 640})

    # A locks in E3 (correct), B locks in A1 (incorrect), C never locks in.
    x, y = await cell_point(a_canvas, 4, 2)
    await a.touchscreen.tap(x, y)
    await a.get_by_role("button", name="Lock in E3").click()
    await expect(a.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    x, y = await cell_point(b_canvas, 0, 0)
    await b.touchscreen.tap(x, y)
    await b.get_by_role("button", name="Lock in A1").click()
    await expect(b.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)

    C.check("nothing revealed before Show Results (no legend on the projector)",
            await projector.get_by_text("blank = no answers").count() == 0)
    # A reloads between answering and results: its results must come from the payload alone.
    await a.reload()
    await expect(a.get_by_text("Waiting for next question…").or_(a.get_by_text("Answer locked in!"))).to_be_visible(timeout=10000)

    since = await proj.click("Show Results")
    await expect(projector.get_by_text("blank = no answers")).to_be_visible(timeout=10000)
    legend = text_squash(await projector.locator("text=blank = no answers").locator("..").inner_text())
    C.check("heatmap legend: N of M answered", "2 of 3 answered" in legend, legend)
    C.check("heatmap legend: Correct key (ACCURACY)", "Correct" in legend, legend)
    await projector.screenshot(path=str(SHOTS / "spot_projector_results.png"))

    a_text = text_squash(await a.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("phone (reloaded before results): You picked E3 — Correct", "You picked E3 — Correct" in a_text, a_text)
    C.check("phone: correct cells row-major (E2, E3)", "Correct cells: E2, E3" in a_text, a_text)
    C.check("phone: 1,000 of 1,000 points", "1,000 of 1,000 points" in a_text, a_text)
    b_text = text_squash(await b.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("phone: You picked A1 — Incorrect, 0 of 1,000 points",
            "You picked A1 — Incorrect" in b_text and "0 of 1,000 points" in b_text, b_text)
    c_text = text_squash(await c.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("phone never locked in: No answer, correct cells, 0 of 1,000 points",
            c_text.startswith("No answer") and "Correct cells: E2, E3" in c_text and "0 of 1,000 points" in c_text, c_text)
    C.check("no horizontal scroll at 360 px (results)", await no_hscroll(b))
    await b.reload()
    await expect(b.get_by_text("Loading results…")).to_be_visible(timeout=10000)
    C.check("phone reloaded during RESULTS shows Loading results… (existing limitation)", True)

    # ---- Q2: COMPLETENESS, answered from the keyboard ----
    await proj.sockets.rejoined(projector, since)  # the projector re-created its socket on results
    since = await proj.click("Next Question")
    for phone in (A, B):
        await expect(phone.page.locator("canvas[data-grid-rect]")).to_be_visible(timeout=15000)
        await phone.ready(since)
    a_canvas = a.locator("canvas[data-grid-rect]")
    await a_canvas.focus()
    for key in ("ArrowRight", "ArrowRight", "ArrowDown", "ArrowRight"):  # none → A1 → B1 → B2 → C2
        await a.keyboard.press(key)
    C.check("keyboard arrows move the phone selection (C2)", await selected_label(a) == "C2", await selected_label(a))
    await a.keyboard.press("Enter")
    await expect(a.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    since = await proj.click("Show Results")
    await expect(projector.get_by_text("blank = no answers")).to_be_visible(timeout=10000)
    legend = text_squash(await projector.locator("text=blank = no answers").locator("..").inner_text())
    C.check("COMPLETENESS heatmap: no Correct key", "Correct" not in legend, legend)
    a_text = text_squash(await a.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("phone: You picked C2 — answer recorded", "You picked C2 — answer recorded" in a_text, a_text)

    # ---- Q3: portrait 6×10; lock and unlock; redraws ----
    await proj.sockets.rejoined(projector, since)
    since = await proj.click("Next Question")
    canvas = projector.locator("canvas[data-grid-rect]")
    await expect(canvas).to_have_attribute("data-grid-rows", "10", timeout=15000)
    await proj.sockets.rejoined(projector, since)
    _, _, w, h = await grid_rect(canvas)
    C.check("projector grid rect has the portrait aspect (900×1600, ±1 px)", aspect_ok(w, h, 900, 1600), (w, h))
    for name, phone in (("390x844@3", A), ("360x640@2", B), ("640x360@1", Cy)):
        phone_canvas = phone.page.locator("canvas[data-grid-rect]")
        await expect(phone_canvas).to_have_attribute("data-grid-rows", "10", timeout=15000)
        await phone.ready(since)
        _, _, pw, ph = await grid_rect(phone_canvas)
        C.check(f"phone {name} portrait grid rect aspect (±1 px)", aspect_ok(pw, ph, 900, 1600), (pw, ph))
    a_canvas = a.locator("canvas[data-grid-rect]")
    x, y = await cell_point(a_canvas, 1, 1)
    await a.touchscreen.tap(x, y)
    drawn = await draws(a)
    # Host Lock then Unlock: the selection stays, Lock in is disabled, then works again — and
    # the page's re-renders (a new grid object each time) don't redraw the canvas.
    await proj.click("Lock Question")
    await expect(a.get_by_role("button", name="Lock in B2")).to_be_disabled(timeout=10000)
    C.check("lock keeps the Spot It selection (disabled)", await selected_label(a) == "B2")
    C.check("the canvas still shows on the projector while locked", await projector.locator("canvas[data-grid-rect]").count() == 1)
    await proj.click("Unlock Question")
    await expect(a.get_by_role("button", name="Lock in B2")).to_be_enabled(timeout=10000)
    C.check("unlock restores Lock in with the selection kept", await selected_label(a) == "B2")
    C.check("lock and unlock re-render the page without redrawing the canvas", await draws(a) == drawn, (drawn, await draws(a)))
    await a.get_by_role("button", name="Lock in B2").click()
    await expect(a.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    since = await proj.click("Show Results")
    await expect(projector.get_by_text("blank = no answers")).to_be_visible(timeout=10000)

    # ---- Game over ----
    await proj.sockets.rejoined(projector, since)
    await proj.click("Show Final Results")
    await expect(projector.get_by_text("Game Over!")).to_be_visible(timeout=15000)
    C.check("projector game over: a heatmap canvas per Spot It card", await projector.locator("canvas[data-grid-rect]").count() == 3)
    await projector.screenshot(path=str(SHOTS / "spot_projector_gameover.png"), full_page=True)
    lines = [text_squash(t) for t in await a.locator("[data-t7-line]").all_inner_texts()]
    C.check("phone game over line (ACCURACY, correct)",
            lines[0] == "Spot It — You picked E3 — Correct · Correct cells: E2, E3 · 1,000 / 1,000 pts", lines)
    C.check("phone game over line (COMPLETENESS)", lines[1] == "Spot It — You picked C2 · 1,000 / 1,000 pts", lines)
    C.check("phone game over line (portrait)",
            lines[2] == "Spot It — You picked B2 — Correct · Correct cells: B2 · 1,000 / 1,000 pts", lines)
    c_lines = [text_squash(t) for t in await c.locator("[data-t7-line]").all_inner_texts()]
    C.check("phone game over line (no answer)", c_lines[0] == "Spot It — No answer · Correct cells: E2, E3 · 0 / 1,000 pts", c_lines)
    b_lines = [text_squash(t) for t in await b.locator("[data-t7-line]").all_inner_texts()]
    C.check("phone game over line (incorrect)", b_lines[0] == "Spot It — You picked A1 — Incorrect · Correct cells: E2, E3 · 0 / 1,000 pts", b_lines)
    C.check("phones never requested an image", not (A.image_requests or B.image_requests or Cy.image_requests))

    # B1 (pre-existing): every screen change re-creates the socket, on both apps.
    C.known_issue("B1", "the player app keeps one socket across its screens",
                  len(Cy.sockets.sockets) > 1, f"player sockets opened: {len(Cy.sockets.sockets)}")
    C.known_issue("B1", "the projector keeps one socket across its screens",
                  len(proj.sockets.sockets) > 1, f"projector sockets opened: {len(proj.sockets.sockets)}")
    for context in (projector.context, a.context, b.context, c.context):
        await context.close()


async def reconnect_keeps_selection(browser, api: Api):
    """Select a cell, drop the phone's socket (no page reload), let socket.io reconnect: the
    late-join `new_question` repeats the same question, and the selection survives it."""
    course_id, game_id = api.setup_game("Spot It reconnect")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    qid = api.question(game_id, spot_body(img, 6, 4, [cell(3, 2)], confirm=stamp(img, 1, 6, 4)))["id"]
    code, _ = api.room(course_id, game_id)
    proj = await open_projector(browser, api, code)
    phone = await join_phone(browser, api, code, "Ren", "390x844@3", proxy=True)
    page = phone.page
    await expect(proj.page.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    since = await proj.click("Start Game")
    canvas = page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    await phone.ready(since)
    x, y = await cell_point(canvas, 3, 2)
    await page.touchscreen.tap(x, y)
    pushes = len(phone.sockets.events("new_question"))
    dropped = await phone.sockets.drop()
    await phone.ready(dropped)
    await until(page, lambda: len(phone.sockets.events("new_question")) > pushes, what="the repeated question")
    reconnected = [sock for sock in phone.sockets.sockets if sock["opened"] >= dropped]
    C.check("dropping the socket makes socket.io reconnect on its own (no reload)",
            len(reconnected) == 1 and reconnected[0]["synced"], len(reconnected))
    repeated = phone.sockets.events("new_question")[-1]
    C.check("after a reconnect the phone gets the same question again", repeated["questionId"] == qid, repeated.get("questionId"))
    C.check("the selection survives the repeated question payload", await selected_label(page) == "D3", await selected_label(page))
    await page.get_by_role("button", name="Lock in D3").click()
    await expect(page.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    await proj.click("Show Results")
    text = text_squash(await page.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("the answer locked in after the reconnect is scored", "You picked D3 — Correct" in text, text)
    await proj.page.context.close()
    await page.context.close()


async def host_disconnect(browser, api: Api):
    """B2 (pre-existing): a real host disconnect mid-question should lock the phones' answers
    and show "Host disconnected" (the documented behaviour T7's lock rules build on). The Redis
    room status stays LOBBY after the start, so the server never runs that handling."""
    course_id, game_id = api.setup_game("Spot It host disconnect")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    code, _ = api.room(course_id, game_id)
    proj = await open_projector(browser, api, code)
    phone = await join_phone(browser, api, code, "Hal", "390x844@3")
    page = phone.page
    await expect(proj.page.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    since = await proj.click("Start Game")
    canvas = page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    await phone.ready(since)
    x, y = await cell_point(canvas, 1, 1)
    await page.touchscreen.tap(x, y)
    await proj.page.context.close()  # the projector goes away mid-question
    banner = page.get_by_text("Host disconnected")
    try:
        await expect(banner).to_be_visible(timeout=5000)
        shown = True
    except AssertionError:
        shown = False
    locked = await page.get_by_role("button", name="Lock in B2").is_disabled()
    C.known_issue("B2", "a host disconnect mid-question shows the banner and locks answers",
                  not shown and not locked, f"banner shown: {shown}; Lock in disabled: {locked}")
    await page.context.close()


async def image_failure(browser, api: Api):
    """The projector's failure placeholder and Retry (T8 §2.12 sequence on the canvas)."""
    course_id, game_id = api.setup_game("Spot It failure")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    code, _ = api.room(course_id, game_id)
    context = await new_context(browser, api.token)
    projector = await context.new_page()
    await projector.route("**/api/images/**", lambda route: route.abort())
    await projector.goto(f"{BASE}/host/game/{code}/lobby")
    phone = await join_phone(browser, api, code, "Dee", "390x844@3")
    await expect(projector.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await projector.get_by_role("button", name="Start Game").click()
    retry = projector.get_by_role("button", name="Retry")
    await expect(retry).to_be_visible(timeout=15000)
    C.check("image failure: the grid stays, with Retry", await projector.locator("canvas[data-grid-rect]").count() == 1)
    await projector.screenshot(path=str(SHOTS / "spot_projector_failure.png"))
    await projector.unroute("**/api/images/**")
    await retry.click()
    await expect(retry).to_have_count(0, timeout=15000)
    C.check("Retry loads the image once links work again", True)
    await context.close()
    await phone.page.context.close()


async def auto_advance(browser, api: Api):
    """With auto-advance on, results appear only after the answer phase ends (1.5 s later)."""
    course_id, game_id = api.setup_game("Spot It auto")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    code, _ = api.room(course_id, game_id)
    proj = await open_projector(browser, api, code)
    phone = await join_phone(browser, api, code, "Eve", "390x844@3")
    await expect(proj.page.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await proj.page.get_by_role("button", name=re.compile("Auto-advance")).click()
    since = await proj.click("Start Game")
    canvas = phone.page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    await phone.ready(since)
    C.check("auto-advance: no reveal while the question is open", await proj.page.get_by_text("blank = no answers").count() == 0)
    x, y = await cell_point(canvas, 1, 1)
    await phone.page.touchscreen.tap(x, y)
    await phone.page.get_by_role("button", name="Lock in B2").click()
    await expect(proj.page.get_by_text("blank = no answers")).to_be_visible(timeout=10000)
    C.check("auto-advance: the heatmap appears after the answer phase ends", True)
    await proj.page.context.close()
    await phone.page.context.close()


async def main():
    api = Api()
    try:
        async with async_playwright() as pw:
            browser = await launch(pw)
            for step in (main_play, reconnect_keeps_selection, host_disconnect, image_failure, auto_advance):
                try:
                    await step(browser, api)
                except Exception as exc:  # record, then continue with the next group
                    C.check(f"{step.__name__} ran to completion", False, repr(exc)[:600])
            await browser.close()
    finally:
        api.cleanup()
    sys.exit(C.summary())


if __name__ == "__main__":
    asyncio.run(main())
