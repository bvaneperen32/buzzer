import { useRef, useState, type CSSProperties } from 'react';
import { ImageOff } from 'lucide-react';
import { cn } from '../../lib/utils';

interface SafeImageProps {
  /** A server-issued link, or null when none is available (shown as unavailable). */
  src: string | null;
  /** The image's description: its alt text, and the text the failure placeholder shows. */
  alt: string;
  /** Sizing and layout classes for the box (it reserves the space while loading). */
  className?: string;
  style?: CSSProperties;
  fit?: 'contain' | 'cover';
  /** Fetches current links (the session link map or the library list). */
  refresh?: () => Promise<void>;
  /** Show a Retry button on the placeholder (projector and editor). */
  showRetry?: boolean;
  /**
   * A thumbnail too small for words: the placeholder is an icon, with "Image unavailable" and
   * the description as its accessible name and tooltip, and with `showRetry` the whole box is
   * the Retry button.
   */
  compact?: boolean;
}

type Status = 'loading' | 'loaded' | 'failed';

/**
 * An image loaded from a signed link, with the T8 spec §2.12 failure sequence:
 *
 * 1. A loading placeholder until the image arrives.
 * 2. On the first error, await `refresh()` if given (a link may have expired or changed).
 * 3. Retry once by bumping the element's key, which remounts it with the current `src` —
 *    the new link if the refresh changed it, otherwise the same one. Error responses carry no
 *    caching headers, so the remount sends a fresh request.
 * 4. On the second error, show "Image unavailable" with the description, plus Retry if
 *    `showRetry`; Retry starts again at step 2.
 *
 * The browser's broken-image icon never appears: the element stays transparent until it has
 * loaded. One instance shows one image — key it by image id where the image can change.
 * Presentational: it never fetches anything itself.
 */
export function SafeImage({
  src,
  alt,
  className,
  style,
  fit = 'contain',
  refresh,
  showRetry = false,
  compact = false,
}: SafeImageProps) {
  const [attempt, setAttempt] = useState(0);
  // A missing src shows as unavailable without entering the sequence, so a link that arrives
  // later (the projector's map loads after the question) still loads normally.
  const [status, setStatus] = useState<Status>('loading');
  const errors = useRef(0);

  async function handleError() {
    errors.current += 1;
    if (errors.current === 1) {
      if (refresh) {
        try {
          await refresh();
        } catch {
          // The retry below still runs; its failure shows the placeholder.
        }
      }
      setStatus('loading');
      setAttempt((a) => a + 1);
    } else {
      setStatus('failed');
    }
  }

  async function retry() {
    errors.current = 0;
    await handleError();
  }

  const failed = status === 'failed' || !src;

  return (
    <div
      className={cn('relative flex items-center justify-center overflow-hidden rounded-lg bg-raised', className)}
      style={style}
      aria-busy={status === 'loading' && !failed}
    >
      {!failed && (
        <img
          key={attempt}
          src={src}
          alt={alt}
          draggable={false}
          onLoad={() => {
            // A later failure (say, a refreshed link that fails once) gets the full sequence again.
            errors.current = 0;
            setStatus('loaded');
          }}
          onError={() => void handleError()}
          // bg-media keeps a transparent diagram readable in dark. Contained images shrink-wrap
          // (no object-contain), so it sits only behind the picture and the bands stay bg-raised.
          className={cn(
            'bg-media transition-opacity duration-200',
            fit === 'cover' ? 'h-full w-full object-cover' : 'max-h-full max-w-full',
            status === 'loaded' ? 'opacity-100' : 'opacity-0',
          )}
        />
      )}
      {status === 'loading' && !failed && (
        <div className="absolute inset-0 animate-pulse bg-selected/40" aria-hidden="true" />
      )}
      {failed && compact && showRetry && src && (
        <button
          type="button"
          onClick={() => void retry()}
          title={`Image unavailable: ${alt}. Click to retry.`}
          aria-label={`Image unavailable: ${alt}. Retry`}
          className="absolute inset-0 flex items-center justify-center hover:bg-hover/60"
        >
          <ImageOff className="h-4 w-4 text-secondary" aria-hidden="true" />
        </button>
      )}
      {failed && compact && !(showRetry && src) && (
        <div
          role="img"
          aria-label={`Image unavailable: ${alt}`}
          title={`Image unavailable: ${alt}`}
          className="absolute inset-0 flex items-center justify-center"
        >
          <ImageOff className="h-4 w-4 text-secondary" aria-hidden="true" />
        </div>
      )}
      {failed && !compact && (
        <div
          role="img"
          aria-label={`Image unavailable: ${alt}`}
          className="absolute inset-0 flex flex-col items-center justify-center gap-1 p-2 text-center"
        >
          <ImageOff className="h-5 w-5 shrink-0 text-secondary" aria-hidden="true" />
          <p className="text-xs font-semibold text-secondary">Image unavailable</p>
          <p className="line-clamp-2 text-xs text-secondary">{alt}</p>
          {showRetry && src && (
            <button
              type="button"
              onClick={() => void retry()}
              className="mt-1 rounded border border-input px-2 py-0.5 text-xs text-primary hover:bg-hover"
            >
              Retry
            </button>
          )}
        </div>
      )}
    </div>
  );
}
