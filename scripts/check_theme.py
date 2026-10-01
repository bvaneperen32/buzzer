#!/usr/bin/env python3
"""
Check the shared colour tokens (T9): WCAG contrast, token names, and leftover colours.

Runs three checks, prints a report, and exits 1 if any check fails, else 0:

  1. Contrast: parses frontend/shared/theme/tokens.css, resolves every token in both
     themes, prints each semantic token's resolved value (compare with the spec's
     section 2.5), and computes the WCAG 2.1 contrast ratio of every pair listed in
     frontend/shared/theme/contrast-pairs.json.
  2. Names: every token that preset.js and base.css refer to, and every pair's fg/bg,
     exists in tokens.css.
  3. Leftovers: no Tailwind palette class, focus-suppressing class, dark: variant, or hex
     colour in app source (src/**/*.ts, *.tsx), each app's src/index.css, index.html and
     tailwind.config.ts, or the other shared theme files. A genuine false positive is
     silenced by the comment "theme-check: allow" on the same line.

Spec: docs/plans/t9-ui-theming.md, sections 2.4 (the tokens.css format) and 4.3.
Standard library only. Paths are resolved from this file, so it runs from any directory.

Usage:
    python scripts/check_theme.py
    .venv\\Scripts\\python scripts\\check_theme.py      (Windows)
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
THEME_DIR = FRONTEND / "shared" / "theme"
TOKENS = THEME_DIR / "tokens.css"
PAIRS = THEME_DIR / "contrast-pairs.json"
APPS = ("host", "player", "admin")
SHARED_SCANNED = ("base.css", "preset.js", "preset.d.ts", "theme.ts")

# tokens.css format (spec section 2.4)
BLOCK_SELECTORS = (":root", ':root[data-theme="dark"]')
COMMENT = re.compile(r"/\*.*?\*/", re.S)
BLOCK = re.compile(r"([^{}]*)\{([^{}]*)\}")
DECLARATION = re.compile(r"--([a-z0-9-]+):\s*(\S.*?)\s*;")
TRIPLE = re.compile(r"(\d{1,3}) (\d{1,3}) (\d{1,3})")
VAR = re.compile(r"var\(--([a-z0-9-]+)\)")
PRIMITIVE = re.compile(r"[a-z]+-\d+")  # --sage-1 ... --pink-12

# How preset.js names a token: its helper v('bg-canvas') -> rgb(var(--bg-canvas) / ...)
PRESET_HELPER = re.compile(r"""\bv\(\s*['"]([a-z0-9-]+)['"]\s*\)""")

# Leftover scan (spec section 4.3)
PALETTE_PREFIXES = (
    "bg text border ring ring-offset divide placeholder from via to fill stroke outline "
    "shadow accent caret decoration"
).split()
PALETTE_NAMES = (
    "slate gray zinc neutral stone red orange amber yellow lime green emerald teal cyan "
    "sky blue indigo violet purple fuchsia pink rose white black"
).split()
LEFTOVERS = (
    (
        "palette class",
        re.compile(
            rf"(?<![\w-])(?:{'|'.join(PALETTE_PREFIXES)})"
            rf"-(?:{'|'.join(PALETTE_NAMES)})(?:-\d+)?(?:/\d+)?(?![\w-])"
        ),
    ),
    (
        "focus class",
        re.compile(
            r"focus:ring|focus:outline|focus:border-transparent|ring-offset-|outline-none"
        ),
    ),
    ("dark: variant", re.compile(r"(?<![\w-])dark:(?=[\w!\[-])")),
    (
        "hex colour",
        re.compile(r"(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b"),
    ),
)
ALLOW = "theme-check: allow"


class TokenError(Exception):
    """tokens.css is malformed, or a token can't be resolved."""


# ---------------------------------------------------------------------------
# tokens.css
# ---------------------------------------------------------------------------


def strip_comments(text: str) -> str:
    """Remove /* ... */ comments, keeping their newlines so line numbers stay right."""
    return COMMENT.sub(lambda m: "\n" * m.group().count("\n"), text)


def parse_block(body: str, first_line: int) -> dict[str, str]:
    """Parse one block's body: one `--name: value;` per line, value a triple or var()."""
    decls: dict[str, str] = {}
    for offset, raw in enumerate(body.split("\n")):
        line = raw.strip()
        if not line:
            continue
        lineno = first_line + offset
        match = DECLARATION.fullmatch(line)
        if not match:
            raise TokenError(f"tokens.css:{lineno}: malformed declaration: {line}")
        name, value = match.groups()
        triple = TRIPLE.fullmatch(value)
        if triple:
            if any(int(c) > 255 for c in triple.groups()):
                raise TokenError(f"tokens.css:{lineno}: channel over 255: {line}")
        elif not VAR.fullmatch(value):
            raise TokenError(
                f"tokens.css:{lineno}: value is neither 'r g b' nor var(--name): {line}"
            )
        if name in decls:
            raise TokenError(f"tokens.css:{lineno}: --{name} declared twice in a block")
        decls[name] = value
    return decls


def parse_tokens(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return the light (:root) and dark-block declarations, name -> raw value."""
    text = strip_comments(path.read_text(encoding="utf-8"))
    blocks: list[tuple[str, dict[str, str]]] = []
    end = 0
    for match in BLOCK.finditer(text):
        selector = " ".join(match.group(1).split())
        first_line = text.count("\n", 0, match.start(2)) + 1
        blocks.append((selector, parse_block(match.group(2), first_line)))
        end = match.end()
    if text[end:].strip():
        raise TokenError("tokens.css: unexpected text after the last block")
    selectors = tuple(selector for selector, _ in blocks)
    if selectors != BLOCK_SELECTORS:
        raise TokenError(
            f"tokens.css: expected exactly the blocks {BLOCK_SELECTORS} in that order, "
            f"found {selectors}"
        )
    return blocks[0][1], blocks[1][1]


def resolve(name: str, decls: dict[str, str], chain: tuple[str, ...] = ()) -> tuple:
    """Follow var(--x) references within one theme down to an (r, g, b) triple."""
    if name in chain:
        raise TokenError("cycle: " + " -> ".join(f"--{n}" for n in (*chain, name)))
    if name not in decls:
        via = f" (referenced by --{chain[-1]})" if chain else ""
        raise TokenError(f"--{name} is not defined{via}")
    value = decls[name]
    ref = VAR.fullmatch(value)
    if ref:
        return resolve(ref.group(1), decls, (*chain, name))
    return tuple(int(c) for c in TRIPLE.fullmatch(value).groups())


def to_hex(rgb: tuple) -> str:
    return "#" + "".join(f"{c:02x}" for c in rgb)


# ---------------------------------------------------------------------------
# WCAG 2.1 contrast
# ---------------------------------------------------------------------------


def luminance(rgb: tuple) -> float:
    def linear(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple, b: tuple) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_contrast(themes: dict[str, dict[str, str]], pairs: list[dict]) -> bool:
    print("== 1. Contrast (WCAG 2.1) ==")
    resolved = {
        theme: {name: resolve(name, decls) for name in decls}
        for theme, decls in themes.items()
    }

    print("\nSemantic tokens, resolved:")
    print(f"  {'token':<22}{'light':<10}dark")
    for name in themes["light"]:
        if PRIMITIVE.fullmatch(name):
            continue
        light = to_hex(resolved["light"][name])
        dark = to_hex(resolved["dark"][name])
        print(f"  {name:<22}{light:<10}{dark}")

    print(f"\nPairs ({len(pairs)}):")
    print(f"  {'':<6}{'fg':<18}{'bg':<19}{'min':<5}{'light':>6}{'dark':>7}   hexes")
    failures = 0
    for pair in pairs:
        fg, bg, need = pair["fg"], pair["bg"], pair["min"]
        ratios, hexes = [], []
        for theme in ("light", "dark"):
            fg_rgb = resolve(fg, themes[theme])
            bg_rgb = resolve(bg, themes[theme])
            ratios.append(contrast(fg_rgb, bg_rgb))
            hexes.append(f"{to_hex(fg_rgb)}/{to_hex(bg_rgb)}")
        ok = all(ratio >= need for ratio in ratios)
        failures += not ok
        print(
            f"  {'ok' if ok else 'FAIL':<6}{fg:<18}{bg:<19}{need:<5}"
            f"{ratios[0]:>6.2f}{ratios[1]:>7.2f}   light {hexes[0]}  dark {hexes[1]}"
        )
    print(f"\n{len(pairs)} pairs, {failures} failing")
    return failures == 0


def check_names(defined: set[str], pairs: list[dict]) -> bool:
    print("\n== 2. Names ==")
    preset = (THEME_DIR / "preset.js").read_text(encoding="utf-8")
    base = strip_comments((THEME_DIR / "base.css").read_text(encoding="utf-8"))
    used = {
        "preset.js": set(VAR.findall(preset)) | set(PRESET_HELPER.findall(preset)),
        "base.css": set(VAR.findall(base)),
        "contrast-pairs.json": {p[k] for p in pairs for k in ("fg", "bg")},
    }
    missing = 0
    for source, names in used.items():
        for name in sorted(names - defined):
            print(f"  {source}: --{name} is not in tokens.css")
            missing += 1
        print(f"  {source}: {len(names)} token names checked")
    print(f"{missing} missing")
    return missing == 0


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for app in APPS:
        base = FRONTEND / app
        files += sorted(
            p for p in (base / "src").rglob("*") if p.suffix in (".ts", ".tsx")
        )
        files += [
            base / "src" / "index.css",
            base / "index.html",
            base / "tailwind.config.ts",
        ]
    files += [THEME_DIR / name for name in SHARED_SCANNED]
    return files


def check_leftovers() -> bool:
    print("\n== 3. Leftovers ==")
    files = scanned_files()
    found = allowed = 0
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if not path.is_file():
            print(f"  {rel}: missing (it should exist and be scanned)")
            found += 1
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            hits = [
                (kind, m.group())
                for kind, pattern in LEFTOVERS
                for m in pattern.finditer(line)
            ]
            if not hits:
                continue
            if ALLOW in line:
                allowed += 1
                continue
            for kind, text in hits:
                print(f"  {rel}:{lineno}: {text}  ({kind})")
                found += 1
    print(
        f"{found} leftover(s) in {len(files)} files scanned; "
        f"{allowed} line(s) allowed by '{ALLOW}'"
    )
    return found == 0


def main() -> int:
    results: dict[str, bool] = {}
    try:
        light, dark = parse_tokens(TOKENS)
        pairs = json.loads(PAIRS.read_text(encoding="utf-8"))
    except (OSError, ValueError, TokenError) as err:
        print(f"ERROR: {err}")
        results["contrast"] = results["names"] = False
    else:
        try:
            themes = {"light": light, "dark": {**light, **dark}}
            results["contrast"] = check_contrast(themes, pairs)
        except TokenError as err:
            print(f"ERROR: {err}")
            results["contrast"] = False
        try:
            results["names"] = check_names(set(light) | set(dark), pairs)
        except OSError as err:
            print(f"ERROR: {err}")
            results["names"] = False
    results["leftovers"] = check_leftovers()

    summary = ", ".join(
        f"{name} {'ok' if ok else 'FAILED'}" for name, ok in results.items()
    )
    print(f"\ncheck_theme: {summary}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
