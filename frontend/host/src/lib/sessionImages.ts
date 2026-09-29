import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import { refreshDelayMs } from './linkExpiry';
import type { LinkEntry, SessionImages } from '../types/images';

/**
 * The projector's image links for one session: full and small links for every image of the
 * session's game, keyed by image id as a string (`GET /game/sessions/{id}/images`).
 *
 * Fetched once the session id is known and again within 10 minutes of the earliest expiry.
 * `refresh` coalesces calls made at the same time into one request; GameLayout also calls it
 * after a socket reconnect and when a question uses an id the map doesn't have, and
 * `SafeImage` calls it on an image's first error.
 *
 * `failed` is true when the last fetch failed (for example a 403 after the host lost the game
 * grant): images the map can't resolve then show the unavailable placeholder rather than
 * silently disappearing.
 */
export function useSessionImageLinks(sessionId: string | null) {
  const [links, setLinks] = useState<Record<string, LinkEntry>>({});
  const [failed, setFailed] = useState(false);
  const inFlight = useRef<Promise<void> | null>(null);

  const refresh = useCallback((): Promise<void> => {
    if (!sessionId) return Promise.resolve();
    if (inFlight.current) return inFlight.current;
    const request = api
      .get<SessionImages>(`/game/sessions/${sessionId}/images`)
      .then((data) => {
        setLinks(data.images ?? {});
        setFailed(false);
      })
      .catch(() => setFailed(true)) // keep the current links; they stay valid until they expire
      .finally(() => {
        inFlight.current = null;
      });
    inFlight.current = request;
    return request;
  }, [sessionId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const delay = refreshDelayMs(Object.values(links).map((l) => l.expires_at));
    if (delay === null) return;
    const id = setTimeout(() => void refresh(), delay);
    return () => clearTimeout(id);
  }, [links, refresh]);

  return { links, refresh, failed };
}
