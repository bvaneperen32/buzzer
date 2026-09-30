// Colours the Spot It canvases draw with, in one place (T9 themes them later).

/** The heat ramp: viridis stops, a colour-blind-safe sequential ramp (spec §2.4). */
export const HEAT_COLORS = ['#440154', '#3b528b', '#21918c', '#5ec962', '#fde725'] as const;
export const HEAT_OPACITY = 0.55;

/** Grid lines are drawn twice, dark then light, so they read over any image. */
export const GRID_DARK = { color: 'rgba(0, 0, 0, 0.5)', width: 3 };
export const GRID_LIGHT = { color: 'rgba(255, 255, 255, 0.8)', width: 1 };

/** "Correct" outlines on the results heatmap: black outside, white inside. */
export const CORRECT_OUTER = '#000000';
export const CORRECT_INNER = '#ffffff';

export const CANVAS_BACKGROUND = '#0f172a';
export const HEADER_TEXT = '#cbd5e1';
export const PLACEHOLDER_TEXT = '#94a3b8';
export const COUNT_TEXT = '#ffffff';
export const TEXT_HALO = 'rgba(0, 0, 0, 0.85)';

/** The editor's accepted cells, and its keyboard cursor. */
export const ACCEPTED_FILL = 'rgba(34, 197, 94, 0.45)';
export const ACCEPTED_EDGE = '#22c55e';
export const CURSOR = '#818cf8';
