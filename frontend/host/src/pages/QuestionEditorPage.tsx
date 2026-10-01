import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Plus, Trash2, ChevronUp, ChevronDown, Download } from 'lucide-react';
import { api } from '../lib/api';
import { useImageLibrary } from '../lib/imageLibrary';
import {
  canvasImageId,
  hasCaption,
  optionImageIds,
  optionLetter,
  promptImageId,
} from '../lib/questionImages';
import {
  cellKey,
  cellLabel,
  defaultGrid,
  fitsNoGrid,
  gridFits,
  maxGrid,
  MAX_GRID,
  MIN_GRID,
  phoneCellSize,
  readCells,
  readGrid,
  reviewText,
  sortCells,
  tooDenseText,
  type Grid,
} from '../lib/spotGrid';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Card, CardContent, CardHeader } from '../components/ui/card';
import { SafeImage } from '../components/ui/SafeImage';
import { ImageLibraryPanel } from '../components/images/ImageLibraryPanel';
import { ImagePicker } from '../components/images/ImagePicker';
import { SpotItCanvas } from '../components/question/SpotItCanvas';
import type { GridCell, QuestionConfig, QuestionType } from '../types/game';
import type { ImageItem } from '../types/images';

type GradingType = 'ACCURACY' | 'COMPLETENESS';

interface Question {
  id: number;
  type: QuestionType;
  grading_type: GradingType;
  prompt: string;
  config: Record<string, unknown>;
  answer_data: Record<string, unknown>;
  time_limit_seconds: number;
  points_value: number;
  order_index: number;
  /** T7: computed by the server for Spot It questions; false / [] for every other type. */
  needs_review?: boolean;
  review_reasons?: string[];
}

interface Game { id: number; title: string }

const T7_TYPES: QuestionType[] = ['sort_into_boxes', 'find_in_image'];

// ---- helpers for building config/answer_data per type ----

interface McOption { text: string; points: number; imageId: number | null }

/** Does any option row have an image? Only then do the image-option rules apply (T8 §2.7). */
function hasOptionImages(options: McOption[]): boolean {
  return options.some((o) => o.imageId !== null);
}

/**
 * With option images, a row with neither text nor image is dropped before anything is derived,
 * so a leftover blank row can't block saving. Without images, rows are used exactly as before.
 */
function optionRows(options: McOption[]): McOption[] {
  return hasOptionImages(options)
    ? options.filter((o) => o.text.trim() || o.imageId !== null)
    : options;
}

/** `options`, plus `option_images` only when some option has an image, so text-only payloads
 * are unchanged. A blank caption on an image option is saved as "". */
function choiceConfig(options: McOption[]): Record<string, unknown> {
  if (!hasOptionImages(options)) return { options: options.map((o) => o.text) };
  return {
    options: options.map((o) => (o.imageId !== null && !o.text.trim() ? '' : o.text)),
    option_images: options.map((o) => o.imageId),
  };
}

function buildMcPayload(options: McOption[], _grading: GradingType) {
  return {
    config: choiceConfig(options),
    answer_data: { answer_points: options.map((o) => o.points) },
  };
}

function buildTfPayload(truePoints: number, falsePoints: number) {
  return {
    config: {},
    answer_data: { answer_points: { true: truePoints, false: falsePoints } },
  };
}

function buildMsPayload(options: McOption[]) {
  return {
    config: choiceConfig(options),
    answer_data: { answer_points: options.map((o) => o.points) },
  };
}

interface FibAnswer { text: string; points: number }

function buildFibPayload(answers: FibAnswer[], editDistance: number) {
  return {
    config: {},
    answer_data: {
      acceptedAnswers: answers.map((a) => a.text),
      answerPoints: answers.map((a) => a.points),
      editDistance,
    },
  };
}

// ---- Sort It (T7 spec §2.3) ----

const SORT_BOXES = { min: 2, max: 4, maxLength: 30 };
const SORT_CARDS = { min: 4, max: 10, maxLength: 60 };

/** A Sort It label row: its text, and the text it was loaded with (null for a new row). */
interface LabelRow { text: string; loaded: string | null }

function loadedRows(labels: unknown, count: number): LabelRow[] {
  if (Array.isArray(labels) && labels.length > 0) {
    return labels.map((label) => {
      const text = typeof label === 'string' ? label : String(label ?? '');
      return { text, loaded: text };
    });
  }
  return Array.from({ length: count }, () => ({ text: '', loaded: null }));
}

const isUnedited = (row: LabelRow) => row.loaded !== null && row.text === row.loaded;

/**
 * The editor label rule: a row whose text equals its loaded text sends the loaded text
 * unchanged (so a U+FEFF from an import survives); any other row sends its text trimmed.
 */
function sentLabel(row: LabelRow): string {
  return isUnedited(row) ? (row.loaded as string) : row.text.trim();
}

/**
 * Early feedback only — approximations of the server's rules: `[...s].length` counts code points
 * as the server does, but `trim()` and `toLowerCase()` differ from Python's `isspace` and
 * `casefold` (U+FEFF, "ß"), so the server's 422 stays authoritative. Unedited rows aren't
 * checked: they're sent exactly as loaded.
 */
function labelProblems(rows: LabelRow[], noun: string, plural: string, maxLength: number): string[] {
  const problems: string[] = [];
  const seen = new Map<string, number>();
  rows.forEach((row, i) => {
    const sent = sentLabel(row);
    if (!isUnedited(row)) {
      if (!sent) problems.push(`${noun} ${i + 1} is empty`);
      else if ([...sent].length > maxLength) {
        problems.push(`${noun} ${i + 1} is longer than ${maxLength} characters`);
      }
    }
    if (!sent) return;
    const folded = sent.toLowerCase();
    const first = seen.get(folded);
    if (first !== undefined) {
      problems.push(`${plural} ${first + 1} and ${i + 1} are the same (ignoring case)`);
    } else {
      seen.set(folded, i);
    }
  });
  return problems;
}

/**
 * The answer key to send (spec §2.3 editor answer-key rule): never partial. ACCURACY needs every
 * card assigned; COMPLETENESS sends one only while "Include an answer key" is on. An untouched key
 * equals the loaded one, so it is sent exactly as loaded.
 */
function sortAnswerData(form: FormState): { answerData: Record<string, unknown>; problem: string | null } {
  const full =
    form.sortKey.length === form.sortCards.length && form.sortKey.every((box) => box !== null);
  if (form.grading === 'COMPLETENESS' && !form.sortKeyOn) return { answerData: {}, problem: null };
  if (!full) {
    return {
      answerData: {},
      problem:
        form.grading === 'ACCURACY'
          ? 'Assign every card a box'
          : 'Assign every card a box, or turn off the answer key',
    };
  }
  return { answerData: { correct_boxes: form.sortKey }, problem: null };
}

// ---- Spot It (T7 spec §2.4, §2.5) ----

interface Stamp { image_id: number; image_version: number; cols: number; rows: number }

function readStamp(value: unknown): Stamp | null {
  if (!value || typeof value !== 'object') return null;
  const s = value as Record<string, unknown>;
  const keys = Object.keys(s).sort().join(',');
  const ok = (n: unknown) => typeof n === 'number' && Number.isInteger(n) && n > 0;
  return keys === 'cols,image_id,image_version,rows' &&
    ok(s.image_id) && ok(s.image_version) && ok(s.cols) && ok(s.rows)
    ? (s as unknown as Stamp)
    : null;
}

