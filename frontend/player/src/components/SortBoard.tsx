import { useRef, useState, type MouseEvent, type PointerEvent } from 'react';

interface SortBoardProps {
  boxes: string[];
  cards: string[];
  /** The shared card display order (`cardOrder`); answers always use the original indices. */
  order: number[];
  /** ACCURACY: show the scoring rule and warn that left-out cards score 0. */
  accuracy: boolean;
  /** After submit and while the question is locked. Never clears the placements. */
  disabled: boolean;
  /** One entry per card in its original order: a box index, or null for a card left out. */
  onSubmit: (placements: (number | null)[]) => void;
}

/** Past this many px, a press on a card is a drag, not a tap. */
const DRAG_THRESHOLD = 8;

interface Drag {
  card: number;
  pointerId: number;
  startX: number;
  startY: number;
  active: boolean;
}

/** "−½" for k = 3: what a wrong card costs, as the phone states the rule. */
function wrongCost(k: number): string {
  return k === 2 ? '1' : k === 3 ? '½' : k === 4 ? '⅓' : `1/${k - 1}`;
}

/**
 * Sort It on the phone (T7 spec §2.3): the card pool (wrapping chips) above the boxes (two
 * columns). Drag a card onto a box, or tap a card and then a box; tapping a placed card selects
 * it to move or return to the pool. Cards and boxes are buttons, so the keyboard works too, and
 * a polite live region announces every move. Placements survive lock, unlock and socket
 * reconnects (the component is keyed by question); only a new question or a reload clears them.
 */
