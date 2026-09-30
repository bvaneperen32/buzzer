"""Spot It browser checks (spec §4.6): play end to end on the projector and three phones —
coordinates, aspect, taps, nudges, rotation, keyboard, reveal, heatmap, phone texts, game over,
image isolation, the projector's image failure and Retry, auto-advance; redraws; one socket per
page for a whole game (B1); a socket drop and reconnect that must keep the selection and deliver
an answer given while offline; a real host disconnect and reconnect (B2), including the phones
hearing the host is back; the projector's own lost connection pausing its controls, and its
auto-advance waiting for the rejoin; and late answers rejected without the error screen.

    cd tests/browser && python bc_spot.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import time

from playwright.async_api import async_playwright, expect

from bc_common import (
    BASE, SHOTS, Api, Checks, SocketProxy, SocketWatch, aspect_ok, cell, cell_point, grid_rect,
    launch, new_context, scene, spot_body, stamp, text_squash, until,
)

C = Checks("spot")


class Phone:
    """A player on a phone-sized context, with its sockets observed (or proxied)."""

    def __init__(self, page, sockets, image_requests):
        self.page, self.sockets, self.image_requests = page, sockets, image_requests


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

    async def click(self, name: str) -> None:
        await self.page.get_by_role("button", name=name).click()


async def open_projector(browser, api, code, *, proxy=False) -> Projector:
    context = await new_context(browser, api.token)
    page = await context.new_page()
    if proxy:
        sockets = SocketProxy()
        await sockets.attach(page)
    else:
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
    await proj.click("Start Game")

    # ---- Q1: landscape 6×4, ACCURACY ----
    canvas = projector.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    _, _, w, h = await grid_rect(canvas)
    C.check("projector grid rect has the image's aspect (1600×900, ±1 px)", aspect_ok(w, h, 1600, 900), (w, h))
    for name, phone in (("390x844@3", A), ("360x640@2", B), ("640x360@1", Cy)):
        phone_canvas = phone.page.locator("canvas[data-grid-rect]")
        await expect(phone_canvas).to_be_visible(timeout=15000)
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

    await proj.click("Show Results")
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

    # ---- Q2: COMPLETENESS, answered from the keyboard the moment the question shows ----
    # No wait for a socket re-join: the phone kept its socket across its screens (B1).
    await proj.click("Next Question")
    for phone in (A, B):
        await expect(phone.page.locator("canvas[data-grid-rect]")).to_be_visible(timeout=15000)
    a_canvas = a.locator("canvas[data-grid-rect]")
    await a_canvas.focus()
    for key in ("ArrowRight", "ArrowRight", "ArrowDown", "ArrowRight"):  # none → A1 → B1 → B2 → C2
        await a.keyboard.press(key)
    C.check("keyboard arrows move the phone selection (C2)", await selected_label(a) == "C2", await selected_label(a))
    await a.keyboard.press("Enter")
    await expect(a.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    C.check("an answer given as soon as the question shows is accepted", True)
    await proj.click("Show Results")
    await expect(projector.get_by_text("blank = no answers")).to_be_visible(timeout=10000)
    legend = text_squash(await projector.locator("text=blank = no answers").locator("..").inner_text())
    C.check("COMPLETENESS heatmap: no Correct key", "Correct" not in legend, legend)
    a_text = text_squash(await a.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("phone: You picked C2 — answer recorded", "You picked C2 — answer recorded" in a_text, a_text)

    # ---- Q3: portrait 6×10; lock and unlock; redraws ----
    await proj.click("Next Question")
    canvas = projector.locator("canvas[data-grid-rect]")
    await expect(canvas).to_have_attribute("data-grid-rows", "10", timeout=15000)
    _, _, w, h = await grid_rect(canvas)
    C.check("projector grid rect has the portrait aspect (900×1600, ±1 px)", aspect_ok(w, h, 900, 1600), (w, h))
    for name, phone in (("390x844@3", A), ("360x640@2", B), ("640x360@1", Cy)):
        phone_canvas = phone.page.locator("canvas[data-grid-rect]")
        await expect(phone_canvas).to_have_attribute("data-grid-rows", "10", timeout=15000)
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
    await proj.click("Show Results")
    await expect(projector.get_by_text("blank = no answers")).to_be_visible(timeout=10000)

    # ---- Game over ----
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

    # B1: a screen change no longer re-creates the socket, in either app. (A reloaded a phone
    # and B reloaded during results, each a new page load: one more socket apiece.)
    for label, sockets, expected in (("the projector", proj.sockets, 1), ("phone 640x360", Cy.sockets, 1),
                                     ("phone 390x844 (reloaded once)", A.sockets, 2),
                                     ("phone 360x640 (reloaded once)", B.sockets, 2)):
        C.check(f"{label} kept one socket per page load across every screen of the game",
                len(sockets.sockets) == expected, f"sockets opened: {len(sockets.sockets)}")
    for context in (projector.context, a.context, b.context, c.context):
        await context.close()


async def reconnect_keeps_selection(browser, api: Api):
    """Select a cell, drop the phone's socket (no page reload), let socket.io reconnect: the
    late-join `new_question` repeats the same question, and the selection survives it. Then drop
    it again and keep the phone offline while it locks in: the answer waits ("Sending your
    answer…", never shown as accepted), and is delivered and scored once the phone has re-joined."""
    course_id, game_id = api.setup_game("Spot It reconnect")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    qid = api.question(game_id, spot_body(img, 6, 4, [cell(3, 2)], confirm=stamp(img, 1, 6, 4)))["id"]
    code, _ = api.room(course_id, game_id)
    proj = await open_projector(browser, api, code)
    phone = await join_phone(browser, api, code, "Ren", "390x844@3", proxy=True)
    page = phone.page
    await expect(proj.page.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await proj.click("Start Game")
    canvas = page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    x, y = await cell_point(canvas, 3, 2)
    await page.touchscreen.tap(x, y)
    pushes = len(phone.sockets.events("new_question"))
    dropped = await phone.sockets.drop()
    await phone.sockets.rejoined(page, dropped)
    await until(page, lambda: len(phone.sockets.events("new_question")) > pushes, what="the repeated question")
    reconnected = [sock for sock in phone.sockets.sockets if sock["opened"] >= dropped]
    C.check("dropping the socket makes socket.io reconnect on its own (no reload)",
            len(reconnected) == 1 and reconnected[0]["synced"], len(reconnected))
    repeated = phone.sockets.events("new_question")[-1]
    C.check("after a reconnect the phone gets the same question again", repeated["questionId"] == qid, repeated.get("questionId"))
    C.check("the selection survives the repeated question payload", await selected_label(page) == "D3", await selected_label(page))

    # Offline, the phone locks in: nothing is sent, and nothing claims it was accepted.
    offline = await phone.sockets.drop(hold=True)
    await until(page, lambda: phone.sockets.attempts_since(offline) > 1, timeout=15,
                what="failed reconnection attempts")
    await expect(page.get_by_role("status")).to_have_text("Connection lost — reconnecting…")
    C.check("offline, failed retries keep the question and selection on screen (no error screen)",
            await selected_label(page) == "D3", await selected_label(page))
    await page.get_by_role("button", name="Lock in D3").click()
    await expect(page.get_by_text("Sending your answer…")).to_be_visible(timeout=5000)
    C.check("offline, the answer waits and isn't shown as accepted",
            await page.get_by_text("Answer locked in!").count() == 0)
    C.check("offline, the projector has no answer yet",
            await proj.page.get_by_text("0 / 1 answered").count() == 1)
    released = await phone.sockets.release()
    await phone.sockets.rejoined(page, released)
    await expect(page.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    C.check("after re-joining, the waiting answer is delivered and accepted", True)
    C.check("the reconnecting banner is gone once connected", await page.get_by_role("status").count() == 0)
    await expect(proj.page.get_by_text("1 / 1 answered")).to_be_visible(timeout=10000)
    await proj.click("Show Results")
    text = text_squash(await page.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("the answer given offline is scored once delivered", "You picked D3 — Correct" in text, text)
    await proj.page.context.close()
    await page.context.close()


async def host_disconnect(browser, api: Api):
    """B2: a real host disconnect mid-question shows "Host disconnected" and locks the phones'
    answers, keeping the selection. The projector coming back unlocks them — but a lock the host
    set by hand survives a disconnect and reconnect. The answer then locked in is scored."""
    course_id, game_id = api.setup_game("Spot It host disconnect")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    code, _ = api.room(course_id, game_id)
    proj = await open_projector(browser, api, code)
    phone = await join_phone(browser, api, code, "Hal", "390x844@3")
    page = phone.page
    await expect(proj.page.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await proj.click("Start Game")
    canvas = page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    x, y = await cell_point(canvas, 1, 1)
    await page.touchscreen.tap(x, y)
    lock_in = page.get_by_role("button", name="Lock in B2")

    await proj.page.context.close()  # the projector goes away mid-question
    await expect(page.get_by_text("Host disconnected")).to_be_visible(timeout=10000)
    C.check("a host disconnect mid-question shows the banner", True)
    await expect(lock_in).to_be_disabled(timeout=10000)
    C.check("a host disconnect locks the phone's answer", True)
    C.check("the selection is kept while locked", await selected_label(page) == "B2", await selected_label(page))

    proj = await open_projector(browser, api, code)  # the host comes back
    await expect(lock_in).to_be_enabled(timeout=10000)
    C.check("the host's return unlocks the answer, selection kept", await selected_label(page) == "B2")
    await expect(page.get_by_text("Host disconnected")).to_have_count(0, timeout=10000)
    C.check("the host's return clears the Host disconnected banner", True)
    await expect(proj.page.locator("canvas[data-grid-rect]")).to_be_visible(timeout=15000)
    C.check("the returning projector is back on the question", True)

    # A lock the host set by hand isn't undone by a disconnect and reconnect.
    await proj.click("Lock Question")
    await expect(lock_in).to_be_disabled(timeout=10000)
    await proj.page.context.close()
    await expect(page.get_by_text("Host disconnected")).to_be_visible(timeout=10000)
    proj = await open_projector(browser, api, code)
    await expect(proj.page.get_by_role("button", name="Unlock Question")).to_be_visible(timeout=15000)
    await expect(page.get_by_text("Host disconnected")).to_have_count(0, timeout=10000)
    C.check("the host's return clears the banner while a manual lock holds", await lock_in.is_disabled())
    C.check("a manual lock survives the host's disconnect and reconnect", await lock_in.is_disabled())
    await proj.click("Unlock Question")
    await expect(lock_in).to_be_enabled(timeout=10000)
    await lock_in.click()
    await expect(page.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    await proj.click("Show Results")
    text = text_squash(await page.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("the answer locked in after the host's return is scored", "You picked B2 — Correct" in text, text)
    await proj.page.context.close()
    await page.context.close()


async def projector_connection_lost(browser, api: Api):
    """The projector's own connection drops mid-question and stays down a while: it says it is
    reconnecting and disables its controls until the server has it back in the room, and the
    game carries on afterwards. (That nothing is sent in the gap, even by a timer, is
    auto_advance_waits_for_the_rejoin's check: a disabled button can't be clicked at all.)"""
    course_id, game_id = api.setup_game("Spot It projector offline")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    code, _ = api.room(course_id, game_id)
    proj = await open_projector(browser, api, code, proxy=True)
    phone = await join_phone(browser, api, code, "Oli", "390x844@3")
    page, projector = phone.page, proj.page
    await expect(projector.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await proj.click("Start Game")
    canvas = page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    x, y = await cell_point(canvas, 1, 1)
    await page.touchscreen.tap(x, y)
    show, lock = (projector.get_by_role("button", name=n) for n in ("Show Results", "Lock Question"))

    offline = await proj.sockets.drop(hold=True)
    await until(projector, lambda: proj.sockets.attempts_since(offline) > 1, timeout=15,
                what="failed reconnection attempts")
    await expect(projector.get_by_role("status")).to_contain_text("Controls are paused")
    C.check("offline, the projector says it is reconnecting", True)
    C.check("offline, Show Results and Lock are disabled",
            await show.is_disabled() and await lock.is_disabled())
    await expect(page.get_by_text("Host disconnected")).to_be_visible(timeout=10000)
    C.check("the phones see the host as disconnected meanwhile", True)

    rejoined = await proj.sockets.release()
    await proj.sockets.rejoined(projector, rejoined)
    await expect(show).to_be_enabled(timeout=10000)
    await expect(projector.get_by_role("status")).to_have_count(0)
    C.check("once the server has it back, the controls work and the banner is gone",
            await lock.is_enabled())
    await expect(page.get_by_text("Host disconnected")).to_have_count(0, timeout=10000)
    await expect(page.get_by_role("button", name="Lock in B2")).to_be_enabled(timeout=10000)
    C.check("the phone hears the host is back and can answer again", await selected_label(page) == "B2")
    await page.get_by_role("button", name="Lock in B2").click()
    await expect(page.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    await proj.click("Show Results")
    text = text_squash(await page.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("the game carries on normally after the projector's reconnect", "You picked B2 — Correct" in text, text)
    await projector.context.close()
    await page.context.close()


async def late_answers_are_not_errors(browser, api: Api):
    """An answer that reaches the server after the question locked, or after results, is refused
    as a normal outcome: the phone keeps its game screen, says the answer didn't count, never
    shows it as accepted, and plays on. The phone is made late by holding back the server's
    lock (then its results) — the race a real tap at the last moment runs."""
    course_id, game_id = api.setup_game("Spot It late answers")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    for prompt in ("First", "Second"):
        api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4), prompt=prompt))
    code, _ = api.room(course_id, game_id)
    proj = await open_projector(browser, api, code)
    phone = await join_phone(browser, api, code, "Lu", "390x844@3", proxy=True)
    page, projector = phone.page, proj.page
    await expect(projector.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await proj.click("Start Game")
    canvas = page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    x, y = await cell_point(canvas, 1, 1)
    await page.touchscreen.tap(x, y)
    lock_in = page.get_by_role("button", name="Lock in B2")
    too_late = page.get_by_text("Too late — answers had already closed")

    async def not_accepted_and_still_playing(what: str) -> None:
        await expect(too_late).to_be_visible(timeout=10000)
        C.check(f"{what}: the phone says the answer didn't count", True)
        C.check(f"{what}: never shown as accepted",
                await page.get_by_text("Answer locked in!").count() == 0
                and await page.get_by_text("Sending your answer…").count() == 0)
        C.check(f"{what}: no error screen; the question and selection stay",
                await page.get_by_text("Back to Join").count() == 0 and await selected_label(page) == "B2")
        C.check(f"{what}: answering is closed", await lock_in.is_disabled())

    # 1. The host locks; the phone hasn't heard yet and locks in.
    phone.sockets.withhold("question_locked")
    await proj.click("Lock Question")
    await expect(projector.get_by_role("button", name="Unlock Question")).to_be_visible(timeout=10000)
    await lock_in.click()
    await not_accepted_and_still_playing("locked before it arrived")
    phone.sockets.deliver_withheld()
    await proj.click("Unlock Question")
    await expect(lock_in).to_be_enabled(timeout=10000)
    C.check("an unlock reopens it, and the too-late note goes", await too_late.count() == 0)

    # 2. The host shows results; the phone hasn't heard yet and locks in.
    phone.sockets.withhold("question_results")
    await proj.click("Show Results")
    await expect(projector.get_by_text("blank = no answers")).to_be_visible(timeout=10000)
    await lock_in.click()
    await not_accepted_and_still_playing("results shown before it arrived")
    phone.sockets.deliver_withheld()
    text = text_squash(await page.locator("[data-t7-results]").inner_text(timeout=10000))
    C.check("the results then say No answer", text.startswith("No answer"), text)

    # The game goes on: the next answer is accepted as usual.
    await proj.click("Next Question")
    await expect(page.get_by_text("Too late")).to_have_count(0, timeout=15000)
    canvas = page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    x, y = await cell_point(canvas, 1, 1)
    await page.touchscreen.tap(x, y)
    await page.get_by_role("button", name="Lock in B2").click()
    await expect(page.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    C.check("the next question's answer is accepted", True)
    await projector.context.close()
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
    await proj.click("Start Game")
    canvas = phone.page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    C.check("auto-advance: no reveal while the question is open", await proj.page.get_by_text("blank = no answers").count() == 0)
    x, y = await cell_point(canvas, 1, 1)
    await phone.page.touchscreen.tap(x, y)
    await phone.page.get_by_role("button", name="Lock in B2").click()
    await expect(proj.page.get_by_text("blank = no answers")).to_be_visible(timeout=10000)
    C.check("auto-advance: the heatmap appears after the answer phase ends", True)
    await proj.page.context.close()
    await phone.page.context.close()


async def auto_advance_waits_for_the_rejoin(browser, api: Api):
    """Auto-advance never fires into a lost connection, nor before the rejoin is confirmed.

    The projector's connection drops inside the 1.5 s pause after the answer phase ends. Its
    advance isn't sent while it's offline, nor once its socket is back but not yet seated (its
    sync_state held back), and is sent once, a full pause after the rejoin. The results
    countdown is the same: dropped midway and kept down past its end, it doesn't fire, and runs
    again from the top after the rejoin. What's checked is what the projector put on the wire,
    every event it sent, not what its buttons showed."""
    course_id, game_id = api.setup_game("Spot It auto reconnect")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    code, _ = api.room(course_id, game_id)
    proj = await open_projector(browser, api, code, proxy=True)
    phone = await join_phone(browser, api, code, "Ola", "390x844@3")
    projector, sockets = proj.page, proj.sockets
    await expect(projector.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await projector.get_by_role("button", name=re.compile("Auto-advance")).click()
    await proj.click("Start Game")
    canvas = phone.page.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    banner = projector.get_by_role("status")

    def controls_sent(since: float) -> list[float]:
        return [at for name in ("host_advance", "host_lock_question") for at in sockets.sent(name) if at >= since]

    async def lost_then_unseated(what: str, offline_s: float, control) -> tuple[float, float]:
        """Drop the connection and stay offline `offline_s`; come back with the rejoin's
        sync_state held back for 2.5 s; then let it through. Returns (dropped, seated)."""
        dropped = await sockets.drop(hold=True)
        await projector.wait_for_timeout(offline_s * 1000)
        C.check(f"{what}: offline, nothing is sent", controls_sent(dropped) == [], controls_sent(dropped))
        sockets.withhold("sync_state")
        back = await sockets.release()
        await until(projector, lambda: [at for at in sockets.sent("join_room") if at >= back],
                    timeout=15, what="the socket's re-join request")
        await projector.wait_for_timeout(2500)
        C.check(f"{what}: connected again but not yet seated: still nothing sent, the control disabled "
                "and the banner up",
                controls_sent(dropped) == [] and await control.is_disabled()
                and "Controls are paused" in (await banner.inner_text()),
                controls_sent(dropped))
        seated = time.monotonic()
        sockets.deliver_withheld()
        return dropped, seated

    # 1. The phone answers: the answer phase ends, the 1.5 s pause starts, and the projector's
    #    connection drops inside it.
    x, y = await cell_point(canvas, 1, 1)
    await phone.page.touchscreen.tap(x, y)
    await phone.page.get_by_role("button", name="Lock in B2").click()
    await until(projector, lambda: sockets.arrived("answer_phase_ended"), what="the answer phase's end")
    ended = sockets.arrived("answer_phase_ended")[0]
    dropped, seated = await lost_then_unseated(
        "the question's pause", 2.5, projector.get_by_role("button", name="Show Results"))
    C.check("the drop came inside the pause", dropped - ended < 1.5, round(dropped - ended, 3))
    await expect(projector.get_by_text("blank = no answers")).to_be_visible(timeout=10000)
    advances = controls_sent(dropped)
    C.check("after the rejoin the pause runs again in full, and the advance is sent once",
            len(advances) == 1 and advances[0] - seated >= 1.4,
            [round(at - seated, 2) for at in advances])

    # 2. The results countdown (10 s): the connection drops 2 s in and stays down past its end.
    await expect(projector.get_by_text(re.compile(r"Auto-advancing in \d+s"))).to_be_visible(timeout=5000)
    await projector.wait_for_timeout(2000)
    dropped, seated = await lost_then_unseated(
        "the results countdown", 9, projector.get_by_role("button", name="Show Final Results"))
    await expect(projector.get_by_text("Game Over!")).to_be_visible(timeout=20000)
    advances = controls_sent(dropped)
    C.check("after the rejoin the countdown runs again from the top, and the advance is sent once",
            len(advances) == 1 and advances[0] - seated >= 9.5,
            [round(at - seated, 2) for at in advances])
    await projector.context.close()
    await phone.page.context.close()


async def main():
    api = Api()
    try:
        async with async_playwright() as pw:
            browser = await launch(pw)
            for step in (main_play, reconnect_keeps_selection, host_disconnect, projector_connection_lost,
                         late_answers_are_not_errors, image_failure, auto_advance,
                         auto_advance_waits_for_the_rejoin):
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
