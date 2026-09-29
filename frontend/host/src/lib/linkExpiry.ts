// Signed image links expire (T8 spec §2.9). Both link sources refresh within 10 minutes of the
// earliest `expires_at`, so a screen left open never shows an expired link.
const REFRESH_BEFORE_EXPIRY_MS = 10 * 60 * 1000;
// Never schedule the next refresh sooner than this. Every fetch replaces the links, which
// schedules the next one; a clock running 1 h 50 min fast would compute 0 every time (fresh
// links expire 2–3 h after the server's time) and refetch in a tight loop. An image that
// fails before then still refreshes at once, through SafeImage.
const MIN_REFRESH_DELAY_MS = 60 * 1000;

/** Milliseconds until the next refresh is due, or null when there are no links. */
export function refreshDelayMs(expiresAt: string[]): number | null {
  const times = expiresAt.map((e) => Date.parse(e)).filter((t) => !Number.isNaN(t));
  if (times.length === 0) return null;
  return Math.max(Math.min(...times) - REFRESH_BEFORE_EXPIRY_MS - Date.now(), MIN_REFRESH_DELAY_MS);
}
