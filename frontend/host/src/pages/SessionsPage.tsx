import { useEffect, useState } from 'react';
import { Download, FileText, Trash2, ChevronDown, ChevronUp } from 'lucide-react';
import { api } from '../lib/api';
import { useMe } from '../lib/me';
import { Button } from '../components/ui/button';
import { Card } from '../components/ui/card';
import { Input } from '../components/ui/input';

interface SessionItem {
  session_id: string;
  room_code: string;
  status: string;
  game_id: number;
  game_title: string;
  course_id: number | null;
  course_name: string;
  course_semester: string;
  host_display_name: string | null;
  created_at: string;
  completed_at: string | null;
  player_count: number;
}

type StatusFilter = 'ALL' | 'LOBBY' | 'IN_PROGRESS' | 'COMPLETED' | 'ABANDONED';

interface ExportOptions {
  format: 'canvas' | 'raw';
  title: string;
  assignmentId: string;
  sisDomain: string;
  rosterOnly: boolean;
  perQuestion: boolean;
}

const STATUS_COLORS: Record<string, string> = {
  LOBBY: 'bg-raised text-secondary',
  IN_PROGRESS: 'bg-success-subtle text-success',
  COMPLETED: 'bg-accent-subtle text-accent',
  ABANDONED: 'bg-danger-subtle text-danger',
};

