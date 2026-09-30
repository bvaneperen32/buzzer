"""Create the test game for the physical-phone checklist (spec §4.7): "T7 phone checklist".

    python tests/browser/phone_checklist_setup.py [--replace] [--game-id N]
    python tests/browser/phone_checklist_setup.py --cleanup [--game-id N]

Same configuration as the browser checks (README.md): BUZZER_BROWSER_BASE_URL and the admin
credentials. The game is kept for re-runs, in a kept ownership ledger
(`phone-checklist.json` in the ledger directory, `tests/.ledgers/` by default) that
`tests/cleanup.py` never touches.

- `--replace` deletes the checklist game first, with its sessions and scores, and creates it
  again.
- `--cleanup` is the explicit cleanup step. It deletes the game, and the course if this script
  created it.
- Both delete only a game the ledger owns, or the one whose id is given with `--game-id` (a game
  created before the ledger existed). The game must still have the checklist's title in the
  checklist's course.
- A course this script didn't create, and guests who joined from phones, are reported, never
  deleted.
"""

from __future__ import annotations

import argparse
import io
import math
import random
import sys

from PIL import Image, ImageDraw

from bc_common import BASE, LEDGERS, Api, cell, sort_body, spot_body, stamp
from tests.ledger import Ledger, teardown

COURSE = {"name": "T7 phone checklist", "semester": "Physical phone"}
TITLE = "T7 phone checklist"


def _png(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def _boat(d: ImageDraw.ImageDraw, cx: int, cy: int, hull: tuple) -> None:
    """A sailing boat spanning cx ± 70 and cy − 75 … cy + 35."""
    d.polygon([(cx - 70, cy), (cx + 70, cy), (cx + 50, cy + 35), (cx - 50, cy + 35)], fill=hull)
    d.line([(cx, cy), (cx, cy - 75)], fill=(60, 45, 30), width=5)
    d.polygon([(cx + 4, cy - 72), (cx + 4, cy - 8), (cx + 55, cy - 8)], fill=(240, 240, 230))


def harbour() -> bytes:
    """1600×900. On the 8×5 grid (200×180 px cells): the red boat in F4, the lighthouse in H2–H3,
    decoy boats in C4 (blue) and D5 (green), the sun in B1."""
    w, h, horizon = 1600, 900, 400
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(horizon):
        t = y / horizon
        d.line([(0, y), (w, y)], fill=(int(40 + 190 * t), int(40 + 90 * t), int(110 - 30 * t)))
    for y in range(horizon, h):
        t = (y - horizon) / (h - horizon)
        d.line([(0, y), (w, y)], fill=(int(20 + 10 * t), int(70 - 30 * t), int(100 - 30 * t)))
    rng = random.Random(7)
    for _ in range(90):
        x, y = rng.randrange(0, w - 40), rng.randrange(horizon + 10, h - 10)
        d.line([(x, y), (x + 30, y)], fill=(90, 140, 160), width=2)
    d.ellipse([240, 30, 360, 150], fill=(255, 214, 90))  # sun
    d.ellipse([1440, 505, 1560, 540], fill=(70, 70, 75))  # the lighthouse's rock
    d.polygon([(1470, 530), (1530, 530), (1518, 230), (1482, 230)], fill=(245, 245, 240))
    for top in (270, 350, 430):  # red bands
        d.rectangle([1470, top, 1530, top + 35], fill=(200, 40, 40))
    d.rectangle([1478, 200, 1522, 230], fill=(255, 230, 120))  # lamp room
    d.polygon([(1472, 200), (1528, 200), (1500, 186)], fill=(200, 40, 40))
    _boat(d, 1100, 650, (200, 30, 30))  # red, F4
    _boat(d, 500, 640, (40, 80, 200))  # blue, C4
    _boat(d, 700, 820, (40, 150, 60))  # green, D5
    d.ellipse([1286, 786, 1314, 814], fill=(240, 240, 240))  # buoy, G5
    return _png(img)


def night_sky() -> bytes:
    """900×1600. On the 5×8 grid (180×200 px cells): the yellow star in B3, the moon in D2, small
    white stars elsewhere, and a skyline along the bottom two rows."""
    w, h = 900, 1600
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(10 + 30 * t), int(15 + 30 * t), int(45 + 50 * t)))
    rng = random.Random(11)
    for _ in range(140):
        x, y = rng.randrange(w), rng.randrange(1150)
        if 170 <= x < 370 and 390 <= y < 610:  # keep B3 for the yellow star
            continue
        r = rng.choice((1, 2, 2, 3))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(235, 235, 255))
    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        radius = 60 if i % 2 == 0 else 25
        points.append((270 + radius * math.cos(angle), 500 + radius * math.sin(angle)))
    d.polygon(points, fill=(255, 210, 40))  # yellow star, B3
    d.ellipse([570, 240, 690, 360], fill=(235, 235, 210))  # moon, D2
    x = 0
    while x < w:
        bw, bh = rng.randrange(70, 150), rng.randrange(180, 380)
        d.rectangle([x, h - bh, x + bw, h], fill=(25, 25, 40))
        for wy in range(h - bh + 15, h - 20, 30):
            for wx in range(x + 10, x + bw - 15, 25):
                if rng.random() < 0.35:
                    d.rectangle([wx, wy, wx + 8, wy + 12], fill=(230, 225, 190))
        x += bw + rng.randrange(0, 12)
    return _png(img)


