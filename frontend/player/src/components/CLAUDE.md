# frontend/player/src/components

## Purpose

Presentational components with no game logic, all under `ui/`. This is the host app's primitive
set retuned for touch — same API, larger targets.

## Contents

- `button.tsx` — `Button`, with the same four variants, three sizes and colour tokens as the
  host's (`default` `bg-accent`, `outline` `border-input`, `ghost` `text-secondary`, `destructive`
  `bg-danger`; no focus classes). Differences are all touch-related: `rounded-xl`,
  `active:scale-95` for press feedback, and taller padding (`md` is `px-5 py-3` here vs `px-4 py-2`
  in host).
- `card.tsx` — `Card`, `CardHeader`, `CardContent`. The host's `bg-surface` / `border-subtle`
  panel, with `rounded-2xl` and slightly tighter padding (`px-5` vs `px-6`).
- `input.tsx` — `Input`. The host's colours (`bg-raised`, `border-input`, accent border on focus)
  with `rounded-xl`, `px-4 py-3`, explicit `text-base` — the last one matters: iOS Safari
  auto-zooms the viewport on focus for any input under 16px.
- `TimerBar.tsx` — countdown bar. Takes `totalSeconds` and `paused`. Thinner than the host's
  (`h-4` vs `h-6`) with a smaller seconds readout, and the same meter colours (`meter-success` →
  `meter-warning` → `meter-danger` on `bg-meter-track`) and thresholds, 60% and 30%.
- `ThemeToggle.tsx` (T9) — the light/dark switch, fixed top-right. The host's component with key
  `buzzer-theme-player` (must match the `index.html` script) and a 44px touch target (host and
  admin: 40px).
- `SafeImage.tsx` (T8) — the phone's copy of the host's image component, deliberately simpler:
  links arrive only in `new_question`, so there's nothing to refresh. On the first error it
  remounts once with the same link (valid for at least 2 hours after it was sent); on the
  second, "Image unavailable" with the description. No Retry button. The element stays
  transparent until it loads, so no broken-image icon. Its retry state is per instance, so
  `QuestionPage` keys each one by its link: a tile that keeps its position across questions
  must not inherit the previous image's failure. **Images are shrink-wrapped** (T9): the wrapper
  centres a `max-h-full max-w-full` `<img>` rather than using `object-contain`, so the light
  `bg-media` sits only behind the picture. A tile's empty bands show the themed `bg-raised`, and
  an image smaller than its tile is no longer stretched up to fill it.

`SortBoard.tsx` and `SpotCanvas.tsx` (T7, beside `ui/`) are the two new answer surfaces:

- `SortBoard` — Sort It: the card pool above the boxes. Drag a card (pointer events with
  capture, an 8 px threshold, `touch-action: none` on the chips only, the drop target by
  `elementFromPoint`) or tap a card, then a box; a placed card can move or go back to the pool.
  Cards and boxes are buttons ("Card Frog, in box Amphibian"), and a polite live region announces
  moves. Submit asks before leaving cards out. **Taps select on `click`, not `pointerup`:**
  selecting re-renders (a "Return to pool" button appears) before the browser's click, which
  would then land on whatever moved under the finger. The click that ends a drag is ignored.
- `SpotCanvas` — Spot It: the grid, labels and the image's proportions on a `<canvas>`, never
  the image. It fits the width and `.spot-height-cap` (55% of the small viewport), with 20 px
  header bands. A tap or drag selects a cell (header taps are ignored); the arrow keys and the
  always-visible nudge buttons move the selection, so every cell is reachable at any size. Cell
  labels are drawn only when cells are ≥ 28 px; in landscape, below 24 px, it suggests turning the
  phone upright. It exposes `data-grid-*` attributes for the browser checks. It keys the grid on
  its values, because the page passes a new grid object on every render: a lock or unlock
  re-renders it without redrawing the canvas.

Both take a `disabled` prop that never clears their state.

## How it fits in

Dumb components: they never read `useGame()`, call `api`, or navigate. Note that the original
four types' answer controls are *not* here — the multiple-choice tiles, True/False buttons, text
field, and multi-select checklist are all inline in `pages/game/QuestionPage.tsx`, because each is
tightly coupled to its submit logic. T7's two are components, because each carries real
interaction state. All styling is Tailwind token classes from the shared theme
([frontend/shared/CLAUDE.md](../../../shared/CLAUDE.md)). `SpotCanvas` draws on a canvas, which
can't use classes: it reads its colours from the tokens at draw time (`lib/canvasColors.ts`) and
redraws when `observeTheme` reports a theme change. Its grid follows the theme (the phone never
shows the image); only the grid lines keep the projector's fixed dark-then-light double stroke.
`SortBoard`'s cards carry no `hover:`, like the other answer controls: hover sticks after a tap.

## Gotchas

- **`TimerBar` here has no `initialSeconds` prop**, unlike the host's. It always counts down from
  `totalSeconds`, so it cannot resume mid-question on its own. The server compensates: the
  late-join `NEW_QUESTION` push overwrites `timeLimitSeconds` with the *remaining* seconds, so a
  reconnecting player gets a correct-looking timer. That coupling is invisible from this file —
  the bar is only right because the server lies to it on purpose.
- **`key={questionId}` at the call site is load-bearing**, same as in the host app. The main
  effect has an empty dependency array and reads props once at mount; without the remount every
  question after the first inherits the previous countdown. `QuestionPage` passes it on all four
  branches.
- The timer uses wall-clock deltas against `Date.now()` rather than decrementing a counter, so it
  survives tab throttling — which is common on phones with the screen locked. Keep that property.
- `totalSeconds <= 0` returns early from the effect and renders a static bar.
- **These are copies of the host's components that have already diverged.** Same names, same
  props, different sizing — and `TimerBar` differs in its actual prop *interface*. Don't assume a
  fix in one app applies to the other, and don't "resync" them without checking whether the
  difference is the deliberate touch tuning.
- There is no barrel file; pages import each component by full path.
