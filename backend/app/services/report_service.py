"""
Standalone HTML session report generator.

Produces a self-contained HTML file with aggregate statistics only —
no player names, no individual scores, no personally identifiable information.
Answer distributions are reconstructed from session_scores.answer_data in MySQL
(Redis may have expired for completed sessions).

T8: images appear as embedded small copies (data URIs, `alt` = the description) — prompt
images on any type, and option images beside multiple-choice bars and in the multi-select
option list — but only when `include_images` says the downloader passes the game rule now.
The report is rebuilt from current content, so an image replaced after the session carries a
note saying players saw an earlier version.

T7 (docs/plans/t7-question-types.md §2.8): a Sort It per-card table and a Spot It heatmap (an
inline SVG over the small copy). Every author-controlled string T7 adds — box and card labels,
the image description — goes through `_esc`; only the prompt stays raw HTML (sanitized on
write). The report stays aggregate-only: the one T7 points change is each T7 question's
maximum-points label, formatted by `format_points`.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.course import Course
from ..models.game import Game, Question
from ..models.image import Image
from ..models.session import GameSession, SessionScore
from . import image_service, sort_it, spot_it
from .image_refs import CANVAS, OPTION, PROMPT, image_refs

WITHHELD_TEXT = "[image withheld — you no longer have access to this game]"
REPLACED_NOTE = "Replaced after this session — players saw an earlier version."
MISSING_IMAGE_TEXT = "[image missing]"
STALE_ANSWERS_NOTE = (
    "{n} answer{s} {were} given to an earlier version of this question and "
    "{are}n't shown."
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _esc(s: object) -> str:
    return html.escape(str(s) if s is not None else "")


def format_points(value: float) -> str:
    """The T7 points display rule (T7 spec §2.2): at most one decimal, rounding the value's
    shortest decimal representation half up, a trailing ".0" dropped. `repr` is what matches
    the apps' `Intl.NumberFormat` (0.15 → "0.2", 2.55 → "2.6"); `Decimal(value)` would round
    the binary value instead ("0.1", "2.5"). Never `normalize()`, which prints 1000 as "1E+3".
    """
    rounded = Decimal(repr(float(value))).quantize(Decimal("0.1"), ROUND_HALF_UP)
    text = format(rounded, "f")
    return text[:-2] if text.endswith(".0") else text


def _t7_points_label(points_value: float) -> str:
    """A T7 question's maximum-points label in the `q-timing` header, e.g. "708.3 pts"."""
    text = format_points(points_value)
    return f"{text} {'pt' if text == '1' else 'pts'}"


def _points_label(question_type: str, points_value: float) -> str:
    """The `q-timing` header's maximum points: T7 types use the T7 display rule; every other
    type keeps its `:g` label unchanged."""
    if question_type in (sort_it.TYPE, spot_it.TYPE):
        return _t7_points_label(points_value)
    return f"{points_value:g} pt{'s' if points_value != 1 else ''}"


def _stale_note(stale: int) -> str:
    if stale <= 0:
        return ""
    plural = stale != 1
    text = STALE_ANSWERS_NOTE.format(
        n=stale,
        s="s" if plural else "",
        were="were" if plural else "was",
        are="are" if plural else "is",
    )
    return f'<p class="t7-note">{_esc(text)}</p>'


def _levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            temp = dp[j]
            dp[j] = prev if a[i - 1] == b[j - 1] else 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]


def _answer_reveal(q: Question) -> dict:
    """Mirror of gateway._answer_reveal — derive correct-answer facts from answer_data."""
    if q.grading_type == "COMPLETENESS":
        return {"type": "completeness"}
    if q.type == "multiple_choice":
        pts: list[float] = q.answer_data.get("answer_points", [])
        return {
            "type": "multiple_choice",
            "correctIndices": [i for i, p in enumerate(pts) if p >= q.points_value],
        }
    if q.type == "true_false":
        pts_map: dict = q.answer_data.get("answer_points", {})
        return {
            "type": "true_false",
            "correctValue": pts_map.get("true", 0) >= q.points_value,
        }
    if q.type == "fill_in_the_blank":
        return {
            "type": "fill_in_the_blank",
            "acceptedAnswers": q.answer_data.get("acceptedAnswers", []),
            "editDistance": q.answer_data.get("editDistance", 0),
        }
    if q.type == sort_it.TYPE:
        return sort_it.reveal(q.grading_type, q.config, q.answer_data)
    if q.type == spot_it.TYPE:
        return spot_it.reveal(q.grading_type, q.config, q.answer_data)
    return {}


def _extract_answer_key(q_type: str, answer_data: dict | None) -> str | None:
    """Convert a player's answer_data blob into the distribution key used for charting."""
    if not answer_data:
        return None
    if q_type == "multiple_choice":
        idx = answer_data.get("selectedIndex")
        return str(idx) if idx is not None else None
    if q_type == "true_false":
        val = answer_data.get("selectedValue")
        return "true" if val else "false" if val is not None else None
    if q_type == "fill_in_the_blank":
        text = answer_data.get("text", "").strip()
        return text.lower() if text else None
    return None


# ---------------------------------------------------------------------------
# Histogram helpers (mirrors GameOverPage logic)
# ---------------------------------------------------------------------------

_TARGET_BUCKETS = 8


