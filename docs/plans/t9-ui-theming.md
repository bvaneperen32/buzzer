# T9 — UI Look-and-Feel & Theming

**Destination:** `docs/plans/t9-ui-theming.md`
**Status:** **Design agreed 2026-09-29; Goldfish-reviewed 2026-09-29 and revised (§6).**
**Implemented 2026-09-30** on `feature/ui-look-and-feel` (§4.8 steps 1–6 and 9). Steps 7–8, the
browser audits and after screenshots, are pending and done by hand (`docs/ui/accessibility.md`).
Changes made during implementation: §4.7 (ImagePicker selection is a border), and §2.5 (every
answer-option tile carries dark text, revised 2026-09-30 after review in the browser).
**T7 (Sort It, Spot It) merged to `main` after this work; merged in and themed 2026-09-30 (§4.12
steps 1–7 and 9). Its screens are in the accessibility record, awaiting the by-hand checks.**

Commit before any implementation (T3). If implementation reveals a flaw, revise this document first.
**Read first:** `frontend/CLAUDE.md`, then the three app-level files (`frontend/host/CLAUDE.md`,
`frontend/player/CLAUDE.md`, `frontend/admin/CLAUDE.md`) and each app's `src/components/CLAUDE.md`.

**Course references.** Task ids come from `instructions.md`:
- **T2** — CI must stay green; the pipeline may not be modified to pass artificially
- **T3** — design before code (this document and its Goldfish review)
- **T7** — two new question types (built by a teammate, not yet on `main`)
- **T8** — image support (on `main`)
- **T9** — this task: semantic tokens, light and dark themes, toggle, WCAG 2.1 AA, evidence
- **T10** — each member's individual document, which links the screenshots and accessibility record

---

## 1. Problem

All three frontend apps (host, player, admin) hardcode one dark palette. The body background is
`#0f172a` in each app's `index.css`, and about **800 raw Tailwind colour classes** (`bg-slate-800`,
`text-indigo-400`, `bg-red-500`, …) are spread across **43 files**. There is no theme layer, no
light mode, and the colours have drifted: text alone uses seven shades of slate.

T9 requires:

| Requirement | What it means here |
|---|---|
| Design tokens | A semantic colour layer (CSS variables mapped into each app's Tailwind theme). Components use tokens; raw palette classes are gone everywhere outside the token definitions. |
| Light and dark themes | Both themes cover every page of all three apps, with no leftover hardcoded colours. |
| Toggle and persistence | A visible toggle in each app. First visit follows the OS (`prefers-color-scheme`); the choice survives reloads. |
| Accessibility | Text meets WCAG 2.1 AA contrast (4.5:1 for body text) in both themes; interactive elements have visible focus. |
| Context fit | Player stays thumb-friendly; the host screen is readable from the back of a classroom; admin stays dense but scannable. |
| Evidence | Before/after screenshots of key screens, both themes, in `docs/ui/`, referenced from each member's T10 document. |

**Design goal agreed with the user:** the two themes must be clearly distinct from each other *and*
from today's look. Light is not bright white and dark is not pure black, but neither sits in the
current navy middle ground. The new identity is a **teal** accent over a faintly green **sage** gray.

### Constraints from the existing system

- **Three independent Vite apps**, no shared package or workspace (`frontend/CLAUDE.md`). Each has its
  own `package.json`, `node_modules`, `tailwind.config.ts`, and `tsconfig`.
- **Stack stays as is:** React 18.3, Tailwind 3.4, Vite 5. No upgrade is part of T9.
- **CI** (`.gitlab-ci.yml`, job `frontend-typecheck`) runs `npx tsc --noEmit` inside each app. That
  checks only `src/` — it never checks `tailwind.config.ts` and never looks at CSS. The local
  `npm run build` runs `tsc -b`, which *does* check the config through `tsconfig.node.json`.
- `tsconfig.node.json` in each app is `"composite": true` with
  `"include": ["vite.config.ts", "tailwind.config.ts"]`.
- **The `.js`-before-`.ts` trap** (`frontend/CLAUDE.md`): `tsc -b` emits gitignored
  `tailwind.config.js` / `vite.config.js` beside the `.ts` sources, and Tailwind and Vite load the
  `.js` first. An edit to `tailwind.config.ts` has no effect in `npm run dev` until the stale `.js`
  is deleted or regenerated.
- **nginx sends no Content-Security-Policy header**, so an inline `<script>` in `index.html` runs.
- **Production shares one origin** (`:8080` serves `/host/`, `/player/`, `/admin/`), so the apps
  share `localStorage`. In dev each port is a separate origin.
- **Player `Button`/`Card`/`Input` are deliberately touch-tuned** (bigger padding, `active:scale-95`,
  16px inputs). Host and admin copies are byte-identical to each other. T9 changes colours only and
  keeps these differences.
- **Fixed-position elements today:** the host game screens' join panel (`fixed bottom-4 right-4`, in
  `host/src/pages/game/GameLayout.tsx`), and the player's "Host disconnected" banner
  (`fixed top-0 inset-x-0 … z-50`, in `player/src/pages/game/GameLayout.tsx`). The host and admin
  shells have a `sticky` 224px sidebar on the left. There are no modals.
- **"Before" screenshots already exist** in `docs/ui/before/` (14 files, taken from `main` before any
  T9 change, named per §2.12).

### Not in scope

- **T7's screens.** T7 is not on `main`. This spec covers the screens on `main` today. §4.10 records
  how screens added later adopt the token system; merging or rebasing T7 is not part of this work.