def checklist_course(api: Api, *, create: bool) -> int | None:
    """The ledger's course if it still exists, else one matching exactly (reused, not owned),
    else — with `create` — a new, owned one."""
    for record in api.ledger.records("courses"):
        r = api.req("GET", f"/api/admin/courses/{record['id']}")
        if r.status_code == 200 and (r.json()["name"], r.json()["semester"]) == (record["name"], record["semester"]):
            return record["id"]
    course = next((c for c in api.ok("GET", "/api/admin/courses")
                   if c["name"] == COURSE["name"] and c["semester"] == COURSE["semester"]), None)
    if course is not None:
        return course["id"]
    return api.course(COURSE["name"], COURSE["semester"]) if create else None


def take_games(api: Api, course_id: int, game_id: int | None) -> list[dict]:
    """The checklist games in the course. The one named by --game-id is put in the ledger;
    any other game the ledger doesn't own is reported and exits."""
    games = [g for g in api.ok("GET", "/api/admin/games") if g["title"] == TITLE and g["course_id"] == course_id]
    owned = {r["id"] for r in api.ledger.records("games")}
    if game_id is not None and game_id not in owned:
        if not any(g["id"] == game_id for g in games):
            sys.exit(f'Game {game_id} isn\'t "{TITLE}" in the checklist course; nothing deleted.')
        api.ledger.record("games", id=game_id, title=TITLE, adopted=True)  # named by the person
        owned.add(game_id)
    untracked = [g["id"] for g in games if g["id"] not in owned]
    if untracked:
        sys.exit(f'"{TITLE}" game(s) {untracked} predate the ownership ledger, so they aren\'t deleted '
                 "automatically. Pass --game-id <id> to name the one to delete.")
    return games