/**
 * The stamp to send (spec §2.5): the loaded stamp exactly while the box has stayed ticked since
 * the form loaded; a fresh stamp for the displayed library version and grid after any unticked →
 * ticked transition (so reconfirming after a replace is one tick); null while unticked.
 */
function spotStamp(form: FormState, image: ImageItem | undefined): Stamp | null {
  if (form.spotStamp === 'loaded') return form.spotStampLoaded;
  if (form.spotStamp === 'fresh' && image && form.spotGrid) {
    return { image_id: image.id, image_version: image.version, cols: form.spotGrid.cols, rows: form.spotGrid.rows };
  }
  return null;
}

const validGrid = (grid: Grid | null): grid is Grid =>
  !!grid &&
  [grid.cols, grid.rows].every((n) => Number.isInteger(n) && n >= MIN_GRID && n <= MAX_GRID);

function outOfBounds(cells: GridCell[], grid: Grid | null): GridCell[] {
  if (!validGrid(grid)) return [];
  return cells.filter((c) => c.col >= grid.cols || c.row >= grid.rows);
}

// ---- QuestionForm subcomponent ----

interface FormState {
  type: QuestionType;
  grading: GradingType;
  prompt: string;
  /** Any type but Spot It can show an image with its prompt (projector only). */
  promptImageId: number | null;
  timeLimitSeconds: number;
  /** The exact number received — display rounding never feeds back into a save. */
  pointsValue: number;
  // multiple_choice
  mcOptions: McOption[];
  // true_false
  tfTruePoints: number;
  tfFalsePoints: number;
  // fill_in_the_blank
  fibAnswers: FibAnswer[];
  fibEditDistance: number;
  // multi_select
  msOptions: McOption[];
  // sort_into_boxes
  sortBoxes: LabelRow[];
  sortCards: LabelRow[];
  /** Per card: its correct box, or null while unassigned. */
  sortKey: (number | null)[];
  /** The loaded `correct_boxes`, or null when it was absent. */
  sortKeyLoaded: number[] | null;
  /** COMPLETENESS: "Include an answer key (not scored)". */
  sortKeyOn: boolean;
  // find_in_image
  spotImageId: number | null;
  spotGrid: Grid | null;
  /** Where the grid came from: a default is only ever replaced by another default. */
  spotGridSource: 'loaded' | 'edited' | 'default' | null;
  /** Accepted cells in stored order: new ones appended, removed ones deleted in place. */
  spotCells: GridCell[];
  /** COMPLETENESS: `accepted_cells` was loaded present (absent stays absent, [] stays []). */
  spotCellsPresent: boolean;
  spotStampLoaded: Stamp | null;
  /** The checkbox, with its history since load: ticked since load, re-ticked, or unticked. */
  spotStamp: 'loaded' | 'fresh' | 'none';
  /** The library version of the form's image the checkbox state was set against. */
  spotVersionSeen: number | null;
  /** The library hadn't loaded the image when the form opened: apply the initial rule later. */
  spotInitPending: boolean;
}

const newLabelRows = (count: number): LabelRow[] =>
  Array.from({ length: count }, () => ({ text: '', loaded: null }));

const defaultForm = (): FormState => ({
  type: 'multiple_choice',
  grading: 'ACCURACY',
  prompt: '',
  promptImageId: null,
  timeLimitSeconds: 30,
  pointsValue: 1,
  mcOptions: [{ text: '', points: 1, imageId: null }, { text: '', points: 0, imageId: null }],
  tfTruePoints: 1,
  tfFalsePoints: 0,
  fibAnswers: [{ text: '', points: 1 }],
  fibEditDistance: 0,
  msOptions: [{ text: '', points: 1, imageId: null }, { text: '', points: 1, imageId: null }],
  sortBoxes: newLabelRows(SORT_BOXES.min),
  sortCards: newLabelRows(SORT_CARDS.min),
  sortKey: Array.from({ length: SORT_CARDS.min }, () => null),
  sortKeyLoaded: null,
  sortKeyOn: false,
  spotImageId: null,
  spotGrid: null,
  spotGridSource: null,
  spotCells: [],
  spotCellsPresent: false,
  spotStampLoaded: null,
  spotStamp: 'none',
  spotVersionSeen: null,
  spotInitPending: false,
});

function questionToForm(q: Question, images: ImageItem[]): FormState {
  const config = q.config as QuestionConfig;
  const imageIds = optionImageIds(config);
  const base: FormState = {
    ...defaultForm(),
    type: q.type,
    grading: q.grading_type,
    prompt: q.prompt,
    promptImageId: promptImageId(config),
    timeLimitSeconds: q.time_limit_seconds,
    pointsValue: q.points_value,
  };
  if (q.type === 'multiple_choice') {
    const opts = (q.config['options'] as string[]) ?? [];
    const pts = (q.answer_data['answer_points'] as number[]) ?? [];
    base.mcOptions = opts.map((text, i) => ({ text, points: pts[i] ?? 0, imageId: imageIds[i] ?? null }));
  } else if (q.type === 'true_false') {
    const ap = q.answer_data['answer_points'] as { true: number; false: number } | undefined;
    base.tfTruePoints = ap?.true ?? 1;
    base.tfFalsePoints = ap?.false ?? 0;
  } else if (q.type === 'fill_in_the_blank') {
    const accepted = (q.answer_data['acceptedAnswers'] as string[]) ?? [];
    const pts = (q.answer_data['answerPoints'] as number[]) ?? [];
    base.fibAnswers = accepted.length
      ? accepted.map((text, i) => ({ text, points: pts[i] ?? 1 }))
      : [{ text: '', points: 1 }];
    base.fibEditDistance = (q.answer_data['editDistance'] as number) ?? 0;
  } else if (q.type === 'multi_select') {
    const opts = (q.config['options'] as string[]) ?? [];
    const pts = (q.answer_data['answer_points'] as number[]) ?? [];
    base.msOptions = opts.length
      ? opts.map((text, i) => ({ text, points: pts[i] ?? 1, imageId: imageIds[i] ?? null }))
      : [{ text: '', points: 1, imageId: null }, { text: '', points: 1, imageId: null }];
  } else if (q.type === 'sort_into_boxes') {
    base.sortBoxes = loadedRows(q.config['boxes'], SORT_BOXES.min);
    base.sortCards = loadedRows(q.config['cards'], SORT_CARDS.min);
    const key = q.answer_data['correct_boxes'];
    base.sortKeyLoaded = Array.isArray(key) ? (key as number[]) : null;
    base.sortKey = base.sortCards.map((_, i) =>
      Array.isArray(key) && typeof key[i] === 'number' ? (key[i] as number) : null,
    );
    base.sortKeyOn = Array.isArray(key);
  } else if (q.type === 'find_in_image') {
    const imageId = canvasImageId(config);
    const grid = readGrid(q.config['grid']);
    const stamp = readStamp(q.answer_data['confirmed_for']);
    const item = images.find((i) => i.id === imageId);
    // Ticked only if the server says confirmed AND the stamp matches the image's current
    // library version, so a momentarily stale list can't resend a stale stamp (spec §2.5).
    const tick =
      !q.needs_review && !!stamp && !!item && stamp.image_id === imageId && stamp.image_version === item.version;
    base.spotImageId = imageId;
    base.spotGrid = grid;
    base.spotGridSource = grid ? 'loaded' : null;
    base.spotCells = readCells(q.answer_data['accepted_cells']);
    base.spotCellsPresent = 'accepted_cells' in q.answer_data;
    base.spotStampLoaded = stamp;
    base.spotStamp = tick ? 'loaded' : 'none';
    base.spotVersionSeen = item?.version ?? null;
    base.spotInitPending = !item && !q.needs_review && !!stamp && imageId !== null;
  }
  return base;
}

