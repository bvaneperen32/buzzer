import { useNavigate } from 'react-router-dom';
import { useGame } from './GameLayout';
import { Button } from '../../components/ui/button';
import { choiceLabel } from '../../lib/questionImages';
import type { PlayerAnswerReveal, QuestionSummaryItem } from '../../types/game';

function describePlayerAnswer(
  playerAnswer: { selectedIndex?: number; selectedValue?: boolean; text?: string; selectedIndices?: number[] } | null,
  type: string,
  options: string[] | undefined,
): string {
  if (!playerAnswer) return 'No answer';
  if (type === 'multiple_choice') {
    const idx = playerAnswer.selectedIndex;
    if (typeof idx === 'number' && options && options[idx] !== undefined) {
      return choiceLabel(idx, options[idx]);
    }
    return '?';
  }
  if (type === 'true_false') {
    return playerAnswer.selectedValue ? 'True' : 'False';
  }
  if (type === 'fill_in_the_blank') {
    return typeof playerAnswer.text === 'string' ? playerAnswer.text : '—';
  }
  if (type === 'multi_select') {
    const indices = playerAnswer.selectedIndices;
    if (!indices || indices.length === 0) return 'No selection';
    if (options) return indices.map(i => choiceLabel(i, options[i])).join(', ');
    return indices.map(i => String.fromCharCode(65 + i)).join(', ');
  }
  return '—';
}

function describeCorrectAnswer(
  answerReveal: PlayerAnswerReveal,
  options: string[] | undefined,
): string {
  if (answerReveal.type === 'multiple_choice') {
    const indices = answerReveal.correctIndices;
    if (options) {
      return indices.map(i => choiceLabel(i, options[i])).join(', ');
    }
    return indices.map(i => String.fromCharCode(65 + i)).join(', ');
  }
  if (answerReveal.type === 'true_false') {
    return answerReveal.correctValue ? 'True' : 'False';
  }
  if (answerReveal.type === 'fill_in_the_blank') {
    const label = answerReveal.acceptedAnswers.join(' / ');
    return answerReveal.editDistance > 0 ? `${label} (±${answerReveal.editDistance})` : label;
  }
  if (answerReveal.type === 'multi_select') {
    const correct = answerReveal.answerPoints
      .map((p, i) => ({ p, i }))
      .filter(({ p }) => p > 0)
      .map(({ i }) => options ? choiceLabel(i, options[i]) : String.fromCharCode(65 + i));
    return correct.join(', ');
  }
  return '';
}

function QuestionRow({ item, index }: { item: QuestionSummaryItem; index: number }) {
  const options = item.config.options;
  const isCompleteness = item.answerReveal.type === 'completeness';
  const noAnswer = !item.playerAnswer;
  const isCorrect = !isCompleteness && !noAnswer && item.pointsAwarded > 0;
  const showCorrectAnswer = !isCompleteness && !isCorrect;

  const playerAnswerLabel = describePlayerAnswer(item.playerAnswer, item.type, options);
  const correctAnswerLabel = !isCompleteness
    ? describeCorrectAnswer(item.answerReveal, options)
    : '';

  return (
    <div className="bg-slate-800/60 rounded-xl p-4 border border-slate-700/50">
      <p className="text-slate-400 text-xs uppercase tracking-wider mb-1">Q{index + 1}</p>
      <p className="text-slate-100 text-sm font-medium leading-snug mb-3">{item.prompt}</p>

      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <p className="text-slate-500 text-xs mb-0.5">Your answer</p>
          <div className="flex items-center gap-2">
            {noAnswer ? (
              <span className="text-slate-600 text-base">—</span>
            ) : isCompleteness ? (
              <span className="text-indigo-400 text-base">●</span>
            ) : isCorrect ? (
              <span className="text-green-400 text-base">✓</span>
            ) : (
              <span className="text-red-400 text-base">✗</span>
            )}
            <span className={`text-sm font-semibold truncate ${
              noAnswer ? 'text-slate-600' :
              isCompleteness ? 'text-indigo-300' :
              isCorrect ? 'text-green-300' : 'text-red-300'
            }`}>
              {playerAnswerLabel}
            </span>
          </div>

          {showCorrectAnswer && correctAnswerLabel && (
            <p className="text-slate-500 text-xs mt-1">
              Correct: <span className="text-green-400 font-medium">{correctAnswerLabel}</span>
            </p>
          )}
        </div>

        <div className="text-right shrink-0">
          <p className={`text-sm font-bold ${
            item.pointsAwarded > 0 ? 'text-green-400' : 'text-slate-600'
          }`}>
            +{item.pointsAwarded.toLocaleString()}
          </p>
          <p className="text-slate-600 text-xs">pts</p>
        </div>
      </div>
    </div>
  );
}

export default function GameOverPage() {
  const { gameOver } = useGame();
  const navigate = useNavigate();

  if (!gameOver) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-slate-400">Loading final results…</p>
      </div>
    );
  }

  const { yourFinalScore, yourFinalRank, playerCount, questionSummary } = gameOver;

  return (
    <div className="min-h-screen flex flex-col p-4 gap-5">
      <div className="text-center pt-4 pb-2">
        <h1 className="text-4xl font-black text-slate-100">Game Over!</h1>
        <div className="mt-3 flex items-baseline justify-center gap-3">
          <span className="text-5xl font-black text-indigo-400">#{yourFinalRank}</span>
          <span className="text-slate-400 text-lg">of {playerCount}</span>
        </div>
        <p className="text-white text-2xl font-bold mt-1">
          {yourFinalScore.toLocaleString()} pts
        </p>
      </div>

      <div className="flex flex-col gap-3 pb-4">
        <p className="text-slate-500 text-xs uppercase tracking-widest text-center">Question Recap</p>
        {questionSummary.map((item, i) => (
          <QuestionRow key={item.questionId} item={item} index={i} />
        ))}
      </div>

      <div className="pb-6 flex justify-center">
        <Button
          variant="outline"
          onClick={() => {
            localStorage.removeItem('token');
            navigate('/join');
          }}
        >
          Play Again
        </Button>
      </div>
    </div>
  );
}
