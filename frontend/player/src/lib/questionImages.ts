import type { OptionImageLink, QuestionPayload } from '../types/game';

// The phone's side of T8's option images. Deliberately a copy of the host's helpers (the apps
// share no code); configs stored before T8 can hold anything, so only a positive integer
// counts as an image id.

function asImageId(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null;
}

/**
 * One entry per option: its small link, or null. An option whose id is missing from the
 * question's `images` map has no image here — the phone shows its letter and caption.
 */
export function optionImageLinks(question: QuestionPayload): (OptionImageLink | null)[] {
  const options = question.config.options ?? [];
  const entries = Array.isArray(question.config.option_images) ? question.config.option_images : [];
  return options.map((_, i) => {
    const id = asImageId(entries[i]);
    return id === null ? null : question.images?.[String(id)] ?? null;
  });
}

export function optionLetter(i: number): string {
  return i < 26 ? String.fromCharCode(65 + i) : String(i + 1);
}

/** A caption counts as empty when it's missing or only whitespace. */
export function hasCaption(caption: unknown): boolean {
  return typeof caption === 'string' ? caption.trim() !== '' : caption != null;
}

/** The phone's option label: "A — Paris", or the letter alone when there's no caption. */
export function choiceLabel(i: number, caption: unknown): string {
  return hasCaption(caption) ? `${optionLetter(i)} — ${caption}` : optionLetter(i);
}

/** The accessible name of an image option (T8 spec §2.12). */
export function imageOptionName(i: number, caption: string | undefined, description: string): string {
  const letter = optionLetter(i);
  return hasCaption(caption)
    ? `Option ${letter}: ${caption}. Image: ${description}`
    : `Option ${letter}. Image: ${description}`;
}