def _choose_bin_width(ceiling: int) -> int:
    N = ceiling + 1
    best_w, best_diff = 1, float("inf")
    w = 1
    while w * w <= N:
        if N % w == 0:
            for cand in (w, N // w):
                nb = N // cand
                if nb < 2:
                    continue
                d = abs(nb - _TARGET_BUCKETS)
                if d < best_diff:
                    best_diff, best_w = d, cand
        w += 1
    if best_diff > _TARGET_BUCKETS:
        best_w = max(1, -(-N // _TARGET_BUCKETS))
    return best_w


def _build_histogram(scores: list[float], max_possible: float) -> list[dict]:
    ceiling = max(int(max_possible), 1)
    width = _choose_bin_width(ceiling)
    num_buckets = -(-(ceiling + 1) // width)
    buckets = []
    for i in range(num_buckets):
        start = i * width
        end = min(start + width - 1, ceiling)
        label = str(start) if start == end else f"{start}–{end}"
        buckets.append({"label": label, "count": 0})
    for s in scores:
        idx = min(int(s) // width, num_buckets - 1)
        buckets[idx]["count"] += 1
    return buckets


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------

# Between an option's letter and its caption, as the report has always rendered it.
_LABEL_SEPARATOR = "\u2002"  # EN SPACE


class _Images:
    """The images one report embeds: the session's game only, with their small copies when
    the downloader may see them (a withheld image still counts as present)."""

    def __init__(
        self, by_id: dict[int, Image], include: bool, session_created_at: object
    ) -> None:
        self.by_id = by_id
        self.include = include
        self.session_created_at = session_created_at

    def for_question(self, q: Question) -> tuple[Image | None, dict[int, Image]]:
        """(prompt image, {option index: image}). Unresolved references are no image."""
        prompt, options = None, {}
        for ref in image_refs(q.config):
            image = self.by_id.get(ref.image_id)
            if image is None:
                continue
            if ref.role == PROMPT:
                prompt = image
            elif ref.role == OPTION:
                options[ref.option_index] = image
        return prompt, options

    def canvas(self, q: Question) -> Image | None:
        """A Spot It question's canvas image (T7); None when it isn't in the library."""
        for ref in image_refs(q.config):
            if ref.role == CANVAS:
                return self.by_id.get(ref.image_id)
        return None

    def replaced(self, image: Image) -> bool:
        # Both from the database's NOW(): a replace committed just before the room was
        # created never produces the note.
        return bool(
            image.replaced_at
            and self.session_created_at
            and image.replaced_at > self.session_created_at
        )

    def img(self, image: Image, css_class: str) -> str:
        if not self.include:
            return f'<span class="img-withheld">{_esc(WITHHELD_TEXT)}</span>'
        return (
            f'<img class="{css_class}" src="{image_service.data_uri(image, "small")}" '
            f'alt="{_esc(image.description)}">'
        )


def _caption(opt: object) -> str:
    return "" if opt is None else str(opt)


def _option_label(index: int, opt: object) -> str:
    """The letter, then the caption when it isn't empty (T8 spec §2.11)."""
    letter = image_service.option_letter(index)
    caption = _caption(opt)
    return f"{letter}{_LABEL_SEPARATOR}{_esc(caption)}" if caption.strip() else letter


def _rich_option_label(
    index: int, opt: object, image: Image | None, images: _Images
) -> str:
    letter = image_service.option_letter(index)
    caption = _caption(opt)
    parts = [f'<span class="opt-letter">{letter}</span>']
    if image is not None:
        parts.append(images.img(image, "opt-thumb"))
    if caption.strip():
        parts.append(f'<span class="opt-caption">{_esc(caption)}</span>')
    return "".join(parts)


def _replaced_notes(option_images: dict[int, Image], images: _Images) -> str:
    if not images.include:
        return ""
    return "".join(
        f'<p class="img-note">Option {image_service.option_letter(index)}: '
        f"{_esc(REPLACED_NOTE)}</p>"
        for index, image in sorted(option_images.items())
        if images.replaced(image)
    )


def _render_prompt_image(image: Image | None, images: _Images) -> str:
    if image is None:
        return ""
    if not images.include:
        return f'<p class="img-withheld q-image">{_esc(WITHHELD_TEXT)}</p>'
    note = (
        f'<figcaption class="img-note">{_esc(REPLACED_NOTE)}</figcaption>'
        if images.replaced(image)
        else ""
    )
    return f'<figure class="q-image">{images.img(image, "prompt-img")}{note}</figure>'


def _render_option_list(
    q: Question, option_images: dict[int, Image], images: _Images
) -> str:
    """multi_select: each option's label, with its thumbnail when it has an image. No counts
    and no correctness — the report has no multi-select distribution chart."""
    options = q.config.get("options", [])
    if not isinstance(options, list) or not options:
        return ""
    rows = [
        f'<li class="opt-row">{_rich_option_label(i, opt, option_images.get(i), images)}</li>'
        for i, opt in enumerate(options)
    ]
    return (
        '<ul class="opt-list">'
        + "".join(rows)
        + "</ul>"
        + _replaced_notes(option_images, images)
    )


def _render_bar_chart(
    q: Question,
    dist: dict[str, int],
    reveal: dict,
    total_players: int,
    option_images: dict[int, Image] | None = None,
    images: _Images | None = None,
) -> str:
    options = q.config.get("options", [])
    option_images = option_images or {}

    if q.type == "multiple_choice":
        bars = [
            {
                "label": (
                    _rich_option_label(i, opt, option_images[i], images)
                    if images is not None and i in option_images
                    else _option_label(i, opt)
                ),
                "rich": images is not None and i in option_images,
                "count": dist.get(str(i), 0),
                "correct": (i in reveal.get("correctIndices", []))
                if reveal.get("type") == "multiple_choice"
                else None,
            }
            for i, opt in enumerate(options)
        ]
    elif q.type == "true_false":
        cv = reveal.get("correctValue") if reveal.get("type") == "true_false" else None
        bars = [
            {"label": "True", "count": dist.get("true", 0), "correct": cv is True},
            {"label": "False", "count": dist.get("false", 0), "correct": cv is False},
        ]
    else:
        bars = []

    if not bars:
        return ""

    max_count = max((b["count"] for b in bars), default=0) or 1
    MAX_BAR_W = 100  # percentage

    rows = []
    for bar in bars:
        pct = int((bar["count"] / max_count) * MAX_BAR_W)
        min_pct = 3 if bar["count"] > 0 else 0
        display_pct = max(pct, min_pct)

        if bar["correct"] is True:
            fill_col = "#166534"
            mark = '<span class="bar-correct-mark">✓ Correct</span>'
        elif bar["correct"] is False:
            fill_col = "#1e293b"
            mark = '<span class="bar-correct-mark"></span>'
        else:
            fill_col = "#3730a3"
            mark = ""

        count_inner = (
            f'<span class="bar-count">{bar["count"]}</span>' if bar["count"] > 0 else ""
        )
        label_class = "bar-label bar-label-rich" if bar.get("rich") else "bar-label"
        rows.append(
            f'<div class="bar-row">'
            f'<span class="{label_class}">{bar["label"]}</span>'
            f'<div class="bar-track">'
            f'<div class="bar-fill" style="width:{display_pct}%;background:{fill_col}">{count_inner}</div>'
            f"</div>"
            f"{mark}"
            f"</div>"
        )

    notes = _replaced_notes(option_images, images) if images is not None else ""
    return '<div class="bar-chart">' + "".join(rows) + "</div>" + notes


def _render_word_cloud(q: Question, dist: dict[str, int], reveal: dict) -> str:
    entries = sorted(dist.items(), key=lambda kv: -kv[1])
    accepted = [a.lower() for a in reveal.get("acceptedAnswers", [])]
    edit_dist = reveal.get("editDistance", 0)

    # Build the correct-answer note first — shown regardless of whether anyone answered
    parts = []
    if accepted and reveal.get("type") == "fill_in_the_blank":
        answer_str = " / ".join(_esc(a) for a in reveal["acceptedAnswers"])
        edit_note = (
            f' <span style="color:#64748b;font-size:0.85em">(±{edit_dist})</span>'
            if edit_dist > 0
            else ""
        )
        parts.append(
            f'<p class="wc-answer-note">Correct answer: '
            f'<span class="wc-answer">{answer_str}</span>{edit_note}</p>'
        )

    if not entries:
        parts.append('<p class="wc-empty">No answers submitted</p>')
        return "\n".join(parts)

    max_count = max(c for _, c in entries) or 1

    def is_correct(word: str) -> bool:
        return any(_levenshtein(word.lower(), a) <= edit_dist for a in accepted)

    def font_size(count: int) -> str:
        f = count / max_count
        if f > 0.75:
            return "2.4rem"
        if f > 0.5:
            return "1.9rem"
        if f > 0.25:
            return "1.4rem"
        if f > 0.1:
            return "1.1rem"
        return "0.9rem"

    parts.append('<div class="word-cloud">')
    for word, count in entries:
        css_class = "wc-word correct" if is_correct(word) else "wc-word"
        s = "s" if count != 1 else ""
        parts.append(
            f'<span class="{css_class}" style="font-size:{font_size(count)}" '
            f'title="{count} player{s}">{_esc(word)}</span>'
        )
    parts.append("</div>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# T7: the Sort It table and the Spot It heatmap
# ---------------------------------------------------------------------------

_DASH = "—"

# The projector's heat ramp (spec §2.4): viridis stops, a colour-blind-safe sequential ramp,
# at 55% opacity.
HEAT_COLORS = ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725")
HEAT_OPACITY = "0.55"

# The heatmap's largest grid area and its header bands, in SVG user units.
_SPOT_AREA_W, _SPOT_AREA_H = 640, 440
_SPOT_BAND = 24


def heat_step(count: int, max_count: int) -> int:
    """The ramp step (1–5) of a cell with answers, 0 without: the count itself when the
    busiest cell has at most 5, else ⌈5·count / max⌉."""
    if count <= 0 or max_count <= 0:
        return 0
    if max_count <= 5:
        return count
    return -(-5 * count // max_count)


def heat_legend(max_count: int) -> list[tuple[int, str]]:
    """(step, count range) for each legend swatch; none when nobody answered."""
    if max_count <= 0:
        return []
    if max_count <= 5:
        return [(step, str(step)) for step in range(1, max_count + 1)]
    legend = []
    for step in range(1, 6):
        lo, hi = (step - 1) * max_count // 5 + 1, step * max_count // 5
        legend.append((step, str(lo) if lo == hi else f"{lo}–{hi}"))
    return legend


def _pct_text(value: int | None) -> str:
    return _DASH if value is None else f"{value}%"


def _label_at(labels: Sequence[object], index: int | None) -> str:
    """The escaped label at `index`, or "—"."""
    if index is None or not 0 <= index < len(labels):
        return _DASH
    return _esc(labels[index])


def _render_sort_table(
    boxes: Sequence[object],
    cards: Sequence[object],
    breakdown: Sequence[Mapping],
    answered: int,
    *,
    accuracy: bool,
) -> str:
    """Sort It's per-card table (spec §2.8), in the author's card order. `breakdown` comes
    from `sort_it.results_breakdown`; every label is escaped."""
    if accuracy:
        head = (
            "<th>Card</th><th>Correct box</th><th>% correct</th>"
            "<th>Most common wrong box</th><th>% left out</th>"
        )
    else:
        head = "<th>Card</th><th>Most common box</th><th>%</th>"
    rows = []
    for row in breakdown:
        card = _label_at(cards, row["card"])
        if accuracy:
            # The bar uses the exact fraction; the number is the integer half-up percentage.
            share = row["correct_count"] / answered * 100 if answered else 0.0
            rows.append(
                f'<tr><td class="sort-card">{card}</td>'
                f"<td>{_label_at(boxes, row['correct_box'])}</td>"
                f'<td class="sort-pct"><span class="sort-bar">'
                f'<span style="width:{share:.2f}%"></span></span>'
                f"{_pct_text(row['pct_correct'])}</td>"
                f"<td>{_label_at(boxes, row['most_common_wrong_box'])}</td>"
                f"<td>{_pct_text(row['pct_left_out'])}</td></tr>"
            )
        else:
            rows.append(
                f'<tr><td class="sort-card">{card}</td>'
                f"<td>{_label_at(boxes, row['most_common_box'])}</td>"
                f"<td>{_pct_text(row['pct_most_common'])}</td></tr>"
            )
    return (
        f'<table class="sort-table"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _num(value: float) -> str:
    """An SVG coordinate: at most two decimals, no trailing zeros."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _wrap(text: str, max_chars: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= max_chars:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines


def _render_spot_svg(
    cols: int,
    rows: int,
    width: int,
    height: int,
    counts: Mapping[tuple[int, int], int],
    accepted: Iterable[tuple[int, int]],
    *,
    image_href: str | None,
    placeholder: str | None,
    description: str,
    answered: int,
    total_players: int,
    replaced: bool,
) -> str:
    """Spot It's heatmap (spec §2.4, §2.8): an inline SVG with the grid area in the stored
    proportions (`width`×`height`), the small copy (`image_href`, a data URI) or `placeholder`
    text over a neutral background, header letters and numbers, heat fills with counts, and
    "Correct" outlines on the `accepted` cells; then the legend and the replaced note.

    `description` labels the SVG (`<title>`, `aria-label`) and is escaped like everything
    else here.
    """
    scale = min(_SPOT_AREA_W / width, _SPOT_AREA_H / height)
    gw, gh = width * scale, height * scale
    cw, ch = gw / cols, gh / rows
    x0 = y0 = _SPOT_BAND
    label = f"Answer heatmap: {description}" if description else "Answer heatmap"
    parts = [
        f'<svg class="spot-svg" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_num(x0 + gw)} {_num(y0 + gh)}" '
        f'width="{_num(x0 + gw)}" height="{_num(y0 + gh)}" '
        f'role="img" aria-label="{_esc(label)}">',
        f"<title>{_esc(label)}</title>",
        f'<rect x="{x0}" y="{y0}" width="{_num(gw)}" height="{_num(gh)}" fill="#0f172a"/>',
    ]
    if image_href is not None:
        parts.append(
            f'<image href="{_esc(image_href)}" x="{x0}" y="{y0}" width="{_num(gw)}" '
            f'height="{_num(gh)}" preserveAspectRatio="xMidYMid meet"/>'
        )
    elif placeholder:
        font = 13
        lines = _wrap(placeholder, max(8, int((gw - 16) / (0.56 * font))))
        top = y0 + gh / 2 - (len(lines) - 1) * font * 0.65
        parts.append(
            f'<text class="spot-placeholder" x="{_num(x0 + gw / 2)}" y="{_num(top)}" '
            f'font-size="{font}" text-anchor="middle">'
            + "".join(
                f'<tspan x="{_num(x0 + gw / 2)}" dy="{0 if i == 0 else _num(font * 1.3)}">'
                f"{_esc(line)}</tspan>"
                for i, line in enumerate(lines)
            )
            + "</text>"
        )

    max_count = max(counts.values(), default=0)
    for (col, row), count in sorted(
        counts.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        step = heat_step(count, max_count)
        if step:
            parts.append(
                f'<rect x="{_num(x0 + col * cw)}" y="{_num(y0 + row * ch)}" '
                f'width="{_num(cw)}" height="{_num(ch)}" fill="{HEAT_COLORS[step - 1]}" '
                f'fill-opacity="{HEAT_OPACITY}"/>'
            )

    # Grid lines twice, dark then light, so they read over any image (as on the canvases).
    lines_d = "".join(
        f"M{_num(x0 + c * cw)},{y0}V{_num(y0 + gh)}" for c in range(cols + 1)
    )
    lines_d += "".join(
        f"M{x0},{_num(y0 + r * ch)}H{_num(x0 + gw)}" for r in range(rows + 1)
    )
    parts.append(
        f'<path d="{lines_d}" fill="none" stroke="#000" stroke-opacity="0.5" stroke-width="3"/>'
        f'<path d="{lines_d}" fill="none" stroke="#fff" stroke-opacity="0.8" stroke-width="1"/>'
    )

    correct = spot_it.sort_cells(set(accepted))
    tag_font = min(11.0, cw / 5.5, ch / 3)
    for col, row in correct:
        x, y = x0 + col * cw, y0 + row * ch
        parts.append(
            f'<rect x="{_num(x + 1.5)}" y="{_num(y + 1.5)}" width="{_num(cw - 3)}" '
            f'height="{_num(ch - 3)}" fill="none" stroke="#000" stroke-width="3"/>'
            f'<rect x="{_num(x + 4)}" y="{_num(y + 4)}" width="{_num(cw - 8)}" '
            f'height="{_num(ch - 8)}" fill="none" stroke="#fff" stroke-width="2"/>'
            f'<text class="spot-correct" x="{_num(x + cw / 2)}" y="{_num(y + 5 + tag_font)}" '
            f'font-size="{_num(tag_font)}" text-anchor="middle">Correct</text>'
        )

    count_font = min(18.0, cw * 0.4, ch * 0.4)
    accepted_set = set(correct)
    for (col, row), count in sorted(
        counts.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        if count <= 0:
            continue
        shift = tag_font / 2 if (col, row) in accepted_set else 0
        parts.append(
            f'<text class="spot-count" x="{_num(x0 + (col + 0.5) * cw)}" '
            f'y="{_num(y0 + (row + 0.5) * ch + shift)}" font-size="{_num(count_font)}" '
            f'text-anchor="middle" dominant-baseline="central">{count}</text>'
        )

    for col in range(cols):
        parts.append(
            f'<text class="spot-head" x="{_num(x0 + (col + 0.5) * cw)}" y="{y0 / 2}" '
            f'text-anchor="middle" dominant-baseline="central">'
            f"{_esc(image_service.option_letter(col))}</text>"
        )
    for row in range(rows):
        parts.append(
            f'<text class="spot-head" x="{x0 / 2}" y="{_num(y0 + (row + 0.5) * ch)}" '
            f'text-anchor="middle" dominant-baseline="central">{row + 1}</text>'
        )
    parts.append("</svg>")

    keys = [
        f'<span class="spot-key"><span class="spot-swatch" '
        f'style="background:{HEAT_COLORS[step - 1]};opacity:{HEAT_OPACITY}"></span>'
        f"{_esc(text)}</span>"
        for step, text in heat_legend(max_count)
    ]
    keys.append('<span class="spot-key">blank = no answers</span>')
    if correct:
        keys.append(
            '<span class="spot-key"><span class="spot-swatch spot-swatch-correct"></span>'
            "Correct</span>"
        )
    keys.append(f'<span class="spot-key">{answered} of {total_players} answered</span>')
    note = f'<p class="img-note">{_esc(REPLACED_NOTE)}</p>' if replaced else ""
    return (
        f'<figure class="spot-figure">{"".join(parts)}'
        f'<div class="spot-legend">{"".join(keys)}</div>{note}</figure>'
    )


def _sort_it_block(q: Question, q_scores: Sequence[SessionScore], reveal: dict) -> str:
    """The Sort It table from this session's stored answers, with the stale-answer note."""
    placements = [
        parsed
        for s in q_scores
        if (parsed := sort_it.parse_answer(q.config, s.answer_data)) is not None
    ]
    note = _stale_note(len(q_scores) - len(placements))
    dims = sort_it.shape(q.config)
    if dims is None:
        return note
    n, k = dims
    dist: dict[str, int] = defaultdict(int)
    for answer in placements:
        for key in sort_it.dist_keys(answer):
            dist[key] += 1
    key = reveal.get("correctBoxes") if reveal.get("type") == sort_it.TYPE else None
    key = key if isinstance(key, list) and len(key) == n else None
    breakdown = sort_it.results_breakdown(n, k, dist, len(placements), key)
    table = _render_sort_table(
        q.config["boxes"],
        q.config["cards"],
        breakdown,
        len(placements),
        accuracy=key is not None,
    )
    return table + note


def _spot_proportions(
    config: Mapping, image: Image | None, cols: int, rows: int
) -> tuple[int, int]:
    """The stored gameplay proportions (spec §2.4); the image's own when they aren't usable;
    square cells as a last resort."""
    width, height = config.get("width"), config.get("height")
    if spot_it.is_int(width) and spot_it.is_int(height) and width > 0 and height > 0:
        return width, height
    if image is not None and image.width and image.height:
        return image.width, image.height
    return cols, rows


def _spot_it_block(
    q: Question,
    q_scores: Sequence[SessionScore],
    reveal: dict,
    images: _Images,
    total_players: int,
) -> str:
    """The Spot It heatmap from this session's stored answers, with the stale-answer note."""
    cells = [
        parsed
        for s in q_scores
        if (parsed := spot_it.parse_answer(q.config, s.answer_data)) is not None
    ]
    note = _stale_note(len(q_scores) - len(cells))
    dims = spot_it.grid(q.config)
    if dims is None:
        return note
    cols, rows = dims
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for cell in cells:
        counts[cell] += 1
    accepted = (
        [(c["col"], c["row"]) for c in reveal.get("acceptedCells", [])]
        if reveal.get("type") == spot_it.TYPE
        else []
    )
    image = images.canvas(q)
    width, height = _spot_proportions(q.config, image, cols, rows)
    href, placeholder, description, replaced = None, None, "", False
    if image is None:
        placeholder = MISSING_IMAGE_TEXT
    elif not images.include:
        placeholder = WITHHELD_TEXT
    else:
        href = image_service.data_uri(image, "small")
        description = image.description or ""
        replaced = images.replaced(image)
    svg = _render_spot_svg(
        cols,
        rows,
        width,
        height,
        counts,
        accepted,
        image_href=href,
        placeholder=placeholder,
        description=description,
        answered=len(cells),
        total_players=total_players,
        replaced=replaced,
    )
    return svg + note


def _render_histogram(buckets: list[dict]) -> str:
    max_count = max((b["count"] for b in buckets), default=0) or 1
    MAX_H = 160  # max bar height px

    bar_cols, label_cols = [], []
    for b in buckets:
        bar_h = max(int((b["count"] / max_count) * MAX_H), 2 if b["count"] > 0 else 0)
        count_str = str(b["count"]) if b["count"] > 0 else ""
        bar_cols.append(
            f'<div class="hist-col">'
            f'<span class="hist-count">{count_str}</span>'
            f'<div class="hist-bar" style="height:{bar_h}px"></div>'
            f"</div>"
        )
        label_cols.append(f'<div class="hist-label">{_esc(b["label"])}</div>')

    return (
        '<div class="histogram">'
        + "".join(bar_cols)
        + "</div>"
        + '<div class="hist-labels">'
        + "".join(label_cols)
        + "</div>"
        + '<p class="hist-axis">Score (points) →</p>'
    )


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
  background:#0f172a;color:#e2e8f0;line-height:1.6;padding:48px 24px;
}
.container{max-width:980px;margin:0 auto}

/* ── Header ── */
.rpt-header{text-align:center;margin-bottom:56px;padding-bottom:40px;border-bottom:1px solid #1e293b}
.rpt-header h1{font-size:2.4rem;font-weight:900;color:#f1f5f9;margin-bottom:12px}
.rpt-meta{color:#64748b;font-size:0.9rem}
.rpt-meta .sep{margin:0 10px;color:#1e293b}

/* ── Section label ── */
.section-label{
  font-size:0.65rem;text-transform:uppercase;letter-spacing:0.15em;
  color:#334155;margin-bottom:20px;
}

/* ── Question cards ── */
.q-card{
  background:#1e293b;border:1px solid #334155;border-radius:16px;
  padding:32px 36px;margin-bottom:24px;
}
.q-meta{display:flex;align-items:center;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.q-num{color:#475569;font-size:0.75rem;font-family:monospace;font-weight:700}
.badge{font-size:0.65rem;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:0.04em}
.badge-mc{background:#1e3a5f;color:#93c5fd}
.badge-tf{background:#1a3a2a;color:#86efac}
.badge-fitb{background:#3b2f1e;color:#fbbf24}
.badge-accuracy{background:#2e1b3d;color:#c084fc}
.badge-completeness{background:#2d2d1a;color:#fde68a}
.q-timing{margin-left:auto;color:#475569;font-size:0.78rem}
.prompt{font-size:1.2rem;font-weight:600;color:#f1f5f9;margin-bottom:28px;line-height:1.5}
.q-stats{margin-top:16px;font-size:0.78rem;color:#475569;text-align:right}
.pct-correct{color:#4ade80;font-weight:700}

/* ── Bar chart ── */
.bar-chart{display:flex;flex-direction:column;gap:10px}
.bar-row{display:flex;align-items:center;gap:12px}
.bar-label{
  font-size:0.88rem;font-weight:500;color:#cbd5e1;
  width:210px;flex-shrink:0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.bar-track{flex:1;height:44px;background:#0f172a;border-radius:8px;overflow:hidden}
.bar-fill{height:100%;border-radius:8px;display:flex;align-items:center;justify-content:flex-end;padding-right:12px}
.bar-count{font-size:0.85rem;font-weight:700;color:#fff}
.bar-correct-mark{font-size:0.72rem;color:#4ade80;font-weight:700;width:68px;flex-shrink:0}

/* ── Images (T8) ── */
.q-image{margin:-12px 0 24px;text-align:center}
.prompt-img{display:block;margin:0 auto;max-width:100%;max-height:340px;border-radius:12px}
.bar-label-rich{white-space:normal;display:flex;align-items:center;gap:8px;overflow:visible}
.opt-letter{font-weight:700;color:#e2e8f0}
.opt-thumb{width:44px;height:44px;object-fit:cover;border-radius:6px;flex-shrink:0}
.opt-caption{overflow:hidden;text-overflow:ellipsis}
.opt-list{list-style:none;display:flex;flex-direction:column;gap:8px}
.opt-row{display:flex;align-items:center;gap:10px;background:#0f172a;border-radius:8px;padding:8px 12px;color:#cbd5e1}
.img-note{font-size:0.75rem;color:#fbbf24;margin-top:6px}
.img-withheld{font-size:0.8rem;color:#94a3b8;font-style:italic}

/* ── Sort It table, Spot It heatmap (T7) ── */
.badge-sort{background:#123a3a;color:#5eead4}
.badge-spot{background:#3a1733;color:#f9a8d4}
.sort-table{width:100%;border-collapse:collapse;font-size:0.86rem;color:#cbd5e1}
.sort-table th{font-size:0.66rem;text-transform:uppercase;letter-spacing:0.08em;color:#64748b;text-align:left;padding:6px 8px;border-bottom:1px solid #334155}
.sort-table td{padding:8px;border-bottom:1px solid #0f172a;vertical-align:middle}
.sort-card{font-weight:600;color:#f1f5f9}
.sort-pct{white-space:nowrap}
.sort-bar{display:inline-block;width:80px;height:10px;background:#0f172a;border-radius:5px;overflow:hidden;vertical-align:middle;margin-right:8px}
.sort-bar span{display:block;height:100%;background:#16a34a}
.spot-figure{margin:0;text-align:center}
.spot-svg{display:block;margin:0 auto;max-width:100%;height:auto}
.spot-svg text{font-family:inherit}
.spot-head{fill:#94a3b8;font-size:12px;font-weight:700}
.spot-count{fill:#fff;font-weight:800;stroke:#000;stroke-width:3px;paint-order:stroke}
.spot-correct{fill:#fff;font-weight:800;stroke:#000;stroke-width:2.5px;paint-order:stroke}
.spot-placeholder{fill:#94a3b8;font-style:italic}
.spot-legend{display:flex;flex-wrap:wrap;justify-content:center;gap:6px 16px;margin-top:12px;font-size:0.78rem;color:#94a3b8}
.spot-key{display:inline-flex;align-items:center;gap:6px}
.spot-swatch{display:inline-block;width:14px;height:14px;border-radius:3px}
.spot-swatch-correct{border:2px solid #fff;outline:2px solid #000}
.t7-note{font-size:0.75rem;color:#fbbf24;margin-top:10px}

/* ── Word cloud ── */
.wc-answer-note{text-align:center;color:#64748b;font-size:0.88rem;margin-bottom:14px}
.wc-answer{color:#4ade80;font-weight:700}
.word-cloud{
  display:flex;flex-wrap:wrap;gap:10px 18px;
  justify-content:center;align-items:center;
  min-height:80px;padding:20px;
  background:#0f172a;border-radius:12px;
}
.wc-word{font-weight:700;color:#64748b}
.wc-word.correct{color:#4ade80}
.wc-empty{color:#334155;font-size:0.9rem;text-align:center;padding:20px}

/* ── Histogram ── */
.summary-card{
  background:#1e293b;border:1px solid #334155;border-radius:16px;
  padding:36px;margin-bottom:24px;
}
.summary-card h2{font-size:1.4rem;font-weight:800;color:#f1f5f9;margin-bottom:32px;text-align:center}
.histogram{display:flex;align-items:flex-end;gap:8px;margin-bottom:0}
.hist-col{flex:1;display:flex;flex-direction:column;align-items:center}
.hist-count{font-size:0.72rem;font-weight:700;color:#94a3b8;margin-bottom:4px;min-height:18px}
.hist-bar{width:100%;background:#6366f1;border-radius:4px 4px 0 0}
.hist-labels{display:flex;gap:8px;margin-top:6px}
.hist-label{flex:1;text-align:center;font-size:0.68rem;color:#475569}
.hist-axis{text-align:center;font-size:0.7rem;color:#334155;margin-top:6px}

/* ── Summary stats ── */
.stats-row{
  display:flex;justify-content:center;
  margin-top:32px;padding-top:28px;border-top:1px solid #334155;
}
.stat-box{flex:1;text-align:center;padding:0 16px}
.stat-box+.stat-box{border-left:1px solid #334155}
.stat-label{font-size:0.65rem;text-transform:uppercase;letter-spacing:0.12em;color:#64748b;margin-bottom:6px}
.stat-value{font-size:2rem;font-weight:900;color:#f1f5f9}

/* ── Footer ── */
.rpt-footer{text-align:center;margin-top:56px;color:#1e293b;font-size:0.72rem}
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def build_session_report(
    db: AsyncSession, session_id: str, *, include_images: bool
) -> tuple[str, bytes]:
    """
    Return (filename, html_bytes) for a standalone session report.
    Aggregate statistics only — no player names or individual scores.

    `include_images`: whether the downloader passes the game rule for the session's game
    now (admins always do). Otherwise each image is replaced by the withheld text.
    """
    session = await db.get(GameSession, session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    game = await db.get(Game, session.game_id)
    course = await db.get(Course, session.course_id)

    # Sorted in Python: MySQL's sort fails for a question row over its sort buffer.
    q_result = await db.execute(
        select(Question).where(Question.game_id == session.game_id)
    )
    questions = sorted(q_result.scalars().all(), key=image_service.question_order)

    # One query for every referenced image, scoped to the session's game (so another
    # game's id is unresolved), with small copies only when they'll be embedded.
    images = _Images(
        await image_service.images_for_refs(
            db,
            session.game_id,
            {ref.image_id for q in questions for ref in image_refs(q.config)},
            with_small=include_images,
        ),
        include_images,
        session.created_at,
    )

    s_result = await db.execute(
        select(SessionScore).where(SessionScore.session_id == session_id)
    )
    all_scores = s_result.scalars().all()

    # Group by question
    by_question: dict[int, list[SessionScore]] = defaultdict(list)
    for s in all_scores:
        by_question[s.question_id].append(s)

    # Unique players and totals for histogram
    player_totals: dict[str, float] = defaultdict(float)
    for s in all_scores:
        player_totals[s.user_id] += float(s.points_awarded)
    final_scores = list(player_totals.values())
    total_players = len(player_totals)

    max_possible = sum(q.points_value for q in questions)
    game_title = game.title if game else "Unknown Game"
    course_name = f"{course.name} — {course.semester}" if course else "Unknown Course"

    ts = session.completed_at or session.created_at
    date_played = ts.strftime("%B %d, %Y") if ts else "Unknown Date"

    # ── Question sections ──────────────────────────────────────────────────
    q_sections: list[str] = []
    for idx, q in enumerate(questions):
        q_scores = by_question.get(q.id, [])
        answered = len(q_scores)
        correct_count = sum(1 for s in q_scores if s.is_correct)

        dist: dict[str, int] = defaultdict(int)
        for s in q_scores:
            key = _extract_answer_key(q.type, s.answer_data)
            if key is not None:
                dist[key] += 1

        reveal = _answer_reveal(q)
        is_accuracy = q.grading_type == "ACCURACY"

        type_badge = {
            "multiple_choice": ("Multiple Choice", "badge-mc"),
            "true_false": ("True / False", "badge-tf"),
            "fill_in_the_blank": ("Fill in the Blank", "badge-fitb"),
            sort_it.TYPE: ("Sort It", "badge-sort"),
            spot_it.TYPE: ("Spot It", "badge-spot"),
        }.get(q.type, (q.type, ""))
        type_label, type_class = type_badge

        grading_label = "Accuracy" if is_accuracy else "Completeness"
        grading_class = "badge-accuracy" if is_accuracy else "badge-completeness"

        pts_label = _points_label(q.type, q.points_value)

        prompt_image, option_images = images.for_question(q)
        if q.type in ("multiple_choice", "true_false"):
            chart = _render_bar_chart(
                q, dict(dist), reveal, total_players, option_images, images
            )
        elif q.type == "fill_in_the_blank":
            chart = _render_word_cloud(q, dict(dist), reveal)
        elif q.type == "multi_select":
            chart = _render_option_list(q, option_images, images)
        elif q.type == sort_it.TYPE:
            chart = _sort_it_block(q, q_scores, reveal)
        elif q.type == spot_it.TYPE:
            chart = _spot_it_block(q, q_scores, reveal, images, total_players)
        else:
            chart = ""

        if is_accuracy and answered > 0:
            pct = round(correct_count / answered * 100)
            # Sort It's `is_correct` is the whole board; say so beside its per-card numbers.
            correct_text = "all cards correct" if q.type == sort_it.TYPE else "correct"
            stats = (
                f'<div class="q-stats">'
                f"{answered} / {total_players} answered"
                f' · <span class="pct-correct">{pct}% {correct_text}</span>'
                f"</div>"
            )
        else:
            stats = f'<div class="q-stats">{answered} / {total_players} answered</div>'

        # Prompt was already bleach-sanitised on write; embed as HTML
        q_sections.append(
            f'<div class="q-card">'
            f'<div class="q-meta">'
            f'<span class="q-num">Q{idx + 1} of {len(questions)}</span>'
            f'<span class="badge {type_class}">{_esc(type_label)}</span>'
            f'<span class="badge {grading_class}">{_esc(grading_label)}</span>'
            f'<span class="q-timing">{q.time_limit_seconds}s · {_esc(pts_label)}</span>'
            f"</div>"
            f'<p class="prompt">{q.prompt}</p>'
            f"{_render_prompt_image(prompt_image, images)}"
            f"{chart}"
            f"{stats}"
            f"</div>"
        )

    # ── Histogram ─────────────────────────────────────────────────────────
    buckets = _build_histogram(final_scores, max_possible)
    histogram = _render_histogram(buckets)
    avg = round(sum(final_scores) / len(final_scores), 1) if final_scores else 0
    high = max(final_scores) if final_scores else 0

    # ── Assemble document ─────────────────────────────────────────────────
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    safe = re.sub(r"[^a-zA-Z0-9-]", "_", game_title)  # spaces and special chars → _
    safe = re.sub(r"_+", "_", safe).strip("_")  # collapse runs, trim edges
    date_str = ts.strftime("%Y-%m-%d") if ts else "unknown"
    filename = f"report_{safe}_{date_str}_{session_id[:8]}.html"

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=1024">
<title>{_esc(game_title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">

<div class="rpt-header">
  <h1>{_esc(game_title)}</h1>
  <div class="rpt-meta">
    <span>{_esc(course_name)}</span>
    <span class="sep">·</span>
    <span>{_esc(date_played)}</span>
    <span class="sep">·</span>
    <span>{total_players} player{"s" if total_players != 1 else ""}</span>
    <span class="sep">·</span>
    <span>Max {max_possible:g} pts</span>
  </div>
</div>

<p class="section-label">Questions &amp; Results</p>

{"".join(q_sections)}

<div class="summary-card">
  <h2>Score Distribution</h2>
  {histogram}
  <div class="stats-row">
    <div class="stat-box">
      <div class="stat-label">Players</div>
      <div class="stat-value">{total_players}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Average Score</div>
      <div class="stat-value">{avg:g}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">High Score</div>
      <div class="stat-value">{high:g}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Max Possible</div>
      <div class="stat-value">{max_possible:g}</div>
    </div>
  </div>
</div>

<div class="rpt-footer">Generated by Buzzer · {_esc(timestamp)}</div>

</div>
</body>
</html>"""

    return filename, doc.encode()