def cleanup(api: Api, game_id: int | None) -> None:
    course_id = checklist_course(api, create=False)
    if course_id is not None:
        take_games(api, course_id, game_id)
    owned_course = any(r["id"] == course_id for r in api.ledger.records("courses"))
    print(f"Deleting: {api.ledger.summary()}")
    problems = teardown(api.ledger, api.http, api.token)
    for problem in problems:
        print(f"  KEPT {problem}")
    if course_id is not None and not owned_course:
        print(f'Course {course_id} ("{COURSE["name"]} ({COURSE["semester"]})") was not created by this script; '
              "it is kept.")
    print("Guests who joined from phones signed in through the app, so they aren't owned here; "
          "the admin console lists guests.")
    sys.exit(1 if problems else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replace", action="store_true", help="delete the checklist game first, then create it again")
    parser.add_argument("--cleanup", action="store_true", help="delete the checklist game (and its course, if created here)")
    parser.add_argument("--game-id", type=int, help="a checklist game created before the ledger, to delete")
    args = parser.parse_args()

    ledger = Ledger.begin(LEDGERS, "phone_checklist_setup", base_url=BASE, name="phone-checklist", keep=True)
    api = Api(ledger)
    if args.cleanup:
        cleanup(api, args.game_id)
    course_id = checklist_course(api, create=True)
    existing = take_games(api, course_id, args.game_id) if args.replace else [
        g for g in api.ok("GET", "/api/admin/games") if g["title"] == TITLE and g["course_id"] == course_id]
    if existing and not args.replace:
        sys.exit(f'"{TITLE}" already exists (game {existing[0]["id"]}); pass --replace to recreate it.')
    for game in existing:
        api.ok("DELETE", f"/api/admin/games/{game['id']}", 204)
        ledger.forget("games", game["id"])
    game_id = api.game(course_id, TITLE, description="Sort It and Spot It on a real phone (T7 spec §4.7)")

    harbour_id = api.upload(game_id, harbour(), "Harbour at dusk with boats and a lighthouse", "harbour.png")["id"]
    sky_id = api.upload(game_id, night_sky(), "Night sky over a city", "night-sky.png")["id"]
    boxes = ["Mammal", "Bird", "Reptile", "Fish"]
    cards = ["Whale", "Eagle", "Gecko", "Salmon", "Bat", "Emperor penguin", "Tortoise", "Hammerhead shark", "Dolphin", "Cobra"]
    key = [0, 1, 2, 3, 0, 1, 2, 3, 0, 2]
    questions = [
        ("Sort It, 10 cards / 4 boxes, 120 s", "items 2, 6 (host Lock / Unlock)",
         sort_body(boxes, cards, key, prompt="Sort the animals by class", time_limit=120)),
        ("Spot It, landscape 8×5, 90 s", "items 3, 4",
         spot_body(harbour_id, 8, 5, [cell(5, 3)], confirm=stamp(harbour_id, 1, 8, 5),
                   prompt="Find the red boat", time_limit=90)),
        ("Spot It, portrait 5×8, 90 s", "items 3 (rotate, background), 4",
         spot_body(sky_id, 5, 8, [cell(1, 2)], confirm=stamp(sky_id, 1, 5, 8),
                   prompt="Find the yellow star", time_limit=90)),
        ("Sort It worth 5.1 points, 60 s", "item 5 (points; the locale's separator)",
         sort_body(["Land", "Sea"], ["Cat", "Shark", "Dog", "Eel"], [0, 1, 0, 1], points=5.1,
                   prompt="Land or sea?", time_limit=60)),
        ("Sort It, COMPLETENESS (no key), 60 s", "item 2 (leave a card out)",
         sort_body(["Yes", "No"], ["Sushi", "Snails", "Durian", "Pineapple pizza"], None,
                   grading="COMPLETENESS", prompt="Would you eat it?", time_limit=60)),
        ("Spot It, COMPLETENESS (no key), 60 s", "item 5 (skip it: \"No answer\")",
         spot_body(harbour_id, 8, 5, None, grading="COMPLETENESS", confirm=stamp(harbour_id, 1, 8, 5),
                   prompt="Where would you swim?", time_limit=60)),
        ("Spot It, landscape 8×5, 20 s", "item 6 (select, don't lock in, let time run out)",
         spot_body(harbour_id, 8, 5, [cell(7, 1), cell(7, 2)], confirm=stamp(harbour_id, 1, 8, 5),
                   prompt="Find the lighthouse", time_limit=20)),
    ]
    for _, _, body in questions:
        api.question(game_id, body)
    stored = api.questions(game_id)
    unconfirmed = [i + 1 for i, q in enumerate(stored) if q.get("needs_review")]
    if unconfirmed:
        sys.exit(f"Questions needing review: {unconfirmed}")

    answers = {
        1: "Mammal: Whale, Bat, Dolphin · Bird: Eagle, Emperor penguin · Reptile: Gecko, Tortoise, Cobra · Fish: Salmon, Hammerhead shark",
        2: "F4",
        3: "B3",
        4: "Land: Cat, Dog · Sea: Shark, Eel (one card wrong → 2.6 of 5.1)",
        7: "H2 or H3",
    }
    print(f'Created "{TITLE}" (game {game_id}) in "{COURSE["name"]} ({COURSE["semester"]})" (course {course_id}):')
    for number, (what, use, _) in enumerate(questions, start=1):
        answer = f" — answer: {answers[number]}" if number in answers else ""
        print(f"  Q{number}  {what}; checklist {use}{answer}")
    print(f"Host: {BASE}/host/ → Play → choose the course and the game → Create Game Room.")


if __name__ == "__main__":
    main()
