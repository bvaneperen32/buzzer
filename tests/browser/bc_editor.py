"""Editor browser checks (spec §4.6): limits vs the server, too-dense drafts, default grids,
authoring dimensions, tick-only reconfirmation, replace warning, live freeze, re-selecting the
current image, the round-trip matrix, the checkbox's initial state, answer keys, text rules,
keyboard on the editor canvas.

    cd tests/browser && python bc_editor.py

Assertions about what a save stored read the save's own response (the question as stored),
because question updates commit only after responding.
"""

from __future__ import annotations

import asyncio
import re
import sys

from playwright.async_api import async_playwright, expect

from bc_common import (
    BASE, SHOTS, Api, Checks, aspect_ok, cell, cell_point, file_payload, grid_rect, launch,
    new_context, png, scene, sort_body, spot_body, stamp, text_squash, until,
)

C = Checks("editor")


class Editor:
    def __init__(self, page, api: Api, game_id: int):
        self.page, self.api, self.game_id = page, api, game_id

    async def open(self):
        await self.page.goto(f"{BASE}/host/games/{self.game_id}")
        # The page renders only once the questions have loaded; then wait for the library.
        await expect(self.page.get_by_role("heading", name="Images")).to_be_visible(timeout=15000)
        await expect(self.page.get_by_text("Loading images…")).to_have_count(0, timeout=15000)

    async def edit(self, index: int):
        await self.page.get_by_role("button", name="Edit", exact=True).nth(index).click()
        await expect(self.save_button).to_be_visible()

    async def add(self):
        await self.page.get_by_role("button", name="Add Question").click()
        await expect(self.save_button).to_be_visible()

    @property
    def save_button(self):
        return self.page.get_by_role("button", name=re.compile("^(Save Question|Saving…)$"))

    async def save(self) -> str:
        """Save; returns the form's error text ('' when the form closed). The server's response
        (the question as stored, with its review status) is kept in `last_response`."""
        def is_save(response):
            return "/questions" in response.url and response.request.method in ("POST", "PUT")

        async with self.page.expect_response(is_save) as info:
            await self.save_button.click()
        response = await info.value
        self.last_response = await response.json()
        alert = self.page.locator("p[role=alert]")
        for _ in range(60):
            await self.page.wait_for_timeout(100)
            if await self.save_button.count() == 0:
                return ""
            if await alert.count():
                return text_squash(await alert.inner_text())
        return "(still open)"

    async def cancel(self):
        await self.page.get_by_role("button", name="Cancel", exact=True).first.click()

    def select(self, n: int):
        return self.page.locator("select").nth(n)

    async def set_type(self, value: str):
        await self.select(0).select_option(value)

    async def set_grading(self, value: str):
        await self.select(1).select_option(value)

    async def pick_spot_image(self, description: str):
        trigger = self.page.get_by_role("button", name=re.compile("^(Add|Change) spot it image$"))
        await trigger.click()
        await self.page.get_by_role("button", name=f"Use image: {description}").click()

    async def grid_values(self) -> tuple[str, str]:
        return (await self.page.get_by_label("Grid columns").input_value(),
                await self.page.get_by_label("Grid rows").input_value())

    async def set_grid(self, cols: int | None = None, rows: int | None = None):
        if cols is not None:
            await self.page.get_by_label("Grid columns").fill(str(cols))
        if rows is not None:
            await self.page.get_by_label("Grid rows").fill(str(rows))

    async def limits(self) -> str | None:
        loc = self.page.locator("[data-spot-limits]")
        return await loc.get_attribute("data-spot-limits") if await loc.count() else None

    @property
    def checkbox(self):
        return self.page.locator('label:has-text("Accepted cells checked") input[type=checkbox]')

    @property
    def canvas(self):
        return self.page.locator("canvas[role=application]")

    async def click_cell(self, col: int, row: int):
        # Wait until the canvas has its layout (a zero-size grid rect would swallow the click).
        for _ in range(50):
            _, _, w, h = await grid_rect(self.canvas)
            if w > 0 and h > 0:
                break
            await self.page.wait_for_timeout(100)
        await self.canvas.scroll_into_view_if_needed()  # mouse coordinates are the viewport's
        x, y = await cell_point(self.canvas, col, row)
        await self.page.mouse.click(x, y)

    async def cell_count(self) -> str:
        return text_squash(await self.page.get_by_text(re.compile(r"of max \d+ accepted cells")).inner_text())

    async def badges(self) -> int:
        return await self.page.locator("[data-needs-review]").count()


