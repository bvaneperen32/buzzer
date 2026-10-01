import { useRef, useState } from 'react';
import { AlertTriangle, Pencil, RefreshCw, Trash2, Upload } from 'lucide-react';
import { Button } from '../ui/button';
import { Card, CardContent, CardHeader } from '../ui/card';
import { Input } from '../ui/input';
import { SafeImage } from '../ui/SafeImage';
import { formatBytes, formatLimit } from '../../lib/format';
import {
  IMAGE_INPUT_LIMIT_BYTES,
  type ImageItem,
  type LibraryResponse,
  type LiveSessionItem,
  type UsedBy,
} from '../../types/images';

const ACCEPT = 'image/png,image/jpeg,image/webp,image/gif';

interface ImageLibraryPanelProps {
  library: LibraryResponse | null;
  loading: boolean;
  error: string;
  onUpload: (file: File, description: string) => Promise<unknown>;
  onReplace: (imageId: number, file: File) => Promise<unknown>;
  onSetDescription: (imageId: number, description: string) => Promise<unknown>;
  onDelete: (imageId: number) => Promise<unknown>;
  /** Reloads the library (links included) — for a thumbnail whose link failed. */
  refresh: () => Promise<void>;
}

/** "Used by Q2 (prompt), Q5 (option B), Q7 (Spot It)", or "Unused" (question numbers as the
 * list shows them). */
function describeUses(uses: UsedBy[]): string {
  if (uses.length === 0) return 'Unused';
  const role = (u: UsedBy) =>
    u.role === 'option' ? `option ${u.option_letter}` : u.role === 'canvas' ? 'Spot It' : u.role;
  return `Used by ${uses.map((u) => `Q${u.number} (${role(u)})`).join(', ')}`;
}

/** T7: a replace makes every Spot It question on the image need review ("Q3 (Spot It) will
 * need review"), because its accepted cells were checked against the old image. */
function canvasUses(uses: UsedBy[]): string[] {
  return uses.filter((u) => u.role === 'canvas').map((u) => `Q${u.number} (Spot It)`);
}

