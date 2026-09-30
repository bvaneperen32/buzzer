"""Sort It browser checks (spec §4.6): drag, tap-tap and keyboard; lock and unlock; time-out;
points display and its rounding regressions; game over; the results cross-check against the
report; one socket per page for a whole game (B1); and the placements surviving a socket drop
and reconnect, and a host disconnect and return (B2).

    cd tests/browser && python bc_sort.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from html.parser import HTMLParser

from playwright.async_api import async_playwright, expect

from bc_common import (
    BASE, SHOTS, Api, Checks, Host, SocketProxy, SocketWatch, cell, cell_point, launch,
    new_context, scene, sort_body, spot_body, stamp, text_squash, until,
)
from tests.integration.engine.socket_client import TestSocketClient

C = Checks("sort")

BOXES = ["Mammal", "Reptile", "Amphibian"]
CARDS = ["Whale", "Gecko", "Frog", "Bat", "Newt", "Tortoise", "Dolphin", "Salamander"]
KEY = [0, 1, 2, 0, 2, 1, 0, 2]


SOCKETS: dict[int, object] = {}  # each phone page's socket log


async def phone_page(browser, api, code, name, shape, *, proxy=False):
    context = await new_context(browser, api.guest(code, name), phone=shape)
    page = await context.new_page()
    if proxy:
        SOCKETS[id(page)] = SocketProxy()
        await SOCKETS[id(page)].attach(page)
    else:
        SOCKETS[id(page)] = SocketWatch(page)
    await page.goto(f"{BASE}/player/game/{code}/lobby")
    await expect(page.get_by_text(re.compile("Waiting for host to start"))).to_be_visible(timeout=15000)
    return page


def chip(page, card: int):
    return page.locator(f'button[data-card="{card}"]')


def box_button(page, box: int):
    return page.locator(f'[data-drop-box="{box}"] > button').first


async def drag(page, card: int, box: int):
    source = await chip(page, card).bounding_box()
    target = await page.locator(f'[data-drop-box="{box}"]').bounding_box()
    await page.mouse.move(source["x"] + source["width"] / 2, source["y"] + source["height"] / 2)
    await page.mouse.down()
    await page.mouse.move(target["x"] + target["width"] / 2, target["y"] + target["height"] - 12, steps=12)
    await page.mouse.up()


async def tap(page, locator):
    b = await locator.bounding_box()
    await page.touchscreen.tap(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)


async def tap_place(page, card: int, box: int):
    await tap(page, chip(page, card))
    await tap(page, box_button(page, box))


async def key_place(page, card: int, box: int):
    await chip(page, card).focus()
    await page.keyboard.press("Enter")
    await box_button(page, box).focus()
    await page.keyboard.press("Enter")


async def in_box(page, card: int) -> str | None:
    return await chip(page, card).evaluate(
        "el => el.closest('[data-drop-box]')?.getAttribute('data-drop-box') ?? null"
    )


async def submit(page, confirm: bool):
    await page.get_by_role("button", name=re.compile(r"^Submit \(")).click()
    if confirm:
        await page.get_by_role("button", name="Submit anyway").click()
    await expect(page.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)


async def result_text(page) -> str:
    return text_squash(await page.locator("[data-t7-results]").inner_text(timeout=15000))


def percent(count: int, answered: int):
    return None if answered <= 0 else (200 * count + answered) // (2 * answered)


def expected_rows(answers: list[list], key: list[int], k: int):
    rows = {}
    for card, correct in enumerate(key):
        counts = [sum(1 for a in answers if a[card] == b) for b in range(k)]
        left = sum(1 for a in answers if a[card] is None)
        wrong = None
        for b, n in enumerate(counts):
            if b != correct and n > 0 and (wrong is None or n > counts[wrong]):
                wrong = b
        rows[card] = (correct, percent(counts[correct], len(answers)), percent(left, len(answers)), wrong)
    return rows


async def table_rows(page, selector) -> dict:
    """Rows of the first matching table (a selector or a Locator), keyed by card."""
    table = page.locator(selector).first if isinstance(selector, str) else selector
    rows = {}
    for row in await table.locator("tbody tr").all():
        attr = lambda name: row.get_attribute(name)  # noqa: E731
        as_int = lambda v: None if v in (None, "") else int(v)  # noqa: E731
        rows[int(await attr("data-card"))] = (
            as_int(await attr("data-correct-box")),
            as_int(await attr("data-pct-correct")),
            as_int(await attr("data-pct-left-out")),
            as_int(await attr("data-wrong-box")),
        )
    return rows


async def play(browser, api: Api):
    course_id, game_id = api.setup_game("Sort It play")
    small = ["Cat", "Shark", "Dog", "Eel"]
    land = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    for body in (
        sort_body(BOXES, CARDS, KEY, prompt="Sort by class"),
        sort_body([*BOXES, "Bird"], CARDS, [0, 1, 2, 0, 2, 1, 0, 3], prompt="Four boxes"),
        sort_body(["Land", "Sea"], small, [0, 1, 0, 1], points=0.3, prompt="Worth 0.3"),
        sort_body(["Land", "Sea"], small, [0, 1, 0, 1], points=5.1, prompt="Worth 5.1"),
        sort_body(["Yes", "No"], small, None, grading="COMPLETENESS", prompt="Pets?"),
        sort_body(BOXES, CARDS, KEY, prompt="Times out", time_limit=6),
        spot_body(land, 6, 4, [cell(1, 1)], confirm=stamp(land, 1, 6, 4), prompt="Spot, times out", time_limit=6),
    ):
        api.question(game_id, body)
    code, session_id = api.room(course_id, game_id)
    projector_ctx = await new_context(browser, api.token)
    projector = await projector_ctx.new_page()
    host = Host(projector)
    await projector.goto(f"{BASE}/host/game/{code}/lobby")
    p1 = await phone_page(browser, api, code, "Pam", "390x844@3")
    p2 = await phone_page(browser, api, code, "Quin", "360x640@2")
    p3 = await phone_page(browser, api, code, "Rae", "390x844@3")
    await expect(projector.get_by_text("3 players joined")).to_be_visible(timeout=15000)
    await host.click("Start Game")

    # ---- S1: 8 cards, 3 boxes ----
    for phone in (p1, p2, p3):
        await expect(chip(phone, 0)).to_be_visible(timeout=15000)
    order = [int(v) for v in await p1.locator("[data-drop-pool] button[data-card]").evaluate_all(
        "els => els.map(e => e.getAttribute('data-card'))")]
    C.check("cards appear in the shared shuffled order, not the author's", sorted(order) == list(range(8)) and order != list(range(8)), order)
    projector_cards = [text_squash(t) for t in await projector.locator('[aria-label="Cards"] span').all_inner_texts()]
    C.check("projector shows the cards in the same order", projector_cards == [CARDS[i] for i in order], projector_cards)
    for other in (p2, p3):
        other_order = [int(v) for v in await other.locator("[data-drop-pool] button[data-card]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-card'))")]
        C.check("every phone has the same card order", other_order == order, other_order)
    label = await chip(p1, 2).get_attribute("aria-label")
    C.check("card accessible name in the pool", label == "Card Frog, in pool", label)

    # P1 drags: 6 right, card 6 wrong (box 1), card 7 left out.
    for card in range(6):
        await drag(p1, card, KEY[card])
    await drag(p1, 6, 1)
    placed = [await in_box(p1, card) for card in range(8)]
    C.check("drag places cards in the box under the pointer", placed == ["0", "1", "2", "0", "2", "1", "1", None], placed)
    C.check("card accessible name once placed", await chip(p1, 2).get_attribute("aria-label") == "Card Frog, in box Amphibian")
    await p1.get_by_role("button", name=re.compile(r"^Submit \(")).click()
    prompt = text_squash(await p1.get_by_role("alertdialog").inner_text())
    C.check("left-out confirmation text", "1 card is left out and scores 0. Submit anyway?" in prompt, prompt)
    await p1.get_by_role("button", name="Submit anyway").click()
    await expect(p1.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)

    # P2 taps a card, then a box, for every card; moves one and returns one first.
    await tap_place(p2, 0, 2)
    await tap(p2, chip(p2, 0))
    await p2.get_by_role("button", name="Return Whale to the pool").click()
    C.check("a placed card can return to the pool", await in_box(p2, 0) is None)
    for card in range(8):
        await tap_place(p2, card, KEY[card])
    await submit(p2, confirm=False)

    # P3 uses the keyboard, and the host locks and unlocks in the middle.
    await key_place(p3, 0, KEY[0])
    await key_place(p3, 1, KEY[1])
    await host.click("Lock Question")
    await expect(chip(p3, 2)).to_be_disabled(timeout=10000)
    kept = [await in_box(p3, 0), await in_box(p3, 1)]
    C.check("lock disables the board and keeps the placements", kept == ["0", "1"], kept)
    await host.click("Unlock Question")
    await expect(chip(p3, 2)).to_be_enabled(timeout=10000)
    C.check("unlock re-enables the board with the placements kept", [await in_box(p3, 0), await in_box(p3, 1)] == ["0", "1"])
    for card in range(2, 8):
        await key_place(p3, card, KEY[card])
    await submit(p3, confirm=False)

    await host.click("Show Results")
    table = "table[data-sort-results='accuracy']"
    await expect(projector.locator(table)).to_be_visible(timeout=10000)
    answers = [[0, 1, 2, 0, 2, 1, 1, None], KEY, KEY]
    shown = await table_rows(projector, table)
    C.check("projector results rows follow the card order", list(shown) == order, list(shown))
    C.check("projector per-card numbers (correct box, % correct, % left out, wrong box)",
            shown == expected_rows(answers, KEY, 3), (shown, expected_rows(answers, KEY, 3)))
    await projector.screenshot(path=str(SHOTS / "sort_projector_results.png"))
    t1 = await result_text(p1)
    C.check("phone: 6 of 8 correct, 1 left out · 687.5 of 1,000 points",
            "6 of 8 correct, 1 left out" in t1 and "687.5 of 1,000 points" in t1, t1)
    t2 = await result_text(p2)
    C.check("phone: 8 of 8 correct · 1,000 of 1,000 points", "8 of 8 correct" in t2 and "1,000 of 1,000 points" in t2, t2)
    C.check("phone (keyboard, locked and unlocked): 8 of 8 correct", "8 of 8 correct" in await result_text(p3))

    async def next_question(expect_card: bool = True):
        # The phones answer as soon as the question shows: no wait for a socket re-join (B1).
        await host.click("Next Question")
        target = (lambda phone: chip(phone, 0)) if expect_card else (lambda phone: phone.locator("canvas[data-grid-rect]"))
        for phone in (p1, p2, p3):
            await expect(target(phone)).to_be_visible(timeout=15000)

    # ---- S2: k = 4 → 708.3 ----
    await next_question()
    for card, box in enumerate([0, 1, 2, 0, 2, 1, 1, None]):
        if box is not None:
            await tap_place(p1, card, box)
    await submit(p1, confirm=True)
    await host.click("Show Results")
    C.check("phone: 708.3 of 1,000 points (k = 4)", "708.3 of 1,000 points" in await result_text(p1))

    # ---- S3 / S4: the rounding regressions (0.15 → 0.2, 2.55 → 2.6) ----
    for worth, expected in ((0.3, "0.2 of 0.3 points"), (5.1, "2.6 of 5.1 points")):
        await next_question()
        for card, box in enumerate([0, 1, 0, 0]):  # 3 right, 1 wrong → credit 0.5
            await tap_place(p1, card, box)
        await submit(p1, confirm=False)
        await host.click("Show Results")
        text = await result_text(p1)
        C.check(f"phone: half credit of {worth} shows {expected}", expected in text, text)

    # ---- S5: COMPLETENESS ----
    await next_question()
    for card in range(3):
        await tap_place(p1, card, card % 2)
    await p1.get_by_role("button", name=re.compile(r"^Submit \(")).click()
    prompt = text_squash(await p1.get_by_role("alertdialog").inner_text())
    C.check("COMPLETENESS confirmation doesn't mention scoring", "1 card is left out. Submit anyway?" in prompt, prompt)
    await p1.get_by_role("button", name="Submit anyway").click()
    await expect(p1.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)
    await host.click("Show Results")
    text = await result_text(p1)
    C.check("phone: Answer recorded — you placed 3 of 4 cards", "Answer recorded — you placed 3 of 4 cards" in text, text)
    await expect(projector.locator("table[data-sort-results='completeness']")).to_be_visible(timeout=10000)

    # ---- S6: time-out with an unsubmitted board ----
    await next_question()
    await tap_place(p2, 0, 0)
    await tap_place(p2, 1, 1)
    await expect(chip(p2, 2)).to_be_disabled(timeout=15000)  # the auto-lock after 6 s
    C.check("time-out keeps the unsubmitted board on screen, disabled", await in_box(p2, 0) == "0")
    await host.click("Show Results")
    text = await result_text(p2)
    C.check("time-out submits nothing: No answer — 0 of 1,000 points", text.startswith("No answer — 0 of 1,000 points"), text)

    # ---- S7: Spot It time-out with a selected but unlocked cell ----
    await next_question(expect_card=False)
    canvas = p2.locator("canvas[data-grid-rect]")
    await expect(canvas).to_be_visible(timeout=15000)
    x, y = await cell_point(canvas, 1, 1)
    await p2.touchscreen.tap(x, y)
    await expect(p2.get_by_role("button", name="Lock in B2")).to_be_disabled(timeout=15000)
    C.check("Spot It time-out keeps the selection, disabled", await p2.locator("[data-selected]").get_attribute("data-selected") == "B2")
    await host.click("Show Results")
    text = await result_text(p2)
    C.check("Spot It time-out submits nothing: No answer", text.startswith("No answer") and "0 of 1,000 points" in text, text)

    # ---- Game over ----
    await host.click("Show Final Results")
    await expect(projector.get_by_text("Game Over!")).to_be_visible(timeout=15000)
    lines = [text_squash(t) for t in await p1.locator("[data-t7-line]").all_inner_texts()]
    expected = [
        "Sort It — 6 of 8 correct, 1 left out · 687.5 / 1,000 pts",
        "Sort It — 6 of 8 correct, 1 left out · 708.3 / 1,000 pts",
        "Sort It — 3 of 4 correct · 0.2 / 0.3 pts",
        "Sort It — 3 of 4 correct · 2.6 / 5.1 pts",
        "Sort It — Placed 3 of 4 cards · 1,000 / 1,000 pts",
        "Sort It — No answer · 0 / 1,000 pts",
        "Spot It — No answer · Correct cells: B2 · 0 / 1,000 pts",
    ]
    for got, want in zip(lines, expected):
        C.check(f"phone game over line: {want}", got == want, got)
    C.check("phone game over has a line per T7 question", len(lines) == 7, lines)
    cards = await projector.locator("table[data-sort-results]").count()
    C.check("projector game over: a Sort It table per Sort It question", cards == 6, cards)
    for label, sockets in (("the projector", host.sockets), ("phone Pam", SOCKETS[id(p1)]),
                           ("phone Quin", SOCKETS[id(p2)]), ("phone Rae", SOCKETS[id(p3)])):
        C.check(f"{label} kept one socket across all 7 questions (B1)", len(sockets.sockets) == 1,
                f"sockets opened: {len(sockets.sockets)}")
    first = await table_rows(projector, projector.locator("table[data-sort-results='accuracy']").first)
    C.check("projector game over rows are in the author's order", list(first) == list(range(8)), list(first))
    C.check("projector game over numbers match the results screen", first == expected_rows(answers, KEY, 3), first)
    await projector.screenshot(path=str(SHOTS / "sort_projector_gameover.png"), full_page=True)
    for context in (projector_ctx, p1.context, p2.context, p3.context):
        await context.close()


class ReportRows(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag == "td":
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.rows[-1].append("".join(self.cell).strip())
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


async def cross_check(browser, api: Api):
    """One scripted session (8 socket players) with a designed tie, a card left out by some
    players and a .5 percentage. The projector's results and game-over numbers must equal the
    downloaded report's table and the unit tests' expected values (tests/unit/test_sort_it.py)."""
    boxes, cards, key = BOXES, ["Whale", "Gecko", "Frog", "Bat"], [0, 1, 2, 0]
    answers = [[0, 1, 2, 0], [0, 1, 0, 0], [0, 1, 0, 1], [0, 1, 0, 1], [0, 1, 0, 2], [1, 1, 1, 2],
               [2, 1, 1, None], [None, 1, None, None]]
    expected = {0: (0, 63, 13, 1), 1: (1, 100, 0, None), 2: (2, 13, 13, 0), 3: (0, 25, 25, 1)}
    course_id, game_id = api.setup_game("Sort It cross-check")
    qid = api.question(game_id, sort_body(boxes, cards, key, prompt="Cross-check"))["id"]
    code, session_id = api.room(course_id, game_id)
    ctx = await new_context(browser, api.token)
    projector = await ctx.new_page()
    host = Host(projector)
    await projector.goto(f"{BASE}/host/game/{code}/lobby")
    players = []
    for i in range(8):
        client = TestSocketClient(BASE, api.guest(code, f"X{i}"), f"x{i}")
        await client.connect()
        await client.emit("join_room", {"room_code": code, "role": "PLAYER"})
        await client.wait_for("sync_state", timeout=15)
        players.append(client)
    await expect(projector.get_by_text("8 players joined")).to_be_visible(timeout=15000)
    await host.click("Start Game")
    for client, answer in zip(players, answers):
        await client.wait_for("new_question", timeout=15)
        await client.emit("submit_answer", {"question_id": qid, "answer_data": {"placements": answer}})
        await client.wait_for("answer_received", timeout=15)
    await host.click("Show Results")
    table = "table[data-sort-results='accuracy']"
    await expect(projector.locator(table)).to_be_visible(timeout=10000)
    results = await table_rows(projector, table)
    C.check("cross-check: results screen equals the expected values", results == expected, results)
    await host.click("Show Final Results")
    await expect(projector.get_by_text("Game Over!")).to_be_visible(timeout=15000)
    summary = await table_rows(projector, table)
    C.check("cross-check: game-over card equals the expected values", summary == expected, summary)
    for client in players:
        await client.disconnect()
    await ctx.close()
    await asyncio.sleep(1.0)
    rows = [r for r in ReportRows(api.report(session_id)).rows if len(r) == 5]
    report = {
        cards.index(r[0]): (
            boxes.index(r[1]),
            None if r[2] == "—" else int(r[2].rstrip("%")),
            None if r[4] == "—" else int(r[4].rstrip("%")),
            None if r[3] == "—" else boxes.index(r[3]),
        )
        for r in rows
    }
    C.check("cross-check: the report's table equals the projector's", report == expected, report)


async def reconnect_keeps_placements(browser, api: Api):
    """Place cards, drop the phone's socket (no page reload), let socket.io reconnect: the
    late-join `new_question` repeats the same question, and the placements survive it."""
    boxes, cards, key = ["Land", "Sea"], ["Cat", "Shark", "Dog", "Eel"], [0, 1, 0, 1]
    course_id, game_id = api.setup_game("Sort It reconnect")
    qid = api.question(game_id, sort_body(boxes, cards, key, prompt="Land or sea"))["id"]
    code, _ = api.room(course_id, game_id)
    ctx = await new_context(browser, api.token)
    projector = await ctx.new_page()
    host = Host(projector)
    await projector.goto(f"{BASE}/host/game/{code}/lobby")
    page = await phone_page(browser, api, code, "Rex", "390x844@3", proxy=True)
    sockets = SOCKETS[id(page)]
    await expect(projector.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await host.click("Start Game")
    await expect(chip(page, 0)).to_be_visible(timeout=15000)
    for card in range(3):
        await tap_place(page, card, key[card])
    placed = [await in_box(page, card) for card in range(4)]
    pushes = len(sockets.events("new_question"))
    dropped = await sockets.drop()
    await sockets.rejoined(page, dropped)
    await until(page, lambda: len(sockets.events("new_question")) > pushes, what="the repeated question")
    reconnected = [sock for sock in sockets.sockets if sock["opened"] >= dropped]
    C.check("dropping the socket makes socket.io reconnect on its own (no reload)",
            len(reconnected) == 1 and reconnected[0]["synced"], len(reconnected))
    C.check("after a reconnect the phone gets the same question again",
            sockets.events("new_question")[-1]["questionId"] == qid)
    after = [await in_box(page, card) for card in range(4)]
    C.check("the placements survive the repeated question payload", after == placed == ["0", "1", "0", None], (placed, after))
    C.check("the board is still enabled", await chip(page, 3).is_enabled())
    await tap_place(page, 3, key[3])
    await submit(page, confirm=False)
    await host.click("Show Results")
    text = await result_text(page)
    C.check("the board submitted after the reconnect is scored", "4 of 4 correct" in text, text)
    await ctx.close()
    await page.context.close()


async def host_disconnect_keeps_placements(browser, api: Api):
    """B2: the projector goes away mid-question. The phone's board locks with its placements and
    the banner shows; the projector's return unlocks it with the placements kept, and the board
    then submitted is scored."""
    boxes, cards, key = ["Land", "Sea"], ["Cat", "Shark", "Dog", "Eel"], [0, 1, 0, 1]
    course_id, game_id = api.setup_game("Sort It host disconnect")
    api.question(game_id, sort_body(boxes, cards, key, prompt="Land or sea"))
    code, _ = api.room(course_id, game_id)

    async def open_projector():
        ctx = await new_context(browser, api.token)
        projector = await ctx.new_page()
        await projector.goto(f"{BASE}/host/game/{code}/lobby")
        return Host(projector)

    host = await open_projector()
    page = await phone_page(browser, api, code, "Ivy", "390x844@3")
    await expect(host.page.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await host.click("Start Game")
    await expect(chip(page, 0)).to_be_visible(timeout=15000)
    for card in range(2):
        await tap_place(page, card, key[card])
    await host.page.context.close()
    await expect(page.get_by_text("Host disconnected")).to_be_visible(timeout=10000)
    await expect(chip(page, 2)).to_be_disabled(timeout=10000)
    kept = [await in_box(page, card) for card in range(4)]
    C.check("a host disconnect locks the board and keeps the placements", kept == ["0", "1", None, None], kept)
    host = await open_projector()
    await expect(chip(page, 2)).to_be_enabled(timeout=10000)
    C.check("the host's return unlocks the board with the placements kept",
            [await in_box(page, card) for card in range(4)] == ["0", "1", None, None])
    await expect(page.get_by_text("Host disconnected")).to_have_count(0, timeout=10000)
    C.check("the host's return clears the Host disconnected banner", True)
    for card in (2, 3):
        await tap_place(page, card, key[card])
    await submit(page, confirm=False)
    await host.click("Show Results")
    text = await result_text(page)
    C.check("the board submitted after the host's return is scored", "4 of 4 correct" in text, text)
    await host.page.context.close()
    await page.context.close()


async def main():
    api = Api()
    try:
        async with async_playwright() as pw:
            browser = await launch(pw)
            for step in (play, reconnect_keeps_placements, host_disconnect_keeps_placements, cross_check):
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
