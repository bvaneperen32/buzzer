import type {
  PlayerAnswerReveal,
  PlayerResultsPayload,
  QuestionSummaryItem,
  QuestionType,
} from '../types/game';
import { formatPoints } from './formatPoints';
import { cellLabel, readCell, sortCells } from './spotGrid';

// The phone's Sort It and Spot It result texts (T7 spec §2.3, §2.4). Built from the server's
// payload alone — the answer, the reveal and the points it sends — never from local state, so
// they're right after a reload between answering and results. Correctness follows the server's
// rules: a Spot It cell is correct when it is an accepted cell; Sort It counts cards.

export type Tone = 'full' | 'partial' | 'none' | 'recorded' | 'missing';

export const TONE_CLASS: Record<Tone, string> = {
  full: 'text-green-400',
  partial: 'text-amber-300',
  none: 'text-red-400',
  recorded: 'text-indigo-300',
  missing: 'text-slate-400',
};

export const T7_TYPES: QuestionType[] = ['sort_into_boxes', 'find_in_image'];

function readPlacements(answer: unknown): (number | null)[] | null {
  if (!answer || typeof answer !== 'object') return null;
  const placements = (answer as { placements?: unknown }).placements;
  return Array.isArray(placements) ? (placements as (number | null)[]) : null;
}

/** "6 of 8 correct, 1 left out" (the left-out part only when a card was left out). */
function sortCountsText(placements: (number | null)[], key: number[]): string {
  const right = placements.filter((box, i) => box !== null && box === key[i]).length;
  const leftOut = placements.filter((box) => box === null).length;
  return `${right} of ${key.length} correct${leftOut > 0 ? `, ${leftOut} left out` : ''}`;
}

function placedText(placements: (number | null)[]): string {
  return `${placements.filter((box) => box !== null).length} of ${placements.length} cards`;
}

function correctCells(reveal: PlayerAnswerReveal): string | null {
  return reveal.type === 'find_in_image'
    ? sortCells(reveal.acceptedCells).map(cellLabel).join(', ')
    : null;
}

function isAccepted(reveal: PlayerAnswerReveal, col: number, row: number): boolean {
  return reveal.type === 'find_in_image' && reveal.acceptedCells.some((c) => c.col === col && c.row === row);
}

/** The results screen: the main line, the extra lines, and "687.5 of 1,000 points". */
export function t7ResultLines(r: PlayerResultsPayload): { main: string; tone: Tone; extra: string[]; points: string } {
  const max = r.pointsValue ?? 0;
  const points = `${formatPoints(r.yourPoints)} of ${formatPoints(max)} points`;
  const tone: Tone = r.yourIsCorrect ? 'full' : r.yourPoints > 0 ? 'partial' : 'none';

  if (r.questionType === 'sort_into_boxes') {
    const placements = r.answered ? readPlacements(r.yourAnswer) : null;
    if (!placements) return { main: `No answer — ${points}`, tone: 'missing', extra: [], points: '' };
    if (r.gradingType === 'COMPLETENESS') {
      return { main: `Answer recorded — you placed ${placedText(placements)}`, tone: 'recorded', extra: [], points };
    }
    const key = r.answerReveal.type === 'sort_into_boxes' ? r.answerReveal.correctBoxes : [];
    return { main: sortCountsText(placements, key), tone, extra: [], points };
  }

  const cells = correctCells(r.answerReveal);
  const pick = r.answered ? readCell(r.yourAnswer) : null;
  if (!pick) {
    return { main: 'No answer', tone: 'missing', extra: cells ? [`Correct cells: ${cells}`] : [], points };
  }
  if (r.gradingType === 'COMPLETENESS') {
    return { main: `You picked ${cellLabel(pick)} — answer recorded`, tone: 'recorded', extra: [], points };
  }
  return {
    main: `You picked ${cellLabel(pick)} — ${r.yourIsCorrect ? 'Correct' : 'Incorrect'}`,
    tone: r.yourIsCorrect ? 'full' : 'none',
    extra: cells ? [`Correct cells: ${cells}`] : [],
    points,
  };
}

/** The game-over line, e.g. "Sort It — 6 of 8 correct, 1 left out · 687.5 / 1,000 pts". */
export function t7GameOverLine(item: QuestionSummaryItem): { text: string; tone: Tone } {
  const points = `${formatPoints(item.pointsAwarded)} / ${formatPoints(item.maxPoints)} pts`;
  const completeness = item.gradingType === 'COMPLETENESS';

  if (item.type === 'sort_into_boxes') {
    const placements = readPlacements(item.playerAnswer);
    if (!placements) return { text: `Sort It — No answer · ${points}`, tone: 'missing' };
    if (completeness) return { text: `Sort It — Placed ${placedText(placements)} · ${points}`, tone: 'recorded' };
    const key = item.answerReveal.type === 'sort_into_boxes' ? item.answerReveal.correctBoxes : [];
    const right = placements.every((box, i) => box === key[i]);
    return {
      text: `Sort It — ${sortCountsText(placements, key)} · ${points}`,
      tone: right ? 'full' : item.pointsAwarded > 0 ? 'partial' : 'none',
    };
  }

  const cells = correctCells(item.answerReveal);
  const cellsText = cells ? ` · Correct cells: ${cells}` : '';
  const pick = readCell(item.playerAnswer);
  if (!pick) return { text: `Spot It — No answer${cellsText} · ${points}`, tone: 'missing' };
  if (completeness) return { text: `Spot It — You picked ${cellLabel(pick)} · ${points}`, tone: 'recorded' };
  const correct = isAccepted(item.answerReveal, pick.col, pick.row);
  return {
    text: `Spot It — You picked ${cellLabel(pick)} — ${correct ? 'Correct' : 'Incorrect'}${cellsText} · ${points}`,
    tone: correct ? 'full' : 'none',
  };
}
