import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import type { GridCell } from '../../types/game';
import {
  cellAt,
  cellKey,
  cellLabel,
  cellName,
  cellRect,
  fitArea,
  heatLegend,
  heatStep,
  layoutCanvas,
  rectAttribute,
  sortCells,
  type Grid,
  type Rect,
} from '../../lib/spotGrid';
import {
  ACCEPTED_EDGE,
  ACCEPTED_FILL,
  CANVAS_BACKGROUND,
  CORRECT_INNER,
  CORRECT_OUTER,
  COUNT_TEXT,
  CURSOR,
  GRID_DARK,
  GRID_LIGHT,
  HEADER_TEXT,
  HEAT_COLORS,
  HEAT_OPACITY,
  PLACEHOLDER_TEXT,
  TEXT_HALO,
} from '../../lib/canvasColors';

/** The image under the grid: a server-issued link (null when none is available). */
export interface CanvasImage {
  url: string | null;
  description: string;
}

interface SpotItCanvasProps {
  /**
   * `question`: the projector's image and transparent labelled grid. `results`: the heatmap —
   * counts, heat fills and "Correct" outlines. `author`: the editor's grid, where clicking or
   * Enter toggles a cell's acceptance.
   */
  mode: 'question' | 'results' | 'author';
  grid: Grid;
  /** The grid area's aspect: gameplay dimensions on the projector, authoring ones in the editor. */
  width: number;
  height: number;
  image?: CanvasImage | null;
  /** Fetches current links after an image error (T8 §2.12: refresh, retry once, then Retry). */
  refreshImage?: () => Promise<void>;
  /** The canvas never grows taller than this share of the viewport. */
  maxHeightVh: number;
  /** Header bands (column letters on top, row numbers on the left), CSS px. */
  band?: number;
  /** results: answers per cell, keyed "col,row". */
  counts?: Record<string, number>;
  /** results: the accepted cells to outline as "Correct"; author: the accepted cells. */
  accepted?: GridCell[];
  /** author: toggle one cell. */
  onToggle?: (cell: GridCell) => void;
  disabled?: boolean;
  /** The canvas's accessible name. */
  label: string;
}

type ImageStatus = 'none' | 'loading' | 'loaded' | 'failed';

/** Loads the image with T8's failure sequence: refresh the links, retry once, then fail. */
function useCanvasImage(url: string | null | undefined, refresh?: () => Promise<void>) {
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  const [status, setStatus] = useState<ImageStatus>(url ? 'loading' : 'none');
  const [attempt, setAttempt] = useState(0);
  const errors = useRef(0);
  // Read through a ref, so a new callback identity never restarts a load.
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    if (!url) {
      setImg(null);
      setStatus('none');
      return;
    }
    let cancelled = false;
    const element = new Image();
    element.decoding = 'async';
    element.onload = () => {
      if (cancelled) return;
      errors.current = 0;
      setImg(element);
      setStatus('loaded');
    };
    element.onerror = () => {
      if (cancelled) return;
      errors.current += 1;
      if (errors.current > 1) {
        setStatus('failed');
        return;
      }
      // A link can expire or change: refresh, then retry once with whatever link is current
      // (a changed link restarts this effect by itself).
      void (async () => {
        try {
          await refreshRef.current?.();
        } catch {
          // The retry below still runs; its failure shows the placeholder.
        }
        if (!cancelled) setAttempt((a) => a + 1);
      })();
    };
    setStatus('loading');
    element.src = url;
    return () => {
      cancelled = true;
    };
  }, [url, attempt]);

  const retry = useCallback(async () => {
    errors.current = 1; // as after a first error: refresh, then one more try
    setStatus('loading');
    try {
      await refreshRef.current?.();
    } catch {
      // fall through to the retry
    }
    setAttempt((a) => a + 1);
  }, []);

  return { img: status === 'loaded' ? img : null, status, retry };
}

