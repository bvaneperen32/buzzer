# frontend/shared

## Purpose

The one thing the three frontend apps share: the colour theme (T9). It holds every colour value in
the frontend, the Tailwind mapping from class names to those colours, and the light/dark switching
logic. It is a plain folder, not a package: no `package.json`, no `node_modules`, no build step.
Each app imports these files by relative path. The design, and the reasons behind it, are in
[docs/plans/t9-ui-theming.md](../../docs/plans/t9-ui-theming.md).

## Contents

`theme/`:

- `tokens.css` — **the only file in the frontend that contains colour values.** Two layers:
  Radix Colors 3.0.0 primitives (`--sage-1` … `--pink-12`, never used by components) and
  role-named semantic tokens (`--bg-surface`, `--text-primary`, `--border-input`, …). Every value
  is a channel triple (`251 253 252`), so Tailwind's opacity modifier works (`bg-surface/90`).
  It also holds fixed tokens that are the same in both themes: `--bg-media`/`--text-on-media`, and
  T7's Spot It canvas colours (`--canvas-ink`, `--canvas-paper`, `--heat-1` … `--heat-5`,
  `--canvas-accepted`, `--canvas-cursor`), which only canvases read and `preset.js` doesn't map.
- `base.css` — `color-scheme`, the page's background and text colour on `html`/`body`, and the
  single global focus style: `html :focus-visible` draws a 2px `--ring-focus` outline, offset
  2px.
- `preset.js` — the Tailwind preset. It **replaces** Tailwind's palette and maps semantic tokens
  per property (`backgroundColor`, `textColor`, `borderColor`, `ringColor`, `outlineColor`,
  `accentColor`), each as `rgb(var(--token) / <alpha-value>)`. So `bg-surface` and `text-primary`
  exist, and `bg-slate-800` and `text-white` no longer do.
- `preset.d.ts` — hand-written types for `preset.js`.
- `theme.ts` — framework-free theme logic: `storedTheme`, `systemTheme`, `currentTheme`,
  `applyTheme`, `setTheme`, `watchTheme`, `observeTheme`. No React; each app's `ThemeToggle` wraps
  it, and the Spot It canvases use `observeTheme` to redraw.
- `contrast-pairs.json` — every foreground/background pair the UI is allowed to use, with its
  WCAG minimum (4.5 text, 3 non-text). `scripts/check_theme.py` checks them against `tokens.css`.

## How it fits in

Each app wires the theme in at four points:

- `src/index.css` starts with `@import '../../shared/theme/tokens.css';` and `base.css`, before
  the `@tailwind` directives.
- `tailwind.config.ts` has `presets: [preset]` (from `../shared/theme/preset.js`).
- `index.html` has an inline script in `<head>` that sets `<html data-theme="light|dark">` before
  first paint: the stored choice, else the OS preference. It repeats `currentTheme`'s rule,
  because it runs before any module loads.
- `src/components/ui/ThemeToggle.tsx` imports `theme.ts`. `App.tsx` renders the toggle once,
  outside the routes, fixed top-right on every screen.

**`<html data-theme>` is the source of truth**, and each app stores the choice under its own
`localStorage` key: `buzzer-theme-host`, `buzzer-theme-player`, `buzzer-theme-admin`. In
production all three share one origin, so separate keys stop a toggle in admin from changing the
projector. Each key appears twice per app, in `index.html` and in `ThemeToggle.tsx`, and the two
must match.

## Rules

1. **Nothing in `shared/` may import a package** — not `react`, not even `tailwindcss` types.
   There is no `node_modules` above this folder, so the import can't resolve. That is why
   `preset.d.ts` is hand-written and why each app has its own React `ThemeToggle`.
2. **Keep `preset.d.ts` in step with `preset.js`**, and keep `preset.js` to static objects of
   strings. A `.ts` preset was tried: it fails `tsc -b` or emits stray `.js` output into this
   folder (spec §2.3).
3. **`tokens.css` has a strict format**, because `check_theme.py` parses it (spec §2.4): exactly
   two blocks, `:root` (light, and the fallback) then `:root[data-theme="dark"]` (only what
   differs in dark). There is one `--name: value;` per line, and the value is a channel triple or
   `var(--name)`. A semantic token is `var(--primitive)` when it is a Radix step in the current
   theme. Otherwise it is a literal with a source comment: an override that fixes a WCAG failure,
   white, or a value from the other theme's scale.
4. **CI does not protect this set-up.** `.gitlab-ci.yml` runs `tsc --noEmit`, which passes shared
   set-ups that fail `tsc -b`. After changing anything here, run `npm run build` in all three apps.

## Checking

```bash
python scripts/check_theme.py        # .venv\Scripts\python scripts\check_theme.py on Windows
```

It resolves every token in both themes and prints the values. It then checks every pair in
`contrast-pairs.json`, confirms each token name that `preset.js`, `base.css` and the pairs file
use exists, and scans app source, `index.css`, `index.html`, `tailwind.config.ts` and the other
`theme/` files for leftover palette classes, focus-suppressing classes, `dark:` variants and hex colours. It
exits 1 on any failure. A genuine false positive is silenced with `theme-check: allow` on the same
line.

**Adding a colour role:** add the token to `tokens.css` (the dark block too, if it differs), map
it in `preset.js`, add its pairs to `contrast-pairs.json`, and re-run the script. **A canvas can't
use Tailwind classes:** read the token at draw time with
`getComputedStyle(document.documentElement).getPropertyValue('--<token>')`, wrap it as
`rgb(<triple>)`, and redraw whenever `<html data-theme>` changes. Subscribe with
**`observeTheme`**, a `MutationObserver` on the attribute, which sees every change: the toggle,
the OS and other tabs. **Don't use `watchTheme` for that:** it reports only OS changes and other
tabs, never a toggle click in the same tab. The T7 Spot It canvases show the pattern: each app's
`lib/canvasColors.ts` reads tokens, and the component keeps the theme in state, subscribes with
`useEffect(() => observeTheme(setTheme), [])`, and lists `theme` in its draw effect's
dependencies. A token that only a canvas reads (not mapped in `preset.js`) isn't covered by the
names check, which is why `token()` throws on an unknown name.

## Gotchas

- **A class the preset doesn't define renders with no colour and no error.** A leftover
  `bg-slate-800` or a typo such as `bg-surfce` builds fine and paints nothing. `check_theme.py`
  finds old palette classes, not misspelled new ones.
- **In the dark block, `var(--red-11)` means the *dark* red-11**, because that block redefines
  every primitive. To use a light-scale value in dark (or the reverse), write it as a literal with
  a comment (e.g. dark `--bg-danger: 206 44 49; /* #ce2c31 light red-11 */`).
- **A config change needs a dev-server restart *and* a hard refresh.** Tailwind loads each app's
  gitignored `tailwind.config.js` before the `.ts` (see [CLAUDE.md](../CLAUDE.md)). An open tab
  can also keep CSS from before a restart. Either way, every token class is missing while
  `base.css` still themes the page, so it looks half-themed: correct page colours, colourless
  buttons, bars and status text. Restart `npm run dev`, then press Ctrl+Shift+R.
- **Answer-option tiles (player only) all carry the same dark text** in both themes. Red, purple
  and pink use lighter fills than Radix's step 11 so that dark text passes 4.5:1. A new tile
  colour must pass with `#1a211e`, not white.
- Don't add `focus:ring-*`, `focus:outline-none` or `outline-none` anywhere. They would suppress
  or duplicate the global outline in `base.css`, and the leftover scan flags them.
