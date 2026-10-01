import {
  pctText,
  sortAccuracyRows,
  sortCompletenessRows,
  type SortAccuracyRow,
  type SortCompletenessRow,
} from '../../lib/sortResults';

interface SortItResultsProps {
  boxes: string[];
  cards: string[];
  /** The card display order: `cardOrder` on the results screen, the author's order at game over. */
  order: number[];
  distribution: Record<string, number>;
  /** The question's answered count, the denominator of every percentage. */
  answered: number;
  /** ACCURACY's answer key; null for COMPLETENESS. */
  correctBoxes: number[] | null;
  /** `results`: the projector's bars. `summary`: the game-over card's compact rows. */
  variant: 'results' | 'summary';
}

const label = (labels: string[], index: number | null) =>
  index === null || labels[index] === undefined ? '—' : labels[index];

/**
 * Sort It's per-card results (T7 spec §2.3). ACCURACY: the correct box, % correct (a bar on the
 * projector), % left out and — on the projector — the most common wrong box. COMPLETENESS: each
 * card's most common box and its %. Every number comes from `lib/sortResults.ts`; each row also
 * carries them as data attributes for the results cross-check. Labels render as React text.
 */
export function SortItResults({
  boxes,
  cards,
  order,
  distribution,
  answered,
  correctBoxes,
  variant,
}: SortItResultsProps) {
  const n = cards.length;
  const k = boxes.length;
  const ordered = <T extends { card: number }>(rows: T[]) =>
    order.filter((i) => i >= 0 && i < rows.length).map((i) => rows[i]);
  const big = variant === 'results';

  if (correctBoxes && correctBoxes.length === n) {
    const rows = ordered(sortAccuracyRows(n, k, distribution, answered, correctBoxes));
    return (
      <table className={`w-full ${big ? 'text-lg' : 'text-sm'} text-primary`} data-sort-results="accuracy">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-secondary">
            <th className="pb-2 pr-3 font-semibold">Card</th>
            <th className="pb-2 pr-3 font-semibold">Correct box</th>
            <th className="pb-2 pr-3 font-semibold">% correct</th>
            <th className="pb-2 pr-3 font-semibold">% left out</th>
            {big && <th className="pb-2 font-semibold">Most common wrong box</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row: SortAccuracyRow) => (
            <tr
              key={row.card}
              className="border-t border-subtle"
              data-card={row.card}
              data-correct-box={row.correctBox}
              data-pct-correct={row.pctCorrect ?? ''}
              data-pct-left-out={row.pctLeftOut ?? ''}
              data-wrong-box={row.mostCommonWrongBox ?? ''}
            >
              <td className="py-1.5 pr-3 font-semibold text-primary">{cards[row.card]}</td>
              <td className="py-1.5 pr-3 text-success">{label(boxes, row.correctBox)}</td>
              <td className="py-1.5 pr-3">
                <span className="flex items-center gap-2">
                  {big && (
                    <span className="h-3 w-40 overflow-hidden rounded-full bg-meter-track" aria-hidden="true">
                      <span
                        className="block h-full rounded-full bg-meter-success"
                        style={{ width: `${answered > 0 ? (row.correctCount / answered) * 100 : 0}%` }}
                      />
                    </span>
                  )}
                  <span className="tabular-nums">{pctText(row.pctCorrect)}</span>
                </span>
              </td>
              <td className="py-1.5 pr-3 tabular-nums text-primary">{pctText(row.pctLeftOut)}</td>
              {big && <td className="py-1.5 text-primary">{label(boxes, row.mostCommonWrongBox)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  const rows = ordered(sortCompletenessRows(n, k, distribution, answered));
  return (
    <table className={`w-full ${big ? 'text-lg' : 'text-sm'} text-primary`} data-sort-results="completeness">
      <thead>
        <tr className="text-left text-xs uppercase tracking-wider text-secondary">
          <th className="pb-2 pr-3 font-semibold">Card</th>
          <th className="pb-2 pr-3 font-semibold">Most common box</th>
          <th className="pb-2 font-semibold">%</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row: SortCompletenessRow) => (
          <tr
            key={row.card}
            className="border-t border-subtle"
            data-card={row.card}
            data-common-box={row.mostCommonBox ?? ''}
            data-pct-common={row.pctMostCommon ?? ''}
          >
            <td className="py-1.5 pr-3 font-semibold text-primary">{cards[row.card]}</td>
            <td className="py-1.5 pr-3 text-accent">{label(boxes, row.mostCommonBox)}</td>
            <td className="py-1.5 tabular-nums">{pctText(row.pctMostCommon)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The projector's Sort It board during a question: box labels and the cards in `cardOrder`. */
export function SortItBoard({ boxes, cards, order }: { boxes: string[]; cards: string[]; order: number[] }) {
  return (
    <div className="flex w-full max-w-5xl flex-col gap-5">
      <div className="flex flex-wrap justify-center gap-3" aria-label="Cards">
        {order
          .filter((i) => cards[i] !== undefined)
          .map((i) => (
            <span
              key={i}
              className="rounded-xl border border-subtle bg-surface px-4 py-2 text-2xl font-semibold text-primary"
            >
              {cards[i]}
            </span>
          ))}
      </div>
      <div
        className="grid gap-4"
        style={{ gridTemplateColumns: `repeat(${Math.max(1, boxes.length)}, minmax(0, 1fr))` }}
        aria-label="Boxes"
      >
        {boxes.map((box, i) => (
          <div
            key={i}
            className="flex min-h-24 items-start justify-center rounded-2xl border-2 border-dashed border-accent bg-accent-subtle p-4"
          >
            <span className="text-2xl font-bold text-accent">{box}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
