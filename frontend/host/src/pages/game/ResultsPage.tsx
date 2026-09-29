import { useEffect, useState } from 'react';
import { useGame } from './GameLayout';
import { Button } from '../../components/ui/button';
import { SafeImage } from '../../components/ui/SafeImage';
import {
  hasCaption,
  imageOptionName,
  optionImageIds,
  optionLetter as optionLabel,
  promptImageId,
} from '../../lib/questionImages';
import type { AnswerReveal } from '../../types/game';
import type { LinkEntry } from '../../types/images';

const RESULTS_DISPLAY_SECONDS = 10;

/** "A  Paris", or the letter alone when the option has no caption (T8 spec §2.11). */
function choiceLabel(i: number, caption: string | undefined): string {
  return hasCaption(caption) ? `${optionLabel(i)}  ${caption}` : optionLabel(i);
}

/** An option's image on the results screen: its link, or none when the map can't resolve it. */
interface BarImage {
  link: LinkEntry | undefined;
}

// ---------------------------------------------------------------------------
// Levenshtein distance — used to colour word-cloud entries for FITB questions
// ---------------------------------------------------------------------------

function levenshtein(a: string, b: string): number {
  const m = a.length, n = b.length;
  const dp: number[][] = Array.from({ length: m + 1 }, (_, i) =>
    Array.from({ length: n + 1 }, (_, j) => (i === 0 ? j : j === 0 ? i : 0))
  );
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] =
        a[i - 1] === b[j - 1]
          ? dp[i - 1][j - 1]
          : 1 + Math.min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1]);
    }
  }
  return dp[m][n];
}

// ---------------------------------------------------------------------------
// Word Cloud — for fill_in_the_blank questions
// ---------------------------------------------------------------------------

interface WordCloudProps {
  distribution: Record<string, number>;
  answerReveal: AnswerReveal;
  totalAnswered: number;
  totalPlayers: number;
}

