"""Answers on the original question types (multiple choice, true/false, fill in the blank) and
what the phone's results say about them (docs/plans/fix-live-session-test-cleanup.md §3.14).

An answer the server counted shows as answered — right, wrong, or worth 0 points — with what
the player sent. One it didn't count shows "No answer", never "Answer recorded!", "Incorrect" or
"You answered …": an answer refused as late, one dropped after a reconnect because the question
had locked meanwhile, and one never given. The results take that from the server's own record
(`answered`); what the phone sent, and zero points, prove nothing. A refusal for another question
leaves the current question's accepted answer alone.

Show Results while an answer is still being recorded past the server's wait (§3.14): nothing is
published, the projector says so and keeps Show Results, and pressing it again once the answer
has finished shows the results, counting it. The answer is held by locking its question's row in
MySQL (an answer's INSERT waits on it), so this script needs the stack's MySQL on 3306
(`BUZZER_TEST_DB_*`, as the integration suite's lock tests).

    cd tests/browser && python bc_answers.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from contextlib import contextmanager

from playwright.async_api import async_playwright, expect

from bc_common import BASE, Api, Checks, SocketProxy, launch, new_context, text_squash, until
from tests.integration.conftest import db_settings

C = Checks("answers")

QUESTIONS = [
    # 1. Ana: a wrong option (accepted, 0 points). Ben: refused as late.
    {"type": "multiple_choice", "grading_type": "ACCURACY", "prompt": "Closest to the Sun?",
     "config": {"options": ["Venus", "Mercury", "Mars", "Earth"]},
     "answer_data": {"answer_points": [0, 1000, 0, 0]}, "points_value": 1000},
    # 2. Completeness worth 0 points. Ana: accepted, still counted. Ben: never answers.
    {"type": "true_false", "grading_type": "COMPLETENESS", "prompt": "Worth nothing",
     "points_value": 0},
    # 3. Ana: right. Ben: sent while offline, dropped on the rejoin (the question had locked).
    {"type": "fill_in_the_blank", "grading_type": "ACCURACY", "prompt": "Capital of France?",
     "answer_data": {"acceptedAnswers": ["Paris"], "answerPoints": [1000], "editDistance": 0},
     "points_value": 1000},
    # 4. Completeness. Ana: accepted. Ben: refused as late.
    {"type": "true_false", "grading_type": "COMPLETENESS", "prompt": "Pick one",
     "points_value": 500},
    # 5. Ben: right, then a stale answer to question 4 is refused. Ana: never answers.
    {"type": "true_false", "grading_type": "ACCURACY", "prompt": "Water boils at 100 °C.",
     "answer_data": {"answer_points": {"true": 1000, "false": 0}}, "points_value": 1000},
    # 6. Ana: right, but still being recorded when the host shows the results. Ben: never answers.
    {"type": "true_false", "grading_type": "ACCURACY", "prompt": "The Moon orbits the Earth.",
     "answer_data": {"answer_points": {"true": 1000, "false": 0}}, "points_value": 1000},
]


@contextmanager
def question_row_held(question_id: int):
    """Lock the question's row (FOR UPDATE) until the block ends. An answer's INSERT share-locks
    its question's row (the foreign key check), so an answer to it stays "being recorded"
    meanwhile. Yields a function that waits until the backend's INSERT is waiting on it."""
    import pymysql

    settings = db_settings()
    connect = dict(host=settings["host"], port=settings["port"], user=settings["user"],
                   password=settings["password"], database=settings["database"], connect_timeout=5)
    lock = pymysql.connect(autocommit=False, **connect)
    watch = pymysql.connect(autocommit=True, **connect)

    def inserts_waiting() -> int:
        with watch.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.processlist "
                        "WHERE db = DATABASE() AND info LIKE 'INSERT INTO session_scores%%'")
            return cur.fetchone()[0]

    async def insert_waiting(page) -> None:
        await until(page, inserts_waiting, what="the answer's INSERT waiting on the row lock")

    try:
        with lock.cursor() as cur:
            cur.execute("SELECT id FROM questions WHERE id = %s FOR UPDATE", (question_id,))
        yield insert_waiting
    finally:
        lock.rollback()
        lock.close()
        watch.close()


class Phone:
    def __init__(self, name, page, sockets):
        self.name, self.page, self.sockets = name, page, sockets

    async def results(self) -> str:
        return text_squash(await self.page.locator("[data-results]").inner_text(timeout=15000))

    async def accepted(self) -> None:
        await expect(self.page.get_by_text("Answer locked in!")).to_be_visible(timeout=10000)

    async def never_accepted(self) -> bool:
        return await self.page.get_by_text("Answer locked in!").count() == 0


