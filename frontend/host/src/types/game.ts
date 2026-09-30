/**
 * A question's `config`, as stored (snake_case keys pass through the socket unchanged).
 * Image references are image ids; one that isn't in the game's library is treated as no image.
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
  /** Spot It (T7): the canvas image, its grid, and the server-written gameplay dimensions. */
  image_id?: number | null;
  grid?: { cols: number; rows: number };
  width?: number;
  height?: number;
}

/** Every question type the server accepts (the backend's `QUESTION_TYPE_PATTERN`). */
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

/** One entry of `new_question`'s `images` map: a small link and the full image's size. */
export interface OptionImageLink {
  url: string;
  width: number;
  height: number;
  description: string;
}

export interface PlayerInfo {
  userId: string;
  displayName: string;
}

export interface SyncStatePayload {
  status: 'LOBBY' | 'IN_PROGRESS' | 'COMPLETED' | 'ABANDONED';
  players: PlayerInfo[];
  playerCount: number;
  questionPhase?: 'QUESTION' | 'RESULTS' | null;
  currentQuestion?: QuestionPayload | null;
  questionLocked?: boolean;
  timerRemainingSeconds?: number | null;
}

export interface PlayerJoinedPayload {
  userId: string;
  displayName: string;
  playerCount: number;
}

export interface PlayerLeftPayload {
  userId: string;
  playerCount: number;
}

export interface QuestionPayload {
  questionId: number;
  questionNumber: number;
  totalQuestions: number;
  type: QuestionType;
  gradingType: 'ACCURACY' | 'COMPLETENESS';
  prompt: string;
  config: QuestionConfig;
  timeLimitSeconds: number;
  pointsValue: number;
  editDistance?: number;
  /**
   * Sort It (T7): the display order of the cards, a permutation of their indices shared by the
   * projector and every phone. Answers always use the original indices.
   */
  cardOrder?: number[];
  startedAt?: string;  // ISO timestamp — only present in sync_state reconnect payloads
  /**
   * Small links for the question's option images, keyed by image id as a string (T8). The
   * projector doesn't use it: it resolves every image through the session link map instead.
   */
  images?: Record<string, OptionImageLink>;
}

export interface AnswerStatusPayload {
  userId: string;
  displayName: string;
  answered: boolean;
  answeredCount: number;
  totalPlayers: number;
}

export interface AnswerPhaseEndedPayload {
  questionId: number;
  answeredCount: number;
  totalPlayers: number;
  allAnswered?: boolean; // true only when every player answered (not just timer expiry)
}

export interface LeaderboardEntry {
  userId: string;
  displayName: string;
  score: number;
  rank: number;
}

export type AnswerReveal =
  | { type: 'multiple_choice'; correctIndices: number[] }
  | { type: 'true_false'; correctValue: boolean }
  | { type: 'fill_in_the_blank'; acceptedAnswers: string[]; editDistance: number }
  | { type: 'completeness' }
  | { type: 'multi_select'; answerPoints: number[] }
  | { type: 'sort_into_boxes'; correctBoxes: number[] }
  | { type: 'find_in_image'; acceptedCells: GridCell[] }
  | Record<string, never>;

export interface HostResultsPayload {
  questionId: number;
  answerReveal: AnswerReveal;
  answerDistribution: Record<string, number>;
  totalAnswered: number;
  totalPlayers: number;
}

export interface HostQuestionSummaryItem {
  questionId: number;
  questionNumber: number;
  prompt: string;
  type: QuestionType;
  gradingType: 'ACCURACY' | 'COMPLETENESS';
  config: QuestionConfig;
  pointsValue: number;
  answerReveal: AnswerReveal;
  answerDistribution: Record<string, number>;
  totalAnswered: number;
  totalPlayers: number;
  correctCount: number;
  avgAnswerTimeMs: number | null;
}

export interface HostGameOverPayload {
  scores: number[];
  playerCount: number;
  maxPossibleScore: number;
  questionSummary: HostQuestionSummaryItem[];
}

export type HostPhase = 'lobby' | 'question' | 'results' | 'gameover';
