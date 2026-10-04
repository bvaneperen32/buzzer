#!/usr/bin/env python3
"""
Embed a hand-written game's images, after checking what the import can't tell you in advance.

The game file is a version 2 bundle written by hand, and the only copy of its questions. Its
images are files in `images/<file stem>/` beside it (`sample_games/wisconsin_sports.json` reads
`sample_games/images/wisconsin_sports/`), and each image entry's `filename` names one of them.
This copies every file's bytes, base64-encoded and otherwise unchanged, into the entry's `data`
and rewrites the bundle in the exporter's format. A bundle with any error is left as it is.

Checked here: the image limits, the image references, Spot It grids against their images, and
T6's coverage (the four classic question types and both grading modes). The import checks the
rest of each question.

Usage (repo root, venv active):
    python scripts/pack_game.py BUNDLE...                # embed the images and rewrite
    python scripts/pack_game.py --check BUNDLE...        # exit 1 if any bundle would change
    python scripts/pack_game.py --preview DIR BUNDLE...  # also draw each Spot It grid in DIR

--preview saves DIR/<image key>_grid.png for each Spot It question: the image under its grid,
every cell labelled ("A1" = column A, row 1) and the accepted cells tinted, so the cells are
chosen by looking. It draws even when the bundle has other errors.

Nothing here re-encodes or resizes an image, or reads the clock, the network or a system font.
Design: docs/plans/t6-brody-quiz-games.md §4.6.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

REPO_ROOT = Path(__file__).resolve().parent.parent

# Tighter than the server's limits: each image is stored twice in the repo (file and base64),
# and an image within 1600 px is stored at the size players see.
MAX_EDGE = 1600
MAX_BYTES = 2 * 1024 * 1024
FORMATS = {"PNG", "JPEG", "WEBP"}

# The import's rules for a bundle image (`schemas/image.py` BundleImage, `image_service`).
ENTRY_FIELDS = {"key", "filename", "description", "data"}
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DESCRIPTION_MAX = 300
FILENAME_MAX = 255

CLASSIC_TYPES = ("multiple_choice", "true_false", "fill_in_the_blank", "multi_select")
NEW_TYPES = ("sort_into_boxes", "find_in_image")
MODES = ("ACCURACY", "COMPLETENESS")

PREVIEW_EDGE = 1000  # small images are enlarged so the cell labels can be read
TINT = (230, 0, 126)


@dataclass
class Result:
    text: str | None = None  # the packed bundle; None when it couldn't be built
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def backend():
    """The backend's pure `image_refs` and `spot_it` modules: reused, not copied. Imported here,
    not at the top, because an import after editing `sys.path` trips ruff's E402. Neither pulls
    in FastAPI or the database."""
    path = str(REPO_ROOT / "backend")
    if path not in sys.path:
        sys.path.insert(0, path)
    from app.services import image_refs, spot_it

    return image_refs, spot_it


def serialize(bundle: dict) -> str:
    """The exporter's format (`bundle_service.serialize`) plus a final newline."""
    return json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"


def plain_filename(name: str) -> bool:
    """A name the import's `clean_filename` keeps exactly: no path, no control characters, no
    surrounding spaces, at most 255 characters."""
    return (
        0 < len(name) <= FILENAME_MAX
        and not re.search(r"[\\/]", name)
        and name == name.strip()
        and all(unicodedata.category(ch) != "Cc" for ch in name)
    )


def open_oriented(path: Path) -> Image.Image:
    """The image as players see it: the server applies the same EXIF orientation."""
    with Image.open(path) as img:
        if img.format not in FORMATS:
            raise ValueError(f"is {img.format}, not PNG, JPEG or WebP")
        if getattr(img, "is_animated", False):
            raise ValueError("is animated")
        return ImageOps.exif_transpose(img)


