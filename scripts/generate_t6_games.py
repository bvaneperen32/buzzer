#!/usr/bin/env python3
"""
Generate Dexter's two T6 sample games, images included.

  sample_games/read_the_graph_data_literacy.json  classroom: Read the Graph
  sample_games/game_night_gauntlet.json           party:     Game Night Gauntlet

Both are version 2 bundles (their images are embedded as base64). Each game covers every
question type, Sort It and Spot It included, and both grading modes.

Every image is drawn here with Pillow from fixed data and fixed random seeds, in Pillow's
built-in font. Each answer that depends on a picture (the correlations, mean vs median,
the percentage increase, the outlier's and the one-pip die's grid cells, which hand is a
straight) is computed from the data that is drawn and checked before anything is written.

Usage (repo root, venv active):
    python scripts/generate_t6_games.py               # rewrite both files
    python scripts/generate_t6_games.py --check       # exit 1 if either file is stale
    python scripts/generate_t6_games.py --images DIR  # also save the PNGs, to look at

Nothing reads the clock, the network or a system font, so every run gives the same output.
Byte-identical PNGs also need the same Pillow build (its FreeType and zlib), so run it from
the repo's venv: under another build, --check can report changed image bytes only.

Import the files through the Admin UI (Import JSON) or POST /api/host/games/import.
"""

from __future__ import annotations

import argparse
import base64
import functools
import io
import json
import math
import random
import re
import statistics
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSROOM_FILE = REPO_ROOT / "sample_games" / "read_the_graph_data_literacy.json"
PARTY_FILE = REPO_ROOT / "sample_games" / "game_night_gauntlet.json"

# T8 limits (docs/plans/t8-image-support.md §2.4) that a bundle must stay within.
MIB = 1024 * 1024
FULL_EDGE = 1600
FULL_MAX_BYTES = 4 * MIB
IMAGE_COUNT_LIMIT = 50
LIBRARY_BUDGET_BYTES = 24 * MIB
IMPORT_LIMIT_BYTES = 40 * MIB
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

CLASSIC_TYPES = {"multiple_choice", "true_false", "fill_in_the_blank", "multi_select"}
NEW_TYPES = {"sort_into_boxes", "find_in_image"}

SS = 2  # drawn at twice the size, then downsampled: ImageDraw doesn't anti-alias shapes

INK = (33, 37, 41)
MUTED = (100, 108, 117)
GRIDLINE = (225, 229, 234)
WHITE = (255, 255, 255)
BLUE = (37, 99, 235)
ORANGE = (234, 88, 12)
FELT = (28, 112, 70)
CARD_RED = (200, 30, 45)
CARD_BLACK = (25, 25, 30)


class Failed(Exception):
    """A generated game breaks one of its own checks; nothing is written."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Failed(message)


# ── Drawing ─────────────────────────────────────────────────────────────────


@functools.lru_cache
def font(px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=px)


class Canvas:
    """An image drawn in final-image pixels on a canvas SS times larger."""

    def __init__(self, width: int, height: int, background, mode: str = "RGB"):
        self.width, self.height = width, height
        self.image = Image.new(mode, (width * SS, height * SS), background)
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def _scaled(*values: float) -> list[int]:
        return [round(v * SS) for v in values]

    def rect(self, box, fill, outline=None, width=0, radius=0) -> None:
        self.draw.rounded_rectangle(
            self._scaled(*box),
            radius=radius * SS,
            fill=fill,
            outline=outline,
            width=width * SS,
        )

    def circle(self, x: float, y: float, r: float, fill, outline=None, width=0) -> None:
        box = self._scaled(x - r, y - r, x + r, y + r)
        self.draw.ellipse(box, fill=fill, outline=outline, width=width * SS)

    def line(self, points, fill, width: float) -> None:
        flat = self._scaled(*(c for point in points for c in point))
        self.draw.line(flat, fill=fill, width=round(width * SS))

    def polygon(self, points, fill) -> None:
        self.draw.polygon(
            self._scaled(*(c for point in points for c in point)), fill=fill
        )

    def text(
        self, x: float, y: float, s: str, px: int, fill, anchor: str = "mm"
    ) -> None:
        self.draw.text(
            (x * SS, y * SS), s, font=font(px * SS), fill=fill, anchor=anchor
        )

    def paste_centered(self, tile: Image.Image, x: float, y: float) -> None:
        """Paste an RGBA tile that is already at canvas scale, centred on (x, y)."""
        corner = (round(x * SS - tile.width / 2), round(y * SS - tile.height / 2))
        self.image.paste(tile, corner, tile)

    def png(self) -> bytes:
        final = self.image.resize((self.width, self.height), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        final.save(out, "PNG")
        return out.getvalue()


def cell_bounds(col: int, row: int, cols: int, rows: int, width: int, height: int):
    """A Spot It cell's box in image pixels: the grid divides the whole image evenly."""
    return (
        col * width / cols,
        row * height / rows,
        (col + 1) * width / cols,
        (row + 1) * height / rows,
    )


# ── Bundle pieces ───────────────────────────────────────────────────────────


def image_entry(key: str, filename: str, description: str, png: bytes) -> dict:
    return {
        "key": key,
        "filename": filename,
        "description": description,
        "data": base64.b64encode(png).decode("ascii"),
    }