def without(q: dict, *fields: str) -> dict:
    return {k: v for k, v in q.items() if k not in fields}


async def limits_and_drafts(page, api: Api):
    course_id, game_id = api.setup_game("Editor limits")
    shapes = {
        "Wide 16:9": ((1600, 900), "10x6", (10, 7)),
        "Tall 9:16": ((900, 1600), "6x10", (7, 10)),
        "Strip 1100x200": ((1100, 200), "10x2", (10, 3)),
        "Strip 1101x200": ((1101, 200), "10x1", None),
        "Pillar 140x830": ((140, 830), "2x10", (3, 10)),
        "Pillar 140x831": ((140, 831), "1x10", None),
        "Box 4:3": ((1600, 1200), "10x8", (10, 9)),
    }
    ids = {name: api.upload(game_id, png("teal", size), name)["id"] for name, (size, _, _) in shapes.items()}
    ed = Editor(page, api, game_id)
    await ed.open()
    await ed.add()
    await ed.set_type("find_in_image")
    for name, (size, expected, over) in shapes.items():
        await ed.pick_spot_image(name)
        shown = await ed.limits()
        C.check(f"editor limits for {name} = {expected}", shown == expected, shown)
        # The server's confirmation limits agree: the limit confirms, one more is refused.
        max_cols, max_rows = (int(v) for v in expected.split("x"))
        image = ids[name]
        if min(max_cols, max_rows) >= 2:
            r = api.req("POST", f"/api/host/games/{game_id}/questions",
                        json=spot_body(image, max_cols, max_rows, [cell(0, 0)], confirm=stamp(image, 1, max_cols, max_rows)))
            C.check(f"server confirms {expected} on {name}", r.status_code == 201, r.text)
        if over:
            r = api.req("POST", f"/api/host/games/{game_id}/questions",
                        json=spot_body(image, over[0], over[1], [cell(0, 0)], confirm=stamp(image, 1, *over)))
            C.check(f"server refuses {over[0]}x{over[1]} on {name}", r.status_code == 422, r.status_code)
        else:
            r = api.req("POST", f"/api/host/games/{game_id}/questions",
                        json=spot_body(image, 2, 2, [cell(0, 0)], confirm=stamp(image, 1, 2, 2)))
            C.check(f"server refuses every confirmation on {name} (fits no grid)", r.status_code == 422, r.status_code)
    await ed.cancel()

    # Default grid: the first image's default, recomputed while untouched; kept once edited.
    await ed.open()
    await ed.add()
    await ed.set_type("find_in_image")
    await ed.pick_spot_image("Wide 16:9")
    C.check("default grid for 16:9 is 6×3", await ed.grid_values() == ("6", "3"), await ed.grid_values())
    await ed.pick_spot_image("Tall 9:16")
    C.check("untouched default is recomputed for 9:16 (6×10)", await ed.grid_values() == ("6", "10"), await ed.grid_values())
    await ed.set_grid(cols=5)
    await ed.pick_spot_image("Box 4:3")
    C.check("an edited grid is kept when the image changes", await ed.grid_values() == ("5", "10"), await ed.grid_values())
    C.check("the new image's limits show", await ed.limits() == "10x8", await ed.limits())

    # A too-dense draft: the checkbox is disabled, the draft still saves (needs review).
    await ed.pick_spot_image("Wide 16:9")
    await ed.set_grid(cols=10, rows=7)
    C.check("checkbox disabled while the grid doesn't fit", await ed.checkbox.is_disabled())
    await ed.click_cell(0, 0)
    await page.get_by_placeholder("Question text…").fill("Too dense draft")
    error = await ed.save()
    C.check("a too-dense draft saves", error == "", error)
    draft = [q for q in api.questions(game_id) if q["prompt"] == "Too dense draft"][0]
    C.check("the draft needs review (grid_too_dense, not_confirmed)",
            draft["review_reasons"] == ["grid_too_dense", "not_confirmed"], draft["review_reasons"])

    # Keyboard on the editor canvas.
    await ed.add()
    await ed.set_type("find_in_image")
    await ed.pick_spot_image("Wide 16:9")
    await ed.canvas.focus()
    for key in ("ArrowRight", "ArrowDown", "Enter"):
        await page.keyboard.press(key)
    status = text_squash(await page.get_by_text(re.compile(r"of max \d+ accepted cells")).inner_text())
    C.check("keyboard toggles a cell on the editor canvas (B2)", "1 of max 9 accepted cells: B2" in status, status)
    await ed.cancel()

    # A loaded question keeps its grid when another image is chosen.
    loaded = [q for q in api.questions(game_id) if q["type"] == "find_in_image"][0]
    index = [q["id"] for q in api.questions(game_id)].index(loaded["id"])
    await ed.edit(index)
    before = await ed.grid_values()
    await ed.pick_spot_image("Box 4:3")
    C.check("a loaded question's grid is never reset", await ed.grid_values() == before, (before, await ed.grid_values()))
    C.check("choosing another image unticks the checkbox", not await ed.checkbox.is_checked())
    await ed.cancel()
    return course_id, game_id