def pack_images(images: list, folder: Path, result: Result) -> dict[str, Image.Image]:
    """Check every image entry and embed its file. Returns each usable image by key."""
    usable: dict[str, Image.Image] = {}
    seen: set[str] = set()
    for number, entry in enumerate(images, start=1):
        if not isinstance(entry, dict):
            result.errors.append(f"image entry {number} isn't an object")
            continue
        key = entry.get("key")
        label = f"image {key}" if isinstance(key, str) else f"image entry {number}"
        if set(entry) != ENTRY_FIELDS:
            result.errors.append(
                f"{label}: needs exactly {', '.join(sorted(ENTRY_FIELDS))}"
            )
        good_key = isinstance(key, str) and KEY_PATTERN.match(key) is not None
        if not good_key:
            result.errors.append(
                f"{label}: the key must be 1–64 letters, digits, _ or -"
            )
        elif key in seen:
            result.errors.append(f"{label}: the key is used more than once")
        else:
            seen.add(key)
        description = entry.get("description")
        if not isinstance(description, str):
            result.errors.append(f"{label}: the description must be text")
        elif not 1 <= len(description.strip()) <= DESCRIPTION_MAX:
            result.errors.append(f"{label}: the description must be 1–300 characters")
        elif description != description.strip():
            result.errors.append(
                f"{label}: the description has spaces the import trims"
            )

        filename = entry.get("filename")
        if not isinstance(filename, str) or not plain_filename(filename):
            result.errors.append(f"{label}: the filename must be a plain file name")
            continue
        file = folder / filename
        if not file.is_file():
            result.errors.append(f"{label}: {file} doesn't exist")
            continue
        data = file.read_bytes()
        if len(data) > MAX_BYTES:
            result.errors.append(f"{label}: {filename} is over 2 MiB")
            continue
        try:
            img = open_oriented(file)
        except ValueError as exc:
            result.errors.append(f"{label}: {filename} {exc}")
            continue
        except (OSError, Image.DecompressionBombError):
            result.errors.append(f"{label}: {filename} can't be opened as an image")
            continue
        if max(img.size) > MAX_EDGE:
            result.errors.append(
                f"{label}: {filename} is {img.width}×{img.height}; "
                f"the long side must be at most {MAX_EDGE} px"
            )
            continue
        entry["data"] = base64.b64encode(data).decode("ascii")
        if good_key:
            usable[key] = img
    return usable


