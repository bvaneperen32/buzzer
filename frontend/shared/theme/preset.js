/**
 * Tailwind preset shared by all three apps (host, player, admin); each app's tailwind.config.ts
 * imports it by relative path with an explicit .js extension.
 *
 * It replaces Tailwind's colour palette (no slate-*, indigo-*, ...) and maps the semantic tokens
 * of tokens.css per utility, so classes read bg-surface, text-primary, border-input, and a
 * mismatch such as bg-primary or text-canvas does not exist. Keys it doesn't set (caret, fill,
 * stroke, shadow colour, gradients) fall back to `colors`: transparent, current and inherit only.
 * divideColor defaults to borderColor, so divide-subtle works without a key.
 *
 * Rules (docs/plans/t9-ui-theming.md sections 2.3 and 2.6):
 * - No imports: frontend/shared/ has no node_modules, so no package can resolve from here.
 * - Only static objects of strings: preset.d.ts declares exactly that shape. Keep it in step.
 * - Every token named here must exist in tokens.css (scripts/check_theme.py verifies it).
 */

/** A token as a Tailwind colour that keeps the opacity modifier working (bg-canvas/80). */
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

const keywords = { transparent: 'transparent', current: 'currentColor', inherit: 'inherit' };

export default {
  theme: {
    colors: { ...keywords },
    backgroundColor: {
      ...keywords,
      canvas: v('bg-canvas'),
      surface: v('bg-surface'),
      raised: v('bg-raised'),
      hover: v('bg-hover'),
      selected: v('bg-selected'),
      accent: v('bg-accent'),
      'accent-hover': v('bg-accent-hover'),
      'accent-subtle': v('bg-accent-subtle'),
      danger: v('bg-danger'),
      'danger-hover': v('bg-danger-hover'),
      'danger-subtle': v('bg-danger-subtle'),
      success: v('bg-success'),
      'success-hover': v('bg-success-hover'),
      'success-subtle': v('bg-success-subtle'),
      warning: v('bg-warning'),
      'warning-hover': v('bg-warning-hover'),
      'warning-subtle': v('bg-warning-subtle'),
      media: v('bg-media'),
      'option-1': v('bg-option-1'),
      'option-2': v('bg-option-2'),
      'option-3': v('bg-option-3'),
      'option-4': v('bg-option-4'),
      'option-5': v('bg-option-5'),
      'option-6': v('bg-option-6'),
      'option-7': v('bg-option-7'),
      'option-8': v('bg-option-8'),
      'meter-success': v('bg-meter-success'),
      'meter-warning': v('bg-meter-warning'),
      'meter-danger': v('bg-meter-danger'),
      'meter-accent': v('bg-meter-accent'),
      'meter-neutral': v('bg-meter-neutral'),
      'meter-track': v('bg-meter-track'),
    },
    textColor: {
      ...keywords,
      primary: v('text-primary'),
      secondary: v('text-secondary'),
      disabled: v('text-disabled'),
      accent: v('text-accent'),
      danger: v('text-danger'),
      success: v('text-success'),
      warning: v('text-warning'),
      'on-accent': v('text-on-accent'),
      'on-danger': v('text-on-danger'),
      'on-success': v('text-on-success'),
      'on-warning': v('text-on-warning'),
      'on-media': v('text-on-media'),
      'on-meter': v('text-on-meter'),
      'on-option-1': v('text-on-option-1'),
      'on-option-2': v('text-on-option-2'),
      'on-option-3': v('text-on-option-3'),
      'on-option-4': v('text-on-option-4'),
      'on-option-5': v('text-on-option-5'),
      'on-option-6': v('text-on-option-6'),
      'on-option-7': v('text-on-option-7'),
      'on-option-8': v('text-on-option-8'),
    },
    borderColor: {
      transparent: 'transparent',
      current: 'currentColor',
      DEFAULT: v('border-subtle'),
      subtle: v('border-subtle'),
      strong: v('border-strong'),
      input: v('border-input'),
      // Status and accent borders reuse the matching text colour (>=4.5:1 on every background).
      accent: v('text-accent'),
      danger: v('text-danger'),
      success: v('text-success'),
      warning: v('text-warning'),
    },
    // Nothing uses ring classes (focus is base.css's outline); these keep Tailwind's defaults
    // pointed at a token instead of its built-in blue.
    ringColor: {
      DEFAULT: v('ring-focus'),
      focus: v('ring-focus'),
    },
    outlineColor: {
      focus: v('ring-focus'),
    },
    accentColor: {
      primary: v('bg-accent'),
    },
  },
};