async def join(browser, api, code, name) -> Phone:
    context = await new_context(browser, api.guest(code, name), phone="390x844@3")
    page = await context.new_page()
    sockets = SocketProxy()
    await sockets.attach(page)
    await page.goto(f"{BASE}/player/game/{code}/lobby")
    await expect(page.get_by_text(re.compile("Waiting for host to start"))).to_be_visible(timeout=15000)
    return Phone(name, page, sockets)


async def answers_and_results(browser, api: Api):
    course_id, game_id = api.setup_game("Answers")
    ids = [api.question(game_id, {**q, "time_limit_seconds": 120})["id"] for q in QUESTIONS]
    code, _ = api.room(course_id, game_id)
    context = await new_context(browser, api.token)
    projector = await context.new_page()
    await projector.goto(f"{BASE}/host/game/{code}/lobby")
    ana = await join(browser, api, code, "Ana")
    ben = await join(browser, api, code, "Ben")
    await expect(projector.get_by_text("2 players joined")).to_be_visible(timeout=15000)

    async def host(button: str) -> None:
        await projector.get_by_role("button", name=button).click()

    async def question(number: int, button: str) -> None:
        await host(button)
        for phone in (ana, ben):
            await expect(phone.page.get_by_text(f"Question {number} of {len(QUESTIONS)}")).to_be_visible(
                timeout=15000)

    async def lock() -> None:
        await host("Lock Question")
        await expect(projector.get_by_role("button", name="Unlock Question")).to_be_visible(timeout=10000)

    async def refused_as_late(phone: Phone, tap) -> None:
        """The phone hasn't heard of the lock (held back) when it answers."""
        phone.sockets.withhold("question_locked")
        await lock()
        await tap()
        await expect(phone.page.get_by_text("Too late — answers had already closed")).to_be_visible(timeout=10000)
        phone.sockets.deliver_withheld()

    async def results() -> tuple[str, str]:
        await host("Show Results")
        return await ana.results(), await ben.results()

    # 1. Multiple choice (accuracy): a wrong answer counts; a late one doesn't.
    await question(1, "Start Game")
    await ana.page.get_by_role("button", name=re.compile("Venus")).click()
    await ana.accepted()
    await refused_as_late(ben, ben.page.get_by_role("button", name=re.compile("Mercury")).click)
    C.check("multiple choice, refused as late: never shown as accepted", await ben.never_accepted())
    a, b = await results()
    C.check("multiple choice, accepted wrong answer: Incorrect, what was picked, +0 pts",
            a.startswith("Incorrect") and "You answered: A — Venus" in a and "+0 pts" in a, a)
    C.check("multiple choice, refused as late: No answer, not what was tapped",
            b.startswith("No answer") and "You answered" not in b and "+0 pts" in b, b)

    # 2. True/false (completeness, worth 0): answered with 0 points still counts.
    await question(2, "Next Question")
    await ana.page.get_by_role("button", name="True").click()
    await ana.accepted()
    a, b = await results()
    C.check("completeness worth 0, accepted: Answer recorded!, not No answer",
            a.startswith("Answer recorded!") and "You answered: True" in a and "+0 pts" in a, a)
    C.check("completeness, never answered: No answer, not Answer recorded!",
            b.startswith("No answer") and "You answered" not in b, b)

    # 3. Fill in the blank: an answer sent while offline is dropped when the phone re-joins a
    #    question that locked meanwhile.
    await question(3, "Next Question")
    await ana.page.get_by_placeholder("Type your answer…").fill("Paris")
    await ana.page.get_by_role("button", name="Submit").click()
    await ana.accepted()
    dropped = await ben.sockets.drop(hold=True)
    await expect(ben.page.get_by_text("Connection lost — reconnecting…")).to_be_visible(timeout=10000)
    await ben.page.get_by_placeholder("Type your answer…").fill("Paris")
    await ben.page.get_by_role("button", name="Submit").click()
    await expect(ben.page.get_by_text("Sending your answer…")).to_be_visible(timeout=5000)
    await lock()
    back = await ben.sockets.release()
    await ben.sockets.rejoined(ben.page, back)
    await expect(ben.page.get_by_text("Answers locked — waiting for results…")).to_be_visible(timeout=10000)
    C.check("fill in the blank, dropped on the rejoin: never sent, never shown as accepted",
            [at for at in ben.sockets.sent("submit_answer") if at >= dropped] == []
            and await ben.never_accepted()
            and await ben.page.get_by_text("Sending your answer…").count() == 0)
    a, b = await results()
    C.check("fill in the blank, accepted right answer: Correct!, what was typed, +1,000 pts",
            a.startswith("Correct!") and "You answered: Paris" in a and "+1,000 pts" in a, a)
    C.check("fill in the blank, dropped on the rejoin: No answer, not what was typed",
            b.startswith("No answer") and "You answered" not in b and "+0 pts" in b, b)

    # 4. True/false (completeness): a late answer doesn't count either.
    await question(4, "Next Question")
    await ana.page.get_by_role("button", name="False").click()
    await ana.accepted()
    await refused_as_late(ben, ben.page.get_by_role("button", name="True").click)
    a, b = await results()
    C.check("completeness, accepted: Answer recorded!, what was picked, +500 pts",
            a.startswith("Answer recorded!") and "You answered: False" in a and "+500 pts" in a, a)
    C.check("completeness, refused as late: No answer, not Answer recorded!",
            b.startswith("No answer") and "You answered" not in b and "+0 pts" in b, b)

    # 5. True/false (accuracy): a refusal for another question leaves this one's answer alone.
    await question(5, "Next Question")
    await ben.page.get_by_role("button", name="True").click()
    await ben.accepted()
    before = len(ben.sockets.events("answer_rejected"))
    ben.sockets.send_as_page("submit_answer", {"question_id": ids[3], "answer_data": {"selectedValue": True}})
    await until(ben.page, lambda: len(ben.sockets.events("answer_rejected")) > before,
                what="the refusal of the stale answer to question 4")
    [stale] = ben.sockets.events("answer_rejected")[before:]
    C.check("a stale answer to an earlier question is refused as closed",
            stale.get("questionId") == ids[3] and stale.get("reason") == "closed", stale)
    C.check("the phone stays on its accepted answer",
            await ben.page.get_by_text("Answer locked in!").is_visible()
            and await ben.page.get_by_text("Too late").count() == 0)
    a, b = await results()
    C.check("accuracy, accepted after another question's refusal: Correct!, what was picked",
            b.startswith("Correct!") and "You answered: True" in b and "+1,000 pts" in b, b)
    C.check("accuracy, never answered: No answer, not Incorrect",
            a.startswith("No answer") and "You answered" not in a, a)

    # 6. Show Results while an answer is still being recorded, past the server's 10 s wait: the
    #    results are held back. Showing them again, once the answer has finished, counts it.
    await question(6, "Next Question")
    notice = projector.locator("[data-results-delayed]")
    show = projector.get_by_role("button", name="Show Results")
    with question_row_held(ids[5]) as insert_waiting:
        await ana.page.get_by_role("button", name="True").click()
        await insert_waiting(projector)  # Ana's answer is being recorded...
        asked = time.monotonic()
        await show.click()  # ...when the host shows the results
        await expect(notice).to_be_visible(timeout=20000)
        waited = time.monotonic() - asked
        C.check("results delayed: after the server's wait, the projector says an answer is still being saved",
                "still being saved" in await notice.inner_text() and waited >= 9, round(waited, 1))
        C.check("results delayed: Show Results stays enabled (still the question), and no phone has results",
                await show.is_enabled()
                and await ana.page.locator("[data-results]").count() == 0
                and await ben.page.locator("[data-results]").count() == 0)
        C.check("results delayed: the phones wait, the answer still sending and the question closed",
                await ana.page.get_by_text("Sending your answer…").is_visible()
                and await ben.page.get_by_text("Answers locked — waiting for results…").is_visible())
    await ana.accepted()  # the row is free: the answer finishes
    await show.click()  # the host shows the results again
    a, b = await ana.results(), await ben.results()
    C.check("shown again: the results count the answer that was still being recorded",
            a.startswith("Correct!") and "You answered: True" in a and "+1,000 pts" in a, a)
    C.check("shown again: a question never answered is still No answer", b.startswith("No answer"), b)
    await expect(projector.get_by_role("button", name="Show Final Results")).to_be_visible(timeout=10000)
    C.check("shown again: the projector shows the results, and the notice is gone",
            await notice.count() == 0)

    await host("Show Final Results")
    await expect(projector.get_by_text("Game Over!")).to_be_visible(timeout=15000)
    C.check("the game finishes normally", True)
    for page in (projector, ana.page, ben.page):
        await page.context.close()


async def main():
    api = Api()
    try:
        async with async_playwright() as pw:
            browser = await launch(pw)
            for step in (answers_and_results,):
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
