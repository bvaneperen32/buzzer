/**
 * A question's `config`, as stored (snake_case keys pass through the socket unchanged).
 * Image references are image ids; an option whose id isn't in `images` has no image here.
 */
export interface QuestionConfig {
  options?: string[];
  maxLength?: number;
  prompt_image_id?: number | null;
  /** Parallel to `options`: an image id per option, or null. */
  option_images?: (number | null)[] | null;
  /** Sort It (T7): 2–4 box labels and 4–10 card labels, in the author's order. */
  boxes?: string[];
  cards?: string[];
  /**
   * Spot It (T7): the grid and the image's proportions. `image_id` is only an id — phones never
   * receive or show the Spot It image.
   */
  image_id?: number | null;
  grid?: { cols: number; rows: number };
  width?: number;
  height?: number;
}

export type QuestionType =
  | 'multiple_choice'
  | 'true_false'
  | 'fill_in_the_blank'
  | 'multi_select'
  | 'sort_into_boxes'
  | 'find_in_image';

/** A Spot It grid cell: column and row, 0-based from the top-left. */
export interface GridCell {
  col: number;
  row: number;
}

/** A Sort It answer: one entry per card (original order), a box index or null (left out). */
export interface SortAnswer {
  placements: (number | null)[];
}

/** One entry of `new_question`'s `images` map: a small link and the full image's size. */
export interface OptionImageLink {
  url: string;
  width: number;
  height: number;
  description: string;
}

export interface SyncStatePayload {
  status: 'LOBBY' | 'IN_PROGRESS' | 'COMPLETED' | 'ABANDONED';
  playerCount: number;
  questionLocked?: boolean;
  /** The raw Redis record (snake_case); only its id is read. */
  currentQuestion?: { question_id: number } | null;
  /** Whether the server already has this player's answer to the current question. */
  hasAnswered?: boolean;
  /** The host has gone and not yet returned (the server's grace period is running). */
  hostDisconnected?: boolean;
}

/** An answer that arrived after its question locked ("locked") or moved on ("closed"). Not an
 *  error: the game goes on, and the answer didn't count. */
export interface AnswerRejectedPayload {
  questionId: number;
  reason: 'locked' | 'closed';
  message: string;
}

export interface PlayerJoinedPayload {
  playerCount: number;
}

export interface QuestionPayload {
  questionId: number;
  questionNumber: number;
  totalQuestions: number;
  type: QuestionType;
  gradingType?: 'ACCURACY' | 'COMPLETENESS';
  prompt: string;
  config: QuestionConfig;
  timeLimitSeconds: number;
  pointsValue: number;
  /** Sort It (T7): the card display order shared with the projector. */
  cardOrder?: number[];
  /**
   * Small links for the question's option images only, keyed by image id as a string (T8).
   * Phones never receive prompt images.
   */
  images?: Record<string, OptionImageLink>;
}

export interface AnswerResultPayload {
  questionId: number;
  isCorrect: boolean;
  pointsAwarded: number;
  totalScore: number;
  alreadyAnswered?: boolean;
}

export type PlayerAnswerReveal =
  | { type: 'multiple_choice'; correctIndices: number[] }
  | { type: 'true_false'; correctValue: boolean }
  | { type: 'fill_in_the_blank'; acceptedAnswers: string[]; editDistance: number }
  | { type: 'completeness' }
  | { type: 'multi_select'; answerPoints: number[] }
  | { type: 'sort_into_boxes'; correctBoxes: number[] }
  | { type: 'find_in_image'; acceptedCells: GridCell[] };

export interface PlayerResultsPayload {
  questionId: number;
  answerReveal: PlayerAnswerReveal;
  yourPoints: number;
  yourScore: number;
  yourRank: number;
  playerCount: number;
  /** Whether the server has this player's answer (every question type). Results say "No
   *  answer" from this alone: the phone's own record of what it sent, and zero points, can't
   *  tell. */
  answered?: boolean;
  // T7 questions only (spec §2.7): enough to render the result from this payload alone, even
  // after a reload between answering and results.
  questionType?: QuestionType;
  gradingType?: 'ACCURACY' | 'COMPLETENESS';
  /** The question's maximum points. */
  pointsValue?: number;
  /** This player's validated answer, or null when they didn't answer. */
  yourAnswer?: SortAnswer | GridCell | null;
  yourIsCorrect?: boolean;
}

export interface QuestionSummaryItem {
  questionId: number;
  prompt: string;
  type: QuestionType;
  gradingType: 'ACCURACY' | 'COMPLETENESS';
  config: QuestionConfig;
  pointsAwarded: number;
  maxPoints: number;
  answerTimeMs: number | null;
  playerAnswer:
    | {
        selectedIndex?: number;
        selectedValue?: boolean;
        text?: string;
        selectedIndices?: number[];
        placements?: (number | null)[];
        col?: number;
        row?: number;
      }
    | null;
  answerReveal: PlayerAnswerReveal;
}

export interface PlayerGameOverPayload {
  yourFinalScore: number;
  yourFinalRank: number;
  playerCount: number;
  questionSummary: QuestionSummaryItem[];
}

export type PlayerPhase = 'lobby' | 'question' | 'feedback' | 'results' | 'gameover';
