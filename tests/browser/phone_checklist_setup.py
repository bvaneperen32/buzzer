"""Create the test game for the physical-phone checklist (spec §4.7): "T7 phone checklist".

    python tests/browser/phone_checklist_setup.py [--replace]

Same configuration as the browser checks (README.md): BUZZER_BROWSER_BASE_URL and the admin
credentials. The game is kept for re-runs. With --replace, an existing game of this title in the
course is deleted first, with its sessions and scores.
"""

from __future__ import annotations

import argparse
import io
import math
import random
import sys

from PIL import Image, ImageDraw

from bc_common import BASE, Api, cell, sort_body, spot_body, stamp

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replace", action="store_true", help="delete an existing game of this title in the course first")
    args = parser.parse_args()

    api = Api()
    course = next((c for c in api.ok("GET", "/api/admin/courses")
                   if c["name"] == COURSE["name"] and c["semester"] == COURSE["semester"]), None)
    if course is None:
        course = api.ok("POST", "/api/admin/courses", 201, json=COURSE)
    existing = [g for g in api.ok("GET", "/api/admin/games") if g["title"] == TITLE and g["course_id"] == course["id"]]
    if existing and not args.replace:
        sys.exit(f'"{TITLE}" already exists (game {existing[0]["id"]}); pass --replace to recreate it.')
    for game in existing:
        api.ok("DELETE", f"/api/admin/games/{game['id']}", 204)
    game_id = api.ok("POST", "/api/admin/games", 201, json={
        "title": TITLE, "course_id": course["id"],
        "description": "Sort It and Spot It on a real phone (T7 spec §4.7)",
    })["id"]

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
    print(f'Created "{TITLE}" (game {game_id}) in "{COURSE["name"]} ({COURSE["semester"]})":')
    for number, (what, use, _) in enumerate(questions, start=1):
        answer = f" — answer: {answers[number]}" if number in answers else ""
        print(f"  Q{number}  {what}; checklist {use}{answer}")
    print(f"Host: {BASE}/host/ → Play → choose the course and the game → Create Game Room.")


if __name__ == "__main__":
    main()
