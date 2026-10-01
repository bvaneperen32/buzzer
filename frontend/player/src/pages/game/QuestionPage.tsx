import { useRef, useState, useCallback } from 'react';
import { useGame } from './GameLayout';
import { SafeImage } from '../../components/ui/SafeImage';
import { TimerBar } from '../../components/ui/TimerBar';
import { SortBoard } from '../../components/SortBoard';
import { SpotCanvas } from '../../components/SpotCanvas';
import { hasCaption, imageOptionName, optionImageLinks } from '../../lib/questionImages';
import { readGrid } from '../../lib/spotGrid';
import type { OptionImageLink } from '../../types/game';

// Answer tiles: a theme-aware fill per option (red, blue, yellow, green, purple, orange, pink,
// teal), cycling modulo 8; every on-option text token is the same dark text, so a question never
// mixes white and dark tile text. No hover colour: on a phone it sticks after a tap.
// border-transparent stops each tile's border-2 drawing the default grey border around the fill.
const OPTION_COLORS = [
  'bg-option-1 text-on-option-1 border-transparent',
  'bg-option-2 text-on-option-2 border-transparent',
  'bg-option-3 text-on-option-3 border-transparent',
  'bg-option-4 text-on-option-4 border-transparent',
  'bg-option-5 text-on-option-5 border-transparent',
  'bg-option-6 text-on-option-6 border-transparent',
  'bg-option-7 text-on-option-7 border-transparent',
  'bg-option-8 text-on-option-8 border-transparent',
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
  const { currentQuestion, questionLocked, pendingAnswerId, rejectedAnswerId, emitAnswer } = useGame();
  const startTimeRef = useRef(Date.now());
  // Sent (or waiting for the socket to rejoin) and not yet acknowledged; the server's
  // acknowledgement moves the phone to "Answer locked in!". If the answer can't be delivered
  // (the question locked while reconnecting), this clears and the page shows the lock.
  const submitted = currentQuestion !== null && pendingAnswerId === currentQuestion.questionId;
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
        <p className="text-secondary">Loading question…</p>
      </div>
    );
  }

  function submit(answerData: Record<string, unknown>) {
    if (submitted) return;
    emitAnswer(currentQuestion!.questionId, answerData, Date.now() - startTimeRef.current);
  }

  const questionLabel = (
    <p className="text-secondary text-sm text-center tracking-wide uppercase mb-1">
      Question {currentQuestion.questionNumber} of {currentQuestion.totalQuestions}
    </p>
  );

  const lockedMsg = (
    <p className="text-center text-warning text-sm mt-4 font-semibold">
      {rejectedAnswerId === currentQuestion.questionId
        ? 'Too late — answers had already closed, so that answer didn’t count.'
        : 'Answers locked — waiting for results…'}
    </p>
  );

  if (currentQuestion.type === 'multiple_choice') {
    const options = currentQuestion.config.options ?? [];
    const links = optionImageLinks(currentQuestion);
    return (
      <div className="pt-16 pb-6 px-4 flex flex-col gap-3">
        <div className="pb-2">
          {questionLabel}
          <TimerBar key={currentQuestion.questionId} totalSeconds={currentQuestion.timeLimitSeconds} paused={questionLocked} />
        </div>
        {links.some(Boolean) ? (
          <OptionTiles
            options={options}
            links={links}
            disabled={submitted || questionLocked}
            tileClass={(i) => OPTION_COLORS[i % OPTION_COLORS.length]}
            badge={optionLabel}
            onTap={(i) => submit({ selectedIndex: i })}
          />
        ) : options.map((opt, i) => (
          <button
            key={i}
            disabled={submitted || questionLocked}
            onClick={() => submit({ selectedIndex: i })}
            className={`w-full flex items-center gap-4 rounded-2xl px-5 py-4 text-left font-semibold text-lg border-2 transition-all
              ${submitted || questionLocked ? 'opacity-50 cursor-not-allowed' : 'active:scale-95'}
              ${OPTION_COLORS[i % OPTION_COLORS.length]}`}
          >
            <span className="text-2xl font-black w-8 shrink-0">{optionLabel(i)}</span>
            <span>{opt}</span>
          </button>
        ))}
        {submitted && <p className="text-center text-secondary text-sm mt-4">Sending your answer…</p>}
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
          className={`w-full rounded-2xl py-8 font-black text-3xl border-2 bg-option-4 text-on-option-4 border-transparent transition-all
            ${submitted || questionLocked ? 'opacity-50 cursor-not-allowed' : 'active:scale-95'}`}
        >
          True
        </button>
        <button
          disabled={submitted || questionLocked}
          onClick={() => submit({ selectedValue: false })}
          className={`w-full rounded-2xl py-8 font-black text-3xl border-2 bg-option-1 text-on-option-1 border-transparent transition-all
            ${submitted || questionLocked ? 'opacity-50 cursor-not-allowed' : 'active:scale-95'}`}
        >
          False
        </button>
        {submitted && <p className="text-center text-secondary text-sm mt-2">Sending your answer…</p>}
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
            <p className="text-primary text-xl font-bold">Sending your answer…</p>
            <p className="text-secondary text-base italic">"{inputValue.trim()}"</p>
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
              className="w-full rounded-xl px-4 py-4 text-xl bg-raised border border-input text-primary placeholder:text-secondary"
            />
            <button
              type="submit"
              disabled={!inputValue.trim()}
              className="w-full rounded-2xl py-5 text-on-accent font-black text-2xl bg-accent hover:bg-accent-hover active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
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
      <div className="pt-16 pb-6 px-4 flex flex-col gap-3">
        <div className="pb-2">
          {questionLabel}
          <TimerBar key={currentQuestion.questionId} totalSeconds={currentQuestion.timeLimitSeconds} paused={questionLocked} />
        </div>
        <p className="text-center text-secondary text-sm">Select all that apply</p>
        {links.some(Boolean) ? (
          <OptionTiles
            options={options}
            links={links}
            disabled={submitted || questionLocked}
            tileClass={(i) =>
              selectedIndices.has(i)
                ? 'bg-accent border-accent text-on-accent'
                : 'bg-raised border-input text-primary'
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
                  ? 'bg-accent border-accent text-on-accent'
                  : 'bg-raised border-input text-primary'
                }`}
            >
              <span className={`text-xl font-black w-8 shrink-0 flex items-center justify-center rounded-md border-2 transition-colors
                ${isSelected ? 'bg-surface border-transparent text-accent' : 'border-input text-secondary'}`}>
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
            className="w-full rounded-2xl py-5 text-on-accent font-black text-2xl bg-accent hover:bg-accent-hover active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed mt-2"
          >
            Submit ({selectedIndices.size} selected)
          </button>
        )}
        {submitted && <p className="text-center text-secondary text-sm mt-4">Sending your answer…</p>}
        {!submitted && questionLocked && lockedMsg}
      </div>
    );
  }

  // T7. Both boards are keyed by question, so a lock, an unlock or a socket reconnect (whose
  // late-join `new_question` repeats the same question) never clears what the player has done;
  // they only toggle `disabled`. Nothing is ever submitted automatically.
  if (currentQuestion.type === 'sort_into_boxes') {
    const cards = currentQuestion.config.cards ?? [];
    return (
      <div className="pt-16 pb-6 px-4 flex flex-col gap-3">
        <div className="pb-2">
          {questionLabel}
          <TimerBar key={currentQuestion.questionId} totalSeconds={currentQuestion.timeLimitSeconds} paused={questionLocked} />
        </div>
        <SortBoard
          key={currentQuestion.questionId}
          boxes={currentQuestion.config.boxes ?? []}
          cards={cards}
          order={currentQuestion.cardOrder ?? cards.map((_, i) => i)}
          accuracy={currentQuestion.gradingType !== 'COMPLETENESS'}
          disabled={submitted || questionLocked}
          onSubmit={(placements) => submit({ placements })}
        />
        {submitted && <p className="text-center text-secondary text-sm mt-2">Sending your answer…</p>}
        {!submitted && questionLocked && lockedMsg}
      </div>
    );
  }

  if (currentQuestion.type === 'find_in_image') {
    const grid = readGrid(currentQuestion.config.grid);
    if (grid) {
      return (
        <div className="pt-16 pb-6 px-4 flex flex-col gap-3">
          <div className="pb-2">
            {questionLabel}
            <TimerBar key={currentQuestion.questionId} totalSeconds={currentQuestion.timeLimitSeconds} paused={questionLocked} />
          </div>
          <p className="text-center text-secondary text-sm">
            Find it on the big screen, then pick the same square here.
          </p>
          <SpotCanvas
            key={currentQuestion.questionId}
            grid={grid}
            width={currentQuestion.config.width ?? 4}
            height={currentQuestion.config.height ?? 3}
            disabled={submitted || questionLocked}
            onLockIn={(cell) => submit({ col: cell.col, row: cell.row })}
          />
          {submitted && <p className="text-center text-secondary text-sm mt-2">Sending your answer…</p>}
          {!submitted && questionLocked && lockedMsg}
        </div>
      );
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <p className="text-secondary">Unsupported question type.</p>
    </div>
  );
}
