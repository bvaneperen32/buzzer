# frontend/host/src/components

## Purpose

Presentational components with no game logic. `ui/` holds a small hand-rolled shadcn-style
primitive set plus two widgets (`TimerBar`, `SafeImage`); `images/` holds the question editor's
image components (T8).

## Contents

`ui/`:

- `button.tsx` — `Button`. Four variants (`default`, `outline`, `ghost`, `destructive`) and three
  sizes (`sm`, `md`, `lg`), composed through `cn` so `className` overrides cleanly. Spreads the
  rest of the native button props.
- `card.tsx` — `Card`, `CardHeader`, `CardContent`. Dark slate panel with a border and shadow.
- `input.tsx` — `Input`. A styled native input; props pass straight through.
- `TimerBar.tsx` — the countdown bar. Takes `totalSeconds`, optional `initialSeconds` (for
  resuming mid-question after a reconnect), and `paused`. Renders a proportional bar that shifts
  green → yellow → red at 60% and 30% remaining, with the integer seconds beside it.
- `SafeImage.tsx` (T8) — every image the host app shows. A loading placeholder, then the spec's
  failure sequence: on the first error `await refresh()` (if given) and retry once by remounting
  the element with the current `src`; on the second, "Image unavailable" with the description,
  plus Retry if `showRetry` (Retry starts the sequence again, as does a successful load). The element stays transparent until
  it loads, so the browser's broken-image icon never shows. `src={null}` shows the unavailable
  placeholder straight away (a link that couldn't be fetched), and a link that arrives later
  still loads. `compact` is for thumbnails too small for words: the placeholder is an icon whose
  accessible name and tooltip carry "Image unavailable" and the description, and with
  `showRetry` the whole box is the Retry button.

`images/` (T8):

- `ImageLibraryPanel.tsx` — the editor's Images panel: meters (image count, library budget,
  export size), a live-session banner that disables Replace and Delete, upload with a required
  description, and per-image Replace (one hidden file input), Edit description (inline), and
  Delete (two-step inline confirm), each showing "Used by Q2 (prompt), Q5 (option B)" or
  "Unused" from the server.
- `ImagePicker.tsx` — pick an image from the library, "No image", or upload one (it goes into the
  library and is selected). A reference the library doesn't have shows "Missing image" — the
  server refuses to save it until the author picks another image or none.

Both refuse a file over the 10 MiB input limit before sending it, and both are presentational: the
page owns the library (`useImageLibrary`) and passes the calls in. T7: the panel labels a canvas
use "Q3 (Spot It)", and Replace on an image a Spot It question uses first warns that the question
will need review.

`question/` (T7):

- `SpotItCanvas.tsx` — a Spot It grid on a `<canvas>`, in three modes: `question` (the projector's
  full image with a transparent labelled grid), `results` (the heatmap — heat fills, counts,
  "Correct" outlines) and `author` (the editor, where a click or Enter toggles a cell). The backing
  store follows the device pixel ratio and it redraws on resize, a pixel-ratio change and state
  change only. Image failures follow T8's sequence (refresh the links, retry once, then "Image
  unavailable — {description}" drawn on the grid with a Retry button); play is never blocked. It
  exposes `data-grid-cols`, `data-grid-rows` and `data-grid-rect` for automated checks. Also
  `HeatLegend`.
- `SortItResults.tsx` — `SortItResults` (the per-card table from `lib/sortResults.ts`, each row
  carrying its numbers as data attributes) and `SortItBoard` (the projector's boxes and cards
  during a question).

## How it fits in

The three primitives are generic and used by every page — the game screens and the management
pages alike.
`TimerBar` is game-specific but purely presentational — it receives seconds and a paused flag and
owns no game state. All styling is Tailwind; there is no CSS module or styled-component here.

These components are *dumb by design*: they never read `useGame()`, never call `api`, and never
navigate. Anything needing game state belongs in [pages/](../pages/CLAUDE.md).

## Gotchas

- **`TimerBar` reads its props only once, at mount.** The main `useEffect` has an empty dependency
  array, so changing `totalSeconds` or `initialSeconds` on a mounted instance does nothing.
  `QuestionPage` compensates with `key={currentQuestion.questionId}`, which forces a remount per
  question — **that `key` is load-bearing**. Remove it and every question after the first inherits
  the previous question's countdown.
- The timer is driven by wall-clock deltas against `Date.now()`, not by decrementing a counter, so
  it stays accurate if the tab is throttled or backgrounded. Keep that property if you refactor.
- Pause works by continuously sliding `endTimeRef` forward while `paused` is true. It's correct
  but means the component holds three refs (`pausedRef`, `endTimeRef`, `frozenRef`) that must stay
  consistent — change one path and check the other two.
- `totalSeconds <= 0` returns early from the effect, so a zero or negative time limit renders a
  static bar with no countdown at all.
- `Button` builds its classes with chained `&&` conditionals rather than a variants library. It's
  fine at this size, but a fifth variant is the point to reach for `cva` instead.
- `Button` renders a native `<button>` with no `type` default, so **a `Button` inside a `<form>`
  defaults to `type="submit"`**. The management pages' create and edit forms rely on this for
  their submit buttons and pass `type="button"` explicitly on Cancel. Omitting it on a new Cancel
  button will submit the form.
- `Button`, `Card`, and `Input` are byte-identical to the admin app's copies — a fix here needs
  the same fix in `frontend/admin/src/components/ui/`. `SafeImage` has a deliberately *different*
  copy in the player app (no refresh, no Retry); don't sync them.
- **Key a `SafeImage` by image id** wherever the same element could show a different image: its
  retry counter is per instance, and a link change for the *same* image (a refresh) must not reset
  it, or a broken image would refresh forever.
- `<select>`, `<textarea>`, and checkbox styling is repeated inline in the management pages (the
  same long class string at each site); there's no primitive for them.
- There is no `components/index.ts` barrel; pages import each component by its full path.
- The image components live in `images/`, a sibling of `ui/`, as game-specific components
  should.
