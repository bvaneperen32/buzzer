# frontend/admin/src/components

## Purpose

Presentational primitives, all under `ui/`. The smallest components folder of the three apps —
admin has no equivalent of `TimerBar` because nothing here is real-time.

## Contents

- `button.tsx` — `Button`. Four variants (`default`, `outline`, `ghost`, `destructive`), three
  sizes. **Byte-identical to the host app's copy.**
- `card.tsx` — `Card`, `CardHeader`, `CardContent`. Byte-identical to the host app's copy.
- `input.tsx` — `Input`, a styled native input. Byte-identical to the host app's copy.

## How it fits in

Every admin page is built from these three plus raw elements. Note what *isn't* here: the app
uses bare `<select>`, `<textarea>`, and `<input type="checkbox">` inline in pages, with the
Tailwind classes copy-pasted at each site — there is no `Select`, `Textarea`, or `Checkbox`
primitive. The same long select class string is repeated in `UserDetailPage` here and across the
host app's management pages (which moved there from this app).

Icons come from `lucide-react`, imported per page rather than wrapped.

## Gotchas

- **These are exact copies of the host app's components, not a shared package.** They have not
  diverged (unlike the player app's touch-tuned versions), which makes it easy to forget they're
  three separate files. A fix here needs the same fix in `frontend/host/src/components/ui/`.
- **`<textarea>` and `<select>` styling is duplicated inline across pages.** If any of them needs
  to change, it has to change at every call site — this is the most likely place for visual drift
  within the app, and the obvious next primitive to extract.
- `Button` renders a native `<button>` with no `type` default, so **a `Button` inside a `<form>`
  defaults to `type="submit"`**. Several pages rely on this for their submit buttons and pass
  `type="button"` explicitly on Cancel. Omitting it on a new Cancel button will submit the form.
- The admin UI is desktop-only: no touch sizing, no responsive breakpoints, and pages assume a
  wide viewport beside the fixed 224px sidebar.
- There is no barrel file; pages import each component by full path.