async def replace_flows(browser, page, api: Api):
    # (a) Tick-only reconfirmation after a same-shape replace; the badge clears at once.
    course_id, game_id = api.setup_game("Editor reconfirm")
    img = api.upload(game_id, scene((1600, 900)), "Harbour")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    api.replace(game_id, img, scene((1600, 900), 3))
    ed = Editor(page, api, game_id)
    await ed.open()
    C.check("Needs review badge after a replace", await ed.badges() == 1)
    await ed.edit(0)
    C.check("the box starts unticked after a replace", not await ed.checkbox.is_checked())
    reason = text_squash(await page.get_by_text(re.compile("^Needs review — ")).first.inner_text())
    C.check("the form shows the reason", "Its image was replaced after the cells were checked" in reason, reason)
    await ed.checkbox.check()
    error = await ed.save()
    C.check("tick-only reconfirmation saves", error == "", error)
    C.check("the badge clears at once (from the save response)", await ed.badges() == 0)
    await ed.open()
    C.check("and stays clear after a reload", await ed.badges() == 0)
    stored = ed.last_response
    C.check("the stored stamp carries the new version", stored["answer_data"]["confirmed_for"]["image_version"] == 2, stored["answer_data"])

    # (b) Landscape → portrait with a 6×6 grid (fits both): tick only.
    img = api.upload(game_id, scene((1600, 900)), "Square grid")["id"]
    api.question(game_id, spot_body(img, 6, 6, [cell(2, 2)], confirm=stamp(img, 1, 6, 6), prompt="Six by six"))
    api.replace(game_id, img, scene((900, 1600)))
    await ed.open()
    await ed.edit(1)
    await ed.checkbox.check()
    error = await ed.save()
    C.check("landscape → portrait, 6×6: tick-only reconfirmation saves", error == "", error)

    # (c) Landscape → portrait with a 10×6 grid: the box stays disabled until the grid fits; the
    #     editor canvas uses the library's (new) dimensions; after the fix the projector does too.
    img = api.upload(game_id, scene((1600, 900)), "Ten by six")["id"]
    q3 = api.question(game_id, spot_body(img, 10, 6, [cell(9, 0), cell(3, 2)], confirm=stamp(img, 1, 10, 6), prompt="Ten by six"))["id"]
    api.replace(game_id, img, scene((900, 1600)))
    await ed.open()
    await ed.edit(2)
    _, _, w, h = await grid_rect(ed.canvas)
    C.check("editor canvas uses the library's dimensions (900:1600 ±1 px)", aspect_ok(w, h, 900, 1600), (w, h))
    stored = api.get_question(game_id, q3)["config"]
    C.check("while the stored config still says 1600×900", (stored["width"], stored["height"]) == (1600, 900), stored)
    C.check("the box is disabled while 10×6 doesn't fit", await ed.checkbox.is_disabled())
    await ed.set_grid(cols=6, rows=10)
    await page.get_by_role("button", name="Remove them").click()
    C.check("once the grid fits, the box is enabled", await ed.checkbox.is_enabled())
    await ed.checkbox.check()
    error = await ed.save()
    C.check("the fixed question confirms", error == "", error)

    # Projector: start a room and read the canvas aspect.
    for q in api.questions(game_id)[:2]:  # only the fixed question in play: drop the other two
        api.req("DELETE", f"/api/host/games/{game_id}/questions/{q['id']}")
    code, _ = api.room(course_id, game_id)
    pctx = await new_context(browser, api.token)
    projector = await pctx.new_page()
    await projector.goto(f"{BASE}/host/game/{code}/lobby")
    phone_ctx = await new_context(browser, api.guest(code, "Pia"), phone="390x844@3")
    phone = await phone_ctx.new_page()
    await phone.goto(f"{BASE}/player/game/{code}/lobby")
    await expect(projector.get_by_text("1 player joined")).to_be_visible(timeout=15000)
    await projector.get_by_role("button", name="Start Game").click()
    canvas = projector.locator("canvas[data-grid-rect]")
    await expect(canvas).to_have_attribute("data-grid-rows", "10", timeout=15000)
    _, _, w, h = await grid_rect(canvas)
    C.check("after fixing and confirming, the projector's grid is 900:1600 too", aspect_ok(w, h, 900, 1600), (w, h))
    await pctx.close()
    await phone_ctx.close()

    # The Replace warning for a canvas use, and Needs review after a replace from the panel —
    # made while the question's form is open, so the form sees the new version arrive.
    course_id, game_id = api.setup_game("Editor panel replace")
    img = api.upload(game_id, scene((1600, 900)), "Panel image")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    ed = Editor(page, api, game_id)
    await ed.open()
    await ed.edit(0)
    C.check("the open form starts ticked (confirmed)", await ed.checkbox.is_checked())
    await page.get_by_role("button", name="Replace Panel image").click()
    warning = text_squash(await page.get_by_role("alert").filter(has_text="will need review").inner_text())
    C.check("Replace warns about the Spot It use", "Q1 (Spot It) will need review" in warning, warning)
    async with page.expect_file_chooser() as chooser:
        await page.get_by_role("button", name="Choose a file").click()
    await (await chooser.value).set_files(file_payload("panel.png", scene((1600, 900), 5)))
    # The open form shows the reloaded question's reason (the list's badge is hidden behind it).
    reason = page.get_by_text(re.compile("^Needs review — "))
    await expect(reason).to_contain_text("Its image was replaced after the cells were checked", timeout=15000)
    C.check("the open form shows Needs review after the replace", True)
    await expect(ed.checkbox).not_to_be_checked(timeout=10000)
    C.check("a replace while the form is open unticks its box (the new version)", True)
    await ed.pick_spot_image("Panel image")
    C.check("re-selecting the replaced image leaves the box unticked", not await ed.checkbox.is_checked())
    await ed.cancel()
    C.check("the question list shows Needs review after the replace", await ed.badges() == 1)


