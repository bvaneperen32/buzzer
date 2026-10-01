# T9 accessibility record

Evidence for T9's accessibility requirement: WCAG 2.1 AA contrast and visible focus in the light and
dark themes, across all three apps. The design and the rules being checked are in
[docs/plans/t9-ui-theming.md](../plans/t9-ui-theming.md) (§2.11 defines the four verification layers
below; §4.9 is the checklist). Screenshots: [before/](before/) and [after/](after/).

## Status

| Layer | What | Status |
|---|---|---|
| 1. Proven at design time | Spec Appendix A: 93 foreground/background pairs, all passing in both themes | Done |
| 2. Re-proven in the repo | `python scripts/check_theme.py` exits 0 | Done, 2026-09-30 ([§1](#1-automated-checks)) |
| 3. Audited in the browser | Lighthouse → Accessibility or axe DevTools, every screen, both themes | To do ([§3](#3-screen-checklist)) |
| 4. Keyboard and colour-only pass | Every screen, plus the states an audit can't see untriggered | To do ([§3](#3-screen-checklist), [§4](#4-states-triggered-by-hand)) |

**Done means** (spec §2.11): `check_theme.py` exits 0; the audits report zero contrast violations on
every screen in both themes, with every "needs review" item resolved by hand; the keyboard and
colour-only pass is complete with no failures.

**Before auditing, restart the dev servers.** Tailwind loads the compiled `tailwind.config.js`, so a
dev server started before the theme was wired in keeps generating CSS from the old palette. Every
token class (`bg-option-*`, `bg-surface`, `text-primary`, ...) is then missing and components render
without colour. Run `npm run build` once in each app (it regenerates the `.js`), then restart
`npm run dev`.

**How to fill in the tables:** ✅ pass, ❌ fail (add a note), n/a where a check doesn't apply. For an
audit, record the tool and anything it flagged, including "needs review" items and how you resolved
them. Disabled controls are exempt from contrast (WCAG 1.4.3), so an audit flag on a disabled
button or on the locked answer tiles is recorded as "exempt", not a failure.

---

## 1. Automated checks

Commands required before the merge request (spec §4.9), run 2026-09-30 on `feature/ui-look-and-feel`:

| Command | Result |
|---|---|
| `python scripts/check_theme.py` | exit 0: 93 pairs, 0 failing; 0 names missing; 0 leftovers in 92 files (re-run after T7's Sort It and Spot It were merged in and converted, spec §4.12; 81 files before) |
| `npm run build` in `frontend/host`, `frontend/player`, `frontend/admin` | all three succeed (runs `tsc -b`, the only check of the shared preset set-up) |
| `npx tsc --noEmit` in each app (what CI runs) | all three pass |
| `ruff check scripts/` and `ruff format --check scripts/` | pass (ruff 0.15.21, the version CI pins) |

The resolved token values match the spec's §2.5 tables exactly, and every ratio matches Appendix A
to ±0.01. T7's fixed canvas tokens (`--canvas-ink`, `--canvas-paper`, `--canvas-accepted`,
`--canvas-cursor`, `--heat-1` … `--heat-5`) match the values in spec §4.12. They have no Appendix A
rows, because what lies under them is an arbitrary image. The script doesn't print `--heat-1` …
`--heat-5` among the resolved tokens: its primitive pattern (`name-N`) matches them. They are still
parsed and resolved.

Two further one-off checks, beyond what `check_theme.py` covers (its leftover scan finds old classes,
not missing new ones):

- Every colour utility used in each app's source (118 distinct classes) is generated in that app's
  built CSS, so no class silently renders without colour. Nothing builds a colour class
  dynamically. After T7's conversion, the 42 class uses it introduced (host 22, player 20) were
  checked the same way: all generated.
- No inline `style` sets a colour, with one allowed exception (spec §4.12): the host Spot It
  heatmap legend's swatches reference the canvas tokens (`rgb(var(--heat-N) / 0.55)`,
  `rgb(var(--canvas-paper))`, `rgb(var(--canvas-ink))`) so they match the canvas exactly. No
  `rgb()`/`rgba()` colour literal remains in `frontend/*/src`; the canvases read tokens at draw
  time through `lib/canvasColors.ts`.
- Every native `<select>`, `<textarea>`, text `<input>` and checkbox carries token classes
  (`border-input` + `text-primary`, or `accent-primary`); file inputs style their button with
  `file:bg-hover`.

<details>
<summary>Full output of <code>python scripts/check_theme.py</code></summary>

```text
== 1. Contrast (WCAG 2.1) ==

Semantic tokens, resolved:
  token                 light     dark
  bg-canvas             #f7f9f8   #101211
  bg-surface            #fbfdfc   #171918
  bg-raised             #eef1f0   #202221
  bg-hover              #e6e9e8   #272a29
  bg-selected           #dfe2e0   #2e3130
  bg-accent             #046f61   #12a594
  bg-accent-hover       #076156   #0eb39e
  bg-accent-subtle      #e0f8f3   #0d2d2a
  bg-danger             #ce2c31   #ce2c31
  bg-danger-hover       #ae262d   #ae262d
  bg-danger-subtle      #feebec   #3b1219
  bg-success            #2a7e3b   #2a7e3b
  bg-success-hover      #276a34   #276a34
  bg-success-subtle     #e9f6e9   #1b2a1e
  bg-warning            #ffc53d   #ffc53d
  bg-warning-hover      #ffba18   #ffd60a
  bg-warning-subtle     #fff7c2   #302008
  text-primary          #1a211e   #eceeed
  text-secondary        #5f6563   #adb5b2
  text-disabled         #b8bcba   #5b625f
  text-accent           #046f61   #0bd8b6
  text-danger           #bc282f   #ff9592
  text-success          #287036   #71d083
  text-warning          #8e550b   #ffca16
  text-on-accent        #ffffff   #101211
  text-on-danger        #ffffff   #ffffff
  text-on-success       #ffffff   #ffffff
  text-on-warning       #1a211e   #101211
  border-subtle         #d7dad9   #373b39
  border-strong         #b8bcba   #5b625f
  border-input          #7c8481   #63706b
  ring-focus            #008573   #12a594
  bg-media              #fbfdfc   #fbfdfc
  text-on-media         #1a211e   #1a211e
  canvas-ink            #000000   #000000
  canvas-paper          #ffffff   #ffffff
  canvas-accepted       #22c55e   #22c55e
  canvas-cursor         #818cf8   #818cf8
  bg-meter-success      #287036   #71d083
  bg-meter-warning      #8e550b   #ffca16
  bg-meter-danger       #bc282f   #ff9592
  bg-meter-accent       #046f61   #0bd8b6
  bg-meter-neutral      #5f6563   #adb5b2
  bg-meter-track        #dfe2e0   #2e3130
  text-on-meter         #ffffff   #101211
  bg-option-1           #ec5d5e   #ec5d5e
  bg-option-2           #0090ff   #3b9eff
  bg-option-3           #ffe629   #ffe629
  bg-option-4           #46a758   #53b365
  bg-option-5           #a670d5   #a670d5
  bg-option-6           #f76b15   #ff801f
  bg-option-7           #de51a8   #de51a8
  bg-option-8           #12a594   #0eb39e
  text-on-option-1      #1a211e   #1a211e
  text-on-option-2      #1a211e   #1a211e
  text-on-option-3      #1a211e   #1a211e
  text-on-option-4      #1a211e   #1a211e
  text-on-option-5      #1a211e   #1a211e
  text-on-option-6      #1a211e   #1a211e
  text-on-option-7      #1a211e   #1a211e
  text-on-option-8      #1a211e   #1a211e

Pairs (93):
        fg                bg                 min   light   dark   hexes
  ok    text-primary      bg-canvas          4.5   15.51  16.14   light #1a211e/#f7f9f8  dark #eceeed/#101211
  ok    text-primary      bg-surface         4.5   16.06  15.16   light #1a211e/#fbfdfc  dark #eceeed/#171918
  ok    text-primary      bg-raised          4.5   14.43  13.73   light #1a211e/#eef1f0  dark #eceeed/#202221
  ok    text-primary      bg-hover           4.5   13.42  12.43   light #1a211e/#e6e9e8  dark #eceeed/#272a29
  ok    text-primary      bg-selected        4.5   12.57  11.27   light #1a211e/#dfe2e0  dark #eceeed/#2e3130
  ok    text-secondary    bg-canvas          4.5    5.63   8.98   light #5f6563/#f7f9f8  dark #adb5b2/#101211
  ok    text-secondary    bg-surface         4.5    5.83   8.44   light #5f6563/#fbfdfc  dark #adb5b2/#171918
  ok    text-secondary    bg-raised          4.5    5.24   7.64   light #5f6563/#eef1f0  dark #adb5b2/#202221
  ok    text-secondary    bg-hover           4.5    4.87   6.92   light #5f6563/#e6e9e8  dark #adb5b2/#272a29
  ok    text-secondary    bg-selected        4.5    4.56   6.28   light #5f6563/#dfe2e0  dark #adb5b2/#2e3130
  ok    text-accent       bg-canvas          4.5    5.75  10.31   light #046f61/#f7f9f8  dark #0bd8b6/#101211
  ok    text-accent       bg-surface         4.5    5.96   9.68   light #046f61/#fbfdfc  dark #0bd8b6/#171918
  ok    text-accent       bg-raised          4.5    5.35   8.77   light #046f61/#eef1f0  dark #0bd8b6/#202221
  ok    text-accent       bg-hover           4.5    4.98   7.94   light #046f61/#e6e9e8  dark #0bd8b6/#272a29
  ok    text-accent       bg-selected        4.5    4.66   7.20   light #046f61/#dfe2e0  dark #0bd8b6/#2e3130
  ok    text-danger       bg-canvas          4.5    5.70   8.92   light #bc282f/#f7f9f8  dark #ff9592/#101211
  ok    text-danger       bg-surface         4.5    5.90   8.38   light #bc282f/#fbfdfc  dark #ff9592/#171918
  ok    text-danger       bg-raised          4.5    5.30   7.59   light #bc282f/#eef1f0  dark #ff9592/#202221
  ok    text-danger       bg-hover           4.5    4.93   6.88   light #bc282f/#e6e9e8  dark #ff9592/#272a29
  ok    text-danger       bg-selected        4.5    4.62   6.24   light #bc282f/#dfe2e0  dark #ff9592/#2e3130
  ok    text-success      bg-canvas          4.5    5.74   9.90   light #287036/#f7f9f8  dark #71d083/#101211
  ok    text-success      bg-surface         4.5    5.94   9.30   light #287036/#fbfdfc  dark #71d083/#171918
  ok    text-success      bg-raised          4.5    5.34   8.42   light #287036/#eef1f0  dark #71d083/#202221
  ok    text-success      bg-hover           4.5    4.97   7.63   light #287036/#e6e9e8  dark #71d083/#272a29
  ok    text-success      bg-selected        4.5    4.65   6.92   light #287036/#dfe2e0  dark #71d083/#2e3130
  ok    text-warning      bg-canvas          4.5    5.75  12.28   light #8e550b/#f7f9f8  dark #ffca16/#101211
  ok    text-warning      bg-surface         4.5    5.95  11.54   light #8e550b/#fbfdfc  dark #ffca16/#171918
  ok    text-warning      bg-raised          4.5    5.35  10.45   light #8e550b/#eef1f0  dark #ffca16/#202221
  ok    text-warning      bg-hover           4.5    4.97   9.46   light #8e550b/#e6e9e8  dark #ffca16/#272a29
  ok    text-warning      bg-selected        4.5    4.66   8.58   light #8e550b/#dfe2e0  dark #ffca16/#2e3130
  ok    text-primary      bg-accent-subtle   4.5   14.76  12.64   light #1a211e/#e0f8f3  dark #eceeed/#0d2d2a
  ok    text-secondary    bg-accent-subtle   4.5    5.35   7.03   light #5f6563/#e0f8f3  dark #adb5b2/#0d2d2a
  ok    text-accent       bg-accent-subtle   4.5    5.47   8.07   light #046f61/#e0f8f3  dark #0bd8b6/#0d2d2a
  ok    text-primary      bg-danger-subtle   4.5   14.30  14.01   light #1a211e/#feebec  dark #eceeed/#3b1219
  ok    text-secondary    bg-danger-subtle   4.5    5.19   7.80   light #5f6563/#feebec  dark #adb5b2/#3b1219
  ok    text-danger       bg-danger-subtle   4.5    5.26   7.75   light #bc282f/#feebec  dark #ff9592/#3b1219
  ok    text-primary      bg-success-subtle  4.5   14.70  12.90   light #1a211e/#e9f6e9  dark #eceeed/#1b2a1e
  ok    text-secondary    bg-success-subtle  4.5    5.34   7.18   light #5f6563/#e9f6e9  dark #adb5b2/#1b2a1e
  ok    text-success      bg-success-subtle  4.5    5.44   7.91   light #287036/#e9f6e9  dark #71d083/#1b2a1e
  ok    text-primary      bg-warning-subtle  4.5   15.10  13.49   light #1a211e/#fff7c2  dark #eceeed/#302008
  ok    text-secondary    bg-warning-subtle  4.5    5.48   7.51   light #5f6563/#fff7c2  dark #adb5b2/#302008
  ok    text-warning      bg-warning-subtle  4.5    5.60  10.26   light #8e550b/#fff7c2  dark #ffca16/#302008
  ok    text-on-accent    bg-accent          4.5    6.08   6.12   light #ffffff/#046f61  dark #101211/#12a594
  ok    text-on-accent    bg-accent-hover    4.5    7.36   7.13   light #ffffff/#076156  dark #101211/#0eb39e
  ok    text-on-danger    bg-danger          4.5    5.21   5.21   light #ffffff/#ce2c31  dark #ffffff/#ce2c31
  ok    text-on-danger    bg-danger-hover    4.5    6.74   6.74   light #ffffff/#ae262d  dark #ffffff/#ae262d
  ok    text-on-success   bg-success         4.5    5.07   5.07   light #ffffff/#2a7e3b  dark #ffffff/#2a7e3b
  ok    text-on-success   bg-success-hover   4.5    6.57   6.57   light #ffffff/#276a34  dark #ffffff/#276a34
  ok    text-on-warning   bg-warning         4.5   10.39  11.92   light #1a211e/#ffc53d  dark #101211/#ffc53d
  ok    text-on-warning   bg-warning-hover   4.5    9.60  13.32   light #1a211e/#ffba18  dark #101211/#ffd60a
  ok    text-on-media     bg-media           4.5   16.06  16.06   light #1a211e/#fbfdfc  dark #1a211e/#fbfdfc
  ok    text-on-option-1  bg-option-1        4.5    4.92   4.92   light #1a211e/#ec5d5e  dark #1a211e/#ec5d5e
  ok    text-on-option-2  bg-option-2        4.5    5.03   5.87   light #1a211e/#0090ff  dark #1a211e/#3b9eff
  ok    text-on-option-3  bg-option-3        4.5   12.97  12.97   light #1a211e/#ffe629  dark #1a211e/#ffe629
  ok    text-on-option-4  bg-option-4        4.5    5.41   6.25   light #1a211e/#46a758  dark #1a211e/#53b365
  ok    text-on-option-5  bg-option-5        4.5    4.61   4.61   light #1a211e/#a670d5  dark #1a211e/#a670d5
  ok    text-on-option-6  bg-option-6        4.5    5.52   6.53   light #1a211e/#f76b15  dark #1a211e/#ff801f
  ok    text-on-option-7  bg-option-7        4.5    4.57   4.57   light #1a211e/#de51a8  dark #1a211e/#de51a8
  ok    text-on-option-8  bg-option-8        4.5    5.34   6.22   light #1a211e/#12a594  dark #1a211e/#0eb39e
  ok    ring-focus        bg-canvas          3      4.32   6.12   light #008573/#f7f9f8  dark #12a594/#101211
  ok    ring-focus        bg-surface         3      4.47   5.75   light #008573/#fbfdfc  dark #12a594/#171918
  ok    ring-focus        bg-raised          3      4.01   5.21   light #008573/#eef1f0  dark #12a594/#202221
  ok    ring-focus        bg-hover           3      3.73   4.72   light #008573/#e6e9e8  dark #12a594/#272a29
  ok    ring-focus        bg-selected        3      3.50   4.28   light #008573/#dfe2e0  dark #12a594/#2e3130
  ok    ring-focus        bg-accent-subtle   3      4.10   4.79   light #008573/#e0f8f3  dark #12a594/#0d2d2a
  ok    ring-focus        bg-danger-subtle   3      3.98   5.31   light #008573/#feebec  dark #12a594/#3b1219
  ok    ring-focus        bg-success-subtle  3      4.09   4.89   light #008573/#e9f6e9  dark #12a594/#1b2a1e
  ok    ring-focus        bg-warning-subtle  3      4.20   5.12   light #008573/#fff7c2  dark #12a594/#302008
  ok    border-input      bg-canvas          3      3.63   3.64   light #7c8481/#f7f9f8  dark #63706b/#101211
  ok    border-input      bg-surface         3      3.76   3.42   light #7c8481/#fbfdfc  dark #63706b/#171918
  ok    border-input      bg-raised          3      3.37   3.09   light #7c8481/#eef1f0  dark #63706b/#202221
  ok    bg-accent         bg-canvas          3      5.75   6.12   light #046f61/#f7f9f8  dark #12a594/#101211
  ok    bg-surface        bg-accent          3      5.96   5.75   light #fbfdfc/#046f61  dark #171918/#12a594
  ok    bg-meter-success  bg-meter-track     3      4.65   6.92   light #287036/#dfe2e0  dark #71d083/#2e3130
  ok    bg-meter-success  bg-surface         3      5.94   9.30   light #287036/#fbfdfc  dark #71d083/#171918
  ok    bg-meter-success  bg-canvas          3      5.74   9.90   light #287036/#f7f9f8  dark #71d083/#101211
  ok    text-on-meter     bg-meter-success   4.5    6.07   9.90   light #ffffff/#287036  dark #101211/#71d083
  ok    bg-meter-warning  bg-meter-track     3      4.66   8.58   light #8e550b/#dfe2e0  dark #ffca16/#2e3130
  ok    bg-meter-warning  bg-surface         3      5.95  11.54   light #8e550b/#fbfdfc  dark #ffca16/#171918
  ok    bg-meter-warning  bg-canvas          3      5.75  12.28   light #8e550b/#f7f9f8  dark #ffca16/#101211
  ok    text-on-meter     bg-meter-warning   4.5    6.08  12.28   light #ffffff/#8e550b  dark #101211/#ffca16
  ok    bg-meter-danger   bg-meter-track     3      4.62   6.24   light #bc282f/#dfe2e0  dark #ff9592/#2e3130
  ok    bg-meter-danger   bg-surface         3      5.90   8.38   light #bc282f/#fbfdfc  dark #ff9592/#171918
  ok    bg-meter-danger   bg-canvas          3      5.70   8.92   light #bc282f/#f7f9f8  dark #ff9592/#101211
  ok    text-on-meter     bg-meter-danger    4.5    6.03   8.92   light #ffffff/#bc282f  dark #101211/#ff9592
  ok    bg-meter-accent   bg-meter-track     3      4.66   7.20   light #046f61/#dfe2e0  dark #0bd8b6/#2e3130
  ok    bg-meter-accent   bg-surface         3      5.96   9.68   light #046f61/#fbfdfc  dark #0bd8b6/#171918
  ok    bg-meter-accent   bg-canvas          3      5.75  10.31   light #046f61/#f7f9f8  dark #0bd8b6/#101211
  ok    text-on-meter     bg-meter-accent    4.5    6.08  10.31   light #ffffff/#046f61  dark #101211/#0bd8b6
  ok    bg-meter-neutral  bg-meter-track     3      4.56   6.28   light #5f6563/#dfe2e0  dark #adb5b2/#2e3130
  ok    bg-meter-neutral  bg-surface         3      5.83   8.44   light #5f6563/#fbfdfc  dark #adb5b2/#171918
  ok    bg-meter-neutral  bg-canvas          3      5.63   8.98   light #5f6563/#f7f9f8  dark #adb5b2/#101211
  ok    text-on-meter     bg-meter-neutral   4.5    5.95   8.98   light #ffffff/#5f6563  dark #101211/#adb5b2

93 pairs, 0 failing

== 2. Names ==
  preset.js: 57 token names checked
  base.css: 3 token names checked
  contrast-pairs.json: 54 token names checked
0 missing

== 3. Leftovers ==
0 leftover(s) in 92 files scanned; 0 line(s) allowed by 'theme-check: allow'

check_theme: contrast ok, names ok, leftovers ok
```

</details>

---

## 2. Theme behaviour

Each app in dev, and once under nginx (`:8080`) after `npm run build` (spec §4.9).

| Check | host | player | admin |
|---|---|---|---|
| Site data cleared, OS (or DevTools → Rendering → "Emulate prefers-color-scheme") dark: first load is dark; light: first load is light | | | |
| No stored choice: changing the emulated OS scheme switches the open page live | | | |
| Toggle, reload: the choice persists; after toggling, OS changes no longer switch it | | | |
| No flash of the other theme on reload (Network throttling "Slow 4G") | | | |
| Two tabs of the same app: toggling in one updates the other | | | |
| Storage blocked (private window, or DevTools → Application → block storage): loads in the OS theme, and the toggle still switches for the session | | | |
| The toggle is present and covers nothing on every screen in §3 | | | |

Under nginx (one origin), toggling in admin does **not** change host or player (per-app keys): ☐

---

## 3. Screen checklist

For every screen, in light **and** dark: the audit, a keyboard pass (Tab through every control: the
outline is visible and not clipped by an `overflow-hidden` parent; the toggle is reachable), a
colour-only check (correct/incorrect, selected options, the timer and the switch carry a non-colour
cue), and toggle clearance (the 56×56px top-right corner is empty).

### Host

| Screen | Audit (light) | Audit (dark) | Keyboard | Colour-only | Toggle clear | Notes |
|---|---|---|---|---|---|---|
| Login | | | | | | |
| Play (`HomePage`), with an active session listed | | | | | | |
| Games list | | | | | | |
| Question editor, multiple choice (with the image picker open) | | | | | | |
| Question editor, true/false | | | | | | |
| Question editor, fill in the blank | | | | | | |
| Question editor, multi-select | | | | | | |
| Question editor, image library panel (meters, live-session banner) | | | | | | |
| Rosters | | | | | | |
| Roster detail, including the CSV wizard | | | | | | |
| Sessions, with the export panel open | | | | | | |
| Game lobby, auto-advance off | | | | | | |
| Game lobby, auto-advance on | | | | | | |
| Question, multiple choice | | | | | | |
| Question, true/false | | | | | | |
| Question, fill in the blank | | | | | | |
| Question, multi-select | | | | | | |
| Question with a prompt image | | | | | | |
| Question with image options | | | | | | |
| Question, locked | | | | | | |
| Results, multiple choice | | | | | | |
| Results, true/false | | | | | | |
| Results, multi-select | | | | | | |
| Results, fill in the blank (word cloud) | | | | | | |
| Game over (answer bars, histogram) | | | | | | |
| Game error screen | | | | | | |
| **T7 (spec §4.12)** | | | | | | |
| Question editor, Sort It (boxes, cards, answer-key selects, "Include an answer key") | | | | | | |
| Question editor, Spot It (accepted cells, keyboard cursor, "Needs review" banner, cells outside the grid) | | | | | | |
| Question editor, Spot It with a missing image (placeholder and Retry on the canvas) | | | | | | |
| Question, Sort It (card chips, box tiles) | | | | | | |
| Question, Spot It (image under the grid) | | | | | | |
| Results, Sort It accuracy (table and % correct bars) | | | | | | |
| Results, Sort It completeness | | | | | | |
| Results, Spot It (heatmap, counts, legend, Correct outlines) | | | | | | |
| Game over, Sort It and Spot It cards | | | | | | |

### Player

| Screen | Audit (light) | Audit (dark) | Keyboard | Colour-only | Toggle clear | Notes |
|---|---|---|---|---|---|---|
| Join | | | | | | |
| Name, guest mode | | | | | | |
| Name, NetID mode | | | | | | |
| Name, local mode | | | | | | |
| Name, signed in ("Use a different account") | | | | | | |
| Lobby, waiting for host to start | | | | | | |
| Lobby, waiting for next question | | | | | | |
| Question, multiple choice text tiles | | | | | | |
| Question, multiple choice image tiles | | | | | | |
| Question, true/false | | | | | | |
| Question, fill in the blank | | | | | | |
| Question, multi-select with some options selected | | | | | | |
| Feedback | | | | | | |
| Results, correct | | | | | | |
| Results, incorrect | | | | | | |
| Game over | | | | | | |
| "Host disconnected" banner | | | | | | |
| Error screen | | | | | | |
| **T7 (spec §4.12)** | | | | | | |
| Sort It: a selected card, a box ready to receive it, "Return to the pool" | | | | | | |
| Sort It: dragging (the ghost card) | | | | | | |
| Sort It: the "cards are left out" dialog | | | | | | |
| Sort It: Submit, enabled and disabled | | | | | | |
| Spot It: a selected cell, nudge buttons, Lock in | | | | | | |
| Results, T7: full (green) | | | | | | |
| Results, T7: partial (warning) | | | | | | |
| Results, T7: none (red) | | | | | | |
| Results, T7: recorded (accent) | | | | | | |
| Results, T7: missing ("No answer") | | | | | | |
| Game over, Sort It and Spot It rows | | | | | | |

### Admin

| Screen | Audit (light) | Audit (dark) | Keyboard | Colour-only | Toggle clear | Notes |
|---|---|---|---|---|---|---|
| Login | | | | | | |
| Non-admin notice | | | | | | |
| Users | | | | | | |
| User detail (course and game grants) | | | | | | |
| Courses | | | | | | |
| Guests | | | | | | |

---

## 4. States triggered by hand

An audit only sees these if they're on screen, so each is checked on purpose, in both themes.

| State | Where | Light | Dark | Notes |
|---|---|---|---|---|
| Hover on buttons, list rows, nav items | host, admin | | | |
| Disabled buttons | all | | | Exempt from contrast |
| Form validation errors | all | | | |
| Locked-question button ("Unlock Question") | host Question | | | |
| Image "unavailable" placeholder (compact and full) | host editor and game screens, player tiles | | | |

### Specific checks from the Goldfish review (spec §4.9)

| Check | Light | Dark | Notes |
|---|---|---|---|
| A wide and a tall image as player option tiles: the picture is shrink-wrapped, the bands are the themed `bg-raised` (never light bars in dark) | | | |
| The same on host game-screen option images | | | |
| A transparent black-line PNG is visible in dark (player tile and host screen) | | | |
| Host Results, large room, one answer on an option: the count in the 4% bar is inside the fill or clipped, never on the track | | | |
| Management pages at a ~1280px-wide window: header buttons clear the toggle | | | |
| Player multiple-choice and multi-select at ~375px wide: the timer clears the toggle | | | |
| Option tiles and true/false buttons: no grey ring around the coloured fill | | | |

### T7 checks (spec §4.12)

| Check | Light | Dark | Notes |
|---|---|---|---|
| Toggling the theme with a Spot It canvas open (host question, results and editor; player) redraws it straight away, with no reload | | | |
| The grid and heatmap read over a light image and over a dark image | | | |
| The phone's grid (no image) is legible in light | | | |
| The canvas focus outline (host editor, player grid) is visible and not clipped | | | |
| Player Sort It and Spot It at ~375px wide: the timer clears the toggle (`pt-16`) | | | |

---

## 5. Found during implementation, to confirm in layer 4

Noted while converting the pages. They aren't colour changes, so they were left for the keyboard
pass to confirm rather than fixed without a decision.

- **Admin user detail, HOST/PLAYER role buttons** (course grant panel): they sit inside a
  `rounded overflow-hidden` container, so the 2px-offset focus outline will likely be clipped.
- **Host image library, a failed thumbnail's Retry** (`SafeImage` with `compact` and `showRetry`):
  the Retry button fills an `overflow-hidden` box (`absolute inset-0`), so its outline, drawn
  outside the button, will likely be clipped entirely.
- **Player game over**: the "Game Over!" heading is centred about 32px from the top; confirm it
  clears the toggle on a 360px-wide phone.
- **Colour-only, host word cloud**: correct words are marked by colour alone, but the "Correct
  answer:" line above the cloud names them in words.
- **Colour-only, player results**: "Correct!" / "Incorrect" is a word with no ✓/✗ symbol; spec
  §2.10 describes both.

From T7's conversion (spec §4.12):

- **Player Sort It, an unselected card inside a box ready to receive it**: the card's
  `border-input` edge on the box's `bg-accent-subtle` measures 3.45 light / **2.85 dark**, and the
  card's `bg-raised` fill barely differs from the box (1.02 / 1.09). This is allowed: the card is
  a button identified by its text (spec §2.5 exempts buttons). Confirm the card still reads as a
  card in dark.
- **Colour-only, player T7 results**: each tone's colour is backed by words: "8 of 8 correct",
  "You picked B3 — Incorrect", "Answer recorded", "No answer", and the points line. A Sort It
  answer can show "4 of 8 correct" in the "none" colour when wrong cards cancel the right ones (4
  wrong, two boxes: 4 − 4 = 0 points). The points line ("0 of 1,000 points") says so.
- **Host Spot It editor, "Accepted cells checked…" checkbox**: its label is `text-disabled` while
  the grid doesn't fit the image. The checkbox is disabled then, so this is exempt, and the reason
  beneath it stays `text-warning`.

---

## 6. After screenshots

`docs/ui/after/<app>-<screen>-<light|dark>.png`: one light and one dark shot for every before file,
plus three key screens that have no before shot (spec §2.12).

| Screen | Light | Dark |
|---|---|---|
| `host-play` | ☐ | ☐ |
| `host-games` | ☐ | ☐ |
| `host-rosters` | ☐ | ☐ |
| `host-sessions` | ☐ | ☐ |
| `host-lobby` | ☐ | ☐ |
| `host-question` | ☐ | ☐ |
| `host-results` | ☐ | ☐ |
| `host-gameover` | ☐ | ☐ |
| `host-question-editor` (no before shot) | ☐ | ☐ |
| `player-join` | ☐ | ☐ |
| `player-question-mc` | ☐ | ☐ |
| `player-results` (no before shot) | ☐ | ☐ |
| `admin-login` | ☐ | ☐ |
| `admin-users` | ☐ | ☐ |
| `admin-courses` | ☐ | ☐ |
| `admin-guests` | ☐ | ☐ |
| `admin-user-detail` (no before shot) | ☐ | ☐ |

---

## 7. Known limitations

From spec §5, noted and not fixed:

- **Player pinch-zoom is blocked** (`user-scalable=no` in `frontend/player/index.html`). This fails
  WCAG 2.1 1.4.4 Resize Text (AA). It's a documented deliberate choice (it prevents accidental zoom
  while tapping fast) and outside T9's stated accessibility items.
- **Older Safari draws the focus outline as a rectangle** on rounded elements (the outline follows
  `border-radius` from Safari 16.4). Cosmetic; the outline is still visible.
- **Automated audits can't judge text over images or canvas content**; the manual checks above
  cover them.
- **The contrast proof covers listed pairs only.** A page that combines tokens outside Appendix A is
  caught only by the browser audit (layer 3).
- **The downloadable HTML session report** keeps its own styling; it's a file, not an app page.
- **Contained images are no longer stretched up.** An original smaller than its box shows at its
  natural size with wider themed bands. Phones get the 400px small copy, larger than any tile, so in
  practice this affects only tiny uploads on the projector.
