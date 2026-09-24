# frontend/host/src/components

## Purpose

Presentational components with no game logic. Everything currently lives in `ui/` — a small,
hand-rolled shadcn-style primitive set plus one game-specific widget.

## Contents

All files are under `ui/`:

- `button.tsx` — `Button`. Four variants (`default`, `outline`, `ghost`, `destructive`) and three
  sizes (`sm`, `md`, `lg`), composed through `cn` so `className` overrides cleanly. Spreads the
  rest of the native button props.
- `card.tsx` — `Card`, `CardHeader`, `CardContent`. Dark slate panel with a border and shadow.
- `input.tsx` — `Input`. A styled native input; props pass straight through.
- `TimerBar.tsx` — the countdown bar. Takes `totalSeconds`, optional `initialSeconds` (for
  resuming mid-question after a reconnect), and `paused`. Renders a proportional bar that shifts
  green → yellow → red at 60% and 30% remaining, with the integer seconds beside it.

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
  the same fix in `frontend/admin/src/components/ui/`.
- `<select>`, `<textarea>`, and checkbox styling is repeated inline in the management pages (the
  same long class string at each site); there's no primitive for them.
- There is no `components/index.ts` barrel; pages import each component by its full path.
- The `ui/` nesting is the only level of structure here — if game-specific components multiply,
  they'd be better in a sibling folder than mixed into `ui/`.
