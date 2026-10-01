import { useNavigate } from 'react-router-dom';
import { useGame } from './GameLayout';
import { Button } from '../../components/ui/button';
import { SafeImage } from '../../components/ui/SafeImage';
import { SpotItCanvas } from '../../components/question/SpotItCanvas';
import { SortItResults } from '../../components/question/SortItResults';
import { canvasImageId, hasCaption, optionImageIds, promptImageId } from '../../lib/questionImages';
import { readGrid } from '../../lib/spotGrid';
import type { AnswerReveal, HostQuestionSummaryItem } from '../../types/game';
import type { LinkEntry } from '../../types/images';

/** An image resolved through the session link map; `link` is undefined when the map failed. */
type ImageFor = (id: number | null) => { link: LinkEntry | undefined } | null;

const TARGET_BUCKETS = 8;

interface Bucket {
  label: string;
  count: number;
  rangeStart: number;
  rangeEnd: number;
}

function chooseWidth(ceiling: number): number {
  const N = ceiling + 1;
  let bestW = 1;
  let bestDiff = Infinity;
  for (let w = 1; w * w <= N; w++) {
    if (N % w !== 0) continue;
    for (const cand of [w, N / w]) {
      const numBins = N / cand;
      if (numBins < 2) continue;
      const diff = Math.abs(numBins - TARGET_BUCKETS);
      if (diff < bestDiff) { bestDiff = diff; bestW = cand; }
    }
  }
  if (bestDiff > TARGET_BUCKETS) bestW = Math.max(1, Math.ceil(N / TARGET_BUCKETS));
  return bestW;
}

function buildBuckets(scores: number[], maxPossibleScore: number): Bucket[] {
  const ceiling = Math.max(maxPossibleScore, 1);
  const width = chooseWidth(ceiling);
  const numBuckets = Math.ceil((ceiling + 1) / width);
  const buckets: Bucket[] = Array.from({ length: numBuckets }, (_, i) => {
    const start = i * width;
    const end = Math.min(start + width - 1, ceiling);
    const label = start === end ? `${start}` : `${start}–${end}`;
    return { label, count: 0, rangeStart: start, rangeEnd: end };
  });
  for (const score of scores) {
    const idx = Math.min(Math.floor(score / width), numBuckets - 1);
    buckets[idx].count++;
  }
  return buckets;
}

// ---------------------------------------------------------------------------
// Per-question summary card
// ---------------------------------------------------------------------------

function optionLabel(i: number) { return String.fromCharCode(65 + i); }

function AnswerBar({ label, count, total, correct, colorClass }: {
  label: string; count: number; total: number; correct: boolean; colorClass: string;
}) {
  const pct = total > 0 ? (count / total) * 100 : 0;
  return (
    <div className="flex items-center gap-3">
      <span className={`w-6 text-center font-bold text-sm shrink-0 ${correct ? 'text-success' : 'text-primary'}`}>
        {label}
      </span>
      <div className="flex-1 h-6 bg-meter-track rounded overflow-hidden">
        <div
          className={`h-full rounded transition-all duration-500 ${colorClass}`}
          style={{ width: `${Math.max(pct, count > 0 ? 2 : 0)}%` }}
        />
      </div>
      <span className={`w-8 text-right text-sm font-semibold shrink-0 ${correct ? 'text-success' : 'text-primary'}`}>
        {count}
      </span>
      {correct && <span className="text-success text-xs font-bold shrink-0">✓</span>}
      {!correct && <span className="w-4 shrink-0" />}
    </div>
  );
}