function formToPayload(form: FormState, images: ImageItem[]) {
  let config: Record<string, unknown> = {};
  let answer_data: Record<string, unknown> = {};
  // Options, their images, their points and points_value all come from the same rows. With an
  // image option those are the filtered rows; without one, exactly today's rows.
  const mcRows = optionRows(form.mcOptions);
  const msRows = optionRows(form.msOptions);
  if (form.type === 'multiple_choice') {
    const p = buildMcPayload(mcRows, form.grading);
    config = p.config;
    answer_data = p.answer_data;
  } else if (form.type === 'true_false') {
    const p = buildTfPayload(form.tfTruePoints, form.tfFalsePoints);
    config = p.config;
    answer_data = p.answer_data;
  } else if (form.type === 'multi_select') {
    const p = buildMsPayload(
      hasOptionImages(form.msOptions) ? msRows : form.msOptions.filter((o) => o.text.trim()),
    );
    config = p.config;
    answer_data = p.answer_data;
  } else if (form.type === 'sort_into_boxes') {
    config = { boxes: form.sortBoxes.map(sentLabel), cards: form.sortCards.map(sentLabel) };
    answer_data = sortAnswerData(form).answerData;
  } else if (form.type === 'find_in_image') {
    // The server writes width/height itself; the canvas image is the question's only image.
    config = { image_id: form.spotImageId, grid: form.spotGrid };
    if (form.grading === 'ACCURACY' || form.spotCellsPresent || form.spotCells.length > 0) {
      answer_data.accepted_cells = form.spotCells;
    }
    answer_data.confirmed_for = spotStamp(form, images.find((i) => i.id === form.spotImageId));
  } else {
    const p = buildFibPayload(form.fibAnswers.filter((a) => a.text.trim()), form.fibEditDistance);
    config = p.config;
    answer_data = p.answer_data;
  }
  if (form.promptImageId !== null && form.type !== 'find_in_image') {
    config = { ...config, prompt_image_id: form.promptImageId };
  }
  const points_value =
    form.grading === 'COMPLETENESS' || T7_TYPES.includes(form.type)
      ? form.pointsValue // T7 types: typed by the author in both modes
      : form.type === 'multiple_choice'
      ? Math.max(0, ...mcRows.map((o) => o.points))
      : form.type === 'true_false'
      ? Math.max(form.tfTruePoints, form.tfFalsePoints)
      : form.type === 'multi_select'
      ? msRows.reduce((sum, o) => sum + (o.points > 0 ? o.points : 0), 0)
      : Math.max(0, ...form.fibAnswers.map((a) => a.points));
  return {
    type: form.type,
    grading_type: form.grading,
    prompt: form.prompt,
    config,
    answer_data,
    time_limit_seconds: form.timeLimitSeconds,
    points_value,
  };
}

/** Problems that block saving a T7 question (early feedback; the server stays authoritative). */
function formProblems(form: FormState, images: ImageItem[]): string[] {
  const problems: string[] = [];
  if (T7_TYPES.includes(form.type) && form.grading === 'ACCURACY' && !(form.pointsValue > 0)) {
    problems.push('Points must be above 0');
  }
  if (form.type === 'sort_into_boxes') {
    problems.push(
      ...labelProblems(form.sortBoxes, 'Box', 'Boxes', SORT_BOXES.maxLength),
      ...labelProblems(form.sortCards, 'Card', 'Cards', SORT_CARDS.maxLength),
    );
    const key = sortAnswerData(form).problem;
    if (key) problems.push(key);
  }
  if (form.type === 'find_in_image') {
    if (form.spotImageId === null) {
      problems.push('Choose the Spot It image');
    } else if (!images.some((i) => i.id === form.spotImageId)) {
      problems.push("The chosen image isn't in this game's library — choose another");
    }
    if (!validGrid(form.spotGrid)) {
      problems.push(`Columns and rows must be whole numbers from ${MIN_GRID} to ${MAX_GRID}`);
    } else {
      const outside = outOfBounds(form.spotCells, form.spotGrid);
      if (outside.length) {
        problems.push(
          `Remove the cells outside the ${form.spotGrid.cols}×${form.spotGrid.rows} grid: ${outside.map(cellLabel).join(', ')}`,
        );
      }
      const half = Math.floor((form.spotGrid.cols * form.spotGrid.rows) / 2);
      const count = form.spotCells.length;
      if (form.grading === 'ACCURACY' && (count < 1 || count > half)) {
        problems.push(`Choose from 1 to ${half} accepted cells (at most half of the grid)`);
      } else if (form.grading === 'COMPLETENESS' && count > half) {
        problems.push(`Choose at most ${half} accepted cells (half of the grid)`);
      }
    }
  }
  return problems;
}

type Payload = ReturnType<typeof formToPayload>;

const selectClass =
  'w-full rounded-lg border border-input bg-raised px-3 py-2 text-primary focus-visible:border-accent text-sm';