def question(qtype, grading, prompt, config, answer_data, seconds, points) -> dict:
    return {
        "type": qtype,
        "grading_type": grading,
        "prompt": prompt,
        "config": config,
        "answer_data": answer_data,
        "time_limit_seconds": seconds,
        "points_value": points,
    }


def choice_points(count: int, correct: int, points: float) -> list[float]:
    return [points if i == correct else 0 for i in range(count)]


def bundle(game: dict, images: list[dict], questions: list[dict]) -> dict:
    return {
        "format": "buzzer/game",
        "version": 2,
        "game": game,
        "images": images,
        "questions": questions,
    }


# ── Classroom: Read the Graph ───────────────────────────────────────────────

BAR_A, BAR_B, BAR_AXIS_START = 52, 56, 50


def truncated_bar_chart() -> bytes:
    c = Canvas(1600, 900, WHITE)
    left, right, top, bottom = 220, 1500, 170, 740
    low, high = BAR_AXIS_START, 57
    c.text(120, 75, "Units sold: Product A vs. Product B", 50, INK, "lm")
    c.text(120, 125, "Example data", 34, MUTED, "lm")

    def y_px(value: float) -> float:
        return bottom - (value - low) / (high - low) * (bottom - top)

    for tick in range(low, high + 1):
        c.line([(left, y_px(tick)), (right, y_px(tick))], GRIDLINE, 2)
        c.text(left - 24, y_px(tick), str(tick), 36, MUTED, "rm")
    for label, value, x in (("Product A", BAR_A, 640), ("Product B", BAR_B, 1080)):
        c.rect((x - 140, y_px(value), x + 140, bottom), BLUE)
        c.text(x, y_px(value) - 36, str(value), 52, INK)
        c.text(x, bottom + 50, label, 44, INK)
    c.line([(left, top - 10), (left, bottom), (right, bottom)], INK, 4)
    return c.png()


HELP_DESK_SEED = 6012
HELP_DESK_VISITORS = 200
BIN_MINUTES, MAX_MINUTES = 5, 60


def help_desk_waits() -> list[float]:
    rng = random.Random(HELP_DESK_SEED)
    waits: list[float] = []
    while len(waits) < HELP_DESK_VISITORS:
        wait = round(rng.lognormvariate(2.3, 0.65), 1)
        if wait < MAX_MINUTES:
            waits.append(wait)
    return waits


