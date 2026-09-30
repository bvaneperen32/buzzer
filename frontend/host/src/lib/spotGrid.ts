import type { GridCell } from '../types/game';

// Spot It's grid geometry (T7 spec §2.4). The limits (`maxGrid`, `gridFits`, `defaultGrid`) are a
// deliberate copy of the backend's `spot_it.max_grid` / `grid_fits` / `default_grid`, which are
// the authority, computed in integers exactly as there: change the two together. The player app
// has no limits; its `lib/spotGrid.ts` repeats only the labels, readers and canvas layout below
// (`cellLabel`, `layoutCanvas`, `cellAt`, …), so keep those in step with it.

export const MIN_GRID = 2;
export const MAX_GRID = 10;

export interface Grid {
  cols: number;
  rows: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const isInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v);

/**
 * The densest grid an image allows: every cell at least 28 px wide and tall on the reference
 * phone's 308×332 grid box (308 / 28 = 11 and 332 / 28 = 83 / 7, so this is exact in integers).
 * An image wider than 11:2 has `rows < 2`, one taller than 14:83 has `cols < 2`: no grid fits.
 */
export function maxGrid(width: number, height: number): Grid {
  if (!isInt(width) || !isInt(height) || width <= 0 || height <= 0) return { cols: 0, rows: 0 };
  if (308 * height <= 332 * width) {
    return { cols: 10, rows: Math.min(10, Math.floor((11 * height) / width)) };
  }
  return { cols: Math.min(10, Math.floor((83 * width) / (7 * height))), rows: 10 };
}

export function fitsNoGrid(width: number, height: number): boolean {
  const max = maxGrid(width, height);
  return max.cols < MIN_GRID || max.rows < MIN_GRID;
}

export function gridFits(grid: Grid, width: number, height: number): boolean {
  const max = maxGrid(width, height);
  return grid.cols <= max.cols && grid.rows <= max.rows;
}

/** The editor's default grid, rounding half up in integers (never Math.round); null when the
 * image fits no grid. */
export function defaultGrid(width: number, height: number): Grid | null {
  const max = maxGrid(width, height);
  if (max.cols < MIN_GRID || max.rows < MIN_GRID) return null;
  const cols = Math.min(6, max.cols);
  const rows = Math.max(
    MIN_GRID,
    Math.min(max.rows, Math.floor((2 * cols * height + width) / (2 * width))),
  );
  return { cols, rows };
}

/** "B3": the column letter (A–J), then the 1-based row number. */
export function cellLabel(cell: GridCell): string {
  return `${String.fromCharCode(65 + cell.col)}${cell.row + 1}`;
}

/** The accessible name of a cell: "column B, row 3". */
export function cellName(cell: GridCell): string {
  return `column ${String.fromCharCode(65 + cell.col)}, row ${cell.row + 1}`;
}

export const cellKey = (cell: GridCell): string => `${cell.col},${cell.row}`;

/** Row-major order (row, then column): the order reveals and displays use. */
export function sortCells(cells: GridCell[]): GridCell[] {
  return [...cells].sort((a, b) => a.row - b.row || a.col - b.col);
}

/** A stored grid, or null when it isn't a well-formed 2–10 × 2–10 grid. */
export function readGrid(value: unknown): Grid | null {
  if (!value || typeof value !== 'object') return null;
  const { cols, rows } = value as Record<string, unknown>;
  const ok = (n: unknown) => isInt(n) && n >= MIN_GRID && n <= MAX_GRID;
  return ok(cols) && ok(rows) ? { cols: cols as number, rows: rows as number } : null;
}

/** Well-formed cells from a stored or received list, in their order. */
export function readCells(value: unknown): GridCell[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (c): c is GridCell =>
      !!c && typeof c === 'object' && isInt((c as GridCell).col) && isInt((c as GridCell).row),
  );
}

/** The largest rectangle of the image's aspect inside a `boxW` × `boxH` box. */
export function fitArea(boxW: number, boxH: number, width: number, height: number) {
  const ratio = width > 0 && height > 0 ? width / height : 1;
  const w = Math.max(0, Math.min(boxW, boxH * ratio));
  return { w, h: ratio > 0 ? w / ratio : 0 };
}

/**
 * A canvas's CSS size and grid rectangle: header bands of `band` px (column letters on top,
 * row numbers on the left), then the grid area at the image's aspect, as large as the
 * container's width and `maxHeight` allow.
 */
