# frontend/player/src/components

## Purpose

Presentational components with no game logic, all under `ui/`. This is the host app's primitive
set retuned for touch — same API, larger targets.

## Contents

- `button.tsx` — `Button`, with the same four variants and three sizes as the host's. Differences
  are all touch-related: `rounded-xl`, `active:scale-95` for press feedback, and taller padding
  (`md` is `px-5 py-3` here vs `px-4 py-2` in host).
- `card.tsx` — `Card`, `CardHeader`, `CardContent`. `rounded-2xl` and slightly tighter padding
  than the host's (`px-5` vs `px-6`).
- `input.tsx` — `Input`. `rounded-xl`, `px-4 py-3`, explicit `text-base` — the last one matters:
  iOS Safari auto-zooms the viewport on focus for any input under 16px.
- `TimerBar.tsx` — countdown bar. Takes `totalSeconds` and `paused`. Thinner than the host's
  (`h-4` vs `h-6`) with a smaller seconds readout, and the same green → yellow → red thresholds
  at 60% and 30%.

## How it fits in

Dumb components: they never read `useGame()`, call `api`, or navigate. Note that the answer
controls are *not* here — the multiple-choice tiles, True/False buttons, text field, and
multi-select checklist are all inline in `pages/game/QuestionPage.tsx`, because each is tightly
coupled to its submit logic.

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
