import { useRef, useState, useCallback } from 'react';
import { useGame } from './GameLayout';
import { SafeImage } from '../../components/ui/SafeImage';
import { TimerBar } from '../../components/ui/TimerBar';
import { hasCaption, imageOptionName, optionImageLinks } from '../../lib/questionImages';
import type { OptionImageLink } from '../../types/game';

const OPTION_COLORS = [
  'bg-red-600 hover:bg-red-500 border-red-500',
  'bg-blue-600 hover:bg-blue-500 border-blue-500',
  'bg-yellow-500 hover:bg-yellow-400 border-yellow-400',
  'bg-green-600 hover:bg-green-500 border-green-500',
  'bg-purple-600 hover:bg-purple-500 border-purple-500',
  'bg-orange-500 hover:bg-orange-400 border-orange-400',
  'bg-pink-600 hover:bg-pink-500 border-pink-500',
  'bg-teal-600 hover:bg-teal-500 border-teal-500',
];

function optionLabel(i: number): string {
  return String.fromCharCode(65 + i); // A, B, C, … Z
}

/**
 * Image options as 2-column tiles (T8): a letter badge, the small image, and the caption if any.
 * An option without an image (or whose id wasn't in `images`) is a text-only tile. Tapping works
 * exactly as with text options; a failed image never blocks it — the letter is always there.
 */
function OptionTiles({
  options,
  links,
  disabled,
  tileClass,
  badge,
  onTap,
  pressed,
}: {
  options: string[];
  links: (OptionImageLink | null)[];
  disabled: boolean;
  tileClass: (i: number) => string;
  badge: (i: number) => string;
  onTap: (i: number) => void;
  pressed?: (i: number) => boolean;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      {options.map((opt, i) => {
        const link = links[i];
        return (
          <button
            key={i}
            disabled={disabled}
            onClick={() => onTap(i)}
            aria-label={link ? imageOptionName(i, opt, link.description) : undefined}
            aria-pressed={pressed ? pressed(i) : undefined}
            className={`flex flex-col overflow-hidden rounded-2xl border-2 text-left font-semibold transition-all
              ${disabled ? 'opacity-50 cursor-not-allowed' : 'active:scale-95'}
              ${tileClass(i)}`}
          >
            <span className="flex items-center gap-2 px-3 py-2">
              <span className="text-xl font-black w-7 shrink-0">{badge(i)}</span>
              {hasCaption(opt) && <span className="text-base leading-tight line-clamp-2">{opt}</span>}
            </span>
            {link && (
              <SafeImage key={link.url} src={link.url} alt={link.description} className="aspect-square w-full rounded-none" />
            )}
          </button>
        );
      })}
    </div>
  );
}