export function SortBoard({ boxes, cards, order, accuracy, disabled, onSubmit }: SortBoardProps) {
  const [placements, setPlacements] = useState<(number | null)[]>(() => cards.map(() => null));
  const [selected, setSelected] = useState<number | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [ghost, setGhost] = useState<{ card: number; x: number; y: number } | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const drag = useRef<Drag | null>(null);
  // Set when a drag ends, so the click the browser may send afterwards isn't also a tap.
  const suppressClick = useRef(false);

  const shown = order.filter((i) => i >= 0 && i < cards.length);
  const placed = placements.filter((box) => box !== null).length;
  const leftOut = cards.length - placed;

  function place(card: number, box: number | null) {
    if (disabled) return;
    setPlacements((prev) => {
      const next = [...prev];
      next[card] = box;
      return next;
    });
    setSelected(null);
    setConfirming(false);
    setAnnouncement(
      box === null ? `${cards[card]} returned to the pool` : `${cards[card]} placed in ${boxes[box]}`,
    );
  }

  function tapCard(card: number) {
    if (disabled) return;
    const next = selected === card ? null : card;
    setSelected(next);
    if (next !== null) {
      setAnnouncement(`${cards[card]} selected. Choose a box${placements[card] !== null ? ' or return it to the pool' : ''}.`);
    }
  }

  function onPointerDown(e: PointerEvent<HTMLButtonElement>, card: number) {
    suppressClick.current = false; // a drag's leftover flag never swallows a later tap
    if (disabled || !e.isPrimary) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { card, pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, active: false };
  }

  function onPointerMove(e: PointerEvent<HTMLButtonElement>) {
    const d = drag.current;
    if (!d || d.pointerId !== e.pointerId) return;
    if (!d.active && Math.hypot(e.clientX - d.startX, e.clientY - d.startY) < DRAG_THRESHOLD) return;
    d.active = true;
    setGhost({ card: d.card, x: e.clientX, y: e.clientY });
  }

  function onPointerUp(e: PointerEvent<HTMLButtonElement>) {
    const d = drag.current;
    drag.current = null;
    if (!d || d.pointerId !== e.pointerId) return;
    setGhost(null);
    // A tap is handled by the click that follows, not here: selecting now would re-render (a
    // placed card grows a "Return to pool" button above the boxes) before that click, which
    // would then land on whatever moved under the finger.
    if (!d.active) return;
    suppressClick.current = true;
    const target = document.elementFromPoint(e.clientX, e.clientY);
    const box = target?.closest('[data-drop-box]');
    if (box) place(d.card, Number(box.getAttribute('data-drop-box')));
    else if (target?.closest('[data-drop-pool]')) place(d.card, null);
  }

  function onPointerCancel() {
    drag.current = null;
    setGhost(null);
  }

  /** A tap, a click or the keyboard selects the card — but not the click that ends a drag. */
  function onCardClick(e: MouseEvent<HTMLButtonElement>, card: number) {
    e.stopPropagation();
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    tapCard(card);
  }

  function chooseBox(box: number) {
    if (selected !== null) place(selected, box);
  }

  function submit() {
    if (disabled || placed === 0) return;
    if (leftOut > 0 && !confirming) {
      setConfirming(true);
      return;
    }
    setConfirming(false);
    onSubmit(placements);
  }

  function chip(card: number) {
    const box = placements[card];
    const isSelected = selected === card;
    return (
      <button
        key={card}
        type="button"
        data-card={card}
        disabled={disabled}
        aria-pressed={isSelected}
        aria-label={`Card ${cards[card]}, ${box === null ? 'in pool' : `in box ${boxes[box]}`}`}
        style={{ touchAction: 'none' }}
        onPointerDown={(e) => onPointerDown(e, card)}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onClick={(e) => onCardClick(e, card)}
        className={`select-none rounded-xl border-2 px-3 py-2 text-base font-semibold transition-colors
          ${isSelected ? 'border-accent bg-accent text-on-accent' : 'border-input bg-raised text-primary'}
          ${disabled ? 'opacity-60' : 'active:scale-95'}
          ${ghost?.card === card ? 'opacity-40' : ''}`}
      >
        {cards[card]}
      </button>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {accuracy ? (
        <p className="text-center text-xs text-secondary">
          Right: +1 card. Wrong: −{wrongCost(boxes.length)} card. Left out: 0. This question's score
          never goes below 0. Unsure? Leaving a card out costs nothing.
        </p>
      ) : (
        <p className="text-center text-xs text-secondary">Sort the cards however you think — any sorting counts.</p>
      )}

      <div
        data-drop-pool
        aria-label="Card pool"
        className="flex min-h-16 flex-wrap gap-2 rounded-2xl border-2 border-dashed border-input p-3"
        onClick={() => selected !== null && placements[selected] !== null && place(selected, null)}
      >
        {shown.filter((card) => placements[card] === null).map(chip)}
        {leftOut === 0 && <span className="self-center text-sm text-secondary">All cards placed</span>}
      </div>

      {selected !== null && placements[selected] !== null && !disabled && (
        <button
          type="button"
          onClick={() => place(selected, null)}
          className="self-start rounded-xl border border-input px-3 py-2 text-sm text-primary hover:bg-hover active:scale-95"
        >
          Return {cards[selected]} to the pool
        </button>
      )}

      <div className="grid grid-cols-2 gap-3">
        {boxes.map((label, box) => (
          <div
            key={box}
            data-drop-box={box}
            onClick={() => chooseBox(box)}
            className={`flex min-h-28 flex-col gap-2 rounded-2xl border-2 p-2 ${
              selected !== null && !disabled ? 'border-accent bg-accent-subtle' : 'border-input bg-raised'
            }`}
          >
            <button
              type="button"
              disabled={disabled}
              onClick={(e) => {
                e.stopPropagation();
                chooseBox(box);
              }}
              aria-label={selected !== null ? `Box ${label}: place ${cards[selected]} here` : `Box ${label}`}
              className="w-full rounded-lg px-1 py-1 text-left text-base font-bold text-accent"
            >
              {label}
            </button>
            <div className="flex flex-wrap gap-1.5">{shown.filter((card) => placements[card] === box).map(chip)}</div>
          </div>
        ))}
      </div>

      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>

      {confirming && !disabled ? (
        <div className="rounded-2xl border border-warning bg-warning-subtle p-3 text-center" role="alertdialog" aria-live="assertive">
          <p className="text-primary">
            {leftOut === 1 ? '1 card is left out' : `${leftOut} cards are left out`}
            {accuracy ? ` and ${leftOut === 1 ? 'scores' : 'score'} 0` : ''}. Submit anyway?
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={submit}
              className="flex-1 rounded-xl bg-accent py-3 font-bold text-on-accent hover:bg-accent-hover active:scale-95"
            >
              Submit anyway
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="flex-1 rounded-xl border border-input py-3 font-semibold text-primary hover:bg-hover active:scale-95"
            >
              Keep sorting
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          disabled={disabled || placed === 0}
          onClick={submit}
          className="w-full rounded-2xl py-5 text-on-accent font-black text-2xl bg-accent hover:bg-accent-hover active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Submit ({placed} of {cards.length} placed)
        </button>
      )}

      {ghost && (
        <div
          className="pointer-events-none fixed z-50 -translate-x-1/2 -translate-y-1/2 rounded-xl border-2 border-accent bg-accent px-3 py-2 text-base font-semibold text-on-accent shadow-xl"
          style={{ left: ghost.x, top: ghost.y }}
          aria-hidden="true"
        >
          {cards[ghost.card]}
        </div>
      )}
    </div>
  );
}
