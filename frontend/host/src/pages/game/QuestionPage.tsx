import { useEffect } from 'react';
import { useGame } from './GameLayout';
import { Button } from '../../components/ui/button';
import { SafeImage } from '../../components/ui/SafeImage';
import { TimerBar } from '../../components/ui/TimerBar';
import { SpotItCanvas } from '../../components/question/SpotItCanvas';
import { SortItBoard } from '../../components/question/SortItResults';
import {
  canvasImageId,
  hasCaption,
  imageOptionName,
  optionImageIds,
  optionLetter,
  promptImageId,
} from '../../lib/questionImages';
import { readGrid } from '../../lib/spotGrid';
import type { LinkEntry } from '../../types/images';

/** Keeps a box at the image's aspect ratio, as large as `maxHeight` and the width allow. */
function fitStyle(link: LinkEntry | undefined, maxHeight: string) {
  const ratio = link && link.width > 0 && link.height > 0 ? link.width / link.height : 4 / 3;
  return { aspectRatio: String(ratio), width: `min(100%, calc(${maxHeight} * ${ratio}))` };
}

export default function QuestionPage() {
  const { currentQuestion, answeredCount, playerCount, allAnswered, answerPhaseEnded, questionLocked, lockedTimerSeconds, autoAdvance, emitAdvance, emitLockQuestion, imageLinks, refreshImageLinks, imageLinksFailed } = useGame();

  useEffect(() => {
    if (!autoAdvance || !answerPhaseEnded) return;
    const id = setTimeout(emitAdvance, 1500);
    return () => clearTimeout(id);
  }, [autoAdvance, answerPhaseEnded, emitAdvance]);

  if (!currentQuestion) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-slate-400">Loading question…</p>
      </div>
    );
  }

  const typeLabel: Record<string, string> = {
    multiple_choice: 'Multiple Choice',
    true_false: 'True / False',
    fill_in_the_blank: 'Fill in the Blank',
    multi_select: 'Multi-Select',
    sort_into_boxes: 'Sort It',
    find_in_image: 'Spot It',
  };

  function editDistanceLabel(d: number): string {
    if (d === 0) return 'Exact spelling required';
    if (d === 1) return 'Up to 1 typo allowed';
    return `Up to ${d} typos allowed`;
  }

  // Images come from the session link map. An id the map doesn't have is no image — unless the
  // map itself couldn't be fetched, in which case the image shows as unavailable.
  const linkFor = (id: number | null): LinkEntry | undefined =>
    id === null ? undefined : imageLinks[String(id)];
  const shown = (id: number | null) => id !== null && (!!linkFor(id) || imageLinksFailed);

  const promptId = promptImageId(currentQuestion.config);
  const promptLink = linkFor(promptId);
  const isChoice = currentQuestion.type === 'multiple_choice' || currentQuestion.type === 'multi_select';
  const options = currentQuestion.config.options ?? [];
  const optionIds = isChoice ? optionImageIds(currentQuestion.config) : [];
  const imageOptions = optionIds.some(shown);
  // T7: Sort It shows its boxes and cards (in the shared card order); Spot It its image under the
  // labelled grid, in the gameplay proportions the server stored.
  const isSort = currentQuestion.type === 'sort_into_boxes';
  const spotGrid = currentQuestion.type === 'find_in_image' ? readGrid(currentQuestion.config.grid) : null;
  const spotLink = linkFor(canvasImageId(currentQuestion.config));
  const hasImages = shown(promptId) || imageOptions || !!spotGrid;

  const remainingSeconds = questionLocked && lockedTimerSeconds !== null
    ? lockedTimerSeconds
    : currentQuestion.startedAt
      ? Math.max(0, currentQuestion.timeLimitSeconds - (Date.now() - new Date(currentQuestion.startedAt).getTime()) / 1000)
      : currentQuestion.timeLimitSeconds;

  return (
    // With images on screen the layout tightens to fit one projector screen, and the image area
    // stays narrower than the viewport by the fixed QR / room-code panel's width on each side, so
    // the panel never covers an image. Text-only questions keep their layout.
    <div className={`min-h-screen flex flex-col items-center justify-center ${hasImages ? 'gap-4 px-8 py-6' : 'p-8 gap-8'}`}>
      <div className="flex items-center gap-3 text-sm uppercase tracking-wider">
        <span className="text-slate-400">
          Question {currentQuestion.questionNumber} of {currentQuestion.totalQuestions}
        </span>
        <span className="text-slate-600">·</span>
        <span className="text-slate-400">{typeLabel[currentQuestion.type] ?? currentQuestion.type}</span>
        <span className="text-slate-600">·</span>
        <span className={currentQuestion.gradingType === 'COMPLETENESS' ? 'text-amber-400' : 'text-indigo-400'}>
          {currentQuestion.gradingType === 'COMPLETENESS' ? 'Participation' : 'Accuracy'}
        </span>
        {currentQuestion.type === 'fill_in_the_blank' && currentQuestion.gradingType === 'ACCURACY' && (
          <>
            <span className="text-slate-600">·</span>
            <span className="text-slate-400 normal-case">
              {editDistanceLabel(currentQuestion.editDistance ?? 0)}
            </span>
          </>
        )}
      </div>

      <div className="w-full max-w-3xl">
        <TimerBar
          key={currentQuestion.questionId}
          totalSeconds={currentQuestion.timeLimitSeconds}
          initialSeconds={remainingSeconds}
          paused={questionLocked}
        />
      </div>

      {!questionLocked && (
        <h2 className="text-4xl font-bold text-slate-100 text-center max-w-3xl leading-tight">
          {currentQuestion.prompt}
        </h2>
      )}

      {isSort && (
        <SortItBoard
          boxes={currentQuestion.config.boxes ?? []}
          cards={currentQuestion.config.cards ?? []}
          order={currentQuestion.cardOrder ?? (currentQuestion.config.cards ?? []).map((_, i) => i)}
        />
      )}

      {/* Spot It: the canvas stays up when the question locks (only the prompt text hides). */}
      {spotGrid && (
        <div className="w-full max-w-[calc(100vw-22rem)]">
          <SpotItCanvas
            key={currentQuestion.questionId}
            mode="question"
            grid={spotGrid}
            width={currentQuestion.config.width ?? spotLink?.width ?? 4}
            height={currentQuestion.config.height ?? spotLink?.height ?? 3}
            image={{ url: spotLink?.url ?? null, description: spotLink?.description ?? 'the Spot It image' }}
            refreshImage={refreshImageLinks}
            maxHeightVh={60}
            band={32}
            label={`Spot It grid, ${spotGrid.cols} columns by ${spotGrid.rows} rows, over ${spotLink?.description ?? 'the Spot It image'}`}
          />
        </div>
      )}

      {hasImages && !spotGrid && (
      <div className="flex w-full max-w-[calc(100vw-22rem)] flex-col items-center gap-4">
      {/* The prompt image stays up when the question locks: the room is still discussing it. */}
      {shown(promptId) && (
        <SafeImage
          key={promptId}
          src={promptLink?.url ?? null}
          alt={promptLink?.description ?? 'Question image'}
          style={fitStyle(promptLink, imageOptions ? '28vh' : '55vh')}
          refresh={refreshImageLinks}
          showRetry
        />
      )}

      {imageOptions && (
        <div
          className={`grid w-full max-w-6xl gap-4 ${
            options.length <= 2 ? 'grid-cols-2' : options.length === 3 ? 'grid-cols-3' : 'grid-cols-4'
          }`}
        >
          {options.map((caption, i) => {
            const link = linkFor(optionIds[i]);
            const withImage = shown(optionIds[i]);
            return (
              <figure
                key={i}
                aria-label={
                  withImage
                    ? imageOptionName(i, caption, link?.description ?? '')
                    : `Option ${optionLetter(i)}${hasCaption(caption) ? `: ${caption}` : ''}`
                }
                className="flex flex-col gap-2 rounded-2xl border border-slate-700 bg-slate-800/60 p-3"
              >
                <div className="flex items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-indigo-600 text-xl font-black text-white">
                    {optionLetter(i)}
                  </span>
                  {hasCaption(caption) && (
                    <figcaption className="text-xl font-semibold text-slate-100 leading-tight">
                      {caption}
                    </figcaption>
                  )}
                </div>
                {withImage && (
                  <SafeImage
                    key={optionIds[i]}
                    src={link?.url ?? null}
                    alt={link?.description ?? `Option ${optionLetter(i)} image`}
                    className={`w-full ${shown(promptId) ? 'h-[20vh]' : 'h-[36vh]'}`}
                    refresh={refreshImageLinks}
                    showRetry
                  />
                )}
              </figure>
            );
          })}
        </div>
      )}
      </div>
      )}

      <div className="text-slate-300 text-xl">
        {answeredCount} / {playerCount} answered
        {allAnswered && <span className="ml-3 text-green-400 font-semibold">All answered!</span>}
        {questionLocked && <span className="ml-3 text-amber-400 font-semibold">· Answers locked</span>}
      </div>

      <div className="flex gap-4">
        <Button
          size="lg"
          variant="outline"
          onClick={emitLockQuestion}
          className={`px-10 ${questionLocked ? 'border-amber-500 text-amber-400 hover:bg-amber-500/10' : ''}`}
        >
          {questionLocked ? 'Unlock Question' : 'Lock Question'}
        </Button>
        <Button size="lg" onClick={emitAdvance} className="px-10">
          Show Results
        </Button>
      </div>
    </div>
  );
}