def skewed_histogram(waits: list[float]) -> bytes:
    bins = [0] * (MAX_MINUTES // BIN_MINUTES)
    for wait in waits:
        bins[int(wait // BIN_MINUTES)] += 1
    c = Canvas(1600, 900, WHITE)
    left, right, top, bottom = 200, 1500, 170, 740
    y_max = math.ceil(max(bins) / 10) * 10
    c.text(120, 75, "Minutes waited at a campus help desk", 50, INK, "lm")
    c.text(1500, 75, f"Example data, {len(waits)} visitors", 34, MUTED, "rm")

    def y_px(count: float) -> float:
        return bottom - count / y_max * (bottom - top)

    for tick in range(0, y_max + 1, 10):
        c.line([(left, y_px(tick)), (right, y_px(tick))], GRIDLINE, 2)
        c.text(left - 24, y_px(tick), str(tick), 34, MUTED, "rm")
    bar_width = (right - left) / len(bins)
    for i, count in enumerate(bins):
        x0 = left + i * bar_width
        c.rect((x0 + 3, y_px(count), x0 + bar_width - 3, bottom), BLUE)
    for i in range(len(bins) + 1):
        c.text(left + i * bar_width, bottom + 32, str(i * BIN_MINUTES), 34, MUTED)
    c.text((left + right) / 2, bottom + 100, "Minutes waited", 40, INK)
    c.text(left - 24, top - 40, "Visitors", 34, INK, "lm")
    c.line([(left, top - 10), (left, bottom), (right, bottom)], INK, 4)
    return c.png()


# (option description, seed, slope, noise): the order is the answer options' order.
CORRELATION_PLOTS = (
    ("rising from lower left to upper right in a tight band", 11, 1.0, 0.1),
    ("spread out with no visible trend", 23, 0.0, 0.3),
    ("falling from upper left to lower right in a tight band", 37, -1.0, 0.1),
    ("drifting loosely downward from left to right, widely scattered", 41, -1.0, 0.57),
)
CORRELATION_POINTS = 30


def correlation_data(seed: int, slope: float, noise: float):
    rng = random.Random(seed)
    n = CORRELATION_POINTS
    xs = [(i + rng.random()) / n for i in range(n)]
    ys = [slope * x + rng.gauss(0, noise) for x in xs]
    return xs, ys


def scatter_option(xs: list[float], ys: list[float]) -> bytes:
    """Square, with big dots and no labels: phones show option images about 160 px wide."""
    c = Canvas(800, 800, WHITE)
    left, right, top, bottom = 80, 760, 40, 720
    pad = 40
    x_lo, x_hi, y_lo, y_hi = min(xs), max(xs), min(ys), max(ys)
    for x, y in zip(xs, ys):
        px = left + pad + (x - x_lo) / (x_hi - x_lo) * (right - left - 2 * pad)
        py = bottom - pad - (y - y_lo) / (y_hi - y_lo) * (bottom - top - 2 * pad)
        c.circle(px, py, 20, BLUE, outline=WHITE, width=3)
    c.line([(left, top), (left, bottom), (right, bottom)], INK, 8)
    return c.png()


OUTLIER = {"cols": 6, "rows": 4, "cell": (4, 2)}
STUDY_SEED = 2718
STUDY_STUDENTS = 36
STUDY_PLOT = {"left": 170, "right": 1540, "top": 170, "bottom": 770}
HOURS_MAX, SCORE_LO, SCORE_HI = 10, 40, 100


def study_scatter():
    """Exam score against hours studied, plus one outlier drawn at the centre of its cell.

    Returns the PNG and the trend and outlier points, for the checks.
    """
    width, height = 1600, 900
    p = STUDY_PLOT
    rng = random.Random(STUDY_SEED)
    trend = []
    for i in range(STUDY_STUDENTS):
        hours = 0.5 + 9 * (i + rng.random()) / STUDY_STUDENTS  # evenly spread, jittered
        trend.append((hours, 52 + 4.4 * hours + rng.gauss(0, 2.2)))

    def to_px(hours: float, score: float) -> tuple[float, float]:
        x = p["left"] + hours / HOURS_MAX * (p["right"] - p["left"])
        y = p["bottom"] - (score - SCORE_LO) / (SCORE_HI - SCORE_LO) * (
            p["bottom"] - p["top"]
        )
        return x, y

    col, row = OUTLIER["cell"]
    x0, y0, x1, y1 = cell_bounds(
        col, row, OUTLIER["cols"], OUTLIER["rows"], width, height
    )
    ox, oy = (x0 + x1) / 2, (y0 + y1) / 2
    outlier = (
        (ox - p["left"]) / (p["right"] - p["left"]) * HOURS_MAX,
        SCORE_LO
        + (p["bottom"] - oy) / (p["bottom"] - p["top"]) * (SCORE_HI - SCORE_LO),
    )

    c = Canvas(width, height, WHITE)
    c.text(120, 60, "Hours studied vs. exam score", 50, INK, "lm")
    c.text(1540, 60, "Example data", 34, MUTED, "rm")
    for score in range(SCORE_LO, SCORE_HI + 1, 10):
        _, y = to_px(0, score)
        c.line([(p["left"], y), (p["right"], y)], GRIDLINE, 2)
        c.text(p["left"] - 20, y, str(score), 34, MUTED, "rm")
    for hours in range(0, HOURS_MAX + 1, 2):
        x, _ = to_px(hours, SCORE_LO)
        c.text(x, p["bottom"] + 32, str(hours), 34, MUTED)
    c.text((p["left"] + p["right"]) / 2, p["bottom"] + 90, "Hours studied", 40, INK)
    c.text(p["left"] - 20, p["top"] - 50, "Exam score", 34, INK, "lm")
    c.line(
        [
            (p["left"], p["top"] - 10),
            (p["left"], p["bottom"]),
            (p["right"], p["bottom"]),
        ],
        INK,
        4,
    )
    for hours, score in [*trend, outlier]:
        c.circle(*to_px(hours, score), 13, BLUE, outline=WHITE, width=2)
    pixels = [to_px(h, s) for h, s in trend]
    return c.png(), trend, outlier, (ox, oy), pixels


def classroom_game() -> dict:
    images: list[dict] = []
    questions: list[dict] = []

    # Q2 — computed, so the key can't drift from the prompt.
    data = [4, 9, 2, 7, 15]
    median = statistics.median(data)
    require(median == 7, "the median question's data no longer has median 7")

    # Q5 — the strongest negative correlation, among four plots.
    correlations = []
    option_keys = []
    for i, (description, seed, slope, noise) in enumerate(CORRELATION_PLOTS):
        xs, ys = correlation_data(seed, slope, noise)
        correlations.append(statistics.correlation(xs, ys))
        key = f"scatter-{'abcd'[i]}"
        option_keys.append(key)
        images.append(
            image_entry(
                key,
                f"scatter_{'abcd'[i]}.png",
                f"Scatter plot of {CORRELATION_POINTS} points {description}",
                scatter_option(xs, ys),
            )
        )
    strong_pos, none, strong_neg, weak_neg = correlations
    require(
        strong_pos >= 0.9, f"plot A should be strongly positive (r={strong_pos:.3f})"
    )
    require(abs(none) <= 0.15, f"plot B should show no correlation (r={none:.3f})")
    require(
        strong_neg <= -0.9, f"plot C should be strongly negative (r={strong_neg:.3f})"
    )
    require(
        -0.6 <= weak_neg <= -0.3, f"plot D should be weakly negative (r={weak_neg:.3f})"
    )
    correct_plot = correlations.index(min(correlations))
    require(
        correct_plot == 2 and sum(r < -0.6 for r in correlations) == 1,
        "plot C must be the only strongly negative plot",
    )

    # Q6 — right skew, with the mean above the median in the data actually drawn.
    waits = help_desk_waits()
    mean_wait, median_wait = statistics.fmean(waits), statistics.median(waits)
    require(
        mean_wait - median_wait >= 1.5,
        f"the help-desk data should have mean well above median "
        f"({mean_wait:.2f} vs {median_wait:.2f})",
    )
    images.append(
        image_entry(
            "help-desk-histogram",
            "help_desk_waits_histogram.png",
            f"Histogram of minutes waited at a campus help desk by {len(waits)} "
            "visitors, in 5-minute bins from 0 to 60. Most bars are on the left, with a "
            "long tail to the right.",
            skewed_histogram(waits),
        )
    )

    # Q8 — the bars exaggerate a small difference.
    visual_ratio = (BAR_B - BAR_AXIS_START) / (BAR_A - BAR_AXIS_START)
    increase = (BAR_B - BAR_A) / BAR_A * 100
    require(visual_ratio == 3, "bar B should look three times as tall as bar A")
    require(
        f"{increase:.1f}" == "7.7", f"A to B should be a 7.7% increase ({increase})"
    )
    increase_options = [
        "About 4%",
        f"About {increase:.1f}%",
        "About 200%",
        "About 300%",
    ]
    images.append(
        image_entry(
            "truncated-bars",
            "truncated_axis_bar_chart.png",
            f"Bar chart of units sold. Product A is {BAR_A} and Product B is {BAR_B}; the "
            f"vertical axis starts at {BAR_AXIS_START} and runs to 57.",
            truncated_bar_chart(),
        )
    )

    # Q9 — the outlier's cell, from where it was drawn.
    png, trend, outlier, outlier_px, trend_px = study_scatter()
    cols, rows, (col, row) = OUTLIER["cols"], OUTLIER["rows"], OUTLIER["cell"]
    x0, y0, x1, y1 = cell_bounds(col, row, cols, rows, 1600, 900)
    ox, oy = outlier_px
    require(
        min(ox - x0, x1 - ox, oy - y0, y1 - oy) >= 40,
        "the outlier must sit well inside its cell",
    )
    require(
        not any(x0 <= x <= x1 and y0 <= y <= y1 for x, y in trend_px),
        "no other student may share the outlier's cell",
    )
    slope, intercept = statistics.linear_regression(*zip(*trend))
    residuals = [abs(s - (intercept + slope * h)) for h, s in trend]
    outlier_residual = abs(outlier[1] - (intercept + slope * outlier[0]))
    require(
        outlier_residual >= 3 * max(residuals),
        f"the outlier should be far off the trend ({outlier_residual:.1f} vs "
        f"{max(residuals):.1f})",
    )
    images.append(
        image_entry(
            "study-scatter",
            "hours_vs_score_scatter.png",
            f"Scatter plot of exam score against hours studied for {len(trend) + 1} "
            "students; scores generally rise with hours studied.",
            png,
        )
    )

    sort_cards = [
        ("Eye color", 0),
        ("Height in cm", 1),
        ("ZIP code", 0),
        ("Number of siblings", 1),
        ("Favorite music genre", 0),
        ("Commute time in minutes", 1),
        ("Jersey number", 0),
        ("Daily high temperature", 1),
    ]
    questions += [
        question(
            "multiple_choice",
            "COMPLETENESS",
            "Warm-up: where do you run into charts and graphs most often?",
            {
                "options": [
                    "News and social media",
                    "Classes and textbooks",
                    "Work or internships",
                    "Sports and game stats",
                ]
            },
            {},
            20,
            1.0,
        ),
        question(
            "fill_in_the_blank",
            "ACCURACY",
            f"What is the median of this data set? {', '.join(map(str, data))}",
            {},
            {
                "acceptedAnswers": ["7", "seven"],
                "answerPoints": [1.0, 1.0],
                "editDistance": 0,
            },
            30,
            1.0,
        ),
        question(
            "multi_select",
            "ACCURACY",
            "Which of these are measures of spread (variability)? Select all that apply.",
            {
                "options": [
                    "Range",
                    "Mean",
                    "Standard deviation",
                    "Median",
                    "Interquartile range (IQR)",
                    "Mode",
                ]
            },
            {"answer_points": [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]},
            30,
            3.0,
        ),
        question(
            "sort_into_boxes",
            "ACCURACY",
            "Sort each variable: is it categorical or quantitative?",
            {
                "boxes": ["Categorical", "Quantitative"],
                "cards": [c for c, _ in sort_cards],
            },
            {"correct_boxes": [box for _, box in sort_cards]},
            60,
            2.0,
        ),
        question(
            "multiple_choice",
            "ACCURACY",
            "Which scatter plot shows the strongest negative correlation?",
            {"options": ["", "", "", ""], "option_images": option_keys},
            {"answer_points": choice_points(4, correct_plot, 1.0)},
            30,
            1.0,
        ),
        question(
            "true_false",
            "ACCURACY",
            "True or false: for the data in this right-skewed histogram, the mean wait "
            "time is greater than the median wait time.",
            {"prompt_image_id": "help-desk-histogram"},
            {"answer_points": {"true": 1.0, "false": 0.0}},
            20,
            1.0,
        ),
        question(
            "true_false",
            "ACCURACY",
            "True or false: a correlation of r = −0.8 shows a weaker linear relationship "
            "than r = 0.5.",
            {},
            {"answer_points": {"true": 0.0, "false": 1.0}},
            20,
            1.0,
        ),
        question(
            "multiple_choice",
            "ACCURACY",
            f"This chart's vertical axis starts at {BAR_AXIS_START}, so bar B looks three "
            f"times as tall as bar A. What is the actual percentage increase from A "
            f"({BAR_A}) to B ({BAR_B})?",
            {"prompt_image_id": "truncated-bars", "options": increase_options},
            {"answer_points": choice_points(4, 1, 1.0)},
            45,
            1.0,
        ),
        question(
            "find_in_image",
            "ACCURACY",
            "Find the outlier: pick the cell with the one student whose score breaks the "
            "trend.",
            {"image_id": "study-scatter", "grid": {"cols": cols, "rows": rows}},
            {"accepted_cells": [{"col": col, "row": row}], "confirmed": True},
            30,
            1.0,
        ),
        question(
            "fill_in_the_blank",
            "ACCURACY",
            "The value that appears most often in a data set is called the ___.",
            {},
            {
                "acceptedAnswers": ["mode", "the mode"],
                "answerPoints": [1.0, 1.0],
                "editDistance": 1,
            },
            20,
            1.0,
        ),
        question(
            "multi_select",
            "COMPLETENESS",
            "Exit ticket: which topics should we review next class? Select all that apply.",
            {
                "options": [
                    "Mean, median and mode",
                    "Measures of spread",
                    "Correlation",
                    "Misleading charts",
                    "Outliers",
                ]
            },
            {},
            25,
            1.0,
        ),
    ]
    game = {
        "title": "Read the Graph: Data Literacy Essentials",
        "description": (
            "A data-literacy check for intro statistics or any class that reads charts: "
            "center and spread, variable types, correlation, skew, outliers and "
            "misleading axes. Every chart uses made-up example data."
        ),
        "max_players": 150,
    }
    return bundle(game, images, questions)


# ── Party: Game Night Gauntlet ──────────────────────────────────────────────

# Standard English-language Scrabble letter values, for the four tiles shown.
TILE_POINTS = {"Q": 10, "U": 1, "I": 1, "Z": 10}


def letter_tiles(word: str) -> bytes:
    c = Canvas(1600, 600, (247, 241, 227))
    size, gap = 300, 50
    start = (1600 - len(word) * size - (len(word) - 1) * gap) / 2
    c.rect((start - 50, 400, 1600 - start + 50, 470), (139, 94, 60), radius=16)
    for i, letter in enumerate(word):
        x = start + i * (size + gap)
        c.rect((x, 110, x + size, 110 + size), (240, 214, 160), (196, 160, 104), 5, 28)
        # Up and left of centre, so Q's tail stays clear of the value in the corner.
        c.text(x + size / 2 - 24, 110 + size / 2 - 20, letter, 180, INK)
        c.text(x + size - 22, 110 + size - 22, str(TILE_POINTS[letter]), 58, INK, "rs")
    return c.png()


RANK_ORDER = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
SUIT_NAMES = {"C": "clubs", "D": "diamonds", "H": "hearts", "S": "spades"}
RANK_NAMES = {"J": "jack", "Q": "queen", "K": "king", "A": "ace"}

# Shown left to right, top row then bottom row; the order is the answer options' order.
POKER_HANDS = (
    ("KC", "2S", "QH", "3H", "AD"),  # wraps around from K to 3: not a straight
    ("9C", "4S", "KS", "4D", "9H"),  # two pair
    ("8S", "5C", "9C", "6D", "7H"),  # straight, 5 to 9
    ("QD", "10H", "2H", "KC", "JS"),  # high card
)


def is_straight(hand) -> bool:
    """Standard rules: five consecutive ranks, ace high or low, never wrapping K-A-2."""
    values = sorted(RANK_ORDER.index(card[:-1]) for card in hand)
    if len(set(values)) != 5:
        return False
    return values[-1] - values[0] == 4 or values == [0, 1, 2, 3, 12]


def is_flush(hand) -> bool:
    return len({card[-1] for card in hand}) == 1


def card_name(card: str) -> str:
    rank = RANK_NAMES.get(card[:-1], card[:-1])
    return f"{rank} of {SUIT_NAMES[card[-1]]}"


def suit_shape(c: Canvas, suit: str, cx: float, cy: float, s: float, color) -> None:
    if suit == "D":
        c.polygon(
            [
                (cx, cy - s / 2),
                (cx + s * 0.38, cy),
                (cx, cy + s / 2),
                (cx - s * 0.38, cy),
            ],
            color,
        )
    elif suit == "H":
        r = s * 0.26
        c.circle(cx - s * 0.23, cy - s * 0.17, r, color)
        c.circle(cx + s * 0.23, cy - s * 0.17, r, color)
        c.polygon(
            [
                (cx - s * 0.48, cy - s * 0.08),
                (cx + s * 0.48, cy - s * 0.08),
                (cx, cy + s / 2),
            ],
            color,
        )
    elif suit == "S":
        r = s * 0.25
        c.circle(cx - s * 0.22, cy + s * 0.1, r, color)
        c.circle(cx + s * 0.22, cy + s * 0.1, r, color)
        c.polygon(
            [
                (cx - s * 0.46, cy + s * 0.04),
                (cx + s * 0.46, cy + s * 0.04),
                (cx, cy - s / 2),
            ],
            color,
        )
        c.polygon(
            [
                (cx, cy + s * 0.12),
                (cx - s * 0.17, cy + s / 2),
                (cx + s * 0.17, cy + s / 2),
            ],
            color,
        )
    else:  # clubs
        r = s * 0.21
        c.circle(cx, cy - s * 0.25, r, color)
        c.circle(cx - s * 0.24, cy + s * 0.05, r, color)
        c.circle(cx + s * 0.24, cy + s * 0.05, r, color)
        c.circle(cx, cy - s * 0.02, r * 0.7, color)
        c.polygon(
            [
                (cx, cy + s * 0.02),
                (cx - s * 0.16, cy + s / 2),
                (cx + s * 0.16, cy + s / 2),
            ],
            color,
        )


def poker_hand(hand) -> bytes:
    """Square, three cards over two: phones show option images about 160 px wide."""
    c = Canvas(800, 800, FELT)
    card_w, card_h, gap = 224, 320, 26
    rows = (hand[:3], hand[3:])
    for r, cards in enumerate(rows):
        top = 60 + r * (card_h + 40)
        start = (800 - len(cards) * card_w - (len(cards) - 1) * gap) / 2
        for i, card in enumerate(cards):
            x = start + i * (card_w + gap)
            color = CARD_RED if card[-1] in "DH" else CARD_BLACK
            c.rect((x, top, x + card_w, top + card_h), WHITE, (180, 180, 180), 3, 22)
            c.text(x + card_w / 2, top + 92, card[:-1], 120, color)
            suit_shape(c, card[-1], x + card_w / 2, top + 222, 120, color)
    return c.png()


DICE = {"cols": 8, "rows": 5, "cell": (6, 3), "offset": (-18, 12), "angle": 17}
DICE_SEED = 31415
DICE_COUNT = 60
DIE_SIZE = 74
DIE_COLORS = (
    ((250, 250, 245), INK),
    ((210, 40, 50), WHITE),
    ((35, 85, 200), WHITE),
    ((245, 200, 40), INK),
    ((30, 30, 35), WHITE),
    ((120, 60, 170), WHITE),
)
# Pip positions on a 3×3 layout of the face, for each value.
PIPS = {
    1: [(1, 1)],
    2: [(0, 0), (2, 2)],
    3: [(0, 0), (1, 1), (2, 2)],
    4: [(0, 0), (2, 0), (0, 2), (2, 2)],
    5: [(0, 0), (2, 0), (1, 1), (0, 2), (2, 2)],
    6: [(0, 0), (0, 1), (0, 2), (2, 0), (2, 1), (2, 2)],
}


def die_tile(value: int, face, pip, angle: float) -> Image.Image:
    """One die as an RGBA tile at canvas scale, rotated."""
    tile = Canvas(DIE_SIZE, DIE_SIZE, (0, 0, 0, 0), mode="RGBA")
    tile.rect((2, 2, DIE_SIZE - 2, DIE_SIZE - 2), face, (0, 0, 0), 2, 13)
    step = (DIE_SIZE - 8) / 4
    for gx, gy in PIPS[value]:
        tile.circle(4 + step * (gx + 1), 4 + step * (gy + 1), 7, pip)
    return tile.image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)


def half_extent(angle: float) -> float:
    """Half the width (and height) of a rotated die's bounding box."""
    rad = math.radians(angle)
    return DIE_SIZE / 2 * (abs(math.cos(rad)) + abs(math.sin(rad)))


def dice_crowd():
    """A table of dice where exactly one shows a single pip, centred in its cell.

    Returns the PNG, the dice as (x, y, value, angle), and the target's bounding box.
    """
    width, height = 1600, 900
    col, row = DICE["cell"]
    x0, y0, x1, y1 = cell_bounds(col, row, DICE["cols"], DICE["rows"], width, height)
    tx, ty = (x0 + x1) / 2 + DICE["offset"][0], (y0 + y1) / 2 + DICE["offset"][1]
    dice = [(tx, ty, 1, DICE["angle"], DIE_COLORS[5])]  # purple: a common colour
    rng = random.Random(DICE_SEED)
    min_gap = DIE_SIZE * math.sqrt(2) + 4  # rotated dice never touch
    attempts = 0
    while len(dice) < DICE_COUNT and attempts < 20000:
        attempts += 1
        x, y = rng.uniform(60, width - 60), rng.uniform(60, height - 60)
        angle = rng.uniform(-40, 40)
        value = rng.randint(2, 6)
        colors = rng.choice(DIE_COLORS)
        if all(math.dist((x, y), (dx, dy)) >= min_gap for dx, dy, *_ in dice):
            dice.append((x, y, value, angle, colors))
    c = Canvas(width, height, FELT)
    for x, y, value, angle, (face, pip) in dice:
        c.paste_centered(die_tile(value, face, pip, angle), x, y)
    e = half_extent(DICE["angle"])
    target_box = (tx - e, ty - e, tx + e, ty + e)
    return c.png(), [(x, y, v, a) for x, y, v, a, _ in dice], target_box


def party_game() -> dict:
    images: list[dict] = []

    word = "QUIZ"
    score = sum(TILE_POINTS[letter] for letter in word)
    require(score == 22, f"QUIZ should score 22 with these tiles ({score})")
    images.append(
        image_entry(
            "quiz-tiles",
            "quiz_letter_tiles.png",
            "Four letter tiles on a rack spelling QUIZ. Each tile shows its point value: "
            + ", ".join(f"{letter} {TILE_POINTS[letter]}" for letter in word)
            + ".",
            letter_tiles(word),
        )
    )

    straights = [is_straight(hand) and not is_flush(hand) for hand in POKER_HANDS]
    require(straights == [False, False, True, False], "only hand C may be a straight")
    require(
        all(len(set(hand)) == 5 for hand in POKER_HANDS),
        "each hand needs 5 distinct cards",
    )
    hand_keys = []
    for i, hand in enumerate(POKER_HANDS):
        key = f"poker-hand-{'abcd'[i]}"
        hand_keys.append(key)
        images.append(
            image_entry(
                key,
                f"poker_hand_{'abcd'[i]}.png",
                "Five playing cards: "
                + ", ".join(card_name(card) for card in hand)
                + ".",
                poker_hand(hand),
            )
        )

    png, dice, target_box = dice_crowd()
    col, row = DICE["cell"]
    cols, rows = DICE["cols"], DICE["rows"]
    x0, y0, x1, y1 = cell_bounds(col, row, cols, rows, 1600, 900)
    bx0, by0, bx1, by1 = target_box
    require(
        min(bx0 - x0, x1 - bx1, by0 - y0, y1 - by1) >= 20,
        "the one-pip die must sit inside its cell, clear of the grid lines",
    )
    require(
        sum(value == 1 for _, _, value, _ in dice) == 1, "exactly one die may show 1"
    )
    require(len(dice) >= 55, f"the table should be crowded ({len(dice)} dice)")
    images.append(
        image_entry(
            "dice-crowd",
            "dice_on_the_table.png",
            f"About {round(len(dice), -1)} colorful dice scattered across a green table, "
            "showing assorted faces.",
            png,
        )
    )

    sort_cards = [
        ("Chess", 0),
        ("Go Fish", 1),
        ("Yahtzee", 2),
        ("Checkers", 0),
        ("Crazy Eights", 1),
        ("Farkle", 2),
        ("Mancala", 0),
        ("Old Maid", 1),
        ("Bunco", 2),
    ]
    dice_games = [
        ("Backgammon", True),
        ("Chess", False),
        ("Monopoly", True),
        ("Scrabble", False),
        ("Risk", True),
        ("Uno", False),
        ("Catan", True),
    ]
    questions = [
        question(
            "true_false",
            "COMPLETENESS",
            "Confession time: I have quit a game of Monopoly before it ended.",
            {},
            {},
            15,
            1000.0,
        ),
        question(
            "multiple_choice",
            "ACCURACY",
            "On a standard six-sided die, the numbers on opposite faces always add up to…",
            {"options": ["6", "7", "8", "12"]},
            {"answer_points": choice_points(4, 1, 1000.0)},
            20,
            1000.0,
        ),
        question(
            "fill_in_the_blank",
            "ACCURACY",
            "Using the tile values shown (standard English-language Scrabble), what is "
            "the base score for QUIZ, with no bonus squares?",
            {"prompt_image_id": "quiz-tiles"},
            {
                "acceptedAnswers": ["22", "22 points", "twenty-two", "twenty two"],
                "answerPoints": [1000.0, 1000.0, 1000.0, 1000.0],
                "editDistance": 0,
            },
            30,
            1000.0,
        ),
        question(
            "multiple_choice",
            "ACCURACY",
            "Under standard poker rules (no wild cards), which of these hands is a "
            "straight?",
            {"options": ["", "", "", ""], "option_images": hand_keys},
            {"answer_points": choice_points(4, straights.index(True), 1000.0)},
            30,
            1000.0,
        ),
        question(
            "multi_select",
            "ACCURACY",
            "Which of these games use dice in their standard rules? Select all that "
            "apply.",
            {"options": [name for name, _ in dice_games]},
            {"answer_points": [250.0 if uses else -250.0 for _, uses in dice_games]},
            30,
            1000.0,
        ),
        question(
            "sort_into_boxes",
            "ACCURACY",
            "Sort each game by what you mainly play it with.",
            {
                "boxes": ["Board game", "Card game", "Dice game"],
                "cards": [card for card, _ in sort_cards],
            },
            {"correct_boxes": [box for _, box in sort_cards]},
            45,
            1000.0,
        ),
        question(
            "find_in_image",
            "ACCURACY",
            "Find the only die showing a single pip (a one)!",
            {"image_id": "dice-crowd", "grid": {"cols": cols, "rows": rows}},
            {"accepted_cells": [{"col": col, "row": row}], "confirmed": True},
            40,
            1000.0,
        ),
        question(
            "true_false",
            "ACCURACY",
            "True or false: not counting castling, the knight is the only chess piece "
            "that can jump over other pieces.",
            {},
            {"answer_points": {"true": 1000.0, "false": 0.0}},
            20,
            1000.0,
        ),
        question(
            "fill_in_the_blank",
            "ACCURACY",
            "In chess, the special move in which the king and a rook move on the same "
            "turn is called ___.",
            {},
            {
                "acceptedAnswers": ["castling", "castle"],
                "answerPoints": [1000.0, 1000.0],
                "editDistance": 1,
            },
            25,
            1000.0,
        ),
        question(
            "multiple_choice",
            "COMPLETENESS",
            "What's the ultimate game-night snack?",
            {"options": ["Pizza", "Chips and dip", "Popcorn", "Candy"]},
            {},
            15,
            1000.0,
        ),
        question(
            "sort_into_boxes",
            "COMPLETENESS",
            "Love it or leave it? Sort these game-night classics.",
            {
                "boxes": ["Love it", "Leave it"],
                "cards": [
                    "Monopoly",
                    "Uno",
                    "Charades",
                    "Twister",
                    "Pictionary",
                    "Poker",
                ],
            },
            {},
            40,
            1000.0,
        ),
    ]
    game = {
        "title": "Game Night Gauntlet",
        "description": (
            "Dice, cards, tiles and hot takes: a game-night trivia mix for any crowd. "
            "No experts required."
        ),
        "max_players": 150,
    }
    return bundle(game, images, questions)


# ── Checks across both games ────────────────────────────────────────────────


def references(q: dict) -> list[str]:
    config = q["config"]
    refs = [config.get("prompt_image_id"), config.get("image_id")]
    refs += config.get("option_images") or []
    return [ref for ref in refs if ref is not None]


def check_bundle(name: str, b: dict) -> None:
    images = b["images"]
    keys = [image["key"] for image in images]
    require(len(keys) == len(set(keys)), f"{name}: image keys must be unique")
    require(all(KEY_PATTERN.match(key) for key in keys), f"{name}: bad image key")
    require(len(images) <= IMAGE_COUNT_LIMIT, f"{name}: too many images")
    library_bytes = 0
    for image in images:
        require(
            set(image) == {"key", "filename", "description", "data"},
            f"{name}: {image['key']} must have exactly the four bundle fields",
        )
        require(
            1 <= len(image["description"].strip()) <= 300,
            f"{name}: {image['key']} needs a 1–300 character description",
        )
        data = base64.b64decode(image["data"], validate=True)
        with Image.open(io.BytesIO(data)) as img:
            require(img.format == "PNG", f"{name}: {image['key']} must be a PNG")
            require(max(img.size) <= FULL_EDGE, f"{name}: {image['key']} is too large")
        require(len(data) <= FULL_MAX_BYTES, f"{name}: {image['key']} is over 4 MiB")
        library_bytes += len(data)
    require(library_bytes <= LIBRARY_BUDGET_BYTES, f"{name}: over the library budget")
    used = [ref for q in b["questions"] for ref in references(q)]
    require(set(used) <= set(keys), f"{name}: a question refers to an unknown key")
    require(set(keys) <= set(used), f"{name}: an image is never used")
    require(
        len(serialize(b).encode("utf-8")) <= IMPORT_LIMIT_BYTES,
        f"{name}: over the 40 MiB import limit",
    )

    types = {q["type"] for q in b["questions"]}
    modes = {q["grading_type"] for q in b["questions"]}
    require(CLASSIC_TYPES <= types, f"{name}: missing {CLASSIC_TYPES - types}")
    require(modes == {"ACCURACY", "COMPLETENESS"}, f"{name}: needs both grading modes")


def check_games(bundles: dict[str, dict]) -> None:
    for name, b in bundles.items():
        check_bundle(name, b)
    require(
        any(NEW_TYPES <= {q["type"] for q in b["questions"]} for b in bundles.values()),
        "at least one game needs both Sort It and Spot It",
    )


def serialize(b: dict) -> str:
    """The exporter's formatting: UTF-8, two-space indent, plus a final newline."""
    return json.dumps(b, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if either file would change"
    )
    parser.add_argument(
        "--images",
        type=Path,
        metavar="DIR",
        help="also save every image as a PNG in DIR",
    )
    args = parser.parse_args(argv)

    try:
        bundles = {
            CLASSROOM_FILE.name: classroom_game(),
            PARTY_FILE.name: party_game(),
        }
        check_games(bundles)
    except Failed as exc:
        print(f"generate_t6_games: {exc}", file=sys.stderr)
        return 1

    stale = []
    for path in (CLASSROOM_FILE, PARTY_FILE):
        text = serialize(bundles[path.name])
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if args.check:
            if current != text:
                stale.append(path)
        elif current != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO_ROOT)}")
        else:
            print(f"unchanged {path.relative_to(REPO_ROOT)}")
        if args.images:
            args.images.mkdir(parents=True, exist_ok=True)
            for image in bundles[path.name]["images"]:
                target = args.images / f"{image['key']}.png"
                target.write_bytes(base64.b64decode(image["data"]))

    if stale:
        for path in stale:
            print(f"out of date: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1
    if args.check:
        print("both games are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
