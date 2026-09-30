import { useGame } from './GameLayout';
import { choiceLabel } from '../../lib/questionImages';
import { T7_TYPES, TONE_CLASS, t7ResultLines } from '../../lib/t7Results';
import type { PlayerResultsPayload } from '../../types/game';

/**
 * Sort It and Spot It results (T7 spec §2.7): read from the results payload alone — its answer,
 * reveal and points — never from this phone's local state, so a player who reloaded between
 * answering and results still sees everything, including an explicit "No answer".
 */
function T7Results({ results }: { results: PlayerResultsPayload }) {
  const { main, tone, extra, points } = t7ResultLines(results);
  return (
    <div className="min-h-screen flex flex-col items-center justify-center p-6 gap-4 text-center" data-t7-results={results.questionType}>
      <p className={`text-3xl font-black leading-tight ${TONE_CLASS[tone]}`}>{main}</p>
      {extra.map((line) => (
        <p key={line} className="text-slate-300 text-base">{line}</p>
      ))}
      {points && <p className="text-slate-100 text-2xl font-bold">{points}</p>}
      <div className="mt-2 space-y-1">
        <p className="text-slate-300 text-lg">
          Total: <span className="font-bold text-white">{results.yourScore.toLocaleString()}</span> pts
        </p>
        <p className="text-slate-400 text-base">
          Rank <span className="font-bold text-white">#{results.yourRank}</span> of {results.playerCount}
        </p>
      </div>
      <p className="text-slate-500 text-sm mt-4">Waiting for next question…</p>
    </div>
  );
}

function describeAnswer(
  lastAnswerData: Record<string, unknown> | null,
  type: string,
  options: string[] | undefined,
): string | null {
  if (!lastAnswerData) return null;
  if (type === 'multiple_choice') {
    const idx = lastAnswerData.selectedIndex;
    if (typeof idx === 'number' && options && options[idx] !== undefined) {
      return choiceLabel(idx, options[idx]);
    }
  }
  if (type === 'true_false') {
    return lastAnswerData.selectedValue ? 'True' : 'False';
  }
  if (type === 'fill_in_the_blank') {
    return typeof lastAnswerData.text === 'string' ? lastAnswerData.text : null;
  }
  return null;
}

export default function ResultsPage() {
  const { questionResults, currentQuestion, lastAnswerData } = useGame();

  if (!questionResults) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-slate-400">Loading results…</p>
      </div>
    );
  }

  if (questionResults.questionType && T7_TYPES.includes(questionResults.questionType)) {
    return <T7Results results={questionResults} />;
  }

  const { answerReveal, yourPoints, yourScore, yourRank, playerCount, answered } = questionResults;
  // Whether the answer counted is the server's to say: what this phone sent proves nothing (it
  // may have been refused as late, or dropped after a reconnect), and neither do zero points (a
  // wrong answer, or a question worth 0, still counts).
  const noAnswer = answered === false;
  const answeredLabel = noAnswer
    ? null
    : describeAnswer(lastAnswerData, currentQuestion?.type ?? '', currentQuestion?.config.options);
  const isCompleteness = answerReveal.type === 'completeness';
  const isFitb = answerReveal.type === 'fill_in_the_blank';
  const isCorrect = !isCompleteness && yourPoints > 0;

  const fitbAccepted: string[] =
    isFitb && 'acceptedAnswers' in answerReveal ? answerReveal.acceptedAnswers : [];

  return (
    <div
      className="min-h-screen flex flex-col items-center justify-center p-6 gap-5 text-center"
      data-results={currentQuestion?.type ?? ''}
    >

      {/* No answer / Recorded / Correct / Wrong */}
      {noAnswer ? (
        <p className={`${TONE_CLASS.missing} text-4xl font-black`}>No answer</p>
      ) : isCompleteness ? (
        <p className="text-indigo-400 text-4xl font-black">Answer recorded!</p>
      ) : isCorrect ? (
        <p className="text-green-400 text-4xl font-black">Correct!</p>
      ) : (
        <p className="text-red-400 text-4xl font-black">Incorrect</p>
      )}

      {/* What the player answered */}
      {answeredLabel && (
        <p className="text-slate-400 text-base">
          You answered: <span className="text-slate-200 font-semibold">{answeredLabel}</span>
        </p>
      )}

      {/* Correct answer for FITB ACCURACY */}
      {isFitb && fitbAccepted.length > 0 && (
        <p className="text-slate-400 text-base">
          Correct: <span className="text-green-400 font-semibold">{fitbAccepted.join(' / ')}</span>
        </p>
      )}

      {/* Points for this question */}
      <p className="text-slate-100 text-3xl font-bold">
        +{yourPoints.toLocaleString()} pts
      </p>

      {/* Running total and rank */}
      <div className="mt-2 space-y-1">
        <p className="text-slate-300 text-lg">
          Total: <span className="font-bold text-white">{yourScore.toLocaleString()}</span> pts
        </p>
        <p className="text-slate-400 text-base">
          Rank <span className="font-bold text-white">#{yourRank}</span> of {playerCount}
        </p>
      </div>

      <p className="text-slate-500 text-sm mt-4">Waiting for next question…</p>
    </div>
  );
}