function statusLabel(s: string) {
  if (s === 'IN_PROGRESS') return 'In Progress';
  return s.charAt(0) + s.slice(1).toLowerCase();
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export default function SessionsPage() {
  // Display only: the server scopes the list (hosts get only COMPLETED sessions they hosted and
  // ignore ?status=) and enforces ADMIN on delete.
  const isAdmin = useMe().role === 'ADMIN';
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('ALL');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [exportOpts, setExportOpts] = useState<Record<string, ExportOptions>>({});
  const [downloading, setDownloading] = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  async function load(status?: string) {
    setLoading(true);
    setError('');
    try {
      const path = status && status !== 'ALL'
        ? `/host/sessions?status=${status}`
        : '/host/sessions';
      const data = await api.get<SessionItem[]>(path);
      setSessions(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load sessions');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(statusFilter); }, [statusFilter]);

  function getOpts(sessionId: string, gameTitle: string): ExportOptions {
    return exportOpts[sessionId] ?? {
      format: 'canvas',
      title: gameTitle,
      assignmentId: '',
      sisDomain: 'wisc.edu',
      rosterOnly: true,
      perQuestion: false,
    };
  }

  function setOpt<K extends keyof ExportOptions>(sessionId: string, gameTitle: string, key: K, val: ExportOptions[K]) {
    setExportOpts((prev) => ({
      ...prev,
      [sessionId]: { ...getOpts(sessionId, gameTitle), [key]: val },
    }));
  }

  async function handleExport(session: SessionItem) {
    const opts = getOpts(session.session_id, session.game_title);
    setDownloading(session.session_id);
    setError('');
    try {
      const params = new URLSearchParams({ format: opts.format });
      if (opts.format === 'canvas') {
        const columnTitle = opts.assignmentId.trim()
          ? `${opts.title} (${opts.assignmentId.trim()})`
          : opts.title;
        if (columnTitle) params.set('title', columnTitle);
        params.set('sis_domain', opts.sisDomain);
        params.set('roster_only', String(opts.rosterOnly));
        params.set('per_question', String(opts.perQuestion));
      } else {
        params.set('per_question', String(opts.perQuestion));
      }
      await api.download(`/host/sessions/${session.session_id}/export?${params}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed');
    } finally {
      setDownloading(null);
    }
  }

  async function handleReport(sessionId: string) {
    setReportLoading(sessionId);
    setError('');
    try {
      await api.download(`/host/sessions/${sessionId}/report`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Report download failed');
    } finally {
      setReportLoading(null);
    }
  }

  async function handleDelete(sessionId: string) {
    try {
      await api.delete(`/game/sessions/${sessionId}`);
      setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
      setConfirmDelete(null);
      setExpandedId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
    }
  }

  const filters: StatusFilter[] = ['ALL', 'IN_PROGRESS', 'COMPLETED', 'LOBBY', 'ABANDONED'];

  return (
    <div className="p-8 max-w-5xl">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold text-primary">
          {isAdmin ? 'Sessions' : 'Completed sessions you hosted'}
        </h2>
        {/* Status filter tabs */}
        {isAdmin && (
          <div className="flex gap-1 bg-raised rounded-lg p-1">
            {filters.map((f) => (
              <button
                key={f}
                onClick={() => setStatusFilter(f)}
                className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                  statusFilter === f
                    ? 'bg-accent text-on-accent'
                    : 'text-secondary hover:text-primary'
                }`}
              >
                {f === 'ALL' ? 'All' : f === 'IN_PROGRESS' ? 'In Progress' : statusLabel(f)}
              </button>
            ))}
          </div>
        )}
      </div>

      {error && <p className="text-danger mb-4 text-sm">{error}</p>}

      {loading ? (
        <p className="text-secondary">Loading…</p>
      ) : sessions.length === 0 ? (
        <p className="text-secondary">No sessions found.</p>
      ) : (
        <div className="space-y-2">
          {sessions.map((s) => {
            const isExpanded = expandedId === s.session_id;
            const opts = getOpts(s.session_id, s.game_title);
            const isDeleting = confirmDelete === s.session_id;
            const isDownloading = downloading === s.session_id;
            // Downloads require COMPLETED for every role, admins included (the server's rule).
            const canDownload = s.status === 'COMPLETED';

            return (
              <Card key={s.session_id} className="overflow-hidden">
                {/* Session row */}
                <div className="flex items-center gap-4 px-5 py-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-primary">{s.game_title}</span>
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_COLORS[s.status] ?? 'bg-raised text-secondary'}`}>
                        {statusLabel(s.status)}
                      </span>
                    </div>
                    <p className="text-secondary text-sm mt-0.5">
                      {s.course_name} ({s.course_semester})
                      {s.host_display_name && <span className="text-secondary"> · {s.host_display_name}</span>}
                    </p>
                    <p className="text-secondary text-xs mt-0.5">
                      Room <span className="font-mono text-secondary">{s.room_code}</span>
                      {' · '}{s.player_count} player{s.player_count !== 1 ? 's' : ''}
                      {' · '}{fmtDate(s.created_at)}
                      {s.completed_at && <span> → {fmtDate(s.completed_at)}</span>}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {canDownload && (
                      <>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={reportLoading === s.session_id}
                          onClick={() => void handleReport(s.session_id)}
                          title="Download standalone HTML report"
                        >
                          <FileText size={13} className="mr-1" />
                          {reportLoading === s.session_id ? 'Building…' : 'Report'}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setExpandedId(isExpanded ? null : s.session_id)}
                        >
                          <Download size={13} className="mr-1" /> Export
                          {isExpanded ? <ChevronUp size={12} className="ml-1" /> : <ChevronDown size={12} className="ml-1" />}
                        </Button>
                      </>
                    )}
                    {isAdmin && (isDeleting ? (
                      <>
                        <span className="text-danger text-xs">Delete all data?</span>
                        <Button size="sm" variant="destructive" onClick={() => void handleDelete(s.session_id)}>
                          Confirm
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(null)}>
                          Cancel
                        </Button>
                      </>
                    ) : (
                      <Button size="sm" variant="destructive" onClick={() => setConfirmDelete(s.session_id)}>
                        <Trash2 size={13} />
                      </Button>
                    ))}
                  </div>
                </div>

                {/* Export options panel */}
                {isExpanded && canDownload && (
                  <div className="border-t border-subtle bg-raised px-5 py-4 space-y-4">
                    {/* Format selector */}
                    <div>
                      <p className="text-xs text-secondary mb-2 font-medium uppercase tracking-wider">Export format</p>
                      <div className="flex gap-2">
                        {(['canvas', 'raw'] as const).map((fmt) => (
                          <button
                            key={fmt}
                            onClick={() => setOpt(s.session_id, s.game_title, 'format', fmt)}
                            className={`px-4 py-2 rounded-lg text-sm font-medium border transition-colors ${
                              opts.format === fmt
                                ? 'bg-accent border-accent text-on-accent'
                                : 'border-input text-secondary hover:bg-hover hover:text-primary'
                            }`}
                          >
                            {fmt === 'canvas' ? 'Canvas Gradebook' : 'Raw CSV'}
                          </button>
                        ))}
                      </div>
                    </div>

                    {opts.format === 'canvas' && (
                      <>
                        {/* Assignment title + ID */}
                        <div className="grid grid-cols-2 gap-4">
                          <div>
                            <label className="block text-xs text-secondary mb-1">Assignment name</label>
                            <Input
                              value={opts.title}
                              onChange={(e) => setOpt(s.session_id, s.game_title, 'title', e.target.value)}
                              placeholder="Quiz title…"
                              className="text-sm"
                            />
                          </div>
                          <div>
                            <label className="block text-xs text-secondary mb-1">
                              Canvas assignment ID
                              <span className="text-secondary ml-1">(optional)</span>
                            </label>
                            <Input
                              value={opts.assignmentId}
                              onChange={(e) => setOpt(s.session_id, s.game_title, 'assignmentId', e.target.value)}
                              placeholder="e.g. 12345"
                              className="text-sm font-mono"
                            />
                          </div>
                        </div>
                        {/* Column header preview */}
                        <p className="text-secondary text-xs -mt-2">
                          Column header preview:{' '}
                          <span className="font-mono text-secondary">
                            {opts.assignmentId.trim()
                              ? `${opts.title} (${opts.assignmentId.trim()})`
                              : opts.title || '—'}
                          </span>
                        </p>
                        {/* SIS domain */}
                        <div className="max-w-xs">
                          <label className="block text-xs text-secondary mb-1">
                            SIS Login ID domain
                            <span className="text-secondary ml-1">(appended to netid)</span>
                          </label>
                          <Input
                            value={opts.sisDomain}
                            onChange={(e) => setOpt(s.session_id, s.game_title, 'sisDomain', e.target.value)}
                            placeholder="wisc.edu"
                            className="text-sm font-mono"
                          />
                          <p className="text-secondary text-xs mt-1">
                            Preview: <span className="font-mono">jsmith{opts.sisDomain ? `@${opts.sisDomain}` : ''}</span>
                          </p>
                        </div>

                        {/* Canvas options */}
                        <div className="space-y-2">
                          <label className="flex items-center gap-2 cursor-pointer select-none">
                            <input
                              type="checkbox"
                              checked={opts.rosterOnly}
                              onChange={(e) => setOpt(s.session_id, s.game_title, 'rosterOnly', e.target.checked)}
                              className="rounded border-input accent-primary"
                            />
                            <span className="text-sm text-secondary">
                              Roster-matched players only
                              <span className="text-secondary ml-1 text-xs">(skip guests and local accounts without a netid)</span>
                            </span>
                          </label>
                          <label className="flex items-center gap-2 cursor-pointer select-none">
                            <input
                              type="checkbox"
                              checked={opts.perQuestion}
                              onChange={(e) => setOpt(s.session_id, s.game_title, 'perQuestion', e.target.checked)}
                              className="rounded border-input accent-primary"
                            />
                            <span className="text-sm text-secondary">
                              Per-question breakdown
                              <span className="text-secondary ml-1 text-xs">(one column per question, otherwise total only)</span>
                            </span>
                          </label>
                        </div>
                      </>
                    )}

                    {opts.format === 'raw' && (
                      <p className="text-secondary text-xs">
                        Raw CSV includes all players, per-question scores, and totals.
                      </p>
                    )}

                    <Button
                      onClick={() => void handleExport(s)}
                      disabled={isDownloading}
                      size="sm"
                    >
                      <Download size={13} className="mr-1.5" />
                      {isDownloading ? 'Downloading…' : 'Download CSV'}
                    </Button>
                  </div>
                )}
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