function useDevicePixelRatio(): number {
  const read = () => Math.min(window.devicePixelRatio || 1, 3);
  const [dpr, setDpr] = useState(read);
  useEffect(() => {
    // Moving the window to another screen or zooming changes the ratio.
    const media = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    const onChange = () => setDpr(read());
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, [dpr]);
  return dpr;
}

function wrapText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const lines: string[] = [];
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const last = lines[lines.length - 1];
    if (last !== undefined && ctx.measureText(`${last} ${word}`).width <= maxWidth) {
      lines[lines.length - 1] = `${last} ${word}`;
    } else {
      lines.push(word);
    }
  }
  return lines;
}

function haloText(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, fill: string) {
  ctx.lineJoin = 'round';
  ctx.lineWidth = 3;
  ctx.strokeStyle = TEXT_HALO;
  ctx.strokeText(text, x, y);
  ctx.fillStyle = fill;
  ctx.fillText(text, x, y);
}

/**
 * A Spot It grid on a `<canvas>` (T7 spec §2.4): the image fitted into the grid area, the grid
 * drawn twice (dark, then light) so it reads over any image, and header letters and numbers.
 * The backing store follows the device pixel ratio (capped at 3), and the canvas redraws on
 * resize, on a pixel-ratio change and on state change only. It exposes `data-grid-cols`,
 * `data-grid-rows` and `data-grid-rect` (the grid area in CSS px) for automated checks.
 * Presentational: links, counts and cells come from the page.
 */
