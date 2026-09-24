const BASE = '/api';

/** A non-2xx response. `status` lets callers tell a 401 from other failures. */
export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * Build an ApiError from a failed response. The backend sends three shapes:
 * `{error, message}` (BuzzerError), `{detail: string}` (HTTPException), and
 * `{detail: [{msg, ...}]}` (request validation).
 */
async function toApiError(res: Response): Promise<ApiError> {
  const body: unknown = await res.json().catch(() => null);
  const err = (body && typeof body === 'object' ? body : {}) as { message?: unknown; detail?: unknown };
  let message = `HTTP ${res.status}`;
  if (typeof err.message === 'string') {
    message = err.message;
  } else if (typeof err.detail === 'string') {
    message = err.detail;
  } else if (Array.isArray(err.detail)) {
    const msgs = err.detail
      .map((d) => (d && typeof d === 'object' ? (d as { msg?: unknown }).msg : undefined))
      .filter((m): m is string => typeof m === 'string');
    if (msgs.length) message = msgs.join('; ');
  }
  return new ApiError(res.status, message);
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
  if (!res.ok) throw await toApiError(res);
  const text = await res.text();
  return text ? (JSON.parse(text) as T) : ({} as T);
}

/** Trigger a file download from a fetch response */
async function downloadFetch(path: string): Promise<void> {
  const token = localStorage.getItem('token');
  const res = await fetch(`${BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw await toApiError(res);
  const disposition = res.headers.get('Content-Disposition') ?? '';
  const match = disposition.match(/filename="([^"]+)"/);
  const filename = match ? match[1] : 'download';
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export const api = {
  get: <T>(path: string) => apiFetch<T>(path),
  post: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'PUT', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'PATCH', body: body !== undefined ? JSON.stringify(body) : undefined }),
  delete: <T>(path: string) => apiFetch<T>(path, { method: 'DELETE' }),
  /** POST multipart/form-data (for file uploads). No Content-Type: the browser adds the boundary. */
  postForm: <T>(path: string, form: FormData) => {
    const token = localStorage.getItem('token');
    return fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    }).then(async (res) => {
      if (!res.ok) throw await toApiError(res);
      const text = await res.text();
      return (text ? JSON.parse(text) : {}) as T;
    });
  },
  download: downloadFetch,
};