async def live_and_play(page, api: Api):
    # Re-selecting the current image, the live freeze's 409 in the form, and the room gate's 409
    # on Play.
    course_id, game_id = api.setup_game("Editor live")
    img = api.upload(game_id, scene((1600, 900)), "Live image")["id"]
    qid = api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))["id"]
    api.room(course_id, game_id)
    ed = Editor(page, api, game_id)
    await ed.open()

    # Choosing the image that is already selected changes nothing, so a prompt-only save in the
    # live lobby still sends the loaded stamp (unticking would un-confirm it: the freeze's 409).
    before = api.get_question(game_id, qid)
    await ed.edit(0)
    grid_before, cells_before = await ed.grid_values(), await ed.cell_count()
    C.check("live lobby: the confirmed question opens ticked", await ed.checkbox.is_checked())
    await ed.pick_spot_image("Live image")
    C.check("re-selecting the current image keeps the box ticked", await ed.checkbox.is_checked())
    C.check("…and the grid and cells", (await ed.grid_values(), await ed.cell_count()) == (grid_before, cells_before),
            (grid_before, cells_before, await ed.grid_values(), await ed.cell_count()))
    await page.get_by_placeholder("Question text…").fill("Find the lighthouse — reworded")
    error = await ed.save()
    C.check("live lobby: a prompt-only save after re-selecting the image succeeds", error == "", error)
    after = ed.last_response
    C.check("…the question stays confirmed", after.get("needs_review") is False and after.get("review_reasons") == [],
            (after.get("needs_review"), after.get("review_reasons")))
    C.check("…and only the prompt changed", without(after, "prompt") == without(before, "prompt"),
            (without(before, "prompt"), without(after, "prompt")))

    await ed.edit(0)
    await ed.set_grid(cols=5)
    await ed.click_cell(0, 0)
    error = await ed.save()
    C.check("live freeze 409 shown in the form",
            error.startswith("Q1 can't be changed while a session of this game is live;"), error)
    await ed.cancel()

    course2, game2 = api.setup_game("Editor play gate")
    img = api.upload(game2, scene((1600, 900)), "Gate image")["id"]
    api.question(game2, spot_body(img, 6, 4, [cell(1, 1)]))  # unconfirmed
    await page.goto(f"{BASE}/host/home")
    course_label = f"BC Editor play gate {api.tag} (T7 checks)"
    await page.locator("select").nth(0).select_option(label=course_label)
    await page.locator("select").nth(1).select_option(label=f"Editor play gate {api.tag}")
    await page.get_by_role("button", name="Create Room").click()
    message = page.get_by_text(re.compile("^These Spot It questions need review"))
    await expect(message).to_be_visible(timeout=10000)
    text = text_squash(await message.inner_text())
    C.check("room 409 on Play names the question and the fix",
            "Q1 — its accepted cells haven't been checked." in text and text.endswith("and confirm it."), text)


