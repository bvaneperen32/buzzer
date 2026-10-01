import { useRef, useState } from 'react';
import { ImagePlus, X } from 'lucide-react';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { SafeImage } from '../ui/SafeImage';
import { formatLimit } from '../../lib/format';
import { IMAGE_INPUT_LIMIT_BYTES, type ImageItem } from '../../types/images';

const ACCEPT = 'image/png,image/jpeg,image/webp,image/gif';

interface ImagePickerProps {
  /** What the image is for, e.g. "Prompt image" or "Option B image". */
  label: string;
  /** The selected image id, or null for no image. */
  value: number | null;
  /** The game's library. */
  images: ImageItem[];
  onSelect: (id: number | null) => void;
  /** Uploads into the library and returns the new item, which the picker then selects. */
  onUpload: (file: File, description: string) => Promise<ImageItem>;
  /** Refreshes the library's links (for a thumbnail whose link failed). */
  refresh?: () => Promise<void>;
  /** A smaller trigger, for option rows. */
  compact?: boolean;
}

/**
 * Choose an image from the game's library, choose none, or upload a new one (which goes into
 * the library and is then selected; if the question is cancelled it stays there, "Unused").
 *
 * A reference the library doesn't have (a legacy question, T8 spec §2.7) shows "Missing image":
 * the server refuses to save it, so the author picks another image or none. Presentational:
 * the page owns the library and passes the upload through.
 */
export function ImagePicker({ label, value, images, onSelect, onUpload, refresh, compact = false }: ImagePickerProps) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [description, setDescription] = useState('');
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  const selected = value !== null ? images.find((i) => i.id === value) : undefined;
  const thumbSize = compact ? 'h-9 w-9' : 'h-16 w-16';

  function chooseFile(next: File | null) {
    setError('');
    if (next && next.size > IMAGE_INPUT_LIMIT_BYTES) {
      setError(`That file is larger than the ${formatLimit(IMAGE_INPUT_LIMIT_BYTES)} limit for images.`);
      setFile(null);
      if (fileRef.current) fileRef.current.value = '';
      return;
    }
    setFile(next);
  }

  async function upload() {
    if (!file || !description.trim()) return;
    setUploading(true);
    setError('');
    try {
      const item = await onUpload(file, description.trim());
      onSelect(item.id);
      setFile(null);
      setDescription('');
      if (fileRef.current) fileRef.current.value = '';
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        {selected ? (
          <SafeImage
            key={selected.id}
            src={selected.small_url}
            alt={selected.description}
            className={`${thumbSize} shrink-0`}
            fit="cover"
            refresh={refresh}
            compact
          />
        ) : value !== null ? (
          <span className="text-warning text-xs whitespace-nowrap" title={`Image ${value} isn't in this game's library`}>
            Missing image
          </span>
        ) : (
          !compact && <span className="text-secondary text-xs">No image</span>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-label={`${value !== null ? 'Change' : 'Add'} ${label.toLowerCase()}`}
          title={label}
        >
          <ImagePlus size={12} className={compact ? '' : 'mr-1'} />
          {!compact && (value !== null ? 'Change image' : 'Add image')}
        </Button>
        {value !== null && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onSelect(null)}
            aria-label={`Remove ${label.toLowerCase()}`}
            title="No image"
          >
            <X size={12} />
          </Button>
        )}
      </div>

      {open && (
        <div className="rounded-lg border border-subtle bg-raised p-3 space-y-3">
          <p className="text-xs text-secondary">{label}: choose from this game's images</p>
          {images.length === 0 ? (
            <p className="text-xs text-secondary">No images in this game's library yet — upload one below.</p>
          ) : (
            <div className="grid grid-cols-6 gap-2">
              {images.map((img) => (
                <button
                  key={img.id}
                  type="button"
                  onClick={() => {
                    onSelect(img.id);
                    setOpen(false);
                  }}
                  title={img.description}
                  aria-label={`Use image: ${img.description}`}
                  aria-pressed={img.id === value}
                  className={`rounded-lg border-2 p-0.5 ${img.id === value ? 'border-accent' : 'border-transparent hover:border-strong'}`}
                >
                  <SafeImage src={img.small_url} alt={img.description} className="aspect-square w-full" fit="cover" refresh={refresh} compact />
                </button>
              ))}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 border-t border-subtle pt-3">
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              onChange={(e) => chooseFile(e.target.files?.[0] ?? null)}
              className="text-xs text-secondary file:mr-2 file:rounded file:border-0 file:bg-hover file:px-2 file:py-1 file:text-primary"
              aria-label="Image file to upload"
            />
            <Input
              placeholder="Describe what it shows (required)"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={300}
              className="flex-1 min-w-48 text-sm"
              aria-label="Image description"
            />
            <Button type="button" size="sm" onClick={() => void upload()} disabled={!file || !description.trim() || uploading}>
              {uploading ? 'Uploading…' : 'Upload & use'}
            </Button>
          </div>
          {error && <p className="text-danger text-xs">{error}</p>}
        </div>
      )}
    </div>
  );
}