function WordCloud({ distribution, answerReveal, totalAnswered, totalPlayers }: WordCloudProps) {
  const entries = Object.entries(distribution).sort(([, a], [, b]) => b - a);
  const maxCount = Math.max(...entries.map(([, c]) => c), 1);

  const isAccuracy =
    'type' in answerReveal && answerReveal.type === 'fill_in_the_blank';
  const acceptedAnswers: string[] =
    isAccuracy && 'acceptedAnswers' in answerReveal
      ? (answerReveal as { type: 'fill_in_the_blank'; acceptedAnswers: string[]; editDistance: number }).acceptedAnswers.map(a => a.toLowerCase())
      : [];
  const editDistance: number =
    isAccuracy && 'editDistance' in answerReveal
      ? (answerReveal as { type: 'fill_in_the_blank'; acceptedAnswers: string[]; editDistance: number }).editDistance
      : 0;

  function sizeClass(count: number): string {
    const f = count / maxCount;
    if (f > 0.75) return 'text-5xl font-black';
    if (f > 0.5)  return 'text-4xl font-bold';
    if (f > 0.25) return 'text-3xl font-semibold';
    if (f > 0.1)  return 'text-2xl font-medium';
    return 'text-xl';
  }

  function isCorrectWord(word: string): boolean {
    if (!isAccuracy || acceptedAnswers.length === 0) return false;
    return acceptedAnswers.some(a => levenshtein(word.toLowerCase(), a) <= editDistance);
  }

  return (
    <div className="w-full max-w-3xl flex flex-col gap-4">
      {isAccuracy && acceptedAnswers.length > 0 && (
        <p className="text-center text-slate-400 text-base">
          Correct answer:{' '}
          <span className="text-green-400 font-semibold">
            {(answerReveal as { acceptedAnswers: string[] }).acceptedAnswers.join(' / ')}
            {editDistance > 0 && (
              <span className="text-slate-500 font-normal text-sm ml-1">(±{editDistance})</span>
            )}
          </span>
        </p>
      )}

      <div className="flex flex-wrap gap-x-5 gap-y-3 justify-center items-center min-h-32 p-4 bg-slate-800/40 rounded-2xl">
        {entries.map(([word, count]) => (
          <span
            key={word}
            title={`${count} player${count !== 1 ? 's' : ''}`}
            className={`${sizeClass(count)} transition-colors ${
              isCorrectWord(word) ? 'text-green-400' : 'text-slate-300'
            }`}
          >
            {word}
          </span>
        ))}
        {entries.length === 0 && (
          <p className="text-slate-600 text-sm">No answers submitted</p>
        )}
      </div>

      <p className="text-slate-500 text-sm text-right">
        {totalAnswered} / {totalPlayers} answered
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bar Chart — for multiple_choice and true_false
// ---------------------------------------------------------------------------

interface Bar {
  label: string;
  count: number;
  correct: boolean | null;
  /** Set on choice options, for the richer label that image options get. */
  index?: number;
  caption?: string;
  image?: BarImage | null;
}

interface BarChartProps {
  bars: Bar[];
  totalAnswered: number;
  totalPlayers: number;
  refreshImages: () => Promise<void>;
}

function AnswerBarChart({ bars, totalAnswered, totalPlayers, refreshImages }: BarChartProps) {
  const maxCount = Math.max(...bars.map(b => b.count), 1);
  // Text-only questions keep today's label; any image option switches every row to
  // letter + thumbnail + caption.
  const rich = bars.some(b => b.image);

  return (
    <div className="w-full max-w-2xl space-y-3">
      {bars.map((bar, i) => {
        const pct = Math.round((bar.count / maxCount) * 100);
        const barColor =
          bar.correct === true
            ? 'bg-green-500'
            : bar.correct === false
            ? 'bg-slate-600'
            : 'bg-indigo-500';

        return (
          <div
            key={i}
            className="flex items-center gap-3"
            aria-label={
              rich && bar.index !== undefined
                ? bar.image
                  ? imageOptionName(bar.index, bar.caption, bar.image.link?.description ?? '')
                  : bar.label
                : undefined
            }
          >
            {rich && bar.index !== undefined ? (
              <span className="flex items-center gap-2 w-60 shrink-0">
                <span className="text-slate-300 font-bold font-mono w-6 text-right shrink-0">
                  {optionLabel(bar.index)}
                </span>
                {bar.image && (
                  <SafeImage
                    src={bar.image.link?.small_url ?? null}
                    alt={bar.image.link?.description ?? ''}
                    className="h-12 w-12 shrink-0"
                    fit="cover"
                    refresh={refreshImages}
                    compact
                  />
                )}
                {hasCaption(bar.caption) && (
                  <span className="text-slate-300 text-sm truncate">{bar.caption}</span>
                )}
              </span>
            ) : (
              <span className="text-slate-300 font-bold font-mono w-8 text-right shrink-0">
                {bar.label}
              </span>
            )}
            <div className="flex-1 bg-slate-800 rounded-full h-10 overflow-hidden">
              <div
                className={`h-full rounded-full flex items-center justify-end pr-3 transition-all duration-500 ${barColor}`}
                style={{ width: `${Math.max(pct, bar.count > 0 ? 4 : 0)}%` }}
              >
                {bar.count > 0 && (
                  <span className="text-white font-bold text-sm">{bar.count}</span>
                )}
              </div>
            </div>
            {bar.correct === true && (
              <span className="text-green-400 text-sm font-semibold shrink-0">✓ Correct</span>
            )}
            {bar.count === 0 && bar.correct !== true && (
              <span className="text-slate-600 text-sm shrink-0">0</span>
            )}
          </div>
        );
      })}

      <p className="text-slate-500 text-sm text-right pt-1">
        {totalAnswered} / {totalPlayers} answered
      </p>
    </div>
  );
}

function buildBars(
  reveal: AnswerReveal,
  distribution: Record<string, number>,
  options: string[] | undefined,
  imageFor: (i: number) => BarImage | null = () => null,
): Bar[] {
  const choice = (opt: string, i: number) => ({
    label: choiceLabel(i, opt),
    index: i,
    caption: opt,
    image: imageFor(i),
    count: distribution[String(i)] ?? 0,
  });

  if (reveal.type === 'multiple_choice') {
    const opts = options ?? [];
    return opts.map((opt, i) => ({
      ...choice(opt, i),
      correct: reveal.correctIndices.includes(i),
    }));
  }

  if (reveal.type === 'multi_select') {
    const opts = options ?? [];
    return opts.map((opt, i) => ({
      ...choice(opt, i),
      correct: (reveal.answerPoints[i] ?? 0) > 0,
    }));
  }

  if (reveal.type === 'true_false') {
    return [
      { label: 'True', count: distribution['true'] ?? 0, correct: reveal.correctValue === true },
      { label: 'False', count: distribution['false'] ?? 0, correct: reveal.correctValue === false },
    ];
  }

  // completeness (non-FITB) — no right/wrong
  if (options && options.length > 0) {
    return options.map((opt, i) => ({ ...choice(opt, i), correct: null }));
  }

  if ('true' in distribution || 'false' in distribution) {
    return [
      { label: 'True', count: distribution['true'] ?? 0, correct: null },
      { label: 'False', count: distribution['false'] ?? 0, correct: null },
    ];
  }

  return [];
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ResultsPage() {
  const { questionResults, currentQuestion, autoAdvance, emitAdvance, imageLinks, refreshImageLinks, imageLinksFailed } = useGame();
  const [countdown, setCountdown] = useState(RESULTS_DISPLAY_SECONDS);

  const isLast = currentQuestion
    ? currentQuestion.questionNumber >= currentQuestion.totalQuestions
    : false;

  useEffect(() => {
    if (!autoAdvance) return;
    setCountdown(RESULTS_DISPLAY_SECONDS);
    const interval = setInterval(() => {
      setCountdown(prev => {
        if (prev <= 1) { clearInterval(interval); emitAdvance(); return 0; }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(interval);
  }, [autoAdvance, questionResults, emitAdvance]);

  const isFitb = currentQuestion?.type === 'fill_in_the_blank';

  // Images resolve through the session link map (an id it doesn't have is no image, unless the
  // map couldn't be fetched at all).
  const imageFor = (id: number | null): BarImage | null => {
    if (id === null) return null;
    const link = imageLinks[String(id)];
    return link || imageLinksFailed ? { link } : null;
  };
  const isChoice =
    currentQuestion?.type === 'multiple_choice' || currentQuestion?.type === 'multi_select';
  const optionIds = isChoice ? optionImageIds(currentQuestion?.config) : [];
  const prompt = imageFor(promptImageId(currentQuestion?.config));

  const bars =
    !isFitb && questionResults
      ? buildBars(
          questionResults.answerReveal,
          questionResults.answerDistribution,
          currentQuestion?.config?.options,
          (i) => imageFor(optionIds[i] ?? null),
        )
      : [];

  const hasImages = !!prompt || bars.some(b => b.image);

  return (
    // With images on screen, the bottom band stays clear for the fixed QR / room-code panel.
    <div className={`min-h-screen flex flex-col items-center justify-center p-8 gap-8 ${hasImages ? 'pb-48' : ''}`}>
      <h2 className="text-3xl font-bold text-slate-100">
        Question {currentQuestion?.questionNumber} Results
      </h2>

      {currentQuestion && (
        <p className="text-slate-300 text-xl text-center max-w-2xl">
          {currentQuestion.prompt}
        </p>
      )}

      {prompt && (
        <SafeImage
          src={prompt.link?.small_url ?? null}
          alt={prompt.link?.description ?? 'Question image'}
          className="h-40"
          style={{
            aspectRatio: prompt.link ? `${prompt.link.width} / ${prompt.link.height}` : '4 / 3',
          }}
          refresh={refreshImageLinks}
          showRetry
        />
      )}

      {questionResults && isFitb ? (
        <WordCloud
          distribution={questionResults.answerDistribution}
          answerReveal={questionResults.answerReveal}
          totalAnswered={questionResults.totalAnswered}
          totalPlayers={questionResults.totalPlayers}
        />
      ) : questionResults ? (
        <AnswerBarChart
          bars={bars}
          totalAnswered={questionResults.totalAnswered}
          totalPlayers={questionResults.totalPlayers}
          refreshImages={refreshImageLinks}
        />
      ) : null}

      <div className="flex flex-col items-center gap-2">
        <Button size="lg" onClick={emitAdvance} className="px-10">
          {isLast ? 'Show Final Results' : 'Next Question'}
        </Button>
        {autoAdvance && (
          <p className="text-slate-500 text-sm tabular-nums">
            Auto-advancing in {countdown}s
          </p>
        )}
      </div>
    </div>
  );
}
