/**
 * Where the host app lives. Under nginx (production) it shares this origin at /host, so the
 * admin's token carries over; in dev it's a separate Vite server with its own token.
 * VITE_HOST_APP_URL overrides both.
 */
export const HOST_APP_URL: string =
  import.meta.env.VITE_HOST_APP_URL || (import.meta.env.PROD ? '/host' : 'http://localhost:5173');

/** An absolute link into the host app, e.g. `hostAppUrl('/games')`. */
export function hostAppUrl(path: string): string {
  return `${HOST_APP_URL.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
}