def draw_grid(img: Image.Image, cols: int, rows: int, cells, cell_label) -> Image.Image:
    """The image under its grid, each cell labelled, the accepted cells tinted."""
    scale = max(1, -(-PREVIEW_EDGE // max(img.size)))
    w, h = img.width * scale, img.height * scale
    base = img.convert("RGBA").resize((w, h), Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def box(col: int, row: int) -> tuple[int, int, int, int]:
        """A cell's bounds: the grid divides the whole image evenly, as on the projector."""
        return (
            round(col * w / cols),
            round(row * h / rows),
            round((col + 1) * w / cols),
            round((row + 1) * h / rows),
        )

    for col, row in cells:
        draw.rectangle(box(col, row), fill=(*TINT, 90), outline=(*TINT, 255), width=4)
    for col in range(1, cols):
        x = box(col, 0)[0]
        draw.line([(x, 0), (x, h)], fill=(255, 255, 255, 255), width=3)
        draw.line([(x, 0), (x, h)], fill=(0, 0, 0, 255), width=1)
    for row in range(1, rows):
        y = box(0, row)[1]
        draw.line([(0, y), (w, y)], fill=(255, 255, 255, 255), width=3)
        draw.line([(0, y), (w, y)], fill=(0, 0, 0, 255), width=1)
    font = ImageFont.load_default(size=max(12, min(w // cols, h // rows) // 6))
    for col in range(cols):
        for row in range(rows):
            x0, y0, _, _ = box(col, row)
            at = (x0 + 5, y0 + 4)
            left, top, right, bottom = draw.textbbox(
                at, cell_label(col, row), font=font
            )
            draw.rectangle(
                (left - 3, top - 3, right + 3, bottom + 3), fill=(0, 0, 0, 170)
            )
            draw.text(at, cell_label(col, row), font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(base, overlay).convert("RGB")


def pack(path: Path, preview: Path | None) -> Result:
    """Check one bundle, embed its images, and draw its Spot It previews."""
    image_refs, spot_it = backend()
    result = Result()
    try:
        bundle = json.loads(path.read_bytes().decode("utf-8"))
    except OSError as exc:
        result.errors.append(f"can't read it: {exc.strerror}")
        return result
    except ValueError as exc:  # a JSON or UTF-8 error is a ValueError
        result.errors.append(f"isn't UTF-8 JSON: {exc}")
        return result
    if (
        not isinstance(bundle, dict)
        or bundle.get("format") != "buzzer/game"
        or bundle.get("version") != 2
    ):
        result.errors.append(
            'not a version 2 bundle ("format": "buzzer/game", "version": 2)'
        )
        return result
    images, questions = bundle.get("images"), bundle.get("questions")
    if not isinstance(images, list) or not isinstance(questions, list):
        result.errors.append('"images" and "questions" must both be lists')
        return result

    usable = pack_images(images, path.parent / "images" / path.stem, result)
    keys = {entry.get("key") for entry in images if isinstance(entry, dict)}
    used = set()
    for number, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            result.errors.append(f"Q{number} isn't an object")
            continue
        config, answer_data = question.get("config"), question.get("answer_data")
        refs: list = []
        image_refs.map_image_refs(config, refs.append)  # visits every reference slot
        for ref in refs:
            if isinstance(ref, str) and ref in keys:
                used.add(ref)
            else:
                result.errors.append(
                    f"Q{number}: refers to {ref!r}, which no image has"
                )
        if question.get("type") != spot_it.TYPE:
            continue

        try:  # the import's own rule; `confirmed` is the bundle's, so it isn't passed on
            spot_it.validate(
                config,
                {k: v for k, v in answer_data.items() if k != "confirmed"}
                if isinstance(answer_data, dict)
                else answer_data,
                question.get("grading_type"),
                question.get("points_value"),
            )
        except (ValueError, TypeError) as exc:
            result.errors.append(f"Q{number}: {exc}")
        if not (isinstance(answer_data, dict) and answer_data.get("confirmed") is True):
            result.warnings.append(
                f"Q{number}: not confirmed, so the game can't start until a host confirms it"
            )
        grid = spot_it.grid(config)
        key = config.get(image_refs.CANVAS_KEY) if isinstance(config, dict) else None
        img = usable.get(key) if isinstance(key, str) else None
        if grid is None or img is None:
            continue
        cols, rows = grid
        if not spot_it.grid_fits(cols, rows, *img.size):
            max_cols, max_rows = spot_it.max_grid(*img.size)
            result.errors.append(
                f"Q{number}: a {cols}×{rows} grid doesn't fit image {key}; "
                f"it allows at most {max_cols}×{max_rows}"
            )
        if preview:
            preview.mkdir(parents=True, exist_ok=True)
            target = preview / f"{key}_grid.png"
            cells = spot_it.accepted_cells(answer_data, cols, rows)
            draw_grid(img, cols, rows, cells, spot_it.cell_label).save(target)
            result.notes.append(f"preview: {target} (Q{number}, {cols}×{rows})")
    for key in sorted(k for k in keys - used if isinstance(k, str)):
        result.errors.append(f"image {key}: no question uses it")

    types = {q.get("type") for q in questions if isinstance(q, dict)}
    modes = {q.get("grading_type") for q in questions if isinstance(q, dict)}
    present = [t for t in CLASSIC_TYPES + NEW_TYPES if t in types]
    result.notes.insert(
        0,
        f"types: {', '.join(present) or 'none'}; "
        f"modes: {', '.join(m for m in MODES if m in modes) or 'none'}",
    )
    missing = [t for t in CLASSIC_TYPES if t not in types]
    missing += [m for m in MODES if m not in modes]
    if missing:
        result.errors.append(f"every T6 game needs {', '.join(missing)}")
    missing = [t for t in NEW_TYPES if t not in types]
    if missing:
        result.warnings.append(
            f"no {', '.join(missing)} (one game per member needs both)"
        )
    result.text = serialize(bundle)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    parser.add_argument("bundles", nargs="+", type=Path, metavar="BUNDLE")
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if any bundle would change",
    )
    parser.add_argument(
        "--preview",
        type=Path,
        metavar="DIR",
        help="also draw each Spot It grid as a PNG in DIR",
    )
    args = parser.parse_args(argv)

    failed = False
    for path in args.bundles:
        result = pack(path, args.preview)
        print(path)
        for line in result.notes:
            print(f"  {line}")
        for line in result.warnings:
            print(f"  warning: {line}")
        for line in result.errors:
            print(f"  error: {line}")
        if result.errors or result.text is None:
            print(f"  not written ({len(result.errors)} error(s))")
            failed = True
            continue
        packed = result.text.encode("utf-8")  # LF line endings on every platform
        if path.read_bytes() == packed:
            print("  up to date" if args.check else "  unchanged")
        elif args.check:
            print("  out of date")
            failed = True
        else:
            path.write_bytes(packed)
            print("  wrote")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