async def round_trips(page, api: Api):
    """Real-editor round trips during a live lobby: change only the prompt; everything else,
    and the review state, stays exactly as stored."""
    course_id, game_id = api.setup_game("Editor round trips")
    img = api.upload(game_id, scene((1600, 900)), "Round trip image")["id"]
    confirmed = stamp(img, 1, 6, 4)
    boxes, cards = ["Mammal", "Reptile", "Amphibian"], ["Whale", "Gecko", "Frog", "Bat"]
    variants = [
        ("Sort It ACCURACY 708.333", sort_body(boxes, cards, [0, 1, 2, 0], points=708.333)),
        ("Sort It COMPLETENESS with key", sort_body(boxes, cards, [0, 1, 2, 0], grading="COMPLETENESS", points=0.1)),
        ("Sort It COMPLETENESS without key", sort_body(boxes, cards, None, grading="COMPLETENESS")),
        ("Sort It U+FEFF label", sort_body(boxes, ["Whale", "Gecko", "﻿Frog", "Bat"], [0, 1, 2, 0])),
        ("Spot It ACCURACY out of order", spot_body(img, 6, 4, [cell(4, 2), cell(1, 0), cell(3, 1)], points=708.333, confirm=confirmed)),
        ("Spot It COMPLETENESS absent", spot_body(img, 6, 4, None, grading="COMPLETENESS", confirm=confirmed)),
        ("Spot It COMPLETENESS empty", spot_body(img, 6, 4, [], grading="COMPLETENESS", confirm=confirmed)),
    ]
    ids = [api.question(game_id, body)["id"] for _, body in variants]
    api.room(course_id, game_id)
    ed = Editor(page, api, game_id)
    for index, ((name, _), qid) in enumerate(zip(variants, ids)):
        before = api.get_question(game_id, qid)
        await ed.open()
        await ed.edit(index)
        prompt = page.get_by_placeholder("Question text…")
        await prompt.fill(f"{before['prompt']} — reworded")
        error = await ed.save()
        C.check(f"round trip in a live lobby saves: {name}", error == "", error)
        after = await until(page, lambda: (lambda q: q if q["prompt"] != before["prompt"] else None)(api.get_question(game_id, qid)),
                            what=f"{name}: the committed prompt")
        C.check(f"round trip changes only the prompt: {name}",
                without(after, "prompt") == without(before, "prompt"), (without(before, "prompt"), without(after, "prompt")))


async def stale_list(page, api: Api):
    """A confirmed question opened right after a replace in another tab (the library shows the
    new version, the list is still stale) starts unticked; a prompt-only save succeeds."""
    course_id, game_id = api.setup_game("Editor stale list")
    img = api.upload(game_id, scene((1600, 900)), "Stale image")["id"]
    api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], confirm=stamp(img, 1, 6, 4)))
    ed = Editor(page, api, game_id)
    await ed.open()
    C.check("confirmed before the replace (no badge)", await ed.badges() == 0)
    api.replace(game_id, img, scene((1600, 900), 4))  # "another tab"
    # Any library write reloads the library (not the list): upload a second image in the panel.
    await page.get_by_label("Image file to upload").first.set_input_files(file_payload("another.png", png("purple", (200, 200))))
    await page.get_by_label("Image description").first.fill("Another image")
    await page.get_by_role("button", name="Upload", exact=True).click()
    await expect(page.get_by_text("Another image")).to_be_visible(timeout=10000)
    await expect(page.get_by_text(re.compile("replaced 1×"))).to_be_visible(timeout=10000)  # the reloaded library
    C.check("the question list is still stale (no badge)", await ed.badges() == 0)
    await ed.edit(0)
    C.check("the box starts unticked: the library shows a newer version", not await ed.checkbox.is_checked())
    await ed.pick_spot_image("Stale image")
    C.check("re-selecting the same (replaced) image doesn't tick it", not await ed.checkbox.is_checked())
    await page.get_by_placeholder("Question text…").fill("Prompt only")
    error = await ed.save()
    C.check("the prompt-only save succeeds (no stale stamp resent)", error == "", error)
    C.check("the question reports not_confirmed", ed.last_response["review_reasons"] == ["not_confirmed"], ed.last_response["review_reasons"])


