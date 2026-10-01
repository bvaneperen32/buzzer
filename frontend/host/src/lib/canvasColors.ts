// The Spot It canvases' colours (T9 spec §4.12). A canvas can't use Tailwind classes, so it reads
// the theme's CSS variables at draw time — never at module load, because the theme can change
// after load — and redraws when observeTheme reports a change. What is drawn on the grid area
// follows the theme; what is drawn over the image uses fixed tokens, because the image looks the
// same in both themes. Only the alphas live here: opacity is not a colour.

/**
 * A token as a canvas colour, `rgb(<channels> / <alpha>)`, from the active theme. Throws on an
 * unknown name: it would read as '', and the canvas would silently keep its previous colour.
 */
export function token(name: string, alpha = 1): string {
  const channels = getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();
  if (!channels) throw new Error(`Unknown colour token --${name}`);
  return `rgb(${channels} / ${alpha})`;
}

/** The heat ramp (viridis, a colour-blind-safe sequential ramp; T7 spec §2.4) is drawn at 55%. */
export const HEAT_OPACITY = 0.55;

/** Every colour the host's Spot It canvas draws with, read from the active theme. Call per draw. */
export function canvasColors() {
  return {
    /** The grid area around the image, themed like SafeImage's bands. */
    background: token('bg-raised'),
    /** Column letters and row numbers: key projector content (T9 spec §2.10). */
    headerText: token('text-primary'),
    placeholderText: token('text-secondary'),
    /** Grid lines are drawn twice, dark then light, so they read over any image. */
    gridDark: { color: token('canvas-ink', 0.5), width: 3 },
    gridLight: { color: token('canvas-paper', 0.8), width: 1 },
    /** "Correct" outlines on the results heatmap: black outside, white inside. */
    correctOuter: token('canvas-ink'),
    correctInner: token('canvas-paper'),
    countText: token('canvas-paper'),
    textHalo: token('canvas-ink', 0.85),
    /** Step 1 first; drawn at HEAT_OPACITY. */
    heat: [1, 2, 3, 4, 5].map((step) => token(`heat-${step}`)),
    /** The editor's accepted cells, and its keyboard cursor. */
    acceptedFill: token('canvas-accepted', 0.45),
    acceptedEdge: token('canvas-accepted'),
    cursor: token('canvas-cursor'),
  };
}
