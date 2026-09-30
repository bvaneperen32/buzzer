// Sort It's per-card results (T7 spec §2.3 "Results percentages"): the one TypeScript port of
// the backend's `sort_it.results_breakdown`, shared by ResultsPage and GameOverPage. The report
// uses the Python original; a browser check compares the two on one session. Change both.

export interface SortAccuracyRow {
  card: number;
  correctBox: number;
  correctCount: number;
  /** Integer percentages; null when nobody answered ("—"). */
  pctCorrect: number | null;
  pctLeftOut: number | null;
  /** The most frequent box other than the correct one; null when nobody placed it wrongly. */
  mostCommonWrongBox: number | null;
}

export interface SortCompletenessRow {
  card: number;
  mostCommonBox: number | null;
  mostCommonCount: number;
  pctMostCommon: number | null;
}

/**
 * A displayed percentage: an integer, rounded half up in integer arithmetic,
 * ⌊(200·count + answered) / (2·answered)⌋ — never Math.round (1 of 8 = 12.5 % → 13 %).
 */
export function percent(count: number, answered: number): number | null {
  if (answered <= 0) return null;
  return Math.floor((200 * count + answered) / (2 * answered));
}

/** The box with the highest count, ties to the lowest index; null when every count is 0. */
function mostCommon(counts: number[], exclude: number | null = null): number | null {
  let best: number | null = null;
  counts.forEach((count, box) => {
    if (box === exclude || count <= 0) return;
    if (best === null || count > counts[best]) best = box;
  });
  return best;
}

function countsFor(card: number, k: number, distribution: Record<string, number>) {
  const counts = Array.from({ length: k }, (_, box) => Number(distribution[`${card}:${box}`] ?? 0));
  return { counts, leftOut: Number(distribution[`${card}:none`] ?? 0) };
}

/** ACCURACY rows, from the `"{card}:{box}"` / `"{card}:none"` answer counts. */
export function sortAccuracyRows(
  n: number,
  k: number,
  distribution: Record<string, number>,
  answered: number,
  key: number[],
): SortAccuracyRow[] {
  return Array.from({ length: n }, (_, card) => {
    const { counts, leftOut } = countsFor(card, k, distribution);
    const correctBox = key[card];
    return {
      card,
      correctBox,
      correctCount: counts[correctBox] ?? 0,
      pctCorrect: percent(counts[correctBox] ?? 0, answered),
      pctLeftOut: percent(leftOut, answered),
      mostCommonWrongBox: answered > 0 ? mostCommon(counts, correctBox) : null,
    };
  });
}

/** COMPLETENESS rows: each card's most common box among the answers that placed it. */
export function sortCompletenessRows(
  n: number,
  k: number,
  distribution: Record<string, number>,
  answered: number,
): SortCompletenessRow[] {
  return Array.from({ length: n }, (_, card) => {
    const { counts } = countsFor(card, k, distribution);
    const box = answered > 0 ? mostCommon(counts) : null;
    return {
      card,
      mostCommonBox: box,
      mostCommonCount: box === null ? 0 : counts[box],
      pctMostCommon: box === null ? null : percent(counts[box], answered),
    };
  });
}

export function pctText(value: number | null): string {
  return value === null ? '—' : `${value}%`;
}