async def answer_keys(page, api: Api):
    course_id, game_id = api.setup_game("Editor keys")
    ed = Editor(page, api, game_id)
    await ed.open()

    async def fill_sort(boxes, cards):
        for i, text in enumerate(boxes):
            if i >= 2:
                await page.get_by_role("button", name="Add box").click()
            await page.get_by_label(f"Box {i + 1}", exact=True).fill(text)
        for i, text in enumerate(cards):
            if i >= 4:
                await page.get_by_role("button", name="Add card").click()
            await page.get_by_label(f"Card {i + 1}", exact=True).fill(text)

    async def problems() -> list[str]:
        return [text_squash(t) for t in await page.locator("ul[aria-live=polite] li").all_inner_texts()]

    # ACCURACY: Save blocked until every card has a box.
    await ed.add()
    await ed.set_type("sort_into_boxes")
    await page.get_by_placeholder("Question text…").fill("Land or sea")
    await page.get_by_label("Points").fill("1000")
    await fill_sort(["Land", "Sea"], ["Cat", "Shark", "Dog", "Eel"])
    for i, box in enumerate(["0", "1", "0"]):
        await page.get_by_label(f"Card {i + 1} correct box").select_option(box)
    C.check("ACCURACY: Save blocked with a card unassigned", await ed.save_button.is_disabled() and "Assign every card a box" in await problems(), await problems())
    await page.get_by_label("Card 4 correct box").select_option("1")
    C.check("ACCURACY: Save enabled once every card has a box", await ed.save_button.is_enabled())
    C.check("ACCURACY key saves", await ed.save() == "")

    # COMPLETENESS: the toggle; on with a card unassigned blocks Save; off saves no key.
    await ed.add()
    await ed.set_type("sort_into_boxes")
    await ed.set_grading("COMPLETENESS")
    await page.get_by_placeholder("Question text…").fill("Pets")
    await fill_sort(["Yes", "No"], ["Cat", "Snake", "Frog", "Goldfish"])
    await page.get_by_label("Include an answer key (not scored)").check()
    for i in range(3):
        await page.get_by_label(f"Card {i + 1} correct box").select_option("0")
    C.check("COMPLETENESS key on with a card unassigned blocks Save",
            await ed.save_button.is_disabled() and "Assign every card a box, or turn off the answer key" in await problems(), await problems())
    await page.get_by_label("Include an answer key (not scored)").uncheck()
    C.check("turning the key off saves", await ed.save() == "")
    pets = [q for q in api.questions(game_id) if q["prompt"] == "Pets"][0]
    C.check("…with no correct_boxes", pets["answer_data"] == {}, pets["answer_data"])

    # Adding a card or removing a box never sends a partial key.
    await ed.edit(0)
    await page.get_by_role("button", name="Add card").click()
    await page.get_by_label("Card 5", exact=True).fill("Frog")
    C.check("a new card starts unassigned: Save blocked", await ed.save_button.is_disabled())
    await page.get_by_role("button", name="Remove card 5").click()
    await page.get_by_role("button", name="Add box").click()
    await page.get_by_label("Box 3", exact=True).fill("Air")
    await page.get_by_label("Card 1 correct box").select_option("2")
    await page.get_by_role("button", name="Remove box 3").click()
    C.check("removing a box unassigns its cards: Save blocked", await ed.save_button.is_disabled())
    await ed.cancel()

    # Grading switches keep the key; with no other edits it saves exactly as loaded.
    land = [q for q in api.questions(game_id) if q["prompt"] == "Land or sea"][0]
    await ed.open()
    await ed.edit(0)
    await ed.set_grading("COMPLETENESS")
    C.check("ACCURACY → COMPLETENESS turns the key toggle on", await page.get_by_label("Include an answer key (not scored)").is_checked())
    await ed.set_grading("ACCURACY")
    C.check("switching back and saving works", await ed.save() == "")
    after = ed.last_response
    C.check("…and the key is exactly as loaded", after["answer_data"] == land["answer_data"], after["answer_data"])

    # Spot It COMPLETENESS: absent stays absent; a loaded non-empty key emptied saves [].
    img = api.upload(game_id, scene((1600, 900)), "Key image")["id"]
    absent = api.question(game_id, spot_body(img, 6, 4, None, grading="COMPLETENESS", confirm=stamp(img, 1, 6, 4), prompt="Absent"))
    full = api.question(game_id, spot_body(img, 6, 4, [cell(1, 1)], grading="COMPLETENESS", confirm=stamp(img, 1, 6, 4), prompt="Emptied"))
    ids = [q["id"] for q in api.questions(game_id)]
    await ed.open()
    await ed.edit(ids.index(absent["id"]))
    await ed.click_cell(2, 2)
    C.check("absent key: a click ticks the cell", (await ed.cell_count()).startswith("1 of max 12 accepted cells: C3"), await ed.cell_count())
    await ed.click_cell(2, 2)
    C.check("absent key: a second click unticks it", (await ed.cell_count()).startswith("0 of max 12"), await ed.cell_count())
    C.check("absent key, ticked and unticked: saves", await ed.save() == "")
    stored = ed.last_response["answer_data"]
    C.check("…and stays absent", "accepted_cells" not in stored, stored)
    await ed.edit(ids.index(full["id"]))
    C.check("loaded key shows its cell", (await ed.cell_count()).startswith("1 of max 12 accepted cells: B2"), await ed.cell_count())
    await ed.click_cell(1, 1)
    C.check("clicking it empties the key", (await ed.cell_count()).startswith("0 of max 12"), await ed.cell_count())
    C.check("a loaded non-empty key emptied: saves", await ed.save() == "")
    stored = ed.last_response["answer_data"]
    C.check("…as []", stored.get("accepted_cells") == [], stored)

    # Spot It ACCURACY → COMPLETENESS → ACCURACY keeps the cells exactly.
    acc = api.question(game_id, spot_body(img, 6, 4, [cell(4, 2), cell(1, 0)], confirm=stamp(img, 1, 6, 4), prompt="Spot switch"))
    ids = [q["id"] for q in api.questions(game_id)]
    await ed.open()
    await ed.edit(ids.index(acc["id"]))
    await ed.set_grading("COMPLETENESS")
    await ed.set_grading("ACCURACY")
    C.check("Spot It grading switched back saves", await ed.save() == "")
    stored = ed.last_response["answer_data"]
    C.check("…with the cells exactly as loaded", stored == acc["answer_data"], stored)

    # Text rules: "Straße" / "STRASSE" pass the editor's check, and the server's 422 shows.
    await ed.add()
    await ed.set_type("sort_into_boxes")
    await page.get_by_placeholder("Question text…").fill("Streets")
    await page.get_by_label("Points").fill("10")
    await fill_sort(["Straße", "STRASSE"], ["a", "b", "c", "d"])
    for i in range(4):
        await page.get_by_label(f"Card {i + 1} correct box").select_option("0")
    C.check("the editor accepts Straße and STRASSE", await ed.save_button.is_enabled(), await problems())
    error = await ed.save()
    C.check("the server's duplicate 422 shows in the form", "Boxes 1 and 2 are the same (ignoring case)" in error, error)
    await ed.cancel()


async def main():
    api = Api()
    failures = 0
    try:
        async with async_playwright() as pw:
            browser = await launch(pw)
            context = await new_context(browser, api.token)
            page = await context.new_page()
            for step in (limits_and_drafts, replace_flows, live_and_play, round_trips, stale_list, answer_keys):
                try:
                    if step is replace_flows:
                        await step(browser, page, api)
                    else:
                        await step(page, api)
                except Exception as exc:  # record and continue with the next group
                    failures += 1
                    C.check(f"{step.__name__} ran to completion", False, repr(exc)[:500])
                    await page.screenshot(path=str(SHOTS / f"editor_{step.__name__}_error.png"), full_page=True)
            await browser.close()
    finally:
        api.cleanup()
    sys.exit(C.summary())


if __name__ == "__main__":
    asyncio.run(main())