export function layoutCanvas(
  containerWidth: number,
  maxHeight: number,
  band: number,
  width: number,
  height: number,
): { cssWidth: number; cssHeight: number; grid: Rect } {
  const area = fitArea(Math.max(0, containerWidth - band), Math.max(0, maxHeight - band), width, height);
  return {
    cssWidth: band + area.w,
    cssHeight: band + area.h,
    grid: { x: band, y: band, w: area.w, h: area.h },
  };
}

export function cellRect(grid: Rect, dims: Grid, cell: GridCell): Rect {
  const w = grid.w / dims.cols;
  const h = grid.h / dims.rows;
  return { x: grid.x + cell.col * w, y: grid.y + cell.row * h, w, h };
}

/** The cell under a point (CSS px relative to the canvas), or null outside the grid area. */
export function cellAt(grid: Rect, dims: Grid, x: number, y: number): GridCell | null {
  if (x < grid.x || y < grid.y || x >= grid.x + grid.w || y >= grid.y + grid.h) return null;
  return {
    col: Math.min(dims.cols - 1, Math.floor(((x - grid.x) / grid.w) * dims.cols)),
    row: Math.min(dims.rows - 1, Math.floor(((y - grid.y) / grid.h) * dims.rows)),
  };
}

/** `data-grid-rect`: the grid area in CSS px, each number with at most two decimals. */
export function rectAttribute(rect: Rect): string {
  const n = (v: number) => String(Math.round(v * 100) / 100);
  return [rect.x, rect.y, rect.w, rect.h].map(n).join(',');
}

/** One cell's size (CSS px) on the reference phone's 308×332 grid box. */
export function phoneCellSize(grid: Grid, width: number, height: number): { w: number; h: number } {
  const area = fitArea(308, 332, width, height);
  return { w: area.w / grid.cols, h: area.h / grid.rows };
}

/** The heat ramp step (1–5) of a cell with answers, 0 without (spec §2.4). */
export function heatStep(count: number, max: number): number {
  if (count <= 0 || max <= 0) return 0;
  if (max <= 5) return count;
  return Math.ceil((5 * count) / max);
}

/** The legend's swatches: each step and the counts it covers. */
export function heatLegend(max: number): { step: number; label: string }[] {
  if (max <= 0) return [];
  if (max <= 5) return Array.from({ length: max }, (_, i) => ({ step: i + 1, label: String(i + 1) }));
  return [1, 2, 3, 4, 5].map((step) => {
    const lo = Math.floor(((step - 1) * max) / 5) + 1;
    const hi = Math.floor((step * max) / 5);
    return { step, label: lo === hi ? String(lo) : `${lo}–${hi}` };
  });
}

// ---------------------------------------------------------------------------
// Review status (spec §2.5): the display texts of the server's reason codes
// ---------------------------------------------------------------------------

export type ReviewReason =
  | 'image_missing'
  | 'grid_too_dense'
  | 'image_changed'
  | 'image_replaced'
  | 'grid_changed'
  | 'not_confirmed';

export function tooDenseText(grid: Grid, width: number, height: number): string {
  const max = maxGrid(width, height);
  if (max.rows < MIN_GRID) return 'its image is too wide for any grid (crop it to at most 11:2)';
  if (max.cols < MIN_GRID) return 'its image is too tall for any grid (crop it to at most 14:83)';
  return `the ${grid.cols}×${grid.rows} grid is too dense for its image (at most ${max.cols}×${max.rows})`;
}

const REASON_TEXTS: Record<string, string> = {
  image_missing: 'its image is missing',
  image_changed: 'a different image was chosen after the cells were checked',
  image_replaced: 'its image was replaced after the cells were checked',
  grid_changed: 'its grid changed after the cells were checked',
  not_confirmed: "its accepted cells haven't been checked",
};

/**
 * The editor's "Needs review" text: each reason's display text, capitalised and joined with
 * "; ". `image` is the question's current library image (for the density limits).
 */
export function reviewText(
  reasons: string[],
  grid: Grid | null,
  image: { width: number; height: number } | undefined,
): string {
  if (reasons.length === 1 && reasons[0] === 'image_missing') {
    return 'Its image is missing; choose an image';
  }
  return reasons
    .map((reason) =>
      reason === 'grid_too_dense' && grid && image
        ? tooDenseText(grid, image.width, image.height)
        : REASON_TEXTS[reason] ?? reason,
    )
    .map((text) => text.charAt(0).toUpperCase() + text.slice(1))
    .join('; ');
}