function QuestionForm({
  initial,
  question,
  onSave,
  onCancel,
  saving,
  saveError,
  images,
  onUpload,
  refreshImages,
}: {
  initial: FormState;
  /** The question being edited, for its review status; undefined for a new question. */
  question?: Question;
  onSave: (payload: Payload) => void;
  onCancel: () => void;
  saving: boolean;
  /** The server's refusal of the last save (a 422 or the live freeze's 409). */
  saveError: string;
  images: ImageItem[];
  onUpload: (file: File, description: string) => Promise<ImageItem>;
  refreshImages: () => Promise<void>;
}) {
  const [form, setForm] = useState<FormState>(initial);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const spotImage = images.find((i) => i.id === form.spotImageId);

  // Spot It's checkbox follows the library: a newer version of the form's image unticks it
  // (a replace after the form opened), and if the library arrived after the form opened, the
  // initial rule is applied once it does.
  useEffect(() => {
    if (form.type !== 'find_in_image' || !spotImage) return;
    if (form.spotInitPending) {
      const stamp = form.spotStampLoaded;
      const tick =
        !!question && !question.needs_review && !!stamp &&
        stamp.image_id === spotImage.id && stamp.image_version === spotImage.version;
      setForm((prev) => ({
        ...prev,
        spotStamp: tick ? 'loaded' : 'none',
        spotVersionSeen: spotImage.version,
        spotInitPending: false,
      }));
    } else if (form.spotVersionSeen !== spotImage.version) {
      setForm((prev) => ({ ...prev, spotStamp: 'none', spotVersionSeen: spotImage.version }));
    }
  }, [form.type, form.spotInitPending, form.spotVersionSeen, form.spotStampLoaded, spotImage, question]);

  /** The image picker for one choice-option row. */
  function optionPicker(key: 'mcOptions' | 'msOptions', i: number) {
    const rows = form[key];
    return (
      <ImagePicker
        label={`Option ${optionLetter(i)} image`}
        value={rows[i].imageId}
        images={images}
        onSelect={(id) => {
          const opts = [...form[key]];
          opts[i] = { ...opts[i], imageId: id };
          set(key, opts);
        }}
        onUpload={onUpload}
        refresh={refreshImages}
        compact
      />
    );
  }

  function changeGrading(grading: GradingType) {
    setForm((prev) => ({
      ...prev,
      grading,
      // Switching never discards an authored key: ACCURACY → COMPLETENESS keeps it, turning
      // "Include an answer key" on when there is one (spec §2.3).
      sortKeyOn:
        grading === 'COMPLETENESS' && prev.grading === 'ACCURACY'
          ? prev.sortKey.some((box) => box !== null) || prev.sortKeyLoaded !== null
          : prev.sortKeyOn,
    }));
  }

  // -- Sort It ---------------------------------------------------------------

  function setLabel(key: 'sortBoxes' | 'sortCards', i: number, text: string) {
    setForm((prev) => {
      const rows = [...prev[key]];
      rows[i] = { ...rows[i], text };
      return { ...prev, [key]: rows };
    });
  }

  function addCard() {
    setForm((prev) => ({
      ...prev,
      sortCards: [...prev.sortCards, { text: '', loaded: null }],
      sortKey: [...prev.sortKey, null], // a new card starts unassigned
    }));
  }

  function removeCard(i: number) {
    setForm((prev) => ({
      ...prev,
      sortCards: prev.sortCards.filter((_, j) => j !== i),
      sortKey: prev.sortKey.filter((_, j) => j !== i),
    }));
  }

  function removeBox(j: number) {
    // Its cards become unassigned and later boxes shift down, so the key must be finished again.
    setForm((prev) => ({
      ...prev,
      sortBoxes: prev.sortBoxes.filter((_, i) => i !== j),
      sortKey: prev.sortKey.map((box) => (box === null || box === j ? null : box > j ? box - 1 : box)),
    }));
  }

  function assign(card: number, box: number | null) {
    setForm((prev) => {
      const key = [...prev.sortKey];
      key[card] = box;
      return { ...prev, sortKey: key };
    });
  }

  // -- Spot It ---------------------------------------------------------------

  function chooseSpotImage(id: number | null) {
    setForm((prev) => {
      // Picking the image that is already selected changes nothing: the grid and the checkbox
      // stay as they are. (A replaced image's new version still unticks the box, through the
      // library effect above — that is a real change of what the cells were checked against.)
      if (id === prev.spotImageId) return prev;
      const item = images.find((i) => i.id === id);
      let grid = prev.spotGrid;
      let source = prev.spotGridSource;
      // A default applies only while the form has no grid, or still has an untouched default.
      if (item && (grid === null || source === 'default')) {
        grid = defaultGrid(item.width, item.height);
        source = grid ? 'default' : null;
      }
      return {
        ...prev,
        spotImageId: id,
        spotGrid: grid,
        spotGridSource: source,
        spotStamp: 'none',
        spotVersionSeen: item?.version ?? null,
        spotInitPending: false,
      };
    });
  }

  function setGridValue(key: 'cols' | 'rows', value: string) {
    setForm((prev) => {
      const base = prev.spotGrid ?? { cols: 6, rows: 4 };
      return {
        ...prev,
        spotGrid: { ...base, [key]: value === '' ? 0 : Number(value) },
        spotGridSource: 'edited',
        spotStamp: 'none', // a grid change unticks the checkbox
        spotInitPending: false,
      };
    });
  }

  function toggleCell(cell: GridCell) {
    setForm((prev) => {
      const index = prev.spotCells.findIndex((c) => cellKey(c) === cellKey(cell));
      return {
        ...prev,
        spotCells: index >= 0 ? prev.spotCells.filter((_, i) => i !== index) : [...prev.spotCells, cell],
      };
    });
  }

  function removeOutside() {
    setForm((prev) => {
      const outside = new Set(outOfBounds(prev.spotCells, prev.spotGrid).map(cellKey));
      return { ...prev, spotCells: prev.spotCells.filter((c) => !outside.has(cellKey(c))) };
    });
  }

  const problems = formProblems(form, images);
  const isT7 = T7_TYPES.includes(form.type);

  return (
    <div className="space-y-4">
      {/* Type + grading */}
      <div className="flex gap-3">
        <div className="flex-1">
          <label className="block text-xs text-secondary mb-1">Question type</label>
          <select
            className={selectClass}
            value={form.type}
            onChange={(e) => set('type', e.target.value as QuestionType)}
          >
            <option value="multiple_choice">Multiple Choice</option>
            <option value="true_false">True / False</option>
            <option value="fill_in_the_blank">Fill in the Blank</option>
            <option value="multi_select">Multi-Select (Select All That Apply)</option>
            <option value="sort_into_boxes">Sort It (cards into boxes)</option>
            <option value="find_in_image">Spot It (find it in the image)</option>
          </select>
        </div>
        <div className="flex-1">
          <label className="block text-xs text-secondary mb-1">Grading</label>
          <select
            className={selectClass}
            value={form.grading}
            onChange={(e) => changeGrading(e.target.value as GradingType)}
          >
            <option value="ACCURACY">Accuracy</option>
            <option value="COMPLETENESS">Completeness</option>
          </select>
        </div>
      </div>

      {/* Prompt */}
      <div>
        <label className="block text-xs text-secondary mb-1">Prompt</label>
        <textarea
          className="w-full rounded-lg border border-input bg-raised px-3 py-2 text-primary placeholder:text-secondary focus-visible:border-accent resize-none text-sm"
          rows={3}
          placeholder="Question text…"
          value={form.prompt}
          onChange={(e) => set('prompt', e.target.value)}
          required
        />
        {form.type !== 'find_in_image' && (
          <div className="mt-2">
            <ImagePicker
              label="Prompt image"
              value={form.promptImageId}
              images={images}
              onSelect={(id) => set('promptImageId', id)}
              onUpload={onUpload}
              refresh={refreshImages}
            />
            <p className="text-secondary text-xs mt-1">
              Shown on the projector below the prompt; phones never show it.
            </p>
          </div>
        )}
      </div>

      {/* Type-specific */}
      {form.type === 'multiple_choice' && (
        <div>
          <label className="block text-xs text-secondary mb-2">Options</label>
          {form.grading === 'ACCURACY' && (
            <div className="flex gap-2 mb-1 px-0.5">
              <span className="flex-1 text-xs text-secondary">Answer text</span>
              <span className="w-24 text-xs text-secondary">Points</span>
              {form.mcOptions.length > 2 && <span className="w-7" />}
            </div>
          )}
          <div className="space-y-2">
            {form.mcOptions.map((opt, i) => (
              <div key={i} className="flex gap-2 items-start">
                <div className="pt-1">{optionPicker('mcOptions', i)}</div>
                <Input
                  placeholder={opt.imageId !== null ? `Caption (optional)` : `Option ${i + 1}`}
                  value={opt.text}
                  onChange={(e) => {
                    const opts = [...form.mcOptions];
                    opts[i] = { ...opts[i], text: e.target.value };
                    set('mcOptions', opts);
                  }}
                  className="flex-1 text-sm"
                />
                {form.grading === 'ACCURACY' && (
                  <Input
                    type="number"
                    placeholder="pts"
                    value={opt.points}
                    onChange={(e) => {
                      const opts = [...form.mcOptions];
                      opts[i] = { ...opts[i], points: Number(e.target.value) };
                      set('mcOptions', opts);
                    }}
                    className="w-24 text-sm"
                    min="0"
                    step="any"
                  />
                )}
                {form.mcOptions.length > 2 && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => set('mcOptions', form.mcOptions.filter((_, j) => j !== i))}
                  >
                    <Trash2 size={12} />
                  </Button>
                )}
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => set('mcOptions', [...form.mcOptions, { text: '', points: 0, imageId: null }])}
            >
              <Plus size={12} className="mr-1" /> Add option
            </Button>
          </div>
        </div>
      )}

      {form.type === 'multi_select' && (
        <div>
          <label className="block text-xs text-secondary mb-2">Options</label>
          {form.grading === 'ACCURACY' && (
            <div className="flex gap-2 mb-1 px-0.5">
              <span className="flex-1 text-xs text-secondary">Answer text</span>
              <span className="w-24 text-xs text-secondary">Points (neg = penalty)</span>
              {form.msOptions.length > 2 && <span className="w-7" />}
            </div>
          )}
          <div className="space-y-2">
            {form.msOptions.map((opt, i) => (
              <div key={i} className="flex gap-2 items-start">
                <div className="pt-1">{optionPicker('msOptions', i)}</div>
                <Input
                  placeholder={opt.imageId !== null ? `Caption (optional)` : `Option ${i + 1}`}
                  value={opt.text}
                  onChange={(e) => {
                    const opts = [...form.msOptions];
                    opts[i] = { ...opts[i], text: e.target.value };
                    set('msOptions', opts);
                  }}
                  className="flex-1 text-sm"
                />
                {form.grading === 'ACCURACY' && (
                  <Input
                    type="number"
                    placeholder="pts"
                    value={opt.points}
                    onChange={(e) => {
                      const opts = [...form.msOptions];
                      opts[i] = { ...opts[i], points: Number(e.target.value) };
                      set('msOptions', opts);
                    }}
                    className="w-24 text-sm"
                    step="any"
                  />
                )}
                {form.msOptions.length > 2 && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => set('msOptions', form.msOptions.filter((_, j) => j !== i))}
                  >
                    <Trash2 size={12} />
                  </Button>
                )}
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => set('msOptions', [...form.msOptions, { text: '', points: 1, imageId: null }])}
            >
              <Plus size={12} className="mr-1" /> Add option
            </Button>
          </div>
          {form.grading === 'ACCURACY' && (
            <p className="text-secondary text-xs mt-1">
              Correct options: positive pts. Distractors: negative pts (penalty). Score = sum of selected, capped at 0.
            </p>
          )}
        </div>
      )}

      {form.type === 'true_false' && form.grading === 'ACCURACY' && (
        <div>
          <label className="block text-xs text-secondary mb-2">Points per answer</label>
          <div className="flex gap-4">
            <div>
              <span className="text-secondary text-sm">True:</span>
              <Input
                type="number"
                value={form.tfTruePoints}
                onChange={(e) => set('tfTruePoints', Number(e.target.value))}
                className="w-28 mt-1 text-sm"
                min="0"
                step="any"
              />
            </div>
            <div>
              <span className="text-secondary text-sm">False:</span>
              <Input
                type="number"
                value={form.tfFalsePoints}
                onChange={(e) => set('tfFalsePoints', Number(e.target.value))}
                className="w-28 mt-1 text-sm"
                min="0"
                step="any"
              />
            </div>
          </div>
        </div>
      )}

      {form.type === 'fill_in_the_blank' && form.grading === 'ACCURACY' && (
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-secondary mb-2">Accepted answers</label>
            <div className="flex gap-2 mb-1 px-0.5">
              <span className="flex-1 text-xs text-secondary">Answer text</span>
              <span className="w-24 text-xs text-secondary">Points</span>
              {form.fibAnswers.length > 1 && <span className="w-7" />}
            </div>
            <div className="space-y-2">
              {form.fibAnswers.map((ans, i) => (
                <div key={i} className="flex gap-2 items-center">
                  <Input
                    placeholder={`Answer ${i + 1}`}
                    value={ans.text}
                    onChange={(e) => {
                      const answers = [...form.fibAnswers];
                      answers[i] = { ...answers[i], text: e.target.value };
                      set('fibAnswers', answers);
                    }}
                    className="flex-1 text-sm"
                  />
                  <Input
                    type="number"
                    placeholder="pts"
                    value={ans.points}
                    onChange={(e) => {
                      const answers = [...form.fibAnswers];
                      answers[i] = { ...answers[i], points: Number(e.target.value) };
                      set('fibAnswers', answers);
                    }}
                    className="w-24 text-sm"
                    min="0"
                    step="any"
                  />
                  {form.fibAnswers.length > 1 && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => set('fibAnswers', form.fibAnswers.filter((_, j) => j !== i))}
                    >
                      <Trash2 size={12} />
                    </Button>
                  )}
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => set('fibAnswers', [...form.fibAnswers, { text: '', points: 1 }])}
              >
                <Plus size={12} className="mr-1" /> Add answer
              </Button>
            </div>
          </div>
          <div>
            <label className="block text-xs text-secondary mb-1">Edit distance tolerance (fuzzy match)</label>
            <Input
              type="number"
              value={form.fibEditDistance}
              onChange={(e) => set('fibEditDistance', Number(e.target.value))}
              className="w-28 text-sm"
              min="0"
            />
          </div>
        </div>
      )}

      {form.type === 'sort_into_boxes' && (
        <div className="space-y-4">
          <div>
            <label className="block text-xs text-secondary mb-2">
              Boxes ({SORT_BOXES.min}–{SORT_BOXES.max}, up to {SORT_BOXES.maxLength} characters)
            </label>
            <div className="space-y-2">
              {form.sortBoxes.map((row, i) => (
                <div key={i} className="flex gap-2 items-center">
                  <Input
                    placeholder={`Box ${i + 1}`}
                    value={row.text}
                    onChange={(e) => setLabel('sortBoxes', i, e.target.value)}
                    className="flex-1 text-sm"
                    aria-label={`Box ${i + 1}`}
                  />
                  {form.sortBoxes.length > SORT_BOXES.min && (
                    <Button type="button" variant="ghost" size="sm" onClick={() => removeBox(i)} aria-label={`Remove box ${i + 1}`}>
                      <Trash2 size={12} />
                    </Button>
                  )}
                </div>
              ))}
              {form.sortBoxes.length < SORT_BOXES.max && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => set('sortBoxes', [...form.sortBoxes, { text: '', loaded: null }])}
                >
                  <Plus size={12} className="mr-1" /> Add box
                </Button>
              )}
            </div>
          </div>

          {form.grading === 'COMPLETENESS' && (
            <label className="flex items-center gap-2 text-sm text-secondary">
              <input
                type="checkbox"
                className="rounded border-input bg-raised accent-primary"
                checked={form.sortKeyOn}
                onChange={(e) => set('sortKeyOn', e.target.checked)}
              />
              Include an answer key (not scored)
            </label>
          )}

          <div>
            <label className="block text-xs text-secondary mb-2">
              Cards ({SORT_CARDS.min}–{SORT_CARDS.max}, up to {SORT_CARDS.maxLength} characters)
              {(form.grading === 'ACCURACY' || form.sortKeyOn) && ' and each card’s correct box'}
            </label>
            <div className="space-y-2">
              {form.sortCards.map((row, i) => (
                <div key={i} className="flex gap-2 items-center">
                  <Input
                    placeholder={`Card ${i + 1}`}
                    value={row.text}
                    onChange={(e) => setLabel('sortCards', i, e.target.value)}
                    className="flex-1 text-sm"
                    aria-label={`Card ${i + 1}`}
                  />
                  {(form.grading === 'ACCURACY' || form.sortKeyOn) && (
                    <select
                      className="w-44 rounded-lg border border-input bg-raised px-2 py-2 text-primary focus-visible:border-accent text-sm"
                      value={form.sortKey[i] === null || form.sortKey[i] === undefined ? '' : String(form.sortKey[i])}
                      onChange={(e) => assign(i, e.target.value === '' ? null : Number(e.target.value))}
                      aria-label={`Card ${i + 1} correct box`}
                    >
                      <option value="">— Choose a box —</option>
                      {form.sortBoxes.map((box, j) => (
                        <option key={j} value={j}>
                          {box.text.trim() || `Box ${j + 1}`}
                        </option>
                      ))}
                    </select>
                  )}
                  {form.sortCards.length > SORT_CARDS.min && (
                    <Button type="button" variant="ghost" size="sm" onClick={() => removeCard(i)} aria-label={`Remove card ${i + 1}`}>
                      <Trash2 size={12} />
                    </Button>
                  )}
                </div>
              ))}
              {form.sortCards.length < SORT_CARDS.max && (
                <Button type="button" variant="outline" size="sm" onClick={addCard}>
                  <Plus size={12} className="mr-1" /> Add card
                </Button>
              )}
            </div>
          </div>
          {form.grading === 'ACCURACY' && (
            <p className="text-secondary text-xs">
              Scoring per card: right +1, wrong −1/(boxes − 1), left out 0; the question never scores
              below 0. Players see the cards in a shuffled order.
            </p>
          )}
        </div>
      )}

      {form.type === 'find_in_image' && (
        <SpotItEditor
          form={form}
          question={question}
          images={images}
          spotImage={spotImage}
          onUpload={onUpload}
          refreshImages={refreshImages}
          chooseImage={chooseSpotImage}
          setGridValue={setGridValue}
          toggleCell={toggleCell}
          removeOutside={removeOutside}
          setConfirmed={(checked) =>
            setForm((prev) => ({
              ...prev,
              // Any unticked → ticked transition builds a fresh stamp at save (spec §2.5).
              spotStamp: checked ? 'fresh' : 'none',
              spotVersionSeen: spotImage?.version ?? prev.spotVersionSeen,
              spotInitPending: false,
            }))
          }
        />
      )}

      {/* Time + points */}
      <div className="flex gap-4">
        <div>
          <label className="block text-xs text-secondary mb-1">Time limit (seconds)</label>
          <Input
            type="number"
            value={form.timeLimitSeconds}
            onChange={(e) => set('timeLimitSeconds', Number(e.target.value))}
            className="w-28 text-sm"
            min="2"
            max="300"
          />
        </div>
        {(form.grading === 'COMPLETENESS' || isT7) && (
          <div>
            <label className="block text-xs text-secondary mb-1">
              {isT7 && form.grading === 'ACCURACY' ? 'Points (for a fully correct answer)' : 'Points value'}
            </label>
            <Input
              type="number"
              value={form.pointsValue}
              onChange={(e) => set('pointsValue', Number(e.target.value))}
              className="w-28 text-sm"
              min="0"
              max="100000"
              step="any"
              aria-label="Points"
            />
          </div>
        )}
      </div>

      {isT7 && problems.length > 0 && (
        <ul className="list-disc pl-5 text-warning text-xs space-y-0.5" aria-live="polite">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {saveError && (
        <p className="text-danger text-sm" role="alert">
          {saveError}
        </p>
      )}

      <div className="flex gap-3">
        <Button
          type="button"
          onClick={() => onSave(formToPayload(form, images))}
          disabled={saving || (isT7 && problems.length > 0)}
        >
          {saving ? 'Saving…' : 'Save Question'}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

// ---- Spot It editor (T7 spec §2.4, §2.5) ----

function SpotItEditor({
  form,
  question,
  images,
  spotImage,
  onUpload,
  refreshImages,
  chooseImage,
  setGridValue,
  toggleCell,
  removeOutside,
  setConfirmed,
}: {
  form: FormState;
  question?: Question;
  images: ImageItem[];
  spotImage: ImageItem | undefined;
  onUpload: (file: File, description: string) => Promise<ImageItem>;
  refreshImages: () => Promise<void>;
  chooseImage: (id: number | null) => void;
  setGridValue: (key: 'cols' | 'rows', value: string) => void;
  toggleCell: (cell: GridCell) => void;
  removeOutside: () => void;
  setConfirmed: (checked: boolean) => void;
}) {
  // Authoring dimensions: the image's current library size, never config.width/height (§2.4).
  const width = spotImage?.width ?? 0;
  const height = spotImage?.height ?? 0;
  const grid = validGrid(form.spotGrid) ? form.spotGrid : null;
  const limits = spotImage ? maxGrid(width, height) : null;
  const fits = !!grid && !!spotImage && gridFits(grid, width, height);
  const outside = outOfBounds(form.spotCells, form.spotGrid);
  const half = grid ? Math.floor((grid.cols * grid.rows) / 2) : 0;
  const cell = grid && spotImage ? phoneCellSize(grid, width, height) : null;
  const inGrid = grid ? form.spotCells.filter((c) => c.col < grid.cols && c.row < grid.rows) : [];

  return (
    <div className="space-y-3">
      <div>
        <ImagePicker
          label="Spot It image"
          value={form.spotImageId}
          images={images}
          onSelect={chooseImage}
          onUpload={onUpload}
          refresh={refreshImages}
        />
        <p className="text-secondary text-xs mt-1">
          The projector shows this image under a labelled grid; players pick the matching cell on
          their phones, which show only the grid — never the image. Spot It is visual-only: it
          needs players who can see the projector.
        </p>
      </div>

      {question?.needs_review && (
        <p className="rounded-lg border border-warning bg-warning-subtle px-3 py-2 text-xs text-warning">
          Needs review — {reviewText(question.review_reasons ?? [], readGrid(question.config['grid']), spotImage)}
        </p>
      )}

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-secondary mb-1">Columns</label>
          <Input
            type="number"
            value={form.spotGrid?.cols || ''}
            onChange={(e) => setGridValue('cols', e.target.value)}
            className="w-20 text-sm"
            min={MIN_GRID}
            max={MAX_GRID}
            disabled={form.spotImageId === null}
            aria-label="Grid columns"
          />
        </div>
        <div>
          <label className="block text-xs text-secondary mb-1">Rows</label>
          <Input
            type="number"
            value={form.spotGrid?.rows || ''}
            onChange={(e) => setGridValue('rows', e.target.value)}
            className="w-20 text-sm"
            min={MIN_GRID}
            max={MAX_GRID}
            disabled={form.spotImageId === null}
            aria-label="Grid rows"
          />
        </div>
        {spotImage && limits && (
          <p className="text-xs text-secondary" data-spot-limits={`${limits.cols}x${limits.rows}`}>
            {fitsNoGrid(width, height)
              ? `This image ${tooDenseText({ cols: 2, rows: 2 }, width, height).replace(/^its image /, '')}; drafts still save, unconfirmed.`
              : `Up to ${limits.cols}×${limits.rows} for this image.`}
            {cell && ` About ${Math.floor(cell.w)}×${Math.floor(cell.h)} px per cell on a phone.`}
          </p>
        )}
      </div>

      {spotImage && grid && (
        <SpotItCanvas
          mode="author"
          grid={grid}
          width={width}
          height={height}
          image={{ url: spotImage.url, description: spotImage.description }}
          refreshImage={refreshImages}
          maxHeightVh={50}
          accepted={inGrid}
          onToggle={toggleCell}
          label={`Spot It grid, ${grid.cols} columns by ${grid.rows} rows, over ${spotImage.description}`}
        />
      )}

      {grid && (
        <p className="text-xs text-secondary">
          {form.spotCells.length} of max {half} accepted cells
          {form.spotCells.length > 0 && `: ${sortCells(form.spotCells).map(cellLabel).join(', ')}`}
          {form.grading === 'COMPLETENESS' && ' (optional, not scored)'}
        </p>
      )}
      {outside.length > 0 && (
        <div className="flex items-center gap-2 text-xs text-warning">
          <span>Outside the grid: {outside.map(cellLabel).join(', ')}</span>
          <Button type="button" size="sm" variant="outline" onClick={removeOutside}>
            Remove them
          </Button>
        </div>
      )}

      <label className={`flex items-start gap-2 text-sm ${fits ? 'text-primary' : 'text-disabled'}`}>
        <input
          type="checkbox"
          className="mt-1 rounded border-input bg-raised accent-primary"
          checked={form.spotStamp !== 'none'}
          disabled={!fits || outside.length > 0}
          onChange={(e) => setConfirmed(e.target.checked)}
        />
        <span>
          Accepted cells checked against this image and grid
          {spotImage && grid && !fits && limits && !fitsNoGrid(width, height) && (
            <span className="block text-xs text-warning">
              This image allows up to {limits.cols}×{limits.rows}; the draft still saves unconfirmed.
            </span>
          )}
        </span>
      </label>
    </div>
  );
}

// ---- Question list thumbnails ----

/** A library image as a list thumbnail, or "Missing image" for a reference the library lacks. */
function ListThumb({ id, images, size, refresh }: {
  id: number | null;
  images: Map<number, ImageItem>;
  size: string;
  refresh: () => Promise<void>;
}) {
  if (id === null) return null;
  const image = images.get(id);
  if (!image) {
    return <span className="text-warning text-xs whitespace-nowrap">Missing image</span>;
  }
  return (
    <SafeImage
      key={image.id}
      src={image.small_url}
      alt={image.description}
      className={`${size} shrink-0`}
      fit="cover"
      refresh={refresh}
      compact
    />
  );
}

// ---- Main page ----

export default function QuestionEditorPage() {
  const { gameId } = useParams<{ gameId: string }>();
  const navigate = useNavigate();
  const [game, setGame] = useState<Game | null>(null);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [formError, setFormError] = useState('');
  const [showAddForm, setShowAddForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const imageLibrary = useImageLibrary(gameId);
  const libraryImages = imageLibrary.library?.images ?? [];
  const imagesById = new Map(libraryImages.map((img) => [img.id, img]));

  async function load() {
    try {
      const [g, qs] = await Promise.all([
        api.get<Game>(`/host/games/${gameId}`),
        api.get<Question[]>(`/host/games/${gameId}/questions`),
      ]);
      setGame(g);
      setQuestions(qs);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [gameId]);

  async function addQuestion(payload: Payload) {
    setSaving(true);
    setFormError('');
    try {
      await api.post(`/host/games/${gameId}/questions`, payload);
      setShowAddForm(false);
      await load(); // creates commit before responding, so this reads the new question
      void imageLibrary.reload(); // uses and the export meter changed
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to save question');
    } finally {
      setSaving(false);
    }
  }

  async function updateQuestion(id: number, payload: Payload) {
    setSaving(true);
    setFormError('');
    try {
      // Adopt the response (review status included) instead of reloading: updates commit after
      // responding, so a reload now could still show the old state.
      const saved = await api.put<Question>(`/host/games/${gameId}/questions/${id}`, payload);
      setQuestions((prev) => prev.map((q) => (q.id === id ? saved : q)));
      setEditingId(null);
      void imageLibrary.reload();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to update question');
    } finally {
      setSaving(false);
    }
  }

  async function replaceImage(imageId: number, file: File) {
    const item = await imageLibrary.replace(imageId, file);
    void load(); // a replace can make Spot It questions need review (it commits before responding)
    return item;
  }

  async function deleteQuestion(id: number) {
    if (!confirm('Delete this question?')) return;
    try {
      await api.delete(`/host/games/${gameId}/questions/${id}`);
      setQuestions((prev) => prev.filter((q) => q.id !== id));
      void imageLibrary.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete question');
    }
  }

  async function moveQuestion(index: number, direction: -1 | 1) {
    const newOrder = [...questions];
    const target = index + direction;
    if (target < 0 || target >= newOrder.length) return;
    [newOrder[index], newOrder[target]] = [newOrder[target], newOrder[index]];
    try {
      await api.post(`/host/games/${gameId}/questions/reorder`, {
        order: newOrder.map((q) => q.id),
      });
      setQuestions(newOrder);
      void imageLibrary.reload(); // "Used by Q2" numbers follow the order
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reorder');
    }
  }

  async function handleExport() {
    setDownloading(true);
    setError('');
    try {
      await api.download(`/host/games/${gameId}/export`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed');
    } finally {
      setDownloading(false);
    }
  }

  const typeLabel: Record<QuestionType, string> = {
    multiple_choice: 'MC',
    true_false: 'T/F',
    fill_in_the_blank: 'Fill',
    multi_select: 'Multi',
    sort_into_boxes: 'Sort It',
    find_in_image: 'Spot It',
  };

  if (loading) return <div className="p-8 text-secondary">Loading…</div>;

  return (
    <div className="p-8 max-w-4xl space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <button
            onClick={() => navigate('/games')}
            className="flex items-center gap-2 text-secondary hover:text-primary text-sm mb-2"
          >
            <ArrowLeft size={14} /> Back to Games
          </button>
          <h2 className="text-2xl font-bold text-primary">{game?.title}</h2>
          <p className="text-secondary text-sm mt-0.5">{questions.length} question{questions.length !== 1 ? 's' : ''}</p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => void handleExport()}
            disabled={downloading}
          >
            <Download size={14} className="mr-1" />
            {downloading ? 'Exporting…' : 'Export JSON'}
          </Button>
          <Button size="sm" onClick={() => { setShowAddForm(true); setEditingId(null); setFormError(''); }}>
            <Plus size={14} className="mr-1" /> Add Question
          </Button>
        </div>
      </div>

      {error && <p className="text-danger text-sm">{error}</p>}

      <ImageLibraryPanel
        library={imageLibrary.library}
        loading={imageLibrary.loading}
        error={imageLibrary.error}
        onUpload={imageLibrary.upload}
        onReplace={replaceImage}
        onSetDescription={imageLibrary.setDescription}
        onDelete={imageLibrary.remove}
        refresh={imageLibrary.reload}
      />

      {/* Add question form */}
      {showAddForm && (
        <Card>
          <CardHeader><h3 className="font-semibold text-primary">New Question</h3></CardHeader>
          <CardContent>
            <QuestionForm
              initial={defaultForm()}
              onSave={(payload) => void addQuestion(payload)}
              onCancel={() => setShowAddForm(false)}
              saving={saving}
              saveError={formError}
              images={libraryImages}
              onUpload={imageLibrary.upload}
              refreshImages={imageLibrary.reload}
            />
          </CardContent>
        </Card>
      )}

      {/* Question list */}
      {questions.length === 0 && !showAddForm && (
        <p className="text-secondary">No questions yet. Add one above.</p>
      )}

      <div className="space-y-4">
        {questions.map((q, i) => (
          <Card key={q.id}>
            {editingId === q.id ? (
              <>
                <CardHeader>
                  <h3 className="font-semibold text-primary">Edit Question {i + 1}</h3>
                </CardHeader>
                <CardContent>
                  <QuestionForm
                    initial={questionToForm(q, libraryImages)}
                    question={q}
                    onSave={(payload) => void updateQuestion(q.id, payload)}
                    onCancel={() => setEditingId(null)}
                    saving={saving}
                    saveError={formError}
                    images={libraryImages}
                    onUpload={imageLibrary.upload}
                    refreshImages={imageLibrary.reload}
                  />
                </CardContent>
              </>
            ) : (
              <div className="flex items-start gap-4 px-6 py-4">
                {/* Reorder buttons */}
                <div className="flex flex-col gap-1 mt-1">
                  <button
                    onClick={() => void moveQuestion(i, -1)}
                    disabled={i === 0}
                    className="text-secondary hover:text-primary disabled:opacity-20"
                  >
                    <ChevronUp size={16} />
                  </button>
                  <button
                    onClick={() => void moveQuestion(i, 1)}
                    disabled={i === questions.length - 1}
                    className="text-secondary hover:text-primary disabled:opacity-20"
                  >
                    <ChevronDown size={16} />
                  </button>
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <span className="text-secondary text-xs font-mono">Q{i + 1}</span>
                    <span className="px-1.5 py-0.5 rounded text-xs bg-raised text-secondary">
                      {typeLabel[q.type] ?? q.type}
                    </span>
                    <span className="px-1.5 py-0.5 rounded text-xs bg-raised text-secondary">
                      {q.grading_type}
                    </span>
                    <span className="text-secondary text-xs">{q.time_limit_seconds}s · {q.points_value}pts</span>
                    {q.needs_review && (
                      <span
                        className="px-1.5 py-0.5 rounded text-xs bg-warning-subtle text-warning font-semibold"
                        data-needs-review="true"
                      >
                        Needs review
                      </span>
                    )}
                  </div>
                  <p className="text-primary text-sm leading-relaxed">{q.prompt}</p>
                  {q.needs_review && (
                    <p className="mt-1 text-warning text-xs">
                      {reviewText(
                        q.review_reasons ?? [],
                        readGrid(q.config['grid']),
                        imagesById.get(canvasImageId(q.config as QuestionConfig) ?? -1),
                      )}
                    </p>
                  )}
                  {promptImageId(q.config as QuestionConfig) !== null && (
                    <div className="mt-2">
                      <ListThumb
                        id={promptImageId(q.config as QuestionConfig)}
                        images={imagesById}
                        size="h-16 w-24"
                        refresh={imageLibrary.reload}
                      />
                    </div>
                  )}

                  {q.type === 'multiple_choice' && (
                    <div className="mt-2 space-y-1">
                      {((q.config['options'] as string[]) ?? []).map((opt, oi) => {
                        const pts = ((q.answer_data['answer_points'] as number[]) ?? [])[oi] ?? 0;
                        return (
                          <div key={oi} className="flex items-center gap-2 text-xs">
                            <span className={pts > 0 ? 'text-success' : 'text-secondary'}>
                              {pts > 0 ? '✓' : '○'}
                            </span>
                            <span className="font-mono text-secondary">{optionLetter(oi)}</span>
                            <ListThumb
                              id={optionImageIds(q.config as QuestionConfig)[oi] ?? null}
                              images={imagesById}
                              size="h-8 w-8"
                              refresh={imageLibrary.reload}
                            />
                            {hasCaption(opt) && (
                              <span className={pts > 0 ? 'text-primary' : 'text-secondary'}>{opt}</span>
                            )}
                            {pts > 0 && <span className="text-secondary">({pts}pts)</span>}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {q.type === 'true_false' && (
                    <div className="mt-2 flex gap-4 text-xs">
                      {(['true', 'false'] as const).map((k) => {
                        const pts = (q.answer_data['answer_points'] as Record<string, number>)?.[k] ?? 0;
                        return (
                          <span key={k} className={pts > 0 ? 'text-success' : 'text-secondary'}>
                            {k.charAt(0).toUpperCase() + k.slice(1)}: {pts}pts
                          </span>
                        );
                      })}
                    </div>
                  )}

                  {q.type === 'fill_in_the_blank' && (
                    <div className="mt-2 text-xs text-secondary">
                      Accepted: {((q.answer_data['acceptedAnswers'] as string[]) ?? []).join(', ')}
                      {(q.answer_data['editDistance'] as number) > 0 && (
                        <span className="ml-2 text-secondary">(±{q.answer_data['editDistance'] as number} edit distance)</span>
                      )}
                    </div>
                  )}

                  {q.type === 'multi_select' && (
                    <div className="mt-2 space-y-1">
                      {((q.config['options'] as string[]) ?? []).map((opt, oi) => {
                        const pts = ((q.answer_data['answer_points'] as number[]) ?? [])[oi] ?? 0;
                        const isCorrect = pts > 0;
                        return (
                          <div key={oi} className="flex items-center gap-2 text-xs">
                            <span className={isCorrect ? 'text-success' : pts < 0 ? 'text-danger' : 'text-secondary'}>
                              {isCorrect ? '✓' : pts < 0 ? '−' : '○'}
                            </span>
                            <span className="font-mono text-secondary">{optionLetter(oi)}</span>
                            <ListThumb
                              id={optionImageIds(q.config as QuestionConfig)[oi] ?? null}
                              images={imagesById}
                              size="h-8 w-8"
                              refresh={imageLibrary.reload}
                            />
                            {hasCaption(opt) && (
                              <span className={isCorrect ? 'text-primary' : 'text-secondary'}>{opt}</span>
                            )}
                            {pts !== 0 && <span className="text-secondary">({pts > 0 ? '+' : ''}{pts}pts)</span>}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {q.type === 'sort_into_boxes' && (() => {
                    const boxes = Array.isArray(q.config['boxes']) ? (q.config['boxes'] as string[]) : [];
                    const cards = Array.isArray(q.config['cards']) ? (q.config['cards'] as string[]) : [];
                    const key = Array.isArray(q.answer_data['correct_boxes'])
                      ? (q.answer_data['correct_boxes'] as number[])
                      : null;
                    return (
                      <div className="mt-2 space-y-1 text-xs">
                        <p className="text-secondary">
                          Sort It · {boxes.length} boxes · {cards.length} cards
                        </p>
                        <div className="flex flex-wrap gap-x-4 gap-y-1 text-secondary">
                          {cards.map((card, ci) => (
                            <span key={ci}>
                              {card}
                              {key && boxes[key[ci]] !== undefined && (
                                <span className="text-success"> → {boxes[key[ci]]}</span>
                              )}
                            </span>
                          ))}
                        </div>
                      </div>
                    );
                  })()}

                  {q.type === 'find_in_image' && (() => {
                    const grid = readGrid(q.config['grid']);
                    const cells = readCells(q.answer_data['accepted_cells']);
                    return (
                      <div className="mt-2 flex items-center gap-3 text-xs text-secondary">
                        <ListThumb
                          id={canvasImageId(q.config as QuestionConfig)}
                          images={imagesById}
                          size="h-12 w-16"
                          refresh={imageLibrary.reload}
                        />
                        <span>
                          Spot It · {grid ? `${grid.cols}×${grid.rows}` : 'no grid'} · {cells.length}{' '}
                          {cells.length === 1 ? 'cell' : 'cells'}
                          {cells.length > 0 && (
                            <span className="text-success"> ({sortCells(cells).map(cellLabel).join(', ')})</span>
                          )}
                        </span>
                      </div>
                    );
                  })()}
                </div>

                <div className="flex gap-1 shrink-0">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => { setEditingId(q.id); setShowAddForm(false); setFormError(''); }}
                  >
                    Edit
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void deleteQuestion(q.id)}
                  >
                    <Trash2 size={14} />
                  </Button>
                </div>
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}
