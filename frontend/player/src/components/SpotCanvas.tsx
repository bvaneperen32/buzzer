import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import type { GridCell } from '../types/game';
import {
  cellAt,
  cellLabel,
  cellName,
  cellRect,
  layoutCanvas,
  rectAttribute,
  type Grid,
} from '../lib/spotGrid';
import { canvasColors } from '../lib/canvasColors';
import { observeTheme } from '../../../shared/theme/theme';

/** Header bands on the phone (column letters on top, row numbers on the left), CSS px. */
const BAND = 20;
/** In-cell labels only when a cell is at least this big (headers are always drawn). */
const LABEL_MIN_CELL = 28;
/** Below this in landscape, suggest turning the phone upright. */
const TINY_CELL = 24;

interface SpotCanvasProps {
  grid: Grid;
  /** The image's proportions (the server-written `config.width/height`); never the image. */
  width: number;
  height: number;
  /** After submit and while the question is locked. Never clears the selection. */
  disabled: boolean;
  onLockIn: (cell: GridCell) => void;
}

function useDevicePixelRatio(): number {
  const read = () => Math.min(window.devicePixelRatio || 1, 3);
  const [dpr, setDpr] = useState(read);
  useEffect(() => {
    const media = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    const onChange = () => setDpr(read());
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, [dpr]);
  return dpr;
}

/**
 * Spot It on the phone (T7 spec §2.4): the grid, its labels and the image's proportions on a
 * `<canvas>` — the image itself is never shown here. A touch (or click) inside the grid selects
 * the cell under it, dragging moves the selection, and header taps are ignored. The arrow keys
 * and the nudge buttons move the selection too (from A1 when there is none), so every cell is
 * reachable at any size; Enter, Space or "Lock in" submits it. The canvas fits the width and at
 * most 55% of the small viewport height, and keeps the selection through rotation and lock.
 */
export function SpotCanvas({ grid: gridProp, width, height, disabled, onLockIn }: SpotCanvasProps) {
  // The page builds a new grid object on every render (a lock, an unlock, any context change);
  // keyed on its values, the canvas redraws only when the grid itself changes.
  const grid = useMemo(() => ({ cols: gridProp.cols, rows: gridProp.rows }), [gridProp.cols, gridProp.rows]);
  const wrapRef = useRef<HTMLDivElement>(null);
  const capRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dpr = useDevicePixelRatio();
  const [selected, setSelected] = useState<GridCell | null>(null);
  const [layout, setLayout] = useState(() => layoutCanvas(0, 0, BAND, width, height));
  const [landscape, setLandscape] = useState(false);
  const dragging = useRef<number | null>(null);
  // Colours are read from the theme at draw time, so a theme change must redraw.
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme);
  useEffect(() => observeTheme(setTheme), []);

  useLayoutEffect(() => {
    const wrap = wrapRef.current;
    const cap = capRef.current;
    if (!wrap || !cap) return;
    const update = () => {
      // The cap is measured from a probe styled `height: 55vh; height: 55svh`, so it follows the
      // visible height behind the browser's toolbars where `svh` is supported.
      const next = layoutCanvas(wrap.clientWidth, cap.getBoundingClientRect().height, BAND, width, height);
      setLayout((prev) => (prev.cssWidth === next.cssWidth && prev.cssHeight === next.cssHeight ? prev : next));
      setLandscape(window.innerWidth > window.innerHeight);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(wrap);
    observer.observe(cap);
    window.addEventListener('resize', update);
    window.addEventListener('orientationchange', update);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', update);
      window.removeEventListener('orientationchange', update);
    };
  }, [width, height]);

  const cellW = layout.grid.w / grid.cols;
  const cellH = layout.grid.h / grid.rows;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const { cssWidth, cssHeight, grid: rect } = layout;
    canvas.width = Math.max(1, Math.round(cssWidth * dpr));
    canvas.height = Math.max(1, Math.round(cssHeight * dpr));
    const ctx = canvas.getContext('2d');
    if (!ctx || rect.w <= 0 || rect.h <= 0) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);
    const colors = canvasColors();
    ctx.fillStyle = colors.background;
    ctx.fillRect(rect.x, rect.y, rect.w, rect.h);

    if (selected) {
      const r = cellRect(rect, grid, selected);
      ctx.fillStyle = colors.selectedFill;
      ctx.fillRect(r.x, r.y, r.w, r.h);
    }

    for (const line of [colors.gridDark, colors.gridLight]) {
      ctx.strokeStyle = line.color;
      ctx.lineWidth = line.width;
      ctx.beginPath();
      for (let c = 0; c <= grid.cols; c++) {
        const x = rect.x + (rect.w * c) / grid.cols;
        ctx.moveTo(x, rect.y);
        ctx.lineTo(x, rect.y + rect.h);
      }
      for (let r = 0; r <= grid.rows; r++) {
        const y = rect.y + (rect.h * r) / grid.rows;
        ctx.moveTo(rect.x, y);
        ctx.lineTo(rect.x + rect.w, y);
      }
      ctx.stroke();
    }

    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const w = rect.w / grid.cols;
    const h = rect.h / grid.rows;
    if (w >= LABEL_MIN_CELL && h >= LABEL_MIN_CELL) {
      ctx.font = `600 ${Math.min(14, w / 3.2)}px ui-sans-serif, system-ui, sans-serif`;
      for (let r = 0; r < grid.rows; r++) {
        for (let c = 0; c < grid.cols; c++) {
          const isSelected = selected?.col === c && selected?.row === r;
          ctx.fillStyle = isSelected ? colors.selectedText : colors.cellText;
          ctx.fillText(cellLabel({ col: c, row: r }), rect.x + w * (c + 0.5), rect.y + h * (r + 0.5));
        }
      }
    }
    if (selected) {
      const r = cellRect(rect, grid, selected);
      ctx.strokeStyle = colors.selectedEdge;
      ctx.lineWidth = 3;
      ctx.strokeRect(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3);
    }

    ctx.font = '700 12px ui-sans-serif, system-ui, sans-serif';
    ctx.fillStyle = colors.headerText;
    for (let c = 0; c < grid.cols; c++) ctx.fillText(String.fromCharCode(65 + c), rect.x + w * (c + 0.5), BAND / 2);
    for (let r = 0; r < grid.rows; r++) ctx.fillText(String(r + 1), BAND / 2, rect.y + h * (r + 0.5));
  }, [layout, dpr, grid, selected, theme]);

  function selectAt(e: PointerEvent<HTMLCanvasElement>) {
    const box = e.currentTarget.getBoundingClientRect();
    return cellAt(layout.grid, grid, e.clientX - box.left, e.clientY - box.top);
  }

  function onPointerDown(e: PointerEvent<HTMLCanvasElement>) {
    if (disabled) return;
    const cell = selectAt(e);
    if (!cell) return; // a tap on a header band selects nothing
    e.currentTarget.setPointerCapture(e.pointerId);
    dragging.current = e.pointerId;
    setSelected(cell);
  }

  function onPointerMove(e: PointerEvent<HTMLCanvasElement>) {
    if (disabled || dragging.current !== e.pointerId) return;
    const cell = selectAt(e);
    if (cell && (cell.col !== selected?.col || cell.row !== selected?.row)) setSelected(cell);
  }

  function endDrag(e: PointerEvent<HTMLCanvasElement>) {
    if (dragging.current === e.pointerId) dragging.current = null;
  }

  function nudge(dc: number, dr: number) {
    if (disabled) return;
    setSelected((prev) =>
      prev
        ? {
            col: Math.max(0, Math.min(grid.cols - 1, prev.col + dc)),
            row: Math.max(0, Math.min(grid.rows - 1, prev.row + dr)),
          }
        : { col: 0, row: 0 },
    );
  }

  function onKeyDown(e: KeyboardEvent<HTMLCanvasElement>) {
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    };
    if (e.key in moves) {
      e.preventDefault();
      nudge(...moves[e.key]);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (!disabled && selected) onLockIn(selected);
    }
  }

  const smallest = Math.min(cellW, cellH);
  const nudgeClass =
    'h-11 w-11 shrink-0 rounded-xl bg-raised text-xl font-bold text-primary active:scale-95 disabled:opacity-40';

  return (
    <div className="flex flex-col gap-3">
      <div ref={wrapRef} className="relative flex w-full justify-center">
        <div ref={capRef} className="spot-height-cap pointer-events-none invisible absolute" aria-hidden="true" />
        <canvas
          ref={canvasRef}
          style={{ width: layout.cssWidth, height: layout.cssHeight, touchAction: 'none' }}
          className={`rounded-lg ${disabled ? 'opacity-60' : ''}`}
          tabIndex={0}
          role="application"
          aria-label={`Grid of ${grid.cols} columns by ${grid.rows} rows. Use the arrow keys to choose a cell, then Enter to lock it in.`}
          aria-disabled={disabled}
          data-grid-cols={grid.cols}
          data-grid-rows={grid.rows}
          data-grid-rect={rectAttribute(layout.grid)}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onKeyDown={onKeyDown}
        />
      </div>

      {landscape && smallest > 0 && smallest < TINY_CELL && (
        <p className="text-center text-sm text-warning">Turn your phone upright for a bigger grid</p>
      )}

      <div className="flex items-center justify-between gap-2">
        <p className="min-w-0 text-base font-semibold text-primary" aria-live="polite" data-selected={selected ? cellLabel(selected) : ''}>
          {selected ? (
            <>
              Selected: {cellLabel(selected)}
              <span className="sr-only"> ({cellName(selected)})</span>
            </>
          ) : (
            'Tap the grid to choose a cell'
          )}
        </p>
        <div className="flex gap-1.5">
          <button type="button" className={nudgeClass} onClick={() => nudge(-1, 0)} disabled={disabled} aria-label="Move left">←</button>
          <button type="button" className={nudgeClass} onClick={() => nudge(1, 0)} disabled={disabled} aria-label="Move right">→</button>
          <button type="button" className={nudgeClass} onClick={() => nudge(0, -1)} disabled={disabled} aria-label="Move up">↑</button>
          <button type="button" className={nudgeClass} onClick={() => nudge(0, 1)} disabled={disabled} aria-label="Move down">↓</button>
        </div>
      </div>

      <button
        type="button"
        disabled={disabled || !selected}
        onClick={() => selected && onLockIn(selected)}
        className="w-full rounded-2xl py-5 text-on-accent font-black text-2xl bg-accent hover:bg-accent-hover active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {selected ? `Lock in ${cellLabel(selected)}` : 'Lock in'}
      </button>
    </div>
  );
}
