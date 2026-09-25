const BASE = '/api';

/**
 * The server's message for a failed response. The backend sends three shapes:
 * `{error, message}` (BuzzerError), `{detail: string}` (HTTPException), and
 * `{detail: [{msg, ...}]}` (request validation).
 */
async function errorMessage(res: Response): Promise<string> {
  const body: unknown = await res.json().catch(() => null);
  const err = (body && typeof body === 'object' ? body : {}) as { message?: unknown; detail?: unknown };
  if (typeof err.message === 'string') return err.message;
  if (typeof err.detail === 'string') return err.detail;
  if (Array.isArray(err.detail)) {
    const msgs = err.detail
      .map((d) => (d && typeof d === 'object' ? (d as { msg?: unknown }).msg : undefined))
      .filter((m): m is string => typeof m === 'string');
    if (msgs.length) return msgs.join('; ');
  }
  return `HTTP ${res.status}`;
}

async function apiFetch<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem('token');
  const res = await fetch(`${BASE}${path}`, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts.headers as Record<string, string> ?? {}),
    },
  });
  if (!res.ok) throw new Error(await errorMessage(res));
  const text = await res.text();
  return text ? (JSON.parse(text) as T) : ({} as T);
}

export const api = {
  get: <T>(path: string) => apiFetch<T>(path),
  post: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
};
