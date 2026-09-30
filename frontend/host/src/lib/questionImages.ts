import type { QuestionConfig } from '../types/game';

// Reading image references out of a question's `config` (T8 spec §2.7). The backend validates
// new writes, but configs stored before T8 can hold anything, so these read defensively: only a
// positive integer counts as an image id.

function asImageId(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null;
}

export function promptImageId(config: QuestionConfig | undefined): number | null {
  return asImageId(config?.prompt_image_id);
}

/** One entry per option: its image id, or null. */
export function optionImageIds(config: QuestionConfig | undefined): (number | null)[] {
  const options = config?.options ?? [];
  const entries = Array.isArray(config?.option_images) ? config.option_images : [];
  return options.map((_, i) => asImageId(entries[i]));
}

/** A Spot It question's canvas image (T7), or null. */
export function canvasImageId(config: QuestionConfig | undefined): number | null {
  return asImageId(config?.image_id);
}

/** Every image id the question refers to (prompt, options and the Spot It canvas). */
export function referencedImageIds(config: QuestionConfig | undefined): number[] {
  const ids = [promptImageId(config), ...optionImageIds(config), canvasImageId(config)];
  return [...new Set(ids.filter((id): id is number => id !== null))];
}

export function optionLetter(i: number): string {
  return i < 26 ? String.fromCharCode(65 + i) : String(i + 1);
}

/** A caption counts as empty when it's missing or only whitespace. */
export function hasCaption(caption: unknown): boolean {
  return typeof caption === 'string' ? caption.trim() !== '' : caption != null;
}

/**
 * The accessible name of an image option (T8 spec §2.12): "Option B: {caption}. Image:
 * {description}", or "Option B. Image: {description}" when the option has no caption.
 */
export function imageOptionName(i: number, caption: string | undefined, description: string): string {
  const letter = optionLetter(i);
  return hasCaption(caption)
    ? `Option ${letter}: ${caption}. Image: ${description}`
    : `Option ${letter}. Image: ${description}`;
}