export default function QuestionPage() {
  const { currentQuestion, questionLocked, emitAnswer } = useGame();
  const startTimeRef = useRef(Date.now());
  const [submitted, setSubmitted] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const [selectedIndices, setSelectedIndices] = useState<Set<number>>(new Set());

  const toggleIndex = useCallback((i: number) => {
    if (submitted || questionLocked) return;
    setSelectedIndices(prev => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  }, [submitted, questionLocked]);

  if (!currentQuestion) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-slate-400">Loading question…</p>
      </div>
    );
  }

  function submit(answerData: Record<string, unknown>) {
    if (submitted) return;
    setSubmitted(true);
    emitAnswer(currentQuestion!.questionId, answerData, Date.now() - startTimeRef.current);
  }

  const questionLabel = (
    <p className="text-slate-400 text-sm text-center tracking-wide uppercase mb-1">
      Question {currentQuestion.questionNumber} of {currentQuestion.totalQuestions}
    </p>
  );

  const lockedMsg = (
    <p className="text-center text-amber-400 text-sm mt-4 font-semibold">
      Answers locked — waiting for results…
    </p>
  );

  if (currentQuestion.type === 'multiple_choice') {
    const options = currentQuestion.config.options ?? [];
    const links = optionImageLinks(currentQuestion);
    return (
      <div className="py-6 px-4 flex flex-col gap-3">
        <div className="pb-2">
          {questionLabel}
          <TimerBar key={currentQuestion.questionId} totalSeconds={currentQuestion.timeLimitSeconds} paused={questionLocked} />
        </div>
        {links.some(Boolean) ? (
          <OptionTiles
            options={options}
            links={links}
            disabled={submitted || questionLocked}
            tileClass={(i) => `text-white ${OPTION_COLORS[i % OPTION_COLORS.length]}`}
            badge={optionLabel}
            onTap={(i) => submit({ selectedIndex: i })}
          />
        ) : options.map((opt, i) => (
          <button
            key={i}
            disabled={submitted || questionLocked}
            onClick={() => submit({ selectedIndex: i })}
            className={`w-full flex items-center gap-4 rounded-2xl px-5 py-4 text-left text-white font-semibold text-lg border-2 transition-all
              ${submitted || questionLocked ? 'opacity-50 cursor-not-allowed' : 'active:scale-95'}
              ${OPTION_COLORS[i % OPTION_COLORS.length]}`}
          >
            <span className="text-2xl font-black w-8 shrink-0">{optionLabel(i)}</span>
            <span>{opt}</span>
          </button>
        ))}
        {submitted && <p className="text-center text-slate-400 text-sm mt-4">Answer submitted — waiting for results…</p>}
        {!submitted && questionLocked && lockedMsg}
      </div>
    );
  }

  if (currentQuestion.type === 'true_false') {
    return (
      <div className="min-h-screen flex flex-col justify-center p-4 gap-4">
        {questionLabel}
        <TimerBar key={currentQuestion.questionId} totalSeconds={currentQuestion.timeLimitSeconds} paused={questionLocked} />
        <button
          disabled={submitted || questionLocked}
          onClick={() => submit({ selectedValue: true })}
          className={`w-full rounded-2xl py-8 text-white font-black text-3xl border-2 border-green-500 transition-all
            ${submitted || questionLocked ? 'opacity-50 cursor-not-allowed bg-green-700' : 'bg-green-600 hover:bg-green-500 active:scale-95'}`}
        >
          True
        </button>
        <button
          disabled={submitted || questionLocked}
          onClick={() => submit({ selectedValue: false })}
          className={`w-full rounded-2xl py-8 text-white font-black text-3xl border-2 border-red-500 transition-all
            ${submitted || questionLocked ? 'opacity-50 cursor-not-allowed bg-red-700' : 'bg-red-600 hover:bg-red-500 active:scale-95'}`}
        >
          False
        </button>
        {submitted && <p className="text-center text-slate-400 text-sm mt-2">Answer submitted — waiting for results…</p>}
        {!submitted && questionLocked && lockedMsg}
      </div>
    );
  }

  if (currentQuestion.type === 'fill_in_the_blank') {
    const maxLength = currentQuestion.config.maxLength ?? 100;
    return (
      <div className="min-h-screen flex flex-col justify-center p-4 gap-5">
        {questionLabel}
        <TimerBar key={currentQuestion.questionId} totalSeconds={currentQuestion.timeLimitSeconds} paused={questionLocked} />

        {submitted ? (
          <div className="flex flex-col items-center gap-3">
            <div className="text-5xl">🔒</div>
            <p className="text-slate-100 text-xl font-bold">Answer locked in!</p>
            <p className="text-slate-400 text-base italic">"{inputValue.trim()}"</p>
            <p className="text-slate-500 text-sm mt-2">Waiting for results…</p>
          </div>
        ) : questionLocked ? (
          <div className="flex flex-col items-center gap-3 mt-4">
            {lockedMsg}
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const trimmed = inputValue.trim();
              if (trimmed) submit({ text: trimmed });
            }}
            className="flex flex-col gap-4"
          >
            <input
              type="text"
              maxLength={maxLength}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder="Type your answer…"
              autoFocus
              className="w-full rounded-xl px-4 py-4 text-slate-900 text-xl bg-white placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
            <button
              type="submit"
              disabled={!inputValue.trim()}
              className="w-full rounded-2xl py-5 text-white font-black text-2xl bg-indigo-600 hover:bg-indigo-500 active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Submit
            </button>
          </form>
        )}
      </div>
    );
  }

  if (currentQuestion.type === 'multi_select') {
    const options = currentQuestion.config.options ?? [];
    const links = optionImageLinks(currentQuestion);
    return (
      <div className="py-6 px-4 flex flex-col gap-3">
        <div className="pb-2">
          {questionLabel}
          <TimerBar key={currentQuestion.questionId} totalSeconds={currentQuestion.timeLimitSeconds} paused={questionLocked} />
        </div>
        <p className="text-center text-slate-400 text-sm">Select all that apply</p>
        {links.some(Boolean) ? (
          <OptionTiles
            options={options}
            links={links}
            disabled={submitted || questionLocked}
            tileClass={(i) =>
              selectedIndices.has(i)
                ? 'bg-indigo-600 border-indigo-400 text-white'
                : 'bg-slate-700 border-slate-600 text-slate-200'
            }
            badge={(i) => (selectedIndices.has(i) ? '✓' : optionLabel(i))}
            onTap={toggleIndex}
            pressed={(i) => selectedIndices.has(i)}
          />
        ) : options.map((opt, i) => {
          const isSelected = selectedIndices.has(i);
          return (
            <button
              key={i}
              disabled={submitted || questionLocked}
              onClick={() => toggleIndex(i)}
              className={`w-full flex items-center gap-4 rounded-2xl px-5 py-4 text-left font-semibold text-lg border-2 transition-all
                ${submitted || questionLocked ? 'opacity-50 cursor-not-allowed' : 'active:scale-95'}
                ${isSelected
                  ? 'bg-indigo-600 border-indigo-400 text-white'
                  : 'bg-slate-700 border-slate-600 text-slate-200 hover:bg-slate-600'
                }`}
            >
              <span className={`text-xl font-black w-8 shrink-0 flex items-center justify-center rounded-md border-2 transition-colors
                ${isSelected ? 'border-white bg-white text-indigo-700' : 'border-slate-500 text-slate-400'}`}>
                {isSelected ? '✓' : optionLabel(i)}
              </span>
              <span>{opt}</span>
            </button>
          );
        })}
        {!submitted && !questionLocked && (
          <button
            disabled={selectedIndices.size === 0}
            onClick={() => submit({ selectedIndices: Array.from(selectedIndices) })}
            className="w-full rounded-2xl py-5 text-white font-black text-2xl bg-indigo-600 hover:bg-indigo-500 active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed mt-2"
          >
            Submit ({selectedIndices.size} selected)
          </button>
        )}
        {submitted && <p className="text-center text-slate-400 text-sm mt-4">Answer submitted — waiting for results…</p>}
        {!submitted && questionLocked && lockedMsg}
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <p className="text-slate-400">Unsupported question type.</p>
    </div>
  );
}
