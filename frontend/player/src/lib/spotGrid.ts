import type { GridCell } from '../types/game';

// Spot It's grid geometry on the phone (T7 spec §2.4): cell labels, readers for received data,
// and the canvas layout and hit-testing, copied from the host app's `lib/spotGrid.ts` (the apps
// share no code) — a change to those functions there belongs here too. There are no grid limits
// here: the phone draws whatever grid the server sent. The limits live in the backend's
// `spot_it` (the authority) and the host's copy, which the editor uses.

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

/** "B3": the column letter (A–J), then the 1-based row number. */
export function cellLabel(cell: GridCell): string {
  return `${String.fromCharCode(65 + cell.col)}${cell.row + 1}`;
}

/** The accessible name of a cell: "column B, row 3". */
export function cellName(cell: GridCell): string {
  return `column ${String.fromCharCode(65 + cell.col)}, row ${cell.row + 1}`;
}

/** Row-major order (row, then column), as the server reveals accepted cells. */
export function sortCells(cells: GridCell[]): GridCell[] {
  return [...cells].sort((a, b) => a.row - b.row || a.col - b.col);
}

/** A received grid, or null when it isn't a well-formed 2–10 × 2–10 grid. */
export function readGrid(value: unknown): Grid | null {
  if (!value || typeof value !== 'object') return null;
  const { cols, rows } = value as Record<string, unknown>;
  const ok = (n: unknown) => isInt(n) && n >= MIN_GRID && n <= MAX_GRID;
  return ok(cols) && ok(rows) ? { cols: cols as number, rows: rows as number } : null;
}

/** A received cell, or null. */
export function readCell(value: unknown): GridCell | null {
  if (!value || typeof value !== 'object') return null;
  const { col, row } = value as Record<string, unknown>;
  return isInt(col) && isInt(row) ? { col, row } : null;
}

/** The largest rectangle of the image's aspect inside a `boxW` × `boxH` box. */
export function fitArea(boxW: number, boxH: number, width: number, height: number) {
  const ratio = width > 0 && height > 0 ? width / height : 1;
  const w = Math.max(0, Math.min(boxW, boxH * ratio));
  return { w, h: ratio > 0 ? w / ratio : 0 };
}

/**
 * The canvas's CSS size and grid rectangle: header bands of `band` px (column letters on top,
 * row numbers on the left), then the grid area at the image's proportions, as large as the
 * container's width and `maxHeight` allow. On the reference phone (a 328 px wide container,
 * 352 px cap, 20 px bands) the grid box is 308×332.
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
