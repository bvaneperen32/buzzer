import { useState, type CSSProperties } from 'react';
import { cn } from '../../lib/utils';

interface SafeImageProps {
  /** The small link from `new_question`'s `images` map. */
  src: string;
  /** The image's description: its alt text, and what the failure placeholder shows. */
  alt: string;
  /** Sizing classes for the box (it reserves the space while loading). */
  className?: string;
  style?: CSSProperties;
}

type Status = 'loading' | 'loaded' | 'failed';

/**
 * The phone's copy of the host's SafeImage (T8 spec §2.12), tuned for touch: links arrive only
 * in `new_question`, so there is nothing to refresh. The first error remounts the element with
 * the same link (valid for at least 2 hours after it was sent, and no image changes version
 * during a live session); a second error shows "Image unavailable" with the description. No
 * Retry button — a failed image never blocks answering, since every option keeps its letter.
 *
 * The browser's broken-image icon never appears. One instance shows one link — key it by the
 * link, so a new question's link starts the sequence afresh.
 */
export function SafeImage({ src, alt, className, style }: SafeImageProps) {
  const [status, setStatus] = useState<Status>('loading');
  const [attempt, setAttempt] = useState(0);

  function handleError() {
    if (attempt === 0) {
      setStatus('loading');
      setAttempt(1); // remount: a fresh request for the same link
    } else {
      setStatus('failed');
    }
  }

  return (
    <div
      className={cn('relative flex items-center justify-center overflow-hidden rounded-xl bg-raised', className)}
      style={style}
    >
      {status !== 'failed' && (
        <img
          key={attempt}
          src={src}
          alt={alt}
          draggable={false}
          onLoad={() => setStatus('loaded')}
          onError={handleError}
          // bg-media keeps a transparent diagram readable in dark. The image shrink-wraps (no
          // object-contain), so it sits only behind the picture and the bands stay bg-raised.
          className={cn(
            'max-h-full max-w-full bg-media transition-opacity duration-200',
            status === 'loaded' ? 'opacity-100' : 'opacity-0',
          )}
        />
      )}
      {status === 'loading' && (
        <div className="absolute inset-0 animate-pulse bg-selected/40" aria-hidden="true" />
      )}
      {status === 'failed' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 p-2 text-center">
          <p className="text-xs font-semibold text-secondary">Image unavailable</p>
          <p className="line-clamp-3 text-xs text-secondary">{alt}</p>
        </div>
      )}
    </div>
  );
}