- **The downloadable HTML session summary** (generated by the backend's `report_service`). It is a
  file, not an app page, and keeps its own styles.
- **Pinch-zoom on the player** (`user-scalable=no`). It fails WCAG 1.4.4 Resize Text, but it is a
  documented deliberate choice and not one of T9's stated accessibility items (§5).
- Any backend change, any new component library, any Tailwind/React upgrade.
- Answer-option colours on the projector. Only the phone colours its option tiles; the host keeps
  its current uncoloured option labels.

---

## 2. Technical Plan

### 2.1 Overview

1. **One shared theme folder**, `frontend/shared/theme/`, is imported by all three apps by relative
   path. It holds the token values (`tokens.css`), global base rules (`base.css`), the Tailwind
   name mapping (`preset.js` + `preset.d.ts`), the framework-free theme logic (`theme.ts`), and the
   contrast pair list (`contrast-pairs.json`).
2. **Two token layers.** Radix Colors 3.0.0 scales are copied in verbatim as the primitive layer;
   semantic tokens (`--bg-surface`, `--text-primary`, …) point at primitives, with a small number of
   documented overrides where a Radix step fails WCAG.
3. **Per-property Tailwind names.** The shared preset replaces Tailwind's palette entirely and maps
   semantic tokens per utility, so classes read `bg-surface`, `text-primary`, `border-input`.
   Raw palette classes (`bg-slate-800`) no longer exist.
4. **Theme switching** sets `data-theme="light|dark"` on `<html>`: an inline script in each
   `index.html` sets it before first paint; a fixed top-right toggle in each app changes and stores
   it, using one `localStorage` key per app.
5. **Focus visibility** comes from one global `:focus-visible` outline in `base.css`, not per-component
   ring classes.
6. **Verification:** contrast is proven in this spec (Appendix A, 93 pairs, all passing), re-proven
   by `scripts/check_theme.py` against the real `tokens.css`, and confirmed per screen in the browser
   (Lighthouse / axe DevTools + a keyboard pass), recorded in `docs/ui/accessibility.md`.

### 2.2 Terms

- **Primitive** — a Radix Colors value, e.g. `--sage-3`, `--teal-11`. Never used by components.
- **Semantic token** — a role-named CSS variable, e.g. `--bg-surface`, `--text-secondary`. The only
  thing Tailwind exposes.
- **Override** — a semantic token whose value is not a Radix step, because the Radix step fails
  WCAG 2.1. Each is a mix between Radix steps 11 and 12 of the same hue, listed in §2.5.
- **Channel triple** — a colour written as three space-separated 0–255 integers, e.g. `251 253 252`.
  Every token stores this form so Tailwind's opacity modifier works.
- **Radix step roles** — Radix numbers each scale 1–12: 1–2 app backgrounds, 3–5 component
  backgrounds, 6–8 borders, 9–10 solid fills, 11–12 text. Radix's own contrast claims use APCA,
  **not** WCAG 2.1, which is why this spec measures WCAG ratios itself.

### 2.3 Sharing across apps — verified facts

A throwaway copy of the host app was built in a scratch folder on 2026-09-29 (Vite 5.4.21,
TypeScript from the app's own `node_modules`) to decide how files can be shared. Results:

| Shared file set-up | `tsc -b` (local build) | `tsc --noEmit` (CI) | Outcome |
|---|---|---|---|
| `preset.ts` imported by `tailwind.config.ts` | ❌ TS6307 "not listed within the file list of project" | ✅ | Rejected |
| `preset.ts` added to each `tsconfig.node.json` `include` | ✅ | ✅ | Rejected: `tsc -b` emits `preset.js` + `preset.d.ts` into `shared/` (not gitignored; stale-`.js` trap) |
| `preset.js`, no type file | ❌ TS7016 "Could not find a declaration file" | ✅ | Rejected |
| **`preset.js` + hand-written `preset.d.ts`** | ✅ | ✅ | **Chosen.** No files emitted into `shared/` |

Also verified with the chosen set-up:
- `@import '../../shared/theme/tokens.css';` at the top of an app's `src/index.css` is inlined into
  the production CSS (both theme blocks present), and the Vite **dev** server serves it and
  hot-reloads on edit.
- `bg-surface/50` builds to `background-color: rgb(var(--<token>) / .5)`: the opacity modifier
  works on channel-triple tokens.
- With the palette replaced, no `slate-*` utilities are generated.
- A preset shaped like the real one — `colors` reduced to `transparent`/`current`/`inherit`, plus
  `backgroundColor`, `textColor`, `borderColor` (with `DEFAULT`), `ringColor`, `ringOffsetColor` —
  type-checks with `presets: [preset]` inside `satisfies Config` when `preset.d.ts` declares
  `{ theme: Record<string, Record<string, string>> }`. Preflight's default border colour becomes
  `var(--border-subtle)`.
- A shared `theme.ts` with **no imports**, imported from an app's `src/main.tsx` by relative path,
  passes `tsc --noEmit`, `tsc -b`, and `vite build` (the module is in the bundle), and the dev server
  serves it (HTTP 200) **without** any `server.fs.allow` change.

**Two hard rules follow:**
1. **No file in `frontend/shared/` may import a package** (not even `tailwindcss` types, not `react`).
   There is no `node_modules` above `shared/`, so the import cannot resolve. That is why
   `preset.d.ts` is hand-written and why the React toggle lives in each app.
2. **CI does not protect the shared set-up.** A wrong variant passes CI and fails only `npm run
   build`. Running `npm run build` in all three apps is a required pre-merge step (§4.9).

### 2.4 Token architecture

**Where values live.** `frontend/shared/theme/tokens.css` is the only file that contains colour
values. Nothing else — no component, page, config or other CSS file — contains a hex code or
palette class (§4.3's leftover scan enforces this for every file it lists: app source, each app's
`index.css`, `index.html` and `tailwind.config.ts`, and the other `shared/theme/` files).

**Format of `tokens.css` (strict, because `scripts/check_theme.py` parses it):**
- Exactly two rule blocks, in this order: `:root { … }` (light values, also the fallback) and
  `:root[data-theme="dark"] { … }` (only what differs in dark).
- Comments are `/* … */` and may appear anywhere, inside or outside a block, and may span several
  lines; the parser strips every comment before reading anything else.
- Inside a block: comments, blank lines, and declarations of the form `--name: value;`, **one per
  line**. `value` is either a channel triple (`251 253 252`) or `var(--other-name)`. A trailing
  `/* … */` comment on the same line is allowed and expected (see below). No other properties
  (`color-scheme` lives in `base.css`).
- **Primitives:** every step 1–12 of the scales `sage`, `teal`, `red`, `grass`, `amber`, `blue`,
  `yellow`, `purple`, `orange`, `pink`, copied verbatim from the npm package
  `@radix-ui/colors@3.0.0` (files `<scale>.css` for light and `<scale>-dark.css` for dark), converted
  to channel triples, each line ending with the original hex as a comment, e.g.
  `--sage-1: 251 253 252; /* #fbfdfc */`. Light values go in `:root`; the dark scale values redefine
  the same names in the dark block.
- **Semantic tokens** follow the primitives in each block. A semantic token is `var(--primitive)`
  when it is a Radix step *in the current theme*, and a literal channel triple with a source comment
  otherwise — an override, white, or a light-scale value used in dark (e.g. dark `--bg-danger` uses
  the **light** `red-11`, which cannot be written as `var(--red-11)` because the dark block
  redefines `--red-11`). Tokens whose definition is the same in both themes are declared only in
  `:root`.

**Why channel triples:** 39 existing usages apply Tailwind opacity modifiers (`bg-slate-800/50`).
Tailwind can only apply `/50` to a colour of the form `rgb(var(--x) / <alpha-value>)`, which needs
`--x` to be channels. Hex values would silently drop the transparency.

**How a class resolves:** `bg-surface` → `background-color: rgb(var(--bg-surface) / 1)` →
`--bg-surface` → `var(--sage-1)` → light `251 253 252`, or in dark (where the block redefines
`--bg-surface: var(--sage-2)` and `--sage-2`) `23 25 24`. All variables are declared on `<html>`,
so references resolve against the active theme.

### 2.5 Token catalogue

Resolved values below are the contract; the check script prints the same values. "override" rows
are the only non-Radix values; each is the lightest mix from step 11 toward step 12 that reaches
≥4.6:1 on `bg-selected` (the darkest light-theme background), or — for the two `-hover` fills — a
fixed 30%/50% mix chosen to look like a pressed state while keeping white text ≥6.5:1.

**Neutral backgrounds** — pages, cards, inputs, interaction states:

| Token | Light | Dark | Use for |
|---|---|---|---|
| `--bg-canvas` | `#f7f9f8` sage-2 | `#101211` sageDark-1 | Page background (`html`/`body`), game screens |
| `--bg-surface` | `#fbfdfc` sage-1 | `#171918` sageDark-2 | Cards, sidebars, panels, toggle button, switch knob |
| `--bg-raised` | `#eef1f0` sage-3 | `#202221` sageDark-3 | Inputs, nested panels, table header rows, unselected answer rows |
| `--bg-hover` | `#e6e9e8` sage-4 | `#272a29` sageDark-4 | Hover state of rows, ghost/outline buttons, nav items |
| `--bg-selected` | `#dfe2e0` sage-5 | `#2e3130` sageDark-5 | Current nav item, selected row |

In light, cards (`surface`) are lighter than the page (`canvas`); in dark they are raised above it.
That is deliberate: each theme has its own depth model.

**Accent and status fills** — solid buttons, badges, tinted boxes:

| Token | Light | Dark |
|---|---|---|
| `--bg-accent` | `#046f61` override teal-11→12 30% | `#12a594` tealDark-9 |
| `--bg-accent-hover` | `#076156` override teal-11→12 50% | `#0eb39e` tealDark-10 |
| `--bg-accent-subtle` | `#e0f8f3` teal-3 | `#0d2d2a` tealDark-3 |
| `--bg-danger` | `#ce2c31` red-11 | `#ce2c31` light red-11 |
| `--bg-danger-hover` | `#ae262d` override red-11→12 30% | same as light |
| `--bg-danger-subtle` | `#feebec` red-3 | `#3b1219` redDark-3 |
| `--bg-success` | `#2a7e3b` grass-11 | `#2a7e3b` light grass-11 |
| `--bg-success-hover` | `#276a34` override grass-11→12 30% | same as light |
| `--bg-success-subtle` | `#e9f6e9` grass-3 | `#1b2a1e` grassDark-3 |
| `--bg-warning` | `#ffc53d` amber-9 | `#ffc53d` amberDark-9 |
| `--bg-warning-hover` | `#ffba18` amber-10 | `#ffd60a` amberDark-10 |
| `--bg-warning-subtle` | `#fff7c2` amber-3 | `#302008` amberDark-3 |

**Text:**

| Token | Light | Dark | Use for |
|---|---|---|---|
| `--text-primary` | `#1a211e` sage-12 | `#eceeed` sageDark-12 | Body text, headings, all key content on the projector |
| `--text-secondary` | `#5f6563` sage-11 | `#adb5b2` sageDark-11 | Labels, hints, metadata, placeholders |
| `--text-disabled` | `#b8bcba` sage-8 | `#5b625f` sageDark-8 | Disabled controls **only** (WCAG exempts them; never for readable content) |
| `--text-accent` | `#046f61` override | `#0bd8b6` tealDark-11 | Links, accent labels |
| `--text-danger` | `#bc282f` override red-11→12 17% | `#ff9592` redDark-11 | Errors, "Incorrect" |
| `--text-success` | `#287036` override grass-11→12 21% | `#71d083` grassDark-11 | "Correct", success notes |
| `--text-warning` | `#8e550b` override amber-11→12 31% | `#ffca16` amberDark-11 | Warnings |
| `--text-on-accent` | `#ffffff` | `#101211` sageDark-1 | Text on `bg-accent` / `bg-accent-hover` |
| `--text-on-danger` | `#ffffff` | `#ffffff` | Text on `bg-danger*` |
| `--text-on-success` | `#ffffff` | `#ffffff` | Text on `bg-success*` |
| `--text-on-warning` | `#1a211e` sage-12 | `#101211` sageDark-1 | Text on `bg-warning*` |

Only two readable text levels exist (`primary`, `secondary`). Today's seven slate shades collapse
into them. **Today's `text-slate-500`/`600` already fail AA** (≈3.1:1 on `slate-800`); mapping them
to `text-secondary` makes that text visibly brighter, which is intended.

**Borders and focus:**

| Token | Light | Dark | Use for |
|---|---|---|---|
| `--border-subtle` | `#d7dad9` sage-6 | `#373b39` sageDark-6 | Card edges, dividers, sidebar edge (decorative; also Tailwind's default border colour) |
| `--border-strong` | `#b8bcba` sage-8 | `#5b625f` sageDark-8 | Stronger decorative separation |
| `--border-input` | `#7c8481` sage-10 | `#63706b` sageDark-9 | The boundary of every input, select, textarea, checkbox, unselected choice box (≥3:1, WCAG 1.4.11) |
| `--ring-focus` | `#008573` teal-11 | `#12a594` tealDark-9 | The global focus outline (§2.8) |

Status and accent borders reuse the matching text colour (`border-accent` → `--text-accent`, etc.),
which is ≥4.5:1 on every neutral background, so a status border always clears 3:1.

**`border-input` is proven only on `bg-canvas`, `bg-surface` and `bg-raised`.** On `bg-hover` it
measures 3.14 light / **2.80 dark**, and on `bg-selected` **2.94 / 2.54**; both fail 3:1. So a control
identified by its `border-input` edge (input, select, textarea, checkbox, unselected choice box)
never takes a `hover:bg-hover` or `bg-selected` fill, and never sits on a row that does. Buttons
are exempt: they are identified by their text (§2.11), so the outline `Button`'s
`border-input hover:bg-hover` is fine. On `main` today the only control this rules out is the
player's unselected multi-select row, which loses its hover (§4.7). No management table puts an
input on a hoverable row.

**Fixed-appearance surfaces** (same in both themes, §2.9):

| Token | Both themes | Use for |
|---|---|---|
| `--bg-media` | `#fbfdfc` light sage-1 | Behind the QR code; on `<img>` elements, sized so it covers only the image's own pixels (§2.9) |
| `--text-on-media` | `#1a211e` light sage-12 | Text inside a `bg-media` box (e.g. "Image unavailable") |

**Meters** — timer fills, answer-distribution bars, histogram, library budget bar. Each fill is the
text colour of its role (so it clears 3:1 against every neutral background); labels printed inside a
fill use `text-on-meter`:

| Token | Light | Dark | Replaces today |
|---|---|---|---|
| `--bg-meter-success` | = `--text-success` | = `--text-success` | timer > 60%, correct-answer bars (`bg-green-500`) |
| `--bg-meter-warning` | = `--text-warning` | = `--text-warning` | timer 30–60% (`bg-yellow-400`), library > 85% (`bg-amber-500`) |
| `--bg-meter-danger` | = `--text-danger` | = `--text-danger` | timer < 30%, over-budget, incorrect bars (`bg-red-500`/`bg-red-700`) |
| `--bg-meter-accent` | = `--text-accent` | = `--text-accent` | histogram and neutral-result bars (`bg-indigo-500`) |
| `--bg-meter-neutral` | = `--text-secondary` | = `--text-secondary` | "incorrect/other" bars (`bg-slate-600`), switch "off" track |
| `--bg-meter-track` | = `--bg-selected` | = `--bg-selected` | The empty part of every bar (`bg-slate-700`/`bg-slate-800`) |
| `--text-on-meter` | `#ffffff` | `#101211` sageDark-1 | Counts printed inside a bar |

**`text-on-meter` is drawn only on a meter fill, never on the track** (`text-on-meter` on
`bg-meter-track` is 1.30 light / 1.43 dark). On `main` the only count inside a bar is host
`ResultsPage`: it is right-aligned inside the fill (`justify-end pr-3`, fill at least 4% wide), and
the track's `overflow-hidden` clips a count too wide for a short fill rather than letting it spill
onto the track. Keep that `overflow-hidden`. Every other count (GameOverPage's answer bars and
histogram) sits outside the bar and uses `text-primary`/`text-secondary` like any other text.

**Known visual trade-off:** in light, the timer's "warning" fill is a deep ochre (`#8e550b`), not
bright yellow. Bright yellow cannot reach 3:1 against a light track. The seconds number beside the
bar is the primary cue, so the bar colour is reinforcement (WCAG 1.4.1).

**Answer-option tiles (player only), theme-aware** — every tile carries the same dark text
(`#1a211e`) in both themes, so a question never mixes white-text and dark-text tiles. Light uses
vivid step 9 where dark text passes; red and pink take their dark-theme step 10, and purple (whose
step 9 fails dark text in both themes) is a purple-9 → purple-8 50% mix. Dark uses mostly step 10:

| Tile | Hue | Light bg | Light text | Dark bg | Dark text |
|---|---|---|---|---|---|
| `option-1` | red | `#ec5d5e` redDark-10 | `#1a211e` | `#ec5d5e` redDark-10 | `#1a211e` |
| `option-2` | blue | `#0090ff` blue-9 | `#1a211e` | `#3b9eff` blueDark-10 | `#1a211e` |
| `option-3` | yellow | `#ffe629` yellow-9 | `#1a211e` | `#ffe629` yellowDark-9 | `#1a211e` |
| `option-4` | green | `#46a758` grass-9 | `#1a211e` | `#53b365` grassDark-10 | `#1a211e` |
| `option-5` | purple | `#a670d5` override | `#1a211e` | `#a670d5` override | `#1a211e` |
| `option-6` | orange | `#f76b15` orange-9 | `#1a211e` | `#ff801f` orangeDark-10 | `#1a211e` |
| `option-7` | pink | `#de51a8` pinkDark-10 | `#1a211e` | `#de51a8` pinkDark-10 | `#1a211e` |
| `option-8` | teal | `#12a594` teal-9 | `#1a211e` | `#0eb39e` tealDark-10 | `#1a211e` |

*Revised 2026-09-30.* The first version gave red (`#ce2c31` red-11), purple (`#8e4ec6` purple-9) and
pink (`#c2298a` pink-11) white text in light, and purple white text in dark. In the browser a white
"A" beside dark "B"/"C"/"D" read as inconsistent. White text on every tile was rejected: yellow
cannot carry it without turning brown.

The CSS variables are `--bg-option-1` … `--bg-option-8` and `--text-on-option-1` … `--text-on-option-8`.
`#1a211e` is the **light** sage-12, written as a literal so it stays dark in both themes. The order matches today's
`OPTION_COLORS` in `player/src/pages/game/QuestionPage.tsx` (red, blue, yellow, green, purple,
orange, pink, teal), and tiles cycle modulo 8 as they do now.

Every foreground/background combination in these tables that the UI is allowed to use is listed with
its measured ratio in **Appendix A** (93 pairs, 0 failures). Appendix A also lists the pairs
measured to **fail**, which the UI must never produce.

### 2.6 Tailwind mapping (`preset.js`)

The preset's `theme` (not `theme.extend`) sets the keys below. Each mapped value is the string
`rgb(var(--<css-var>) / <alpha-value>)`. Keys not listed (e.g. `caretColor`, `fill`, `stroke`,
`boxShadowColor`, `gradientColorStops`) fall back to `colors`, which contains only
`transparent`, `current` (`currentColor`), and `inherit`; none are used for colour today.
`divideColor` defaults to `borderColor`, so `divide-subtle` works with no extra key.

| Theme key | Keys → CSS variable | Example classes |
|---|---|---|
| `colors` | `transparent`, `current`, `inherit` only | — |
| `backgroundColor` | `transparent`, `current`, `inherit`; `canvas`, `surface`, `raised`, `hover`, `selected`, `accent`, `accent-hover`, `accent-subtle`, `danger`, `danger-hover`, `danger-subtle`, `success`, `success-hover`, `success-subtle`, `warning`, `warning-hover`, `warning-subtle`, `media`, `option-1`…`option-8`, `meter-success`, `meter-warning`, `meter-danger`, `meter-accent`, `meter-neutral`, `meter-track` → `--bg-<key>` | `bg-surface`, `hover:bg-accent-hover`, `bg-canvas/80` |
| `textColor` | `transparent`, `current`, `inherit`; `primary`, `secondary`, `disabled`, `accent`, `danger`, `success`, `warning`, `on-accent`, `on-danger`, `on-success`, `on-warning`, `on-media`, `on-meter`, `on-option-1`…`on-option-8` → `--text-<key>` | `text-primary`, `placeholder:text-secondary` |
| `borderColor` | `DEFAULT` and `subtle` → `--border-subtle`; `strong` → `--border-strong`; `input` → `--border-input`; `accent`/`danger`/`success`/`warning` → `--text-<key>`; `transparent`, `current` | `border`, `border-input`, `border-danger` |
| `ringColor` | `DEFAULT` and `focus` → `--ring-focus` | (not used by any class; focus uses §2.8) |
| `outlineColor` | `focus` → `--ring-focus` | — |
| `accentColor` | `primary` → `--bg-accent` | `accent-primary` (native checkboxes) |

Per-property keys (agreed with the user) give readable names (`text-primary`, not `text-text-primary`)
and make misuse impossible: `bg-primary` or `text-canvas` simply don't exist.

**How this differs from the preset tested in §2.3.** `ringOffsetColor` was in the test preset and is
**not** set here. Every `ring-offset-*` class is removed (§2.8), so it falls back to `colors`.
`outlineColor` and `accentColor` were **not** in the test preset. They have the same shape (a static
map of strings), so `preset.d.ts` still describes them, and step 6's `npm run build` (§4.8)
confirms them. Don't use a bare `ring` class. Tailwind 3 builds the default ring colour with its own
colour parser, which may not accept a `<alpha-value>` string. Nothing needs `ring`, because focus is
the §2.8 outline.

`preset.d.ts` declares exactly: a default export of type
`{ theme: Record<string, Record<string, string>> }`. The preset must therefore contain only static
objects of strings (no functions), which the table above satisfies.

### 2.7 Theme switching

**Attribute.** The active theme is `document.documentElement.dataset.theme`, `"light"` or `"dark"`.
It is always set by the inline script, so CSS needs only the two `tokens.css` blocks. If the script
somehow fails, `:root` values apply and the app renders in light.

**`base.css`** (shared) sets `color-scheme: light` on `:root` and `color-scheme: dark` on
`:root[data-theme="dark"]` (native scrollbars, `<select>` popups and date pickers follow the theme),
sets `background-color: rgb(var(--bg-canvas))` and `color: rgb(var(--text-primary))` on `html` and
`body`, and holds the focus rule (§2.8).

**Storage keys — one per app** (agreed; the projector's theme depends on the room, admin is
personal): `buzzer-theme-host`, `buzzer-theme-player`, `buzzer-theme-admin`. Values: `"light"` or
`"dark"`. **Absent key = follow the OS.** Any other value is treated as absent.

**Inline script in `<head>`** of each `index.html`, placed directly after `<meta charset>`, as a
classic (non-module) inline script, ~8 lines, identical in all three apps except the key:
1. Read the app's key from `localStorage` inside `try/catch` (storage can throw in private modes).
2. If it is `"light"` or `"dark"`, use it; otherwise use
   `matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'`.
3. Set `document.documentElement.dataset.theme` to the result.
Because it runs before the body is parsed, there is no flash of the wrong theme. The key literal
must equal the one `ThemeToggle` uses (§4.5); a comment beside each says so.

**`theme.ts`** (shared, no imports) exports:
- `type Theme = 'light' | 'dark'`
- `storedTheme(key): Theme | null` — `try/catch` around `localStorage`; invalid → `null`
- `systemTheme(): Theme` — from `matchMedia`
- `currentTheme(key): Theme` — `storedTheme(key) ?? systemTheme()`
- `applyTheme(t)` — sets `document.documentElement.dataset.theme`
- `setTheme(key, t)` — stores (`try/catch`; a storage failure still applies the theme for the
  session) and applies
- `watchTheme(key, onChange): () => void` — subscribes to (a) the `prefers-color-scheme` media
  query `change` event, applying the system theme **only while no stored choice exists**, and
  (b) the window `storage` event for this key, applying the new value from another tab. Calls
  `onChange(currentTheme(key))` after each applied change. Returns an unsubscribe function.

**Toggle behaviour:** two states. Clicking switches to the opposite of the current theme and stores
it. From then on the stored choice wins over the OS. There is no "system" option; clearing site
data returns to following the OS. (Meets T9: first visit follows the OS; the choice persists.)

**`ThemeToggle` component** (per app, `src/components/ui/ThemeToggle.tsx`, rendered once in each
app's `App.tsx` outside the route tree so it appears on **every route**, including login pages,
notices, error screens, and game screens):
- A native `<button type="button">`, `fixed top-3 right-3 z-40`, round, `bg-surface border
  border-subtle text-secondary shadow`, `hover:bg-hover hover:text-primary`.
- Size: **40×40px** in host and admin; **44×44px** in player (touch target).
- Icon from `lucide-react` (already a dependency): `Moon` while light is active, `Sun` while dark
  is active — the icon shows the theme you will switch **to**.
- `aria-label` and `title`: "Switch to dark theme" / "Switch to light theme".
- State: `useState(() => currentTheme(KEY))`; `useEffect(() => watchTheme(KEY, setTheme), [])`
  pattern (the React state mirrors the attribute; the attribute is the source of truth).
- `z-40` keeps it under the player's "Host disconnected" banner (`z-50`), which covers it while the
  banner shows. That is accepted: the banner is transient.
- **Clearance:** the 56×56px top-right region of every route must be empty so the toggle never
  covers content. Where a page's top row reaches that corner (a header with right-aligned actions,
  or the top of a full-height player layout), add top or right padding to that page's root/header.
  This is checked for every screen in the §4.9 checklist. Known cases on `main`:
  - **Host and admin management pages.** Each page's root is `p-8` (first row at 32px), and seven
    have a header with right-aligned buttons (host Games, Sessions, Roster, Question editor; admin
    Users, Courses, User detail). Their `max-w-*` keeps the header clear of the corner on wide
    windows, but not on a ~1280px laptop. Host `HomePage` has no `max-w` at all. **Fix once per
    shell:** `HostShell`'s and admin `AdminLayout`'s `<main>` gets `pt-6`, so every page inside the
    shell starts at 56px whatever the window width. Don't pad each page separately.
  - **Player multiple-choice and multi-select question screens.** Their root is `py-6 px-4` with
    the timer bar at about 48–64px, running the full width, so it runs under the toggle.
    `py-6` → `pt-16 pb-6`. True/false and fill-in-the-blank are vertically centred and need nothing.
  - Login pages, notices, and the other player screens are centred cards; host game screens are
    centred. The checklist confirms these.

### 2.8 Focus visibility (WCAG 2.4.7, 1.4.11)

One rule in `base.css` gives every focusable element — buttons, links, inputs, selects, textareas,
checkboxes, option tiles, nav links, the toggle — the same visible focus indicator:

- Selector `html :focus-visible` (specificity 0,1,1 — deliberately higher than Tailwind preflight's
  Firefox `:-moz-focusring { outline: auto }`, which is emitted *after* `base.css` because CSS
  `@import`s must come first in `index.css`).
- `outline: 2px solid rgb(var(--ring-focus)); outline-offset: 2px;`
- The 2px offset shows the surrounding background between element and outline, so the outline is
  measured against the background it sits on. On any neutral or tinted background it is ≥3.50:1
  light (worst: `bg-selected`) and ≥4.28:1 dark (Appendix A). On the page, card and input
  backgrounds it is ≥4.01:1 / ≥5.21:1. This holds around coloured option tiles and solid buttons too.

Consequently every existing `focus:outline-none`, `focus:ring-*`, `focus:ring-offset-*`,
`focus:border-transparent`, `outline-none`, and `ring-offset-slate-900` class is **removed** (they
would suppress or duplicate the global outline). Inputs may keep a border colour change on focus
(`focus-visible:border-accent`) in addition to the outline. `:focus-visible` matches text inputs on
mouse focus too, so inputs always show focus.

### 2.9 Fixed-appearance surfaces

- **QR code** (host lobby and the host game join panel): the white tile around `QRCodeSVG` becomes
  `bg-media`. `QRCodeSVG` keeps its default black-on-white colours. Phones need dark-on-light to scan
  reliably, so the QR never follows the theme.
- **Images:** `bg-media` goes on the `<img>` element itself, not the surrounding frame, so that a
  black-line diagram uploaded as a transparent PNG stays visible in dark. For that to show `bg-media`
  **only behind the image's own pixels**, the `<img>` box must be exactly the drawn image. An `<img>`
  that fills a differently-shaped box with `object-contain` would paint `bg-media` across the empty
  bands too, putting light bars around every non-matching image in dark. How each SafeImage use
  is sized on `main`:
  - Host prompt images (game question, results, game-over cards) size their box from the image's
    own `width`/`height` (`aspectRatio`), and every host thumbnail uses `fit="cover"`. These boxes
    have no empty bands, so `h-full w-full` stays.
  - Player option tiles (`aspect-square w-full`) and host game-screen option images
    (`w-full h-[20vh]`/`h-[36vh]`) use `object-contain` in a box of a different shape. These
    letterbox.

  So **SafeImage's contain case shrink-wraps the image instead of using `object-contain`:** the
  wrapper adds `flex items-center justify-center`, and the `<img>` is `max-h-full max-w-full`
  (no `h-full w-full`, no `object-contain`). The image keeps its shape at the largest size that
  fits. The empty bands show the wrapper's themed `bg-raised`, and `bg-media` sits only behind the
  image. Host's `fit="cover"` branch keeps `h-full w-full object-cover`. **Trade-off:** an image
  smaller than its box is no longer stretched up to fill it. Phones get the 400px small copy
  (larger than any tile) and the projector gets the full image, so this only affects originals
  smaller than their box. Loading, retry and refresh behaviour are unchanged.
  The frame, loading shimmer, and "Image unavailable" placeholder follow the theme (`bg-raised`,
  `text-secondary`; §4.6).

### 2.10 Screen-specific rules (context fit)

- **Host game screens (projector):** prompts, room code, option labels, counts, and scores use
  `text-primary` only; `text-secondary` is limited to small labels ("Room Code", axis labels).
  No text is made smaller or thinner than today. Primary text is ≥15:1 on the game screens'
  `bg-canvas`/`bg-surface` in both themes (≥11.2:1 on any background), well above AAA (7:1),
  which is what "readable from the back of a classroom" needs on a washed-out projector.
- **Player:** sizing, padding, `active:scale-95`, and 16px inputs are unchanged. Option tiles and
  the true/false buttons lose their per-hue `hover:` and `border-*` colours (hover is meaningless
  on touch, and on a phone it sticks after a tap). They keep `active:scale-95` for tap feedback.
  They also keep their `border-2`, now `border-transparent`, so no tile changes size. Without
  `border-transparent`, the default border colour (`--border-subtle`) would draw a grey ring
  around every coloured tile. Correct/incorrect always shows a word and an icon or symbol
  (✓/✗), never colour alone (WCAG 1.4.1).
- **Admin:** density unchanged. Tables use `bg-raised` header rows, `divide-subtle` dividers, and
  `hover:bg-hover` rows.
- **Shell sidebars (host `HostShell`, admin `App`)** become `bg-surface` with a `border-subtle`
  right edge, so they separate from the `bg-canvas` page in both themes (today both are `slate-900`).

### 2.11 Accessibility verification

T9 names WCAG 2.1 AA contrast and visible focus. Theming can also break two neighbouring AA
criteria, so they are covered too:

| Criterion | Requirement | Applies to |
|---|---|---|
| **1.4.3 Contrast (Minimum)** | Text ≥4.5:1; large text (≥24px, or ≥18.66px bold) ≥3:1. Placeholder text counts. Disabled controls are exempt. | All text, both themes |
| **2.4.7 Focus Visible** | Keyboard focus is always visible | Every interactive element |
| **1.4.11 Non-text Contrast** | ≥3:1 for component boundaries needed to identify them, state indicators, and meaningful graphics | Input borders, focus outline, checkbox/switch state, bars |
| **1.4.1 Use of Color** | Colour is never the only means of conveying information | Correct/incorrect, selected multi-select options, timer, switch |

Buttons and option tiles are identified by their text, so their fill-vs-background contrast is not
required by 1.4.11 (a yellow tile on a light page is fine); their **text** must pass, and it does.

**Four layers of verification — all required:**

1. **Proven at design time.** Appendix A lists every allowed foreground/background pair with WCAG
   ratios measured from the Radix 3.0.0 values: 93 pairs, all passing in both themes. Any page that
   only uses token pairs from Appendix A passes contrast by construction. §2.5's "Use for" columns
   are the rules for which pairs are allowed.
2. **Re-proven in the repo.** `scripts/check_theme.py` (§4.3) reads the real `tokens.css`, resolves
   both themes, computes every pair in `contrast-pairs.json`, and exits non-zero on any failure. It
   also cross-checks preset ↔ tokens names and scans all app source for leftovers. It runs locally
   before every merge request; **no CI job is added** (T2 forbids modifying the pipeline).
3. **Audited in the browser.** For every screen in the §4.9 checklist, in **both** themes: run
   Chrome's built-in **Lighthouse → Accessibility** audit or the free **axe DevTools** extension.
   These measure the colours actually rendered, which catches what layer 2 cannot: opacity-modified
   backgrounds (`bg-canvas/80`), text on images, and any token pair used outside Appendix A.
   Items reported as "needs review" (text over images) are checked by eye and recorded.
4. **Keyboard and colour-only pass.** On each checklist screen: Tab through every control and
   confirm the outline is visible and not clipped by an `overflow-hidden` parent; confirm the
   toggle is reachable; confirm correct/incorrect, selected options, and the timer carry a
   non-colour cue. States an automated audit only sees if triggered — hover, disabled, form error,
   locked question, host disconnected, error screen — are listed and checked separately.

**Done means:** `check_theme.py` exits 0; the audits report zero contrast violations on every
checklist screen in both themes (with every "needs review" item resolved by hand); the keyboard and
colour-only pass is complete with no failures. All three are recorded in `docs/ui/accessibility.md`.

### 2.12 Evidence

- **Before** (done 2026-09-29, current dark-only UI from `main`), in `docs/ui/before/`:
  `host-play.png`, `host-games.png`, `host-rosters.png`, `host-sessions.png`, `host-lobby.png`,
  `host-question.png`, `host-results.png`, `host-gameover.png`, `player-join.png`,
  `player-question-mc.png`, `admin-login.png`, `admin-users.png`, `admin-courses.png`,
  `admin-guests.png`.
- **After:** `docs/ui/after/<app>-<screen>-<light|dark>.png` — one light and one dark shot for
  **every** before file above (same `<app>-<screen>` stem), plus these key screens that have no before
  shot: host question editor (`host-question-editor`), player results (`player-results`), admin
  user detail (`admin-user-detail`).
- **`docs/ui/accessibility.md`:** the `check_theme.py` output; a table of audit results per screen
  per theme; the keyboard/colour-only checklist with results; the known limitation (§5).
- Each member's `docs/<netid>.md` (T10) links these files.

---

## 3. Alternatives

### 3.1 Component library vs manual tokens

- **shadcn/ui** (CSS-variable tokens, Radix-based components): closest to the existing hand-rolled
  primitives, but its current CLI targets Tailwind v4 (and React 19 for some parts), copying its
  components would overwrite the player's touch-tuned primitives, and it adds dependencies to three
  apps. Its main benefit — new components (Dialog, Select, Switch) — isn't needed for T9.
- **daisyUI 5** requires Tailwind v4; its class-based components and token names would replace ours.
- **HeroUI v3** requires React 19 and Tailwind v4.
- **Flowbite React** uses palette classes with `dark:` variants, not semantic tokens, so the token
  layer would still be manual — and T9 forbids raw palette utilities.
- **Radix Themes, Mantine** are not Tailwind-based and conflict with "CSS variables mapped into each
  app's Tailwind theme".
- **Tailwind v4 upgrade** (implied by most of the above): rejected because v4 requires Safari 16.4+,
  and the player app runs on students' own phones.
- **Chosen:** manual token layer with values borrowed from **Radix Colors** (paired light/dark
  scales, no components, no Tailwind requirement). Every one of the ~800 class replacements and the
  game-specific tokens are needed with any library anyway.

### 3.2 Token structure

- **One layer (semantic tokens with literal values, ~60 variables)** — smaller, but values can't be
  audited line-by-line against Radix. Rejected (user's choice: two layers).
- **Hex values** — break every opacity modifier (`/50`). Rejected.
- **`color-mix()` for opacity** — needs Safari 16.2+/Chrome 111+, the same phone-support problem as
  Tailwind v4. Rejected.
- **`dark:` variants in components** — doubles every colour class and isn't a token layer. Rejected.
- **Flat Tailwind `colors` names** — produce `text-text-primary`, `accent-accent`, and allow a text
  colour to be used as a background. Rejected for per-property keys (user's choice).
- **Three text levels (primary/secondary/muted)** — Radix has only two AA-safe text steps (11, 12);
  a third level below step 11 fails 4.5:1. Rejected: two levels plus `disabled`.
- **Unmodified Radix steps everywhere** — measured failures in light: teal-11 text 3.73–4.47:1,
  amber-11 text 3.77–4.36:1, white on red-10 4.37:1 and on grass-10 3.50:1, teal-8 focus ring 2.2:1,
  gray-7/8 input borders 1.5–2.8:1, amber on a light track 1.21:1. Rejected: the overrides, the
  step-11 solids, `border-input`, teal-11 focus, and text-coloured meters fix every one (Appendix A).

### 3.3 Sharing

- **Three copies of everything** — guarantees drift (the primitives already drifted). Rejected.
- **Shared `preset.ts`** — fails `tsc -b` (TS6307) or, if added to `tsconfig.node.json`, emits
  stray outputs into `shared/`. Rejected (§2.3).
- **Shared React toggle** — a file in `shared/` cannot resolve `react`. Rejected; the logic is shared
  (`theme.ts`), the component is per app.
- **npm workspace / shared package** — a structural change to the frontend beyond T9. Rejected.

### 3.4 Switching and persistence

- **One shared storage key** — in production (one origin) toggling in admin would change the
  projector. Rejected for per-app keys (user's choice).
- **Three-state toggle (light / dark / system)** — not required by T9; more UI. Rejected.
- **CSS-only OS fallback** (`@media (prefers-color-scheme)` + attribute overrides) — duplicates every
  dark value in `tokens.css` to cover a no-JS case that can't happen (the apps are React). Rejected;
  the inline script always sets the attribute.
- **Toggle inside each shell's sidebar** — misses login pages and game screens, which are outside the
  shells. Rejected for one fixed top-right toggle on every route (user's choice). Top-right because
  bottom-right holds the host join panel, bottom-left overlaps the shells' sidebars, and the
  player's layouts are vertically centred, leaving the top clear.

### 3.5 Focus

- **Per-component `focus-visible:ring-*` classes** — must be added to every interactive element by
  hand, and anything missed has no themed focus. Rejected for one global `:focus-visible` outline.

### 3.6 Answer options

- **Same tile colours in both themes** — fewer values, but not tuned to either background. Rejected
  for theme-aware tiles (user's choice).
- **Matching colours on the projector** — beyond T9; the host shows no coloured options today.
  Rejected (user's choice).

### 3.7 Verification

- **Adding `check_theme.py` to CI** — T2 says not to modify the pipeline. Rejected; it is a required
  local step instead.
- **Automated browser audits (Playwright + axe)** — no frontend test tooling exists; manual
  Lighthouse/axe runs on a fixed checklist are proportionate. Rejected for now.

---

## 4. Detailed Implementation

### 4.1 File inventory

**New files**

| File | Purpose |
|---|---|
| `frontend/shared/theme/tokens.css` | All colour values: primitives + semantic tokens, two blocks (§2.4, §2.5) |
| `frontend/shared/theme/base.css` | `color-scheme`, `html`/`body` colours, global focus outline (§2.7, §2.8) |
| `frontend/shared/theme/preset.js` | Tailwind preset: palette replacement + per-property mapping (§2.6) |
| `frontend/shared/theme/preset.d.ts` | Hand-written type for `preset.js` (§2.3, §2.6) |
| `frontend/shared/theme/theme.ts` | Framework-free theme logic, no imports (§2.7) |
| `frontend/shared/theme/contrast-pairs.json` | The Appendix A pair list, read by the check script |
| `frontend/shared/CLAUDE.md` | Context file for the new directory (§4.11) |
| `frontend/{host,player,admin}/src/components/ui/ThemeToggle.tsx` | The toggle (§2.7) |
| `scripts/check_theme.py` | Contrast check + name cross-check + leftover scan (§4.3) |
| `docs/ui/accessibility.md` | Accessibility evidence (§2.12) |
| `docs/ui/after/*.png` | After screenshots (§2.12) |

**Modified — wiring (all three apps):** `index.html` (inline script), `src/index.css` (imports,
hex removed), `tailwind.config.ts` (preset), `src/App.tsx` (render `ThemeToggle`).
**Not modified:** `vite.config.ts`, `tsconfig.json`, `tsconfig.node.json`, `package.json`,
`.gitignore`, `.gitlab-ci.yml` (all verified unnecessary in §2.3).

**Modified — colour conversion** (raw palette class count per file on `main`, 2026-09-29):

| App | File | Count |
|---|---|---|
| host | `src/components/images/ImageLibraryPanel.tsx` | 20 |
| host | `src/components/images/ImagePicker.tsx` | 9 |
| host | `src/components/ui/SafeImage.tsx` | 9 |
| host | `src/components/ui/TimerBar.tsx` | 5 |
| host | `src/components/ui/button.tsx` | 5 |
| host | `src/components/ui/card.tsx` | 1 |
| host | `src/components/ui/input.tsx` | 3 |
| host | `src/pages/GamesPage.tsx` | 21 |
| host | `src/pages/HomePage.tsx` | 22 |
| host | `src/pages/HostShell.tsx` | 11 |
| host | `src/pages/LoginPage.tsx` | 8 |
| host | `src/pages/QuestionEditorPage.tsx` | 50 |
| host | `src/pages/RosterPage.tsx` | 40 |
| host | `src/pages/RostersPage.tsx` | 7 |
| host | `src/pages/SessionsPage.tsx` | 37 |
| host | `src/pages/game/GameLayout.tsx` | 6 |
| host | `src/pages/game/GameOverPage.tsx` | 50 |
| host | `src/pages/game/LobbyPage.tsx` | 12 |
| host | `src/pages/game/QuestionPage.tsx` | 16 |
| host | `src/pages/game/ResultsPage.tsx` | 21 |
| player | `src/components/ui/SafeImage.tsx` | 4 |
| player | `src/components/ui/TimerBar.tsx` | 5 |
| player | `src/components/ui/button.tsx` | 5 |
| player | `src/components/ui/card.tsx` | 1 |
| player | `src/components/ui/input.tsx` | 3 |
| player | `src/pages/JoinPage.tsx` | 3 |
| player | `src/pages/LoginPage.tsx` | 3 |
| player | `src/pages/NamePage.tsx` | 16 |
| player | `src/pages/game/FeedbackPage.tsx` | 3 |
| player | `src/pages/game/GameLayout.tsx` | 3 |
| player | `src/pages/game/GameOverPage.tsx` | 21 |
| player | `src/pages/game/LobbyPage.tsx` | 5 |
| player | `src/pages/game/QuestionPage.tsx` | 33 |
| player | `src/pages/game/ResultsPage.tsx` | 14 |
| admin | `src/App.tsx` | 15 |
| admin | `src/components/ui/button.tsx` | 5 |
| admin | `src/components/ui/card.tsx` | 1 |
| admin | `src/components/ui/input.tsx` | 3 |
| admin | `src/pages/CoursesPage.tsx` | 9 |
| admin | `src/pages/GuestsPage.tsx` | 10 |
| admin | `src/pages/LoginPage.tsx` | 3 |
| admin | `src/pages/UserDetailPage.tsx` | 35 |
| admin | `src/pages/UsersPage.tsx` | 9 |

`check_theme.py`'s leftover scan (§4.3) is the authority: when it reports nothing, conversion is
complete, whatever this table says.

**Modified — context docs:** see §4.11.

### 4.2 Shared theme files

**`tokens.css`** — format per §2.4, values per §2.5. Layout:
1. Header comment: purpose; "the only file with colour values"; "format is parsed by
   `scripts/check_theme.py` — see `docs/plans/t9-ui-theming.md` §2.4"; source
   `@radix-ui/colors@3.0.0`.
2. `:root {` — light primitives (10 scales × 12 steps, grouped per scale with a comment), then every
   semantic token. **Light definitions:** a token whose light value in §2.5 is a Radix step is
   `var(--<scale>-<step>)` (e.g. `--bg-canvas: var(--sage-2);`). Everything else is a literal
   channel triple with a source comment:
   - overrides — `--bg-accent` and `--text-accent` `4 111 97` (#046f61), `--bg-accent-hover`
     `7 97 86` (#076156), `--bg-danger-hover` `174 38 45` (#ae262d), `--bg-success-hover`
     `39 106 52` (#276a34), `--text-danger` `188 40 47` (#bc282f), `--text-success` `40 112 54`
     (#287036), `--text-warning` `142 85 11` (#8e550b), `--bg-option-5` `166 112 213` (#a670d5,
     purple-9 → purple-8 50%);
   - dark-scale values used in light — `--bg-option-1` `236 93 94` (#ec5d5e, redDark-10),
     `--bg-option-7` `222 81 168` (#de51a8, pinkDark-10);
   - white `255 255 255` — `--text-on-accent`, `--text-on-danger`, `--text-on-success`,
     `--text-on-meter`;
   - fixed-appearance — `--bg-media` `251 253 252`, `--text-on-media` `26 33 30`;
   - the tile dark text `26 33 30` — all eight, `--text-on-option-1` … `-8`.
   Meter tokens are references to other semantic tokens: `--bg-meter-success: var(--text-success);`,
   likewise `-warning`, `-danger`, `-accent` (→ `--text-accent`), `-neutral` (→ `--text-secondary`),
   and `--bg-meter-track: var(--bg-selected);`. They follow the theme automatically.
3. `:root[data-theme="dark"] {` — dark primitives (same 120 names), then **exactly these 21**
   semantic redeclarations (every other token resolves correctly from its `:root` definition):
   `--bg-canvas: var(--sage-1)`; `--bg-surface: var(--sage-2)`; `--bg-accent: var(--teal-9)`;
   `--bg-accent-hover: var(--teal-10)`; `--bg-danger: 206 44 49` (light red-11, #ce2c31);
   `--bg-success: 42 126 59` (light grass-11, #2a7e3b); `--text-accent: var(--teal-11)`;
   `--text-danger: var(--red-11)`; `--text-success: var(--grass-11)`;
   `--text-warning: var(--amber-11)`; `--text-on-accent: var(--sage-1)`;
   `--text-on-warning: var(--sage-1)`; `--text-on-meter: var(--sage-1)`;
   `--border-input: var(--sage-9)`; `--ring-focus: var(--teal-9)`;
   `--bg-option-1: var(--red-10)`; `--bg-option-2: var(--blue-10)`;
   `--bg-option-4: var(--grass-10)`; `--bg-option-6: var(--orange-10)`;
   `--bg-option-7: var(--pink-10)`; `--bg-option-8: var(--teal-10)`.
   (`--bg-option-3` uses step 9 in both themes and Radix's dark yellow-9 is the same colour;
   `--bg-option-5` and every `--text-on-option-N` are literals that hold in both themes. None needs
   a redeclaration. Before the 2026-09-30 tile revision this list was 23, with
   `--text-on-option-1` and `-7` in it.)
   **Check:** `check_theme.py` must print exactly the light and dark columns of §2.5. A mismatch
   usually means a `var()` resolved against a primitive the dark block redefines.

**`base.css`** — four rules, nothing else: `:root { color-scheme: light }`;
`:root[data-theme="dark"] { color-scheme: dark }`; `html, body { background-color:
rgb(var(--bg-canvas)); color: rgb(var(--text-primary)) }`; and the §2.8 focus rule.

**`preset.js`** — ES module, default export `{ theme: { … } }` per §2.6, built with a tiny local
helper `v(name)` returning `rgb(var(--name) / <alpha-value>)`. **No imports.** A header comment
states: shared by all three apps; keep `preset.d.ts` in step; every `var(--…)` here must exist in
`tokens.css` (the check script verifies).

**`preset.d.ts`** — exactly: declares `preset` as `{ theme: Record<string, Record<string, string>> }`
and default-exports it.

**`theme.ts`** — per §2.7. **No imports.** Must compile under each app's `tsconfig.json`
(`strict`, `noUnusedLocals`, `noUnusedParameters`, `lib` includes `DOM`).

**`contrast-pairs.json`** — an array of `{ "fg": "<css var name without -->", "bg": "…", "min": 4.5 | 3 }`,
one entry per Appendix A row, in the same order.

### 4.3 `scripts/check_theme.py`

Python 3, **standard library only** (runs with the repo venv or any Python). Must pass
`ruff check scripts/` and `ruff format --check scripts/` (CI lints `scripts/`). Paths are resolved
relative to the script file, so it runs from any directory:
`python scripts/check_theme.py` (on Windows: `.venv\Scripts\python scripts\check_theme.py`).

It runs three checks, prints a report, and exits **1 if any check fails**, else 0:

1. **Contrast.**
   - Parse `frontend/shared/theme/tokens.css` per §2.4: find the `:root {` and
     `:root[data-theme="dark"] {` blocks; read `--name: value;` lines, ignoring comments.
     Error (exit 1) on a malformed declaration line or a missing block.
   - Light = `:root` declarations. Dark = `:root` declarations overlaid by the dark block.
   - Resolve each token by following `var(--x)` chains within the same theme; error on a missing
     name or a cycle.
   - For each pair in `contrast-pairs.json`, compute the WCAG 2.1 contrast ratio in both themes:
     relative luminance with sRGB linearisation (`c/12.92` if `c ≤ 0.03928`, else
     `((c+0.055)/1.055)^2.4`), `L = 0.2126R + 0.7152G + 0.0722B`, ratio `(L1+0.05)/(L2+0.05)` with
     `L1 ≥ L2`. Print one row per pair (fg, bg, min, light ratio, dark ratio, resolved hexes) and
     mark failures. Also print the resolved hex of every semantic token per theme, so it can be
     compared with §2.5.
2. **Names.** Every `var(--name)` in `frontend/shared/theme/preset.js` and `base.css` exists in
   `tokens.css`, and every `fg`/`bg` in `contrast-pairs.json` exists. Missing → fail.
3. **Leftovers.** Scan `frontend/{host,player,admin}/src/**/*.{ts,tsx}`, each app's
   `src/index.css`, `index.html` and `tailwind.config.ts`, and `frontend/shared/theme/`'s `base.css`,
   `preset.js`, `preset.d.ts` and `theme.ts` (everything that could hold a colour except
   `tokens.css`). Report `file:line: match` for any of:
   - a Tailwind palette class: a prefix among `bg text border ring ring-offset divide placeholder
     from via to fill stroke outline shadow accent caret decoration` followed by `-` and a palette
     name among `slate gray zinc neutral stone red orange amber yellow lime green emerald teal cyan
     sky blue indigo violet purple fuchsia pink rose white black`, optionally with `-<number>` and
     `/<number>` (word-bounded; e.g. `text-white`, `bg-slate-800/50`);
   - `focus:ring`, `focus:outline`, `focus:border-transparent`, `ring-offset-`, or `outline-none`;
   - a `dark:` variant (`dark:` directly followed by a class name; the `prefers-color-scheme: dark`
     media query and `data-theme="dark"` selectors do not match);
   - a hex colour literal `#` + 3, 4, 6 or 8 hex digits (word-bounded), in any scanned file.
   Any match → fail. A genuine false positive (e.g. an anchor like `href="#add"`) is silenced by the
   comment `theme-check: allow` on the same line; the script reports how many lines were allowed,
   and each use must be justifiable in review.

The pair list lives in `contrast-pairs.json` (next to the tokens) rather than in the script, so
adding a token and its allowed pairs happens in one folder.

### 4.4 Per-app wiring (host, player, admin)

1. **`index.html`** — the §2.7 inline script right after `<meta charset>`, with this app's key and
   a comment "must match `THEME_KEY` in `src/components/ui/ThemeToggle.tsx`".
2. **`src/index.css`** — first lines `@import '../../shared/theme/tokens.css';` then
   `@import '../../shared/theme/base.css';`, then the existing `@tailwind` directives. Remove the
   `body { background: #0f172a; color: #f8fafc; }` colours (now in `base.css`); keep
   `margin: 0`, `box-sizing`, and the player's `-webkit-tap-highlight-color: transparent`.
3. **`tailwind.config.ts`** — `import preset from '../shared/theme/preset.js';` (explicit `.js`),
   `presets: [preset]`, keep `content` and the `fontFamily` extension, keep `satisfies Config`.
4. **Delete the stale compiled `tailwind.config.js` and `tailwind.config.d.ts`** (gitignored) before
   running `npm run dev`, or run `npm run build` first; otherwise the dev server keeps the old
   palette (`frontend/CLAUDE.md` gotcha). Restart the dev server after config changes.
5. **`src/App.tsx`** — render `<ThemeToggle />` once, outside `<Routes>` (so every route has it).

Note: the moment step 3 lands, every unconverted class renders with no colour (the palette no longer
exists). Convert an app completely before judging it visually.

### 4.5 `ThemeToggle.tsx` (each app)

Per §2.7. `THEME_KEY` constant: `'buzzer-theme-host'` / `'buzzer-theme-player'` /
`'buzzer-theme-admin'`. Import `currentTheme`, `setTheme` (rename locally to avoid clashing with the
React setter), and `watchTheme` from `'../../../../shared/theme/theme'` (path from
`src/components/ui/`). Styling uses only tokens from §2.5. No `focus:` classes (global outline).
Player's copy differs only in size (44px) and key.

**Click handler**, exactly:
1. `next` = `'light'` if `document.documentElement.dataset.theme === 'dark'`, else `'dark'`. Read
   the attribute (the source of truth), not the React state.
2. Call the shared `setTheme(THEME_KEY, next)`, which stores the choice and applies it.
3. Call the React setter with `next`. This is required. The `storage` event fires only in *other*
   tabs, so `watchTheme` never reports a change made in this tab, and without this step the icon
   and label stay stale.

### 4.6 Primitives

Colour classes only; sizes, padding, and player touch tuning unchanged. In all three apps:

- **`button.tsx`** — remove `focus:outline-none focus:ring-2 focus:ring-offset-2
  focus:ring-offset-slate-900` and every variant's `focus:ring-*`.
  - `default`: `bg-accent text-on-accent hover:bg-accent-hover`
  - `outline`: `border border-input text-primary hover:bg-hover`
  - `ghost`: `text-secondary hover:bg-hover hover:text-primary`
  - `destructive`: `bg-danger text-on-danger hover:bg-danger-hover`
  - keep `disabled:opacity-50 disabled:pointer-events-none` (disabled is exempt from contrast).
- **`card.tsx`** — `bg-surface border border-subtle` (shadow unchanged).
- **`input.tsx`** — `bg-raised border border-input text-primary placeholder:text-secondary
  focus-visible:border-accent`; remove `focus:outline-none focus:ring-*`.
- **`TimerBar.tsx`** (host and player) — track `bg-meter-track`; fill `bg-meter-success` (>60%),
  `bg-meter-warning` (>30%), `bg-meter-danger` (else); seconds text `text-secondary` →
  **`text-primary`** (it is the primary, non-colour cue). Timer logic untouched.
- **`SafeImage.tsx`** (host and player) — per §2.9:
  - Wrapper: `bg-slate-800` → `bg-raised`, plus `flex items-center justify-center`.
  - `<img>`: `bg-media`. For the contain case (player: always; host: when `fit` is not
    `'cover'`), `h-full w-full object-contain` → `max-h-full max-w-full`. Host's `'cover'` case
    keeps `h-full w-full object-cover`. The `opacity-0`/`opacity-100` load fade is unchanged.
  - Loading shimmer: `bg-slate-700/40` → `bg-selected/40` (a neutral overlay, so it keeps `/α`).
  - "Image unavailable" text and the `ImageOff` icon: `text-secondary`.
  - Host's compact Retry overlay: `hover:bg-slate-700/60` → `hover:bg-hover/60`. Host's text Retry
    button becomes `border border-input text-primary hover:bg-hover` (the outline `Button`'s
    classes).
  - Retry/refresh logic untouched. The absolutely positioned overlays are unaffected by the
    wrapper becoming a flex container.

### 4.7 Conversion table

Apply per occurrence, **judging by what the element is**, not just by the class. The "usually"
column is the default; the special cases below override it.

**Opacity modifiers:** keep `/α` only on neutral backgrounds used as overlays or translucent panels
(`bg-surface/90`); the browser audit covers those. **Drop** `/α` from text classes (e.g.
`text-amber-200/80` → `text-warning`: transparency lowers contrast below the Appendix A figures) and
from status and subtle tokens (already tuned per theme).

**`opacity-*` utilities** dim everything inside them, text included, so the same rule applies:
- **On disabled controls, keep them** (`disabled:opacity-50`, the submitted/locked answer tiles'
  `opacity-50`, the editor's `disabled:opacity-20`). WCAG exempts disabled controls.
- **On readable, non-disabled content, remove them** and express the state another way. On
  `main` there are two cases, both in host `RosterPage`: the CSV preview's skipped header row
  (`opacity-30 line-through`) → `text-secondary line-through`, and inactive roster rows
  (`opacity-50`) → the row's cells become `text-secondary`. The "Inactive" badge carries the
  state in words, and the line-through carries the skip.

**Checked and absent on `main`** (2026-09-29), so no mapping is needed: `bg-white/α` or
`bg-black/α` scrims, `rgb()`/`hsl()` colour literals, colours in inline `style` props (inline styles
set only widths, heights and aspect ratios), and a `<meta name="theme-color">` tag. **T7 adds two of
these** (canvas `rgba()` colours and a legend's inline `style` colour); §4.12 covers them.

| Today | Usually becomes | Notes |
|---|---|---|
| `bg-slate-900` | `bg-canvas` (page) / `bg-surface` (sidebar, panel) | Sidebars → `bg-surface` (§2.10) |
| `bg-slate-800` | `bg-surface` | Bar/timer tracks → `bg-meter-track` |
| `bg-slate-700` | `bg-raised` | As a hover target → `bg-hover`; bar tracks → `bg-meter-track` |
| `bg-slate-600` | `bg-hover` / `bg-selected` | Bars → `bg-meter-neutral`; switch "off" → `bg-meter-neutral` |
| `bg-slate-*/α` | same mapping, keep `/α` | e.g. `bg-slate-900/90` → `bg-surface/90` |
| `text-slate-100`, `-200`, `text-white` | `text-primary` | On an accent/danger/success fill → `text-on-<fill>`; inside a bar → `text-on-meter` |
| `text-slate-300`, `-400` | `text-secondary` | Key projector content → `text-primary` (§2.10) |
| `text-slate-500`, `-600` | `text-secondary` | Only on a disabled control → `text-disabled` |
| `text-slate-900` | `text-primary` | Inside `bg-media` → `text-on-media` |
| `placeholder-slate-400`, `placeholder:text-slate-*` | `placeholder:text-secondary` | |
| `border-slate-800`, `-700`, `-600` | `border-subtle` | On an input/select/textarea/checkbox/choice box → `border-input` |
| `border-slate-500` | `border-strong` | On a control → `border-input` |
| `divide-slate-*` | `divide-subtle` | |
| `bg-indigo-600` | `bg-accent` | |
| `hover:bg-indigo-500`, `-700` | `hover:bg-accent-hover` | |
| `bg-indigo-500` (non-hover) | `bg-accent` | In a bar/histogram → `bg-meter-accent` |
| `bg-indigo-900`, `bg-indigo-950/α` | `bg-accent-subtle` | Drop the `/α` (e.g. a highlighted drop zone) |
| `text-indigo-200`, `-300`, `-400`, `-700` | `text-accent` | On an accent fill → `text-on-accent` |
| `border-indigo-400`, `-500`, `border-indigo-500/α` | `border-accent` | Drop the `/α` |
| `border-white` | `border-accent` | A selected item on an accent fill (`border-white bg-indigo-500 text-white` → `border-accent bg-accent text-on-accent`) |
| `ring-indigo-*`, `focus:ring-*`, `focus-visible:ring-*`, `focus:ring-offset-*`, `focus:outline-none`, `outline-none` | **remove** | Global outline (§2.8); `focus:border-indigo-500` → `focus-visible:border-accent`. The leftover scan does not flag `focus-visible:ring-*`; search for it |
| `accent-indigo-500` | `accent-primary` | Native checkboxes |
| `text-red-*` | `text-danger` | |
| `bg-red-500`, `-600` (buttons, badges) | `bg-danger` + `text-on-danger` | In a bar/timer → `bg-meter-danger` |
| `hover:bg-red-500`, `-700` | `hover:bg-danger-hover` | |
| `bg-red-700` (bar) | `bg-meter-danger` | GameOverPage incorrect bars |
| `bg-red-900`, `bg-red-900/α` | `bg-danger-subtle` | Drop the `/α` (subtle tokens are already theme-tuned) |
| `border-red-500` | `border-danger` | |
| `text-green-*` | `text-success` | |
| `bg-green-500`, `-600`, `-700` (buttons, badges) | `bg-success` + `text-on-success` | In a bar/timer → `bg-meter-success` |
| `bg-green-900`, `bg-green-900/α` | `bg-success-subtle` | Drop the `/α` |
| `border-green-500`, `-700` | `border-success` | |
| `text-amber-*`, `text-yellow-*` | `text-warning` | On `bg-warning` → `text-on-warning` |
| `bg-amber-500` (bar) | `bg-meter-warning` | |
| `bg-amber-900/α`, `bg-amber-500/10` | `bg-warning-subtle` | Drop the `/α` |
| `border-amber-500`, `border-amber-600/α` | `border-warning` | |
| `bg-yellow-400` (timer) | `bg-meter-warning` | |
| `bg-white` | see special cases | QR tile → `bg-media`; switch knob → `bg-surface` |

**Special cases (verified in source on `main`):**

- **`player/src/pages/game/QuestionPage.tsx` `OPTION_COLORS`** — becomes eight entries
  `'bg-option-N text-on-option-N border-transparent'` (N = 1…8, same order). Remove the per-hue
  `hover:bg-*` and `border-*` parts. Both render sites keep `border-2` (§2.10). The render sites
  that add `text-white` (the multiple-choice `tileClass` callback and the text-option button)
  drop it, because the entry supplies the text colour.
- **Same file, true/false buttons** (today `border-2 border-green-500` / `border-red-500`, fill
  `bg-green-600 hover:bg-green-500` / `bg-red-600 hover:bg-red-500`, and `bg-green-700` /
  `bg-red-700` when submitted or locked):
  - **True** → `bg-option-4 text-on-option-4 border-transparent` (green).
  - **False** → `bg-option-1 text-on-option-1 border-transparent` (red).
  - Keep `border-2`. Drop `text-white`, the `hover:` classes, and the `-700` swap. The disabled
    state keeps the same fill plus the existing `opacity-50`, which is exempt.
  - Why tile tokens and not `bg-success`/`bg-danger`: these are answer choices, not verdicts.
    `success`/`danger` stay reserved for "Correct"/"Incorrect". A player sees the same green and
    red on the multiple-choice tiles, and the pairs are already in Appendix A.
- **Same file, multi-select choices** — the same classes apply to the text-option rows **and** to
  the image tiles' `tileClass` (`OptionTiles`, which today use the identical indigo/slate strings).
  - Selected: `bg-accent border-accent text-on-accent`. The row's letter badge (today
    `border-white bg-white text-indigo-700`) → `bg-surface border-transparent text-accent`. The
    badge shows ✓, so selection isn't colour-only. The image tile's badge is plain text and
    inherits `text-on-accent`.
  - Unselected: `bg-raised border-input text-primary`, with **no** `hover:` class. `border-input`
    fails on `bg-hover` in dark (§2.5), and hover sticks after a tap. The row's badge →
    `border-input text-secondary`.
- **Player `QuestionPage` multiple-choice and multi-select roots** — `py-6` → `pt-16 pb-6` (toggle
  clearance, §2.7).
- **Same file, fill-in-the-blank input** (today `bg-white text-slate-900 placeholder:text-slate-400`)
  — `bg-raised border border-input text-primary placeholder:text-secondary`. Keep `text-xl` and
  its sizes.
- **`player/src/pages/game/GameLayout.tsx` banner** — `bg-yellow-600/90 text-yellow-100` →
  `bg-warning text-on-warning` (keep `fixed top-0 inset-x-0 … z-50`).
- **`host/src/pages/game/GameLayout.tsx` join panel** — `bg-slate-900/90 border-slate-700` →
  `bg-surface/90 border-subtle`; QR tile `bg-white` → `bg-media`; "Room Code" label → `text-secondary`;
  code → `text-primary`.
- **`host/src/pages/game/LobbyPage.tsx`** — QR tile `bg-white` → `bg-media`; room code `text-white`
  → `text-primary`. Auto-advance switch: track on `bg-accent`, off `bg-meter-neutral`; knob
  `bg-white` → `bg-surface` (knob vs track ≥5.75:1 both states and themes, Appendix A); the visible
  "Auto-advance" label already names the control.
- **`host/src/pages/game/ResultsPage.tsx` bars** — track `bg-slate-800` → `bg-meter-track`; the
  `barColor` expression: correct `bg-green-500` → `bg-meter-success`, incorrect `bg-slate-600` →
  `bg-meter-neutral`, otherwise (indigo) → `bg-meter-accent`; count inside the bar `text-white` →
  `text-on-meter`. The existing "✓ Correct" label → `text-success`.
- **`host/src/pages/game/GameOverPage.tsx`** — `AnswerBar` track → `bg-meter-track`; its `colorClass`
  arguments: `bg-green-500` → `bg-meter-success`, `bg-slate-600` → `bg-meter-neutral`,
  `bg-red-700` → `bg-meter-danger`; histogram column track → `bg-meter-track`, bar `bg-indigo-500`
  → `bg-meter-accent`; stat values `text-white` → `text-primary`.
- **`host/src/components/images/ImageLibraryPanel.tsx`** — budget meter fill: over →
  `bg-meter-danger`, >85% → `bg-meter-warning`, else → `bg-meter-accent`; track → `bg-meter-track`.
  Live-session banner (`border-amber-600/60 bg-amber-900/20`) → `border-warning bg-warning-subtle`,
  its text → `text-primary`/`text-warning`.
- **`host/src/pages/game/QuestionPage.tsx` lock button** — locked state `border-amber-500
  text-amber-400 hover:bg-amber-500/10` → `border-warning text-warning hover:bg-warning-subtle`.
- **`host/src/pages/HostShell.tsx`, `admin/src/App.tsx`** — sidebar `bg-slate-900 border-slate-700`
  → `bg-surface border-subtle`; titles `text-white` → `text-primary`; nav: active `bg-selected
  text-primary`, inactive `text-secondary hover:bg-hover hover:text-primary`. The `<main>` beside
  the sidebar gains `pt-6` (toggle clearance, §2.7).
- **`host/src/pages/RosterPage.tsx` dimmed rows** — per the `opacity-*` rule above.
- **`host/src/components/images/ImagePicker.tsx` library tiles** (found during implementation) —
  the selected tile's `ring-2 ring-indigo-500` is a **selection** indicator, not focus, so the
  "remove `ring-indigo-500`" row does not apply to it. Every tile gets `border-2`: selected
  `border-accent`, otherwise `border-transparent hover:border-strong` (replacing
  `hover:ring-2 hover:ring-slate-500`). No ring classes remain; `border-accent` keeps the state
  indicator ≥3:1 (1.4.11), and the constant `border-2` stops the tile shifting between states.
- **Inline `<select>`, `<textarea>`, checkbox class strings in management pages** — same classes as
  `input.tsx` (`bg-raised border border-input text-primary …`); checkboxes `accent-primary`.

### 4.8 Order of work

The user wants one pass; this order keeps each step checkable:

1. ~~Rename the before screenshots (§2.12).~~ Done 2026-09-29.
2. ~~Create `frontend/shared/theme/` files (§4.2) and `scripts/check_theme.py` (§4.3). Run the script:
   the contrast and names checks must pass now (the leftover scan will fail until step 6). Compare
   the printed resolved values with §2.5.~~ Done 2026-09-29.
3. ~~Wire all three apps (§4.4), add `ThemeToggle` (§4.5).~~ Done.
4. ~~Primitives in all three apps (§4.6).~~ Done.
5. ~~Convert pages, one app at a time (§4.7): admin (smallest), player, host.~~ Done 2026-09-30.
6. ~~`check_theme.py` exits 0 (leftovers gone). `npm run build` succeeds in all three apps.~~ Done.
7. Manual verification and audits (§4.9) → `docs/ui/accessibility.md`. **Pending (by hand).**
8. After screenshots (§2.12). **Pending (by hand).**
9. ~~Context docs (§4.11).~~ Done 2026-09-30.
10. ~~T7's screens (§4.12), after `main` is merged in.~~ Done 2026-09-30, except §4.12 step 8's
    by-hand checks, which join step 7.

Commits: group coarsely (e.g. shared theme + script; app conversion; evidence + docs), per the
user's preference. The spec itself is committed before step 1 (T3).

### 4.9 Verification checklist

**Commands (all must pass before the merge request):**
- `python scripts/check_theme.py` → exit 0
- `npm run build` in `frontend/host`, `frontend/player`, `frontend/admin` (runs `tsc -b`, which is
  the only check of the shared preset set-up — CI's `tsc --noEmit` does not cover it)
- `npx tsc --noEmit` in each app (what CI runs)
- `ruff check scripts/` and `ruff format --check scripts/`

**Theme behaviour (each app, dev and once under nginx `:8080` after `npm run build`):**
- Clear site data; with the OS (or Chrome DevTools → Rendering → "Emulate prefers-color-scheme") on
  dark, first load is dark; on light, first load is light.
- With no stored choice, changing the emulated OS scheme switches the open page live.
- Toggle, reload: the choice persists; after toggling, OS changes no longer switch it.
- No flash of the other theme on reload (DevTools → Network throttling "Slow 4G").
- Under nginx (one origin): toggling in admin does **not** change host or player (per-app keys).
- Two tabs of the same app: toggling in one updates the other.
- In a private window with storage blocked (or DevTools → Application → block storage), the app
  still loads in the OS theme and the toggle still switches for the session.
- The toggle is present and covers nothing on every screen below.

**Screen checklist** (each in light **and** dark: Lighthouse/axe audit, keyboard pass, colour-only
check, toggle clearance):
- **Host:** Login; Play (`HomePage`); Games list; Question editor for each question type, including
  the image picker and image library panel; Rosters; Roster detail including the CSV wizard;
  Sessions; game Lobby (QR tile, auto-advance switch on and off); Question (each type, with and
  without a prompt image, locked state); Results (each type, including the fill-in word cloud); Game
  over (answer bars, histogram); the game error screen.
- **Player:** Join; Name (guest, NetID, local modes; "Use a different account"); Lobby (both
  messages); Question — multiple-choice text tiles, image tiles, true/false, fill-in-the-blank,
  multi-select with some options selected; Feedback; Results (correct and incorrect); Game over;
  "Host disconnected" banner; the error screen.
- **Admin:** Login; non-admin notice; Users; User detail (grants); Courses; Guests.
- **States to trigger by hand:** hover on buttons/rows/nav, disabled buttons, form validation
  errors, the locked-question button, image "unavailable" placeholder.
- **Specific checks from the Goldfish review (§6):**
  - Images: a wide and a tall image as player option tiles, and as host game-screen option images.
    The picture is shrink-wrapped, the bands are the themed `bg-raised` (never light bars in
    dark), and a transparent black-line PNG is visible in dark.
  - Host Results with a large room and one answer on an option: the count in the 4% bar is inside
    the fill or clipped, never drawn on the track.
  - Management pages at a ~1280px-wide window: header buttons clear the toggle.
  - Player multiple-choice and multi-select on a ~375px-wide viewport: the timer clears the toggle.
  - Option tiles and true/false buttons: no grey ring around the coloured fill.

### 4.10 Screens added later (e.g. T7)

Any screen added after this work uses only §2.6 classes, only Appendix A pairs, and must pass
`check_theme.py` (its leftover scan covers every file in `src/`). T7's screens have their own plan
in §4.12. A new colour role means: add the token to `tokens.css` (both blocks as needed), map it
in `preset.js`, add its pairs to `contrast-pairs.json`, and re-run the script. **A canvas cannot
use Tailwind classes:** read token values at draw time with
`getComputedStyle(document.documentElement).getPropertyValue('--<token>')` (a channel triple; wrap
it as `rgb(<triple>)`, or `rgb(<triple> / <alpha>)`), and redraw whenever `<html data-theme>`
changes, using `observeTheme` (§4.12). **Not `watchTheme`:** it reports only OS changes and other
tabs, never a toggle click in the same tab, so a canvas redrawn from it stays in the old theme.
(Corrected 2026-09-30; the first version of this section said `watchTheme`.)

### 4.11 Context documents to update

- `frontend/CLAUDE.md` — replace "Styling is Tailwind with hardcoded slate/indigo colours, dark-only,
  no theme layer" with the token system summary; amend "no shared package" to note
  `frontend/shared/theme/` (imported by relative path; no package, no workspace); add the two §2.3
  rules as gotchas.
- New `frontend/shared/CLAUDE.md` — what `theme/` holds, the no-imports rule, the `preset.d.ts`
  sync rule, the `tokens.css` format rule, and `check_theme.py`.
- Each app's `CLAUDE.md` and `src/CLAUDE.md` — remove "assumes a dark background"; mention the
  toggle, the storage key, and the inline script.
- Each app's `src/components/CLAUDE.md` — add `ThemeToggle`; update the `Button`/`Card`/`Input`
  descriptions (token classes, no focus classes). In host and player, note that `SafeImage`
  shrink-wraps a contained image so `bg-media` sits only behind its pixels (§2.9), and that it no
  longer stretches a small image up to fill its box.
- Root `CLAUDE.md` "Scripts" list and `README.md` scripts section — add
  `python scripts/check_theme.py`.

### 4.12 Applying the theme to T7 (Sort It, Spot It)

T7 (`docs/plans/t7-question-types.md`, merged to `main` as `aad922e`) adds two question types,
Sort It (`sort_into_boxes`) and Spot It (`find_in_image`). It was written against the old dark
palette, and its colour constants say "T9 themes it later"; this section is that plan. The figures
come from a trial merge of `main` into `feature/ui-look-and-feel` on 2026-09-30
(`git merge-tree`); step 3 gives the live list after the real merge. **Change colours only:** keep
T7's layout, sizing and behaviour, apart from toggle clearance (§2.7).

**Order of work:**

1. ~~Merge `main` into this branch.~~ Done 2026-09-30.
2. ~~**Resolve the conflicts.**~~ Done 2026-09-30, by the rule below: conflicted hunks took T7's
   markup with T9's colours, T7's `selectClass` constant got T9's classes, and the `CLAUDE.md`
   files kept both sides. The merge had six conflicts: `frontend/CLAUDE.md`,
   `frontend/host/src/CLAUDE.md`, `frontend/player/src/CLAUDE.md`,
   `frontend/player/src/components/CLAUDE.md`, `frontend/host/src/pages/QuestionEditorPage.tsx`,
   and `frontend/host/src/pages/game/GameOverPage.tsx`. Rule: **T7's structure and behaviour, T9's
   colours.** Take T7's markup and logic, then apply §4.7 to every class on the conflicted lines,
   and never keep a palette class from either side. In the `CLAUDE.md` files keep both sides: T7's
   new sections and T9's theme statements.
3. ~~Run `python scripts/check_theme.py`.~~ Done. Its leftover list is the to-do list. After the real merge
   (steps 1–2 done 2026-09-30) it had **132 leftovers in 13 files**: host
   `QuestionEditorPage.tsx` 26, `SortItResults.tsx` 20, `lib/canvasColors.ts` 13,
   `SpotItCanvas.tsx` 9, `ImageLibraryPanel.tsx` 1, `game/ResultsPage.tsx` 1; player
   `SortBoard.tsx` 30, `SpotCanvas.tsx` 9, `game/ResultsPage.tsx` 7, `lib/t7Results.ts` 5,
   `lib/canvasColors.ts` 4, `game/GameOverPage.tsx` 4, `game/QuestionPage.tsx` 3. All three apps
   build at that point, and contrast and names already pass; only the leftover scan fails.
4. ~~Convert the classes with §4.7 and the notes below.~~ Done 2026-09-30.
5. ~~Convert the canvas colours with the canvas table below.~~ Done 2026-09-30.
6. ~~**Search for what the scan misses:** `rgba(`/`rgb(` colour literals and `focus-visible:ring`
   in `frontend/*/src`. After step 5 neither `canvasColors.ts` holds any colour literal.~~ Done:
   the only `rgb(` left wraps a token (`token()`, and the legend's `rgb(var(--heat-N) / 0.55)`).
7. ~~`check_theme.py` exits 0, and `npm run build` and `npx tsc --noEmit` pass in all three
   apps.~~ Done 2026-09-30: 0 leftovers in 92 files; all three apps build and type-check.
8. Add the T7 screens below to `docs/ui/accessibility.md` and check each in light and dark.
   **Screens added 2026-09-30; the checks are pending (by hand, with §4.8 step 7).**
9. ~~Update the context docs that describe the T7 colours (host and player `lib/CLAUDE.md` and
   `components/CLAUDE.md`, which say "T9 themes it later").~~ Done 2026-09-30, plus
   `frontend/CLAUDE.md` and `frontend/shared/CLAUDE.md` (`observeTheme`, the canvas tokens).

**Decided during implementation** (the notes below didn't name these):
- Host `SortItResults`' "Most common wrong box" column → `text-primary`: box names are key
  projector content (§2.10).
- The Spot It editor's "Accepted cells checked…" label → `text-disabled` when its checkbox is
  disabled (the grid doesn't fit the image), per §4.7's `text-slate-500` rule; the reason beneath
  it stays `text-warning`. Its two checkboxes get the management pages' `rounded border-input
  bg-raised accent-primary`.
- The canvas Retry button (host) → `border-input bg-surface/80 text-primary hover:bg-hover`, the
  outline `Button`'s colours over a translucent panel.
- The phone's nudge buttons → `bg-raised text-primary`; its Lock in button → the Submit classes.
- Measured afterwards: an unselected Sort It card inside a receiving box puts `border-input` on
  `bg-accent-subtle`, 3.45 light / **2.85 dark**. Allowed, because the card is a button identified
  by its text (§2.5). Recorded in the accessibility record for the manual pass.

**Class notes** (anything not listed follows §4.7):

- **Player `SortBoard.tsx`:** a selected card, and the drag ghost that follows the finger →
  `border-accent bg-accent text-on-accent`. Unselected cards → `border-input bg-raised
  text-primary`, with no `hover:` (as the other answer controls). The dashed "left out" tray →
  `border-input` (it is a drop target, so it needs 3:1). A box that can receive the selected card
  → `border-accent bg-accent-subtle`, otherwise `border-input bg-raised`, and box names →
  `text-accent`. The "some cards are left out" dialog → `border-warning bg-warning-subtle`, text
  `text-primary`. Its confirm button and the Submit button → `bg-accent text-on-accent
  hover:bg-accent-hover`, and the secondary buttons → the outline `Button` classes (`border
  border-input text-primary hover:bg-hover`).
- **Host `SortItResults.tsx`** (projector): card names, counts and percentages → `text-primary`
  (§2.10), and column headers → `text-secondary`. Row dividers → `border-subtle`. The correct-box
  column → `text-success`, and the most-common-box column → `text-accent`. The accuracy bar →
  track `bg-meter-track`, fill `bg-meter-success`. The same file's `SortItBoard`, which the
  projector's question screen shows: box tiles → `border-accent bg-accent-subtle` with
  `text-accent` names, and card chips → `border-subtle bg-surface text-primary`.
- **Player `lib/t7Results.ts` `TONE_CLASS`:** `full` → `text-success`, `partial` → `text-warning`,
  `none` → `text-danger`, `recorded` → `text-accent`, `missing` → `text-secondary`. Keep full class
  strings there, because Tailwind scans `.ts` files but can't see a class built from fragments.
  Each tone's text must also say the result in words (WCAG 1.4.1); check that it does.
- **Canvas elements with `tabIndex`** (`SpotItCanvas`, `SpotCanvas`): remove `focus:outline-none
  focus-visible:ring-2 focus-visible:ring-indigo-400`. The global `:focus-visible` outline applies
  to a focusable canvas. Check that no `overflow-hidden` parent clips it.
- **Toggle clearance (§2.7):** check that the player Sort It and Spot It screens clear the toggle
  at 375px, and add `pt-16` as the multiple-choice screen has if they don't.

**Canvas colours.** A canvas colour is **themed** when it is drawn on the page-like background
(the grid area around the image, and the phone's grid, which never shows the image). It is
**fixed** when it is drawn over the image, which looks the same in both themes. Fixed colours keep
T7's values exactly and move into `tokens.css`, declared once in `:root` with a source comment and
never redeclared in the dark block (the same pattern as `--bg-media`). Alpha values (0.5, 0.8,
0.55, 0.45, 0.85) stay in `canvasColors.ts` as numbers, because opacity is not a colour.

| Constant (app) | What it draws | Treatment | Becomes |
|---|---|---|---|
| `CANVAS_BACKGROUND` (host, player) | The grid area around the image; the phone's whole grid | Themed | `--bg-raised` (like `SafeImage`'s bands) |
| `HEADER_TEXT` (host) | Column and row labels on the projector | Themed | `--text-primary` (§2.10) |
| `HEADER_TEXT` (player) | Column and row labels on the phone | Themed | `--text-secondary` |
| `PLACEHOLDER_TEXT` (host) | The missing-image message | Themed | `--text-secondary` |
| `CELL_TEXT` (player) | Cell names on the phone's grid | Themed; drop the 0.55 alpha (§4.7) | `--text-secondary` |
| `SELECTED_FILL`, `SELECTED_TEXT`, `SELECTED_EDGE` (player) | The chosen cell (no image under it) | Themed; opaque | `--bg-accent`, `--text-on-accent`, `--text-on-accent` |
| `GRID_DARK`, `GRID_LIGHT` (host, player) | Grid lines, stroked dark then light | Fixed | `--canvas-ink` at 0.5, `--canvas-paper` at 0.8 |
| `TEXT_HALO`, `COUNT_TEXT` (host) | Counts and "Correct" over the heatmap | Fixed | `--canvas-ink` at 0.85, `--canvas-paper` |
| `CORRECT_OUTER`, `CORRECT_INNER` (host) | Outlines of the correct cells | Fixed | `--canvas-ink`, `--canvas-paper` |
| `HEAT_COLORS` (host) | The 5-step viridis ramp at 0.55 | Fixed | `--heat-1` … `--heat-5` |
| `ACCEPTED_FILL`, `ACCEPTED_EDGE` (host editor) | Accepted cells: fill at 0.45, and edge | Fixed | `--canvas-accepted` |
| `CURSOR` (host editor) | The keyboard cursor cell | Fixed | `--canvas-cursor` |

The new fixed tokens, with T7's values: `--canvas-ink` `0 0 0`, `--canvas-paper` `255 255 255`,
`--heat-1` `68 1 84` (#440154), `--heat-2` `59 82 139` (#3b528b), `--heat-3` `33 145 140`
(#21918c), `--heat-4` `94 201 98` (#5ec962), `--heat-5` `253 231 37` (#fde725),
`--canvas-accepted` `34 197 94` (#22c55e), `--canvas-cursor` `129 140 248` (#818cf8). They need no
Appendix A rows: what lies under them is an arbitrary image, and T7 already handles that with
double strokes and a text halo. The themed colours use pairs Appendix A already proves
(`text-primary` and `text-secondary` on `bg-raised`; `text-on-accent` on `bg-accent`). No
`preset.js` change is needed.

**How the canvases read and follow the theme:**

- Each app's `lib/canvasColors.ts` becomes a reader. A small `token(name, alpha = 1)` returns
  `rgb(<triple> / <alpha>)` from `getComputedStyle(document.documentElement)`, and the draw code
  calls it at draw time, never at module load (the theme can change after load). **An unknown
  token name returns an empty string, and the canvas silently keeps its previous `fillStyle`**, so
  `token()` should throw when the value is empty. `check_theme.py` doesn't check token names used
  in TypeScript.
- Add **`observeTheme(onChange: (theme: Theme) => void): () => void`** to
  `frontend/shared/theme/theme.ts`. It uses a `MutationObserver` on `document.documentElement`
  with `attributeFilter: ['data-theme']`, needs no imports (§2.3 rule 1), and returns the
  disconnect function. It sees every theme change, because the toggle, the OS listener and the
  other-tab listener all end in `applyTheme` setting the attribute. Each canvas component holds
  `const [theme, setTheme] = useState(() => document.documentElement.dataset.theme)`, subscribes
  with `useEffect(() => observeTheme(setTheme), [])`, and adds `theme` to its draw effect's
  dependencies.
- The heatmap legend and "Correct" legend in `SpotItCanvas` set colours in inline `style`. They
  must match the canvas exactly, so they keep inline styles but reference tokens
  (`rgb(var(--heat-1) / 0.55)`, `rgb(var(--canvas-paper))`, `rgb(var(--canvas-ink))`) and
  contain no literal colour. This is the only allowed inline colour style.

**Screens to add to `docs/ui/accessibility.md`** (each in light and dark: audit, keyboard pass,
colour-only check, toggle clearance):

- **Host:** question editor for Sort It and for Spot It (accepted cells, keyboard cursor, missing
  image); game Question for each; Results for each (the Sort It table and bars; the Spot It
  heatmap, counts, legend and Correct outlines); the Game over cards for each.
- **Player:** Sort It (a selected card, dragging, a box ready to receive, the "left out" dialog,
  Submit); Spot It (a selected cell, Submit); Results for each tone (full, partial, none,
  recorded, missing); Game over.
- **Specific checks:** toggling the theme with a Spot It canvas open redraws it straight away, with
  no reload. The grid and heatmap read over both a light and a dark image. The phone's grid is
  legible in light. The canvas focus outline is visible and not clipped.

---

## 5. Known limitations (noted, not fixed)

- **Player pinch-zoom is blocked** (`user-scalable=no` in `player/index.html`). This fails WCAG 2.1
  1.4.4 Resize Text (AA). It is a documented deliberate choice (prevents accidental zoom while
  tapping fast) and outside T9's stated accessibility items. Recorded in `docs/ui/accessibility.md`
  as a known limitation.
- **Older Safari draws the focus outline as a rectangle** on rounded elements (outline follows
  `border-radius` from Safari 16.4). Cosmetic only; the outline is still visible.
- **Automated audits cannot judge text over images or canvas content**; the manual checklist covers
  them.
- **The contrast proof covers listed pairs only.** A page combining tokens outside Appendix A is
  caught only by the browser audit (layer 3).
- **The downloadable HTML report** keeps its own styling (not an app page).
- **Contained images are no longer stretched up** (§2.9). An original smaller than its box shows
  at its natural size with wider themed bands. Phones get the 400px small copy, which is larger
  than any tile, so in practice this affects only tiny uploads on the projector.

---

## 6. Goldfish review (2026-09-29)

A fresh session that had not seen the design discussion read this spec with only the context
hierarchy (`frontend/CLAUDE.md` and its app, `src/` and `components/` files) and ran the
comprehension, critic and readiness checks. **Comprehension:** it explained the design back
without contradiction. **Critic:** it recomputed all of Appendix A (every figure matched) and traced
the dark-block cascade (exactly 23 redeclarations, as §4.2 says). Its findings, then checked
against `main`, and where each is now resolved:

| # | Finding | Checked on `main` | Resolved in |
|---|---|---|---|
| A | `bg-media` on an `object-contain` `<img>` paints light bars around non-matching images in dark | Real: player option tiles, host game option images | §2.9, §4.6, §4.9, §5 |
| B | A count inside a short bar could spill onto the track (1.30 / 1.43) | Doesn't happen: `overflow-hidden` clips it; other counts sit outside bars | §2.5 Meters rule, Appendix A failures, §4.9 |
| C | `border-input` on `bg-hover` is 2.80 in dark | Only the player's unselected multi-select row | §2.5 Borders rule, §4.7, Appendix A failures |
| D | Colours outside the palette-class mapping | No scrims, `rgb()`, inline colours or `theme-color`. Found instead: `opacity-*` dimming readable RosterPage rows; option tiles' `border-2` turning grey; true/false buttons unmapped; multi-select image tiles unmentioned | §4.7 (opacity rule, absent list, three new special cases), §2.10 |
| E | Toggle click handler unspecified; the same-tab `storage` event never fires | — | §4.5 |
| F | Preset keys differed from the §2.3 test; hex-scan scope narrower than §2.4 claimed; focus outline unproven on hover/selected/tinted backgrounds; multi-line comments unspecified | — | §2.6, §4.3, Appendix A (+6 rows → 93), §2.8, §2.4 |
| G | Toggle-clearance pages not identified | Seven management headers reach the corner at ~1280px; player MC/multi-select timer runs under it | §2.7, §4.7 |

**Readiness after revision:** implementation can start at §4.8 step 2 with no follow-up questions.

---

## Appendix A — Contrast matrix

WCAG 2.1 contrast ratios, computed from `@radix-ui/colors@3.0.0` values and the §2.5 overrides.
"Need" is 4.5 for text and 3 for non-text (1.4.11). All 93 pairs pass in both themes.
`contrast-pairs.json` contains exactly these pairs. (Recomputed independently from the §2.5 hex
values during the Goldfish review: every figure matches to ±0.01.)

| Foreground | Background | Need | Light | Dark |
|---|---|---|---|---|
| `text-primary` | `bg-canvas` | 4.5 | 15.51 | 16.14 |
| `text-primary` | `bg-surface` | 4.5 | 16.06 | 15.16 |
| `text-primary` | `bg-raised` | 4.5 | 14.43 | 13.73 |
| `text-primary` | `bg-hover` | 4.5 | 13.42 | 12.43 |
| `text-primary` | `bg-selected` | 4.5 | 12.57 | 11.27 |
| `text-secondary` | `bg-canvas` | 4.5 | 5.63 | 8.98 |
| `text-secondary` | `bg-surface` | 4.5 | 5.83 | 8.44 |
| `text-secondary` | `bg-raised` | 4.5 | 5.24 | 7.64 |
| `text-secondary` | `bg-hover` | 4.5 | 4.87 | 6.92 |
| `text-secondary` | `bg-selected` | 4.5 | 4.56 | 6.28 |
| `text-accent` | `bg-canvas` | 4.5 | 5.75 | 10.31 |
| `text-accent` | `bg-surface` | 4.5 | 5.96 | 9.68 |
| `text-accent` | `bg-raised` | 4.5 | 5.35 | 8.77 |
| `text-accent` | `bg-hover` | 4.5 | 4.98 | 7.94 |
| `text-accent` | `bg-selected` | 4.5 | 4.66 | 7.20 |
| `text-danger` | `bg-canvas` | 4.5 | 5.70 | 8.92 |
| `text-danger` | `bg-surface` | 4.5 | 5.90 | 8.38 |
| `text-danger` | `bg-raised` | 4.5 | 5.30 | 7.59 |
| `text-danger` | `bg-hover` | 4.5 | 4.93 | 6.88 |
| `text-danger` | `bg-selected` | 4.5 | 4.62 | 6.24 |
| `text-success` | `bg-canvas` | 4.5 | 5.74 | 9.90 |
| `text-success` | `bg-surface` | 4.5 | 5.94 | 9.30 |
| `text-success` | `bg-raised` | 4.5 | 5.34 | 8.42 |
| `text-success` | `bg-hover` | 4.5 | 4.97 | 7.63 |
| `text-success` | `bg-selected` | 4.5 | 4.65 | 6.92 |
| `text-warning` | `bg-canvas` | 4.5 | 5.75 | 12.28 |
| `text-warning` | `bg-surface` | 4.5 | 5.95 | 11.54 |
| `text-warning` | `bg-raised` | 4.5 | 5.35 | 10.45 |
| `text-warning` | `bg-hover` | 4.5 | 4.97 | 9.46 |
| `text-warning` | `bg-selected` | 4.5 | 4.66 | 8.58 |
| `text-primary` | `bg-accent-subtle` | 4.5 | 14.76 | 12.64 |
| `text-secondary` | `bg-accent-subtle` | 4.5 | 5.35 | 7.03 |
| `text-accent` | `bg-accent-subtle` | 4.5 | 5.47 | 8.07 |
| `text-primary` | `bg-danger-subtle` | 4.5 | 14.30 | 14.01 |
| `text-secondary` | `bg-danger-subtle` | 4.5 | 5.19 | 7.80 |
| `text-danger` | `bg-danger-subtle` | 4.5 | 5.26 | 7.75 |
| `text-primary` | `bg-success-subtle` | 4.5 | 14.70 | 12.90 |
| `text-secondary` | `bg-success-subtle` | 4.5 | 5.34 | 7.18 |
| `text-success` | `bg-success-subtle` | 4.5 | 5.44 | 7.91 |
| `text-primary` | `bg-warning-subtle` | 4.5 | 15.10 | 13.49 |
| `text-secondary` | `bg-warning-subtle` | 4.5 | 5.48 | 7.51 |
| `text-warning` | `bg-warning-subtle` | 4.5 | 5.60 | 10.26 |
| `text-on-accent` | `bg-accent` | 4.5 | 6.08 | 6.12 |
| `text-on-accent` | `bg-accent-hover` | 4.5 | 7.36 | 7.13 |
| `text-on-danger` | `bg-danger` | 4.5 | 5.21 | 5.21 |
| `text-on-danger` | `bg-danger-hover` | 4.5 | 6.74 | 6.74 |
| `text-on-success` | `bg-success` | 4.5 | 5.07 | 5.07 |
| `text-on-success` | `bg-success-hover` | 4.5 | 6.57 | 6.57 |
| `text-on-warning` | `bg-warning` | 4.5 | 10.39 | 11.92 |
| `text-on-warning` | `bg-warning-hover` | 4.5 | 9.60 | 13.32 |
| `text-on-media` | `bg-media` | 4.5 | 16.06 | 16.06 |
| `text-on-option-1` | `bg-option-1` | 4.5 | 4.92 | 4.92 |
| `text-on-option-2` | `bg-option-2` | 4.5 | 5.03 | 5.87 |
| `text-on-option-3` | `bg-option-3` | 4.5 | 12.97 | 12.97 |
| `text-on-option-4` | `bg-option-4` | 4.5 | 5.41 | 6.25 |
| `text-on-option-5` | `bg-option-5` | 4.5 | 4.61 | 4.61 |
| `text-on-option-6` | `bg-option-6` | 4.5 | 5.52 | 6.53 |
| `text-on-option-7` | `bg-option-7` | 4.5 | 4.57 | 4.57 |
| `text-on-option-8` | `bg-option-8` | 4.5 | 5.34 | 6.22 |
| `ring-focus` | `bg-canvas` | 3 | 4.32 | 6.12 |
| `ring-focus` | `bg-surface` | 3 | 4.47 | 5.75 |
| `ring-focus` | `bg-raised` | 3 | 4.01 | 5.21 |
| `ring-focus` | `bg-hover` | 3 | 3.73 | 4.72 |
| `ring-focus` | `bg-selected` | 3 | 3.50 | 4.28 |
| `ring-focus` | `bg-accent-subtle` | 3 | 4.10 | 4.79 |
| `ring-focus` | `bg-danger-subtle` | 3 | 3.98 | 5.31 |
| `ring-focus` | `bg-success-subtle` | 3 | 4.09 | 4.89 |
| `ring-focus` | `bg-warning-subtle` | 3 | 4.20 | 5.12 |
| `border-input` | `bg-canvas` | 3 | 3.63 | 3.64 |
| `border-input` | `bg-surface` | 3 | 3.76 | 3.42 |
| `border-input` | `bg-raised` | 3 | 3.37 | 3.09 |
| `bg-accent` | `bg-canvas` | 3 | 5.75 | 6.12 |
| `bg-surface` | `bg-accent` | 3 | 5.96 | 5.75 |
| `bg-meter-success` | `bg-meter-track` | 3 | 4.65 | 6.92 |
| `bg-meter-success` | `bg-surface` | 3 | 5.94 | 9.30 |
| `bg-meter-success` | `bg-canvas` | 3 | 5.74 | 9.90 |
| `text-on-meter` | `bg-meter-success` | 4.5 | 6.07 | 9.90 |
| `bg-meter-warning` | `bg-meter-track` | 3 | 4.66 | 8.58 |
| `bg-meter-warning` | `bg-surface` | 3 | 5.95 | 11.54 |
| `bg-meter-warning` | `bg-canvas` | 3 | 5.75 | 12.28 |
| `text-on-meter` | `bg-meter-warning` | 4.5 | 6.08 | 12.28 |
| `bg-meter-danger` | `bg-meter-track` | 3 | 4.62 | 6.24 |
| `bg-meter-danger` | `bg-surface` | 3 | 5.90 | 8.38 |
| `bg-meter-danger` | `bg-canvas` | 3 | 5.70 | 8.92 |
| `text-on-meter` | `bg-meter-danger` | 4.5 | 6.03 | 8.92 |
| `bg-meter-accent` | `bg-meter-track` | 3 | 4.66 | 7.20 |
| `bg-meter-accent` | `bg-surface` | 3 | 5.96 | 9.68 |
| `bg-meter-accent` | `bg-canvas` | 3 | 5.75 | 10.31 |
| `text-on-meter` | `bg-meter-accent` | 4.5 | 6.08 | 10.31 |
| `bg-meter-neutral` | `bg-meter-track` | 3 | 4.56 | 6.28 |
| `bg-meter-neutral` | `bg-surface` | 3 | 5.83 | 8.44 |
| `bg-meter-neutral` | `bg-canvas` | 3 | 5.63 | 8.98 |
| `text-on-meter` | `bg-meter-neutral` | 4.5 | 5.95 | 8.98 |

The switch knob uses `bg-surface` against `bg-accent` (on) and `bg-meter-neutral` (off); both rows
above (`bg-surface`/`bg-accent`, and `bg-meter-neutral`/`bg-surface`) clear 3:1.

**Measured failures — combinations the UI must never produce.** These are not in
`contrast-pairs.json`, since the script would fail on them. They are the reason for the rules
cited:

| Foreground | Background | Need | Light | Dark | Rule |
|---|---|---|---|---|---|
| `text-on-meter` | `bg-meter-track` | 4.5 | 1.30 | 1.43 | Counts stay inside the fill (§2.5 Meters) |
| `border-input` | `bg-hover` | 3 | 3.14 | 2.80 | No hover fill on or under a bordered control (§2.5 Borders) |
| `border-input` | `bg-selected` | 3 | 2.94 | 2.54 | Same |