function Meter({ label, value, limit, format }: {
  label: string;
  value: number;
  limit: number;
  format: (n: number) => string;
}) {
  const pct = limit > 0 ? Math.min(100, (value / limit) * 100) : 0;
  const over = value > limit;
  return (
    <div className="flex-1 min-w-40">
      <div className="flex justify-between text-xs mb-1">
        <span className="text-secondary">{label}</span>
        <span className={over ? 'text-danger font-semibold' : 'text-secondary'}>
          {format(value)} / {format(limit)}
          {over && ' — over the limit'}
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-meter-track overflow-hidden">
        <div
          className={`h-full rounded-full ${over ? 'bg-meter-danger' : pct > 85 ? 'bg-meter-warning' : 'bg-meter-accent'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function LiveBanner({ sessions }: { sessions: LiveSessionItem[] }) {
  return (
    <div role="status" className="rounded-lg border border-warning bg-warning-subtle p-3 text-sm space-y-1">
      <p className="flex items-center gap-2 font-semibold text-warning">
        <AlertTriangle size={14} /> A session of this game is live, so images can't be replaced or deleted.
      </p>
      <ul className="text-primary text-xs space-y-0.5">
        {sessions.map((s) => (
          <li key={s.session_id}>
            Room <span className="font-mono">{s.room_code}</span> · {s.status === 'LOBBY' ? 'in the lobby' : 'in progress'}
            {' · '}hosted by {s.host_name ?? 'an unknown host'} · created {new Date(s.created_at).toLocaleString()}
          </li>
        ))}
      </ul>
      <p className="text-xs text-primary">
        The session's host can rejoin and finish it, or delete it from Play (deleting discards its partial scores).
        Anyone else can ask that host or an admin; admins can delete sessions from Sessions. Uploads and description
        edits still work.
      </p>
    </div>
  );
}

/**
 * The editor's Images panel (T8 spec §2.8): every image of the game with its thumbnail,
 * description, size and uses; Upload, Replace, Edit description and Delete; meters for the image
 * count, library budget and export size; and a banner when a live session blocks Replace and
 * Delete (the server's 409 stays the backstop). Presentational — the page owns the library.
 */
export function ImageLibraryPanel({
  library,
  loading,
  error,
  onUpload,
  onReplace,
  onSetDescription,
  onDelete,
  refresh,
}: ImageLibraryPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState('');
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const [replaceTarget, setReplaceTarget] = useState<number | null>(null);
  const [confirmReplace, setConfirmReplace] = useState<number | null>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const replaceRef = useRef<HTMLInputElement>(null);

  const images = library?.images ?? [];
  const live = library?.live_sessions ?? [];
  const blocked = live.length > 0;
  const meters = library?.meters;

  function tooLarge(f: File): boolean {
    if (f.size <= IMAGE_INPUT_LIMIT_BYTES) return false;
    setActionError(`That file is larger than the ${formatLimit(IMAGE_INPUT_LIMIT_BYTES)} limit for images.`);
    return true;
  }

  async function run(label: string, action: () => Promise<unknown>) {
    setBusy(label);
    setActionError('');
    try {
      await action();
      return true;
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Something went wrong');
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function upload() {
    if (!file || !description.trim()) return;
    if (await run('upload', () => onUpload(file, description.trim()))) {
      setFile(null);
      setDescription('');
      if (uploadRef.current) uploadRef.current.value = '';
    }
  }

  async function replaceWith(f: File | null) {
    const target = replaceTarget;
    if (replaceRef.current) replaceRef.current.value = '';
    setReplaceTarget(null);
    if (!f || target === null || tooLarge(f)) return;
    await run(`replace-${target}`, () => onReplace(target, f));
  }

  async function saveDescription() {
    if (!editing || !editing.text.trim()) return;
    const { id, text } = editing;
    if (await run(`describe-${id}`, () => onSetDescription(id, text.trim()))) setEditing(null);
  }

  async function remove(id: number) {
    if (await run(`delete-${id}`, () => onDelete(id))) setConfirmDelete(null);
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-primary">Images</h3>
          <span className="text-secondary text-xs">PNG, JPEG, WebP or GIF · up to {formatLimit(IMAGE_INPUT_LIMIT_BYTES)} each</span>
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          {meters && (
            <div className="flex flex-wrap gap-4">
              <Meter label="Images" value={meters.image_count} limit={meters.image_limit} format={String} />
              <Meter label="Library" value={meters.library_bytes} limit={meters.library_budget} format={formatBytes} />
              <Meter label="Export size" value={meters.export_bytes} limit={meters.export_limit} format={formatBytes} />
            </div>
          )}

          {blocked && <LiveBanner sessions={live} />}
          {error && <p className="text-danger text-sm">{error}</p>}
          {actionError && <p className="text-danger text-sm">{actionError}</p>}

          {/* Upload */}
          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={uploadRef}
              type="file"
              accept={ACCEPT}
              onChange={(e) => {
                const f = e.target.files?.[0] ?? null;
                setActionError('');
                if (f && tooLarge(f)) {
                  e.target.value = '';
                  setFile(null);
                } else {
                  setFile(f);
                }
              }}
              className="text-xs text-secondary file:mr-2 file:rounded file:border-0 file:bg-hover file:px-2 file:py-1 file:text-primary"
              aria-label="Image file to upload"
            />
            <Input
              placeholder="Describe what the image shows — not which answer is correct"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={300}
              className="flex-1 min-w-64 text-sm"
              aria-label="Image description"
            />
            <Button type="button" size="sm" onClick={() => void upload()} disabled={!file || !description.trim() || busy !== null}>
              <Upload size={12} className="mr-1" /> {busy === 'upload' ? 'Uploading…' : 'Upload'}
            </Button>
          </div>

          {/* One hidden input serves every Replace button. */}
          <input
            ref={replaceRef}
            type="file"
            accept={ACCEPT}
            className="hidden"
            onChange={(e) => void replaceWith(e.target.files?.[0] ?? null)}
          />

          {loading && !library ? (
            <p className="text-secondary text-sm">Loading images…</p>
          ) : images.length === 0 ? (
            <p className="text-secondary text-sm">No images yet. Upload one here, or from a question's image picker.</p>
          ) : (
            <ul className="space-y-2">
              {images.map((img: ImageItem) => (
                <li key={img.id} className="flex items-start gap-3 rounded-lg border border-subtle bg-raised p-2">
                  <SafeImage
                    key={img.id}
                    src={img.small_url}
                    alt={img.description}
                    className="h-16 w-16 shrink-0"
                    fit="cover"
                    refresh={refresh}
                    showRetry
                    compact
                  />
                  <div className="flex-1 min-w-0 space-y-1">
                    {editing?.id === img.id ? (
                      <div className="flex gap-2">
                        <Input
                          value={editing.text}
                          onChange={(e) => setEditing({ id: img.id, text: e.target.value })}
                          maxLength={300}
                          className="flex-1 text-sm"
                          aria-label="New description"
                          autoFocus
                        />
                        <Button type="button" size="sm" onClick={() => void saveDescription()} disabled={!editing.text.trim() || busy !== null}>
                          {busy === `describe-${img.id}` ? 'Saving…' : 'Save'}
                        </Button>
                        <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
                      </div>
                    ) : (
                      <p className="text-primary text-sm break-words">{img.description}</p>
                    )}
                    <p className="text-secondary text-xs truncate">
                      {img.filename} · {img.width}×{img.height} · {formatBytes(img.byte_size)}
                      {img.version > 1 && ` · replaced ${img.version - 1}×`}
                    </p>
                    <p className={`text-xs ${img.used_by.length ? 'text-primary' : 'text-secondary'}`}>
                      {describeUses(img.used_by)}
                    </p>
                    {confirmReplace === img.id && (
                      <div className="flex flex-wrap items-center gap-2 pt-1" role="alert">
                        <span className="text-warning text-xs">
                          {canvasUses(img.used_by).join(', ')} will need review: the accepted cells
                          must be checked against the new image.
                        </span>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => {
                            setConfirmReplace(null);
                            setReplaceTarget(img.id);
                            replaceRef.current?.click();
                          }}
                        >
                          Choose a file
                        </Button>
                        <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmReplace(null)}>Cancel</Button>
                      </div>
                    )}
                    {confirmDelete === img.id && (
                      <div className="flex items-center gap-2 pt-1">
                        <span className="text-danger text-xs">Delete this image from the library?</span>
                        <Button type="button" size="sm" variant="destructive" onClick={() => void remove(img.id)} disabled={busy !== null}>
                          {busy === `delete-${img.id}` ? 'Deleting…' : 'Confirm'}
                        </Button>
                        <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmDelete(null)}>Cancel</Button>
                      </div>
                    )}
                  </div>
                  <div className="flex gap-1 shrink-0">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      title="Edit description"
                      aria-label={`Edit description of ${img.description}`}
                      onClick={() => setEditing({ id: img.id, text: img.description })}
                    >
                      <Pencil size={14} />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      title={blocked ? 'Not while a session is live' : 'Replace the image file'}
                      aria-label={`Replace ${img.description}`}
                      disabled={blocked || busy !== null}
                      onClick={() => {
                        if (canvasUses(img.used_by).length > 0) {
                          setConfirmReplace(img.id); // warn first: Spot It questions need review
                          return;
                        }
                        setReplaceTarget(img.id);
                        replaceRef.current?.click();
                      }}
                    >
                      {busy === `replace-${img.id}` ? '…' : <RefreshCw size={14} />}
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      title={blocked ? 'Not while a session is live' : 'Delete'}
                      aria-label={`Delete ${img.description}`}
                      disabled={blocked || busy !== null}
                      onClick={() => setConfirmDelete(img.id)}
                    >
                      <Trash2 size={14} />
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
