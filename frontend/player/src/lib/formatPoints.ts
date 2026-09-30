// The T7 points display rule (spec §2.2), used by the phone's Sort It and Spot It results and
// game-over lines: at most one decimal, rounding half away from zero, a trailing ".0" dropped,
// in the viewer's locale (en-US: 687.5, 708.3, 1,000). It rounds the value's shortest decimal
// representation, like the report's `Decimal(repr(x))`: 0.15 → 0.2 and 2.55 → 2.6 in both.
// Stored and scored values are never rounded; only this display is.

const formatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

export function formatPoints(value: number): string {
  return formatter.format(value);
}