export function SpotItCanvas({
  mode,
  grid,
  width,
  height,
  image,
  refreshImage,
  maxHeightVh,
  band = 28,
  counts,
  accepted = [],
  onToggle,
  disabled = false,
  label,
}: SpotItCanvasProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dpr = useDevicePixelRatio();
  const { img, status, retry } = useCanvasImage(image?.url, refreshImage);
  const [layout, setLayout] = useState(() => layoutCanvas(0, 0, band, width, height));
  const [cursor, setCursor] = useState<GridCell | null>(null);
  const [focused, setFocused] = useState(false);
  const [announcement, setAnnouncement] = useState('');

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => {
      const next = layoutCanvas(el.clientWidth, (window.innerHeight * maxHeightVh) / 100, band, width, height);
      setLayout((prev) =>
        prev.cssWidth === next.cssWidth && prev.cssHeight === next.cssHeight ? prev : next,
      );
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    window.addEventListener('resize', update);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', update);
    };
  }, [maxHeightVh, band, width, height]);

  const acceptedKeys = new Set(accepted.map(cellKey));
  const acceptedSignature = [...acceptedKeys].sort().join(';');
  const countsSignature = JSON.stringify(counts ?? {});
  const imageMissing = !!image && (status === 'failed' || status === 'none');

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
    ctx.fillStyle = CANVAS_BACKGROUND;
    ctx.fillRect(rect.x, rect.y, rect.w, rect.h);

    if (img) {
      const fit = fitArea(rect.w, rect.h, img.naturalWidth, img.naturalHeight);
      ctx.drawImage(img, rect.x + (rect.w - fit.w) / 2, rect.y + (rect.h - fit.h) / 2, fit.w, fit.h);
    } else if (imageMissing && image) {
      ctx.font = '600 16px ui-sans-serif, system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = PLACEHOLDER_TEXT;
      const lines = wrapText(ctx, `Image unavailable — ${image.description}`, rect.w - 24);
      const top = rect.y + rect.h / 2 - ((lines.length - 1) * 20) / 2 - 18;
      lines.forEach((line, i) => ctx.fillText(line, rect.x + rect.w / 2, top + i * 20));
    }

    const cellOf = (cell: GridCell): Rect => cellRect(rect, grid, cell);
    const max = Math.max(0, ...Object.values(counts ?? {}));
    if (mode === 'results' && counts) {
      ctx.globalAlpha = HEAT_OPACITY;
      for (const [key, count] of Object.entries(counts)) {
        const [col, row] = key.split(',').map(Number);
        const step = heatStep(count, max);
        if (!step || col >= grid.cols || row >= grid.rows) continue;
        const r = cellOf({ col, row });
        ctx.fillStyle = HEAT_COLORS[step - 1];
        ctx.fillRect(r.x, r.y, r.w, r.h);
      }
      ctx.globalAlpha = 1;
    }
    if (mode === 'author') {
      for (const cell of accepted) {
        const r = cellOf(cell);
        ctx.fillStyle = ACCEPTED_FILL;
        ctx.fillRect(r.x, r.y, r.w, r.h);
      }
    }

    for (const line of [GRID_DARK, GRID_LIGHT]) {
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

    const cellW = rect.w / grid.cols;
    const cellH = rect.h / grid.rows;
    if (mode === 'author') {
      ctx.strokeStyle = ACCEPTED_EDGE;
      ctx.lineWidth = 2;
      for (const cell of accepted) {
        const r = cellOf(cell);
        ctx.strokeRect(r.x + 2, r.y + 2, r.w - 4, r.h - 4);
      }
    }
    const tagFont = Math.max(9, Math.min(14, cellW / 5, cellH / 3));
    if (mode === 'results') {
      for (const cell of accepted) {
        const r = cellOf(cell);
        ctx.lineWidth = 3;
        ctx.strokeStyle = CORRECT_OUTER;
        ctx.strokeRect(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3);
        ctx.lineWidth = 2;
        ctx.strokeStyle = CORRECT_INNER;
        ctx.strokeRect(r.x + 4, r.y + 4, r.w - 8, r.h - 8);
        ctx.font = `800 ${tagFont}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        haloText(ctx, 'Correct', r.x + r.w / 2, r.y + 6, COUNT_TEXT);
      }
      if (counts) {
        const countFont = Math.max(10, Math.min(28, cellW * 0.4, cellH * 0.4));
        ctx.font = `800 ${countFont}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        for (const [key, count] of Object.entries(counts)) {
          const [col, row] = key.split(',').map(Number);
          if (count <= 0 || col >= grid.cols || row >= grid.rows) continue;
          const r = cellOf({ col, row });
          const shift = acceptedKeys.has(key) ? tagFont / 2 : 0;
          haloText(ctx, String(count), r.x + r.w / 2, r.y + r.h / 2 + shift, COUNT_TEXT);
        }
      }
    }

    const headerFont = Math.max(10, Math.min(18, band * 0.55));
    ctx.font = `700 ${headerFont}px ui-sans-serif, system-ui, sans-serif`;
    ctx.fillStyle = HEADER_TEXT;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (let c = 0; c < grid.cols; c++) {
      ctx.fillText(String.fromCharCode(65 + c), rect.x + cellW * (c + 0.5), band / 2);
    }
    for (let r = 0; r < grid.rows; r++) {
      ctx.fillText(String(r + 1), band / 2, rect.y + cellH * (r + 0.5));
    }

    if (mode === 'author' && focused && cursor) {
      const r = cellOf(cursor);
      ctx.strokeStyle = CURSOR;
      ctx.lineWidth = 3;
      ctx.setLineDash([6, 3]);
      ctx.strokeRect(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3);
      ctx.setLineDash([]);
    }
    // Everything the drawing depends on is listed; the signatures stand for the arrays.
  }, [layout, dpr, img, imageMissing, mode, grid.cols, grid.rows, band, countsSignature, acceptedSignature, focused, cursor, image?.description]);

  function toggle(cell: GridCell) {
    if (!onToggle || disabled) return;
    const wasAccepted = acceptedKeys.has(cellKey(cell));
    onToggle(cell);
    setAnnouncement(`${cellLabel(cell)} (${cellName(cell)}) ${wasAccepted ? 'removed' : 'accepted'}`);
  }

  function onPointerDown(e: PointerEvent<HTMLCanvasElement>) {
    if (mode !== 'author') return;
    const box = e.currentTarget.getBoundingClientRect();
    const cell = cellAt(layout.grid, grid, e.clientX - box.left, e.clientY - box.top);
    if (!cell) return; // header bands are ignored
    setCursor(cell);
    toggle(cell);
  }

  function onKeyDown(e: KeyboardEvent<HTMLCanvasElement>) {
    if (mode !== 'author') return;
    const current = cursor ?? { col: 0, row: 0 };
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    };
    if (e.key in moves) {
      e.preventDefault();
      const [dc, dr] = moves[e.key];
      const next = cursor
        ? {
            col: Math.max(0, Math.min(grid.cols - 1, current.col + dc)),
            row: Math.max(0, Math.min(grid.rows - 1, current.row + dr)),
          }
        : current;
      setCursor(next);
      setAnnouncement(`${cellLabel(next)} (${cellName(next)})${acceptedKeys.has(cellKey(next)) ? ', accepted' : ''}`);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      setCursor(current);
      toggle(current);
    }
  }

  const accessibleName =
    mode === 'author'
      ? `${label}. ${accepted.length ? `Accepted: ${sortCells(accepted).map(cellLabel).join(', ')}.` : 'No accepted cells yet.'} Arrow keys move, Enter toggles the cell.`
      : label;

  return (
    <div ref={wrapRef} className="relative flex w-full justify-center">
      <canvas
        ref={canvasRef}
        style={{ width: layout.cssWidth, height: layout.cssHeight }}
        className={
          mode === 'author'
            ? `rounded-lg ${disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'} focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400`
            : 'rounded-lg'
        }
        role={mode === 'author' ? 'application' : 'img'}
        aria-label={accessibleName}
        aria-disabled={mode === 'author' ? disabled : undefined}
        tabIndex={mode === 'author' && !disabled ? 0 : undefined}
        data-grid-cols={grid.cols}
        data-grid-rows={grid.rows}
        data-grid-rect={rectAttribute(layout.grid)}
        onPointerDown={onPointerDown}
        onKeyDown={onKeyDown}
        onFocus={() => {
          setFocused(true);
          setCursor((c) => c ?? { col: 0, row: 0 });
        }}
        onBlur={() => setFocused(false)}
      />
      {imageMissing && image?.url !== undefined && (
        <button
          type="button"
          onClick={() => void retry()}
          className="absolute top-1/2 left-1/2 mt-6 -translate-x-1/2 rounded border border-slate-500 bg-slate-900/80 px-3 py-1 text-sm text-slate-200 hover:bg-slate-700"
        >
          Retry
        </button>
      )}
      {mode === 'author' && (
        <span className="sr-only" aria-live="polite">
          {announcement}
        </span>
      )}
    </div>
  );
}

/**
 * The heatmap's legend (spec §2.4): a swatch per ramp step with the counts it covers, "blank =
 * no answers", the Correct outline for ACCURACY, and "N of M answered".
 */
export function HeatLegend({ counts, answered, total, showCorrect }: {
  counts: Record<string, number>;
  answered: number;
  total: number;
  showCorrect: boolean;
}) {
  const max = Math.max(0, ...Object.values(counts));
  return (
    <div className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-sm text-slate-300">
      {heatLegend(max).map(({ step, label }) => (
        <span key={step} className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-4 w-4 rounded"
            style={{ background: HEAT_COLORS[step - 1], opacity: HEAT_OPACITY }}
            aria-hidden="true"
          />
          {label}
        </span>
      ))}
      <span className="text-slate-400">blank = no answers</span>
      {showCorrect && (
        <span className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-4 w-4 rounded-sm"
            style={{ border: `2px solid ${CORRECT_INNER}`, outline: `2px solid ${CORRECT_OUTER}` }}
            aria-hidden="true"
          />
          Correct
        </span>
      )}
      <span className="text-slate-400">
        {answered} of {total} answered
      </span>
    </div>
  );
}
