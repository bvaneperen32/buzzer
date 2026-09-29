import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';
import { refreshDelayMs } from './linkExpiry';
import type { ImageItem, LibraryResponse } from '../types/images';

function upsert(images: ImageItem[], item: ImageItem): ImageItem[] {
  const next = images.some((i) => i.id === item.id)
    ? images.map((i) => (i.id === item.id ? item : i))
    : [...images, item];
  return next.sort((a, b) => a.id - b.id);
}

/**
 * One game's image library for the editor (`/host/games/{id}/images`): the images with their
 * links and uses, the live sessions that block Replace and Delete, and the meters.
 *
 * Mutations adopt the returned item (with its new links) at once, then reload, because a
 * write can change other items' uses and the meters. `reload` coalesces calls made at the
 * same time into one request; a response that started before a write is dropped, so it
 * can't overwrite the adopted item. The library also reloads within 10 minutes of the
 * earliest link expiry. Mutations throw the server's error (an `ApiError`).
 */
export function useImageLibrary(gameId: string | undefined) {
  const [library, setLibrary] = useState<LibraryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const inFlight = useRef<Promise<void> | null>(null);
  const writes = useRef(0);

  const reload = useCallback((): Promise<void> => {
    if (!gameId) return Promise.resolve();
    if (inFlight.current) return inFlight.current;
    const startedAfter = writes.current;
    const request = api
      .get<LibraryResponse>(`/host/games/${gameId}/images`)
      .then((data) => {
        if (writes.current === startedAfter) {
          setLibrary(data);
          setError('');
        }
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Failed to load images'))
      .finally(() => {
        setLoading(false);
        inFlight.current = null;
      });
    inFlight.current = request;
    return request;
  }, [gameId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    const delay = refreshDelayMs((library?.images ?? []).map((i) => i.expires_at));
    if (delay === null) return;
    const id = setTimeout(() => void reload(), delay);
    return () => clearTimeout(id);
  }, [library, reload]);

  /** After a write: ignore any older in-flight response, then load the current state. */
  const reloadAfterWrite = useCallback((): Promise<void> => {
    writes.current += 1;
    return inFlight.current ? inFlight.current.then(reload) : reload();
  }, [reload]);

  const adopt = useCallback((item: ImageItem) => {
    setLibrary((prev) => (prev ? { ...prev, images: upsert(prev.images, item) } : prev));
  }, []);

  const upload = useCallback(
    async (file: File, description: string): Promise<ImageItem> => {
      const form = new FormData();
      form.append('file', file);
      form.append('description', description);
      const item = await api.postForm<ImageItem>(`/host/games/${gameId}/images`, form);
      adopt(item);
      void reloadAfterWrite();
      return item;
    },
    [gameId, adopt, reloadAfterWrite],
  );

  const replace = useCallback(
    async (imageId: number, file: File): Promise<ImageItem> => {
      const form = new FormData();
      form.append('file', file);
      const item = await api.postForm<ImageItem>(
        `/host/games/${gameId}/images/${imageId}`,
        form,
        'PUT',
      );
      adopt(item);
      void reloadAfterWrite();
      return item;
    },
    [gameId, adopt, reloadAfterWrite],
  );

  const setDescription = useCallback(
    async (imageId: number, description: string): Promise<ImageItem> => {
      const item = await api.patch<ImageItem>(`/host/games/${gameId}/images/${imageId}`, {
        description,
      });
      adopt(item);
      void reloadAfterWrite();
      return item;
    },
    [gameId, adopt, reloadAfterWrite],
  );

  const remove = useCallback(
    async (imageId: number): Promise<void> => {
      await api.delete(`/host/games/${gameId}/images/${imageId}`);
      setLibrary((prev) =>
        prev ? { ...prev, images: prev.images.filter((i) => i.id !== imageId) } : prev,
      );
      void reloadAfterWrite();
    },
    [gameId, reloadAfterWrite],
  );

  return { library, loading, error, reload, upload, replace, setDescription, remove };
}

export type ImageLibrary = ReturnType<typeof useImageLibrary>;