function QuestionCard({ item, index, imageFor, refreshImages }: {
  item: HostQuestionSummaryItem;
  index: number;
  imageFor: ImageFor;
  refreshImages: () => Promise<void>;
}) {
  const { type, gradingType, prompt, config, answerReveal, answerDistribution, totalAnswered, totalPlayers, correctCount, avgAnswerTimeMs, pointsValue } = item;
  const promptImage = imageFor(promptImageId(config));
  const optionIds = type === 'multiple_choice' || type === 'multi_select' ? optionImageIds(config) : [];

  const typeLabel: Record<string, string> = {
    multiple_choice: 'Multiple Choice',
    true_false: 'True / False',
    fill_in_the_blank: 'Fill in the Blank',
    multi_select: 'Multi-Select',
    sort_into_boxes: 'Sort It',
    find_in_image: 'Spot It',
  };
  const spotGrid = type === 'find_in_image' ? readGrid(config.grid) : null;
  const spotImage = spotGrid ? imageFor(canvasImageId(config)) : null;

  const answeredPct = totalPlayers > 0 ? Math.round((totalAnswered / totalPlayers) * 100) : 0;
  const correctPct = totalAnswered > 0 ? Math.round((correctCount / totalAnswered) * 100) : 0;

  const reveal = answerReveal as AnswerReveal;

  function isCorrectIndex(i: number): boolean {
    if (reveal.type === 'multiple_choice') {
      return (reveal as { type: 'multiple_choice'; correctIndices: number[] }).correctIndices.includes(i);
    }
    if (reveal.type === 'multi_select') {
      return ((reveal as { type: 'multi_select'; answerPoints: number[] }).answerPoints[i] ?? 0) > 0;
    }
    return false;
  }
  function isCorrectTF(val: 'true' | 'false'): boolean {
    if (reveal.type !== 'true_false') return false;
    const rv = reveal as { type: 'true_false'; correctValue: boolean };
    return val === 'true' ? rv.correctValue : !rv.correctValue;
  }

  return (
    <div className="w-full bg-surface border border-subtle rounded-2xl p-5 space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-secondary text-xs uppercase tracking-widest font-semibold">
            Q{index + 1}
          </span>
          <span className="text-secondary">·</span>
          <span className="text-secondary text-xs uppercase tracking-widest">
            {typeLabel[type] ?? type}
          </span>
          <span className="text-secondary">·</span>
          <span className={`text-xs uppercase tracking-widest font-semibold ${gradingType === 'COMPLETENESS' ? 'text-warning' : 'text-accent'}`}>
            {gradingType === 'COMPLETENESS' ? 'Participation' : 'Accuracy'}
          </span>
          <span className="text-secondary">·</span>
          <span className="text-secondary text-xs">{pointsValue} {pointsValue === 1 ? 'pt' : 'pts'}</span>
        </div>
        <div className="text-right text-xs text-primary shrink-0">
          <span className="text-primary font-semibold">{totalAnswered}</span>/{totalPlayers} answered
          {avgAnswerTimeMs !== null && (
            <span className="ml-2 text-secondary">· avg {(avgAnswerTimeMs / 1000).toFixed(1)}s</span>
          )}
        </div>
      </div>

      {/* Prompt */}
      <p className="text-primary text-lg font-semibold leading-snug">{prompt}</p>
      {promptImage && (
        <SafeImage
          src={promptImage.link?.small_url ?? null}
          alt={promptImage.link?.description ?? 'Question image'}
          className="h-28"
          style={{
            aspectRatio: promptImage.link
              ? `${promptImage.link.width} / ${promptImage.link.height}`
              : '4 / 3',
          }}
          refresh={refreshImages}
          showRetry
        />
      )}

      {/* Distribution */}
      <div className="space-y-2">
        {(type === 'multiple_choice' || type === 'multi_select') && (config.options ?? []).map((opt, i) => {
          const count = answerDistribution[String(i)] ?? 0;
          const correct = isCorrectIndex(i);
          const image = imageFor(optionIds[i] ?? null);
          return (
            <div key={i} className="space-y-0.5">
              <div className="flex items-center gap-2 text-xs text-primary">
                {/* "A." then the caption; the letter alone when there's no caption (§2.11). */}
                <span className={`font-bold ${correct ? 'text-success' : ''}`}>
                  {hasCaption(opt) ? `${optionLabel(i)}.` : optionLabel(i)}
                </span>
                {image && (
                  <SafeImage
                    src={image.link?.small_url ?? null}
                    alt={image.link?.description ?? ''}
                    className="h-10 w-10 shrink-0"
                    fit="cover"
                    refresh={refreshImages}
                    compact
                  />
                )}
                {hasCaption(opt) && <span className={correct ? 'text-success' : ''}>{opt}</span>}
              </div>
              <AnswerBar
                label={optionLabel(i)}
                count={count}
                total={totalPlayers}
                correct={correct}
                colorClass={correct ? 'bg-meter-success' : 'bg-meter-neutral'}
              />
            </div>
          );
        })}

        {type === 'true_false' && (['true', 'false'] as const).map((val) => {
          const count = answerDistribution[val] ?? 0;
          const correct = isCorrectTF(val);
          return (
            <AnswerBar
              key={val}
              label={val === 'true' ? 'T' : 'F'}
              count={count}
              total={totalPlayers}
              correct={correct}
              colorClass={correct ? 'bg-meter-success' : 'bg-meter-danger'}
            />
          );
        })}

        {type === 'sort_into_boxes' && (
          <SortItResults
            boxes={config.boxes ?? []}
            cards={config.cards ?? []}
            order={(config.cards ?? []).map((_, i) => i)}
            distribution={answerDistribution}
            answered={totalAnswered}
            correctBoxes={reveal.type === 'sort_into_boxes' ? reveal.correctBoxes : null}
            variant="summary"
          />
        )}

        {spotGrid && (
          <SpotItCanvas
            mode="results"
            grid={spotGrid}
            width={config.width ?? spotImage?.link?.width ?? 4}
            height={config.height ?? spotImage?.link?.height ?? 3}
            image={{
              url: spotImage?.link?.small_url ?? null,
              description: spotImage?.link?.description ?? 'the Spot It image',
            }}
            refreshImage={refreshImages}
            maxHeightVh={32}
            band={22}
            counts={answerDistribution}
            accepted={reveal.type === 'find_in_image' ? reveal.acceptedCells : []}
            label={`Answers on the ${spotGrid.cols} by ${spotGrid.rows} grid`}
          />
        )}

        {type === 'fill_in_the_blank' && (
          <div className="space-y-2">
            {reveal.type === 'fill_in_the_blank' && (
              <p className="text-secondary text-sm">
                Accepted:{' '}
                {(reveal as { type: 'fill_in_the_blank'; acceptedAnswers: string[]; editDistance: number }).acceptedAnswers.map((a, i, arr) => (
                  <span key={i}>
                    <span className="text-success font-mono">"{a}"</span>
                    {i < arr.length - 1 && <span className="text-secondary">, </span>}
                  </span>
                ))}
                {(reveal as { type: 'fill_in_the_blank'; acceptedAnswers: string[]; editDistance: number }).editDistance > 0 && (
                  <span className="text-secondary ml-1">
                    (±{(reveal as { type: 'fill_in_the_blank'; acceptedAnswers: string[]; editDistance: number }).editDistance} typo)
                  </span>
                )}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Footer stats */}
      <div className="flex items-center gap-4 pt-1 border-t border-subtle text-sm">
        {gradingType === 'ACCURACY' ? (
          <>
            {/* Sort It's `correct` is a whole board; the per-card numbers are above. */}
            <span className="text-success font-semibold">
              {correctCount} {type === 'sort_into_boxes' ? 'all cards correct' : 'correct'}
            </span>
            <span className="text-secondary">·</span>
            <span className="text-primary">{correctPct}% accuracy</span>
            <span className="text-secondary">·</span>
            <span className="text-primary">{answeredPct}% responded</span>
          </>
        ) : (
          <>
            <span className="text-warning font-semibold">{totalAnswered} completed</span>
            <span className="text-secondary">·</span>
            <span className="text-primary">{answeredPct}% response rate</span>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function GameOverPage() {
  const { gameOver, imageLinks, refreshImageLinks, imageLinksFailed } = useGame();
  const navigate = useNavigate();

  const imageFor: ImageFor = (id) => {
    if (id === null) return null;
    const link = imageLinks[String(id)];
    return link || imageLinksFailed ? { link } : null;
  };

  if (!gameOver) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-secondary">Loading final results…</p>
      </div>
    );
  }

  const { scores, playerCount, maxPossibleScore, questionSummary = [] } = gameOver;
  const buckets = buildBuckets(scores, maxPossibleScore);
  const maxCount = Math.max(...buckets.map(b => b.count), 1);

  const avg = scores.length > 0
    ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length)
    : 0;
  const high = scores.length > 0 ? Math.max(...scores) : 0;

  return (
    <div className="min-h-screen flex flex-col items-center p-8 gap-8">
      <div className="text-center">
        <h1 className="text-5xl font-black text-primary">Game Over!</h1>
        <p className="text-primary mt-2">{playerCount} players · Max possible: {maxPossibleScore.toLocaleString()} pts</p>
      </div>

      {/* Histogram */}
      <div className="w-full max-w-3xl">
        <p className="text-secondary text-sm uppercase tracking-widest text-center mb-4">Score Distribution</p>
        <div className="flex items-end gap-2 h-48">
          {buckets.map((bucket, i) => {
            const heightPct = (bucket.count / maxCount) * 100;
            return (
              <div key={i} className="flex-1 flex flex-col items-center gap-1">
                <span className="text-primary text-sm font-semibold">
                  {bucket.count > 0 ? bucket.count : ''}
                </span>
                <div className="w-full bg-meter-track rounded-t-lg relative" style={{ height: '160px' }}>
                  <div
                    className="absolute bottom-0 left-0 right-0 bg-meter-accent rounded-t-lg transition-all duration-700"
                    style={{ height: `${Math.max(heightPct, bucket.count > 0 ? 4 : 0)}%` }}
                  />
                </div>
                <span className="text-secondary text-xs">{bucket.label}</span>
              </div>
            );
          })}
        </div>
        <p className="text-secondary text-xs text-center mt-1">Score (points) →</p>
      </div>

      {/* Summary stats */}
      <div className="flex gap-12 text-center">
        <div>
          <p className="text-secondary text-xs uppercase tracking-widest">Average</p>
          <p className="text-primary text-2xl font-bold">{avg.toLocaleString()}</p>
        </div>
        <div>
          <p className="text-secondary text-xs uppercase tracking-widest">High Score</p>
          <p className="text-primary text-2xl font-bold">{high.toLocaleString()}</p>
        </div>
        <div>
          <p className="text-secondary text-xs uppercase tracking-widest">Players</p>
          <p className="text-primary text-2xl font-bold">{playerCount}</p>
        </div>
      </div>

      <Button onClick={() => navigate('/home')}>New Game</Button>

      {/* Per-question breakdown */}
      {questionSummary.length > 0 && (
        <div className="w-full max-w-3xl space-y-4 pb-8">
          <p className="text-secondary text-sm uppercase tracking-widest text-center">
            Question Breakdown
          </p>
          {questionSummary.map((item, i) => (
            <QuestionCard
              key={item.questionId}
              item={item}
              index={i}
              imageFor={imageFor}
              refreshImages={refreshImageLinks}
            />
          ))}
        </div>
      )}
    </div>
  );
}
