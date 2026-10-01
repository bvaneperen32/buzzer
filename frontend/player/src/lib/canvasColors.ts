// The phone's Spot It canvas colours (T9 spec §4.12). A canvas can't use Tailwind classes, so it
// reads the theme's CSS variables at draw time — never at module load, because the theme can
// change after load — and redraws when observeTheme reports a change. The phone never shows the
// image, so its grid follows the theme; only the grid lines keep the projector's fixed double
// stroke. Only the alphas live here: opacity is not a colour.

/**
 * A token as a canvas colour, `rgb(<channels> / <alpha>)`, from the active theme. Throws on an
 * unknown name: it would read as '', and the canvas would silently keep its previous colour.
 */
export function token(name: string, alpha = 1): string {
  const channels = getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();
  if (!channels) throw new Error(`Unknown colour token --${name}`);
  return `rgb(${channels} / ${alpha})`;
}

/** Every colour the phone's Spot It canvas draws with, read from the active theme. Call per draw. */
export function canvasColors() {
  return {
    background: token('bg-raised'),
    /** Grid lines are drawn twice, dark then light, as on the projector. */
    gridDark: { color: token('canvas-ink', 0.5), width: 3 },
    gridLight: { color: token('canvas-paper', 0.8), width: 1 },
    headerText: token('text-secondary'),
    cellText: token('text-secondary'),
    /** The chosen cell: opaque, so its label keeps the proven text-on-accent contrast. */
    selectedFill: token('bg-accent'),
    selectedEdge: token('text-on-accent'),
    selectedText: token('text-on-accent'),
  };
}
