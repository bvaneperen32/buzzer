import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, List, Upload, Trash2, Pencil } from 'lucide-react';
import { api } from '../lib/api';
import { useMe } from '../lib/me';
import { Button } from '../components/ui/button';
import { Card, CardContent, CardHeader } from '../components/ui/card';
import { Input } from '../components/ui/input';

interface Course { id: number; name: string; semester: string }

interface Game {
  id: number;
  title: string;
  description: string;
  max_players: number;
  course_id: number | null;
  course_name: string | null;
  course_semester: string | null;
}

interface GameGroup { key: string; label: string; games: Game[] }

const selectClass =
  'w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500';

/**
 * Group by course id, not name: same-name courses in different semesters stay separate. Groups
 * keep the server's order (course name, then semester); "Unassigned" goes last. Only admins are
 * ever sent unassigned games, so the group needs no role check.
 */
function groupByCourse(games: Game[]): GameGroup[] {
  const groups = new Map<number, GameGroup>();
  const unassigned: Game[] = [];
  for (const g of games) {
    if (g.course_id === null) {
      unassigned.push(g);
      continue;
    }
    let group = groups.get(g.course_id);
    if (!group) {
      group = { key: String(g.course_id), label: `${g.course_name} (${g.course_semester})`, games: [] };
      groups.set(g.course_id, group);
    }
    group.games.push(g);
  }
  const result = [...groups.values()];
  if (unassigned.length) result.push({ key: 'unassigned', label: 'Unassigned', games: unassigned });
  return result;
}

export default function GamesPage() {
  const isAdmin = useMe().role === 'ADMIN';
  const [games, setGames] = useState<Game[]>([]);
  const [courses, setCourses] = useState<Course[]>([]);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [courseId, setCourseId] = useState<number | ''>('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [maxPlayers, setMaxPlayers] = useState('150');
  const [saving, setSaving] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [importCourseId, setImportCourseId] = useState<number | ''>('');
  const [importing, setImporting] = useState(false);
  const [editingGame, setEditingGame] = useState<Game | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editMaxPlayers, setEditMaxPlayers] = useState('');
  const [editSaving, setEditSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const noCourses = loaded && courses.length === 0;

  async function load() {
    try {
      const [g, c] = await Promise.all([
        api.get<Game[]>('/game/my-games'),
        api.get<Course[]>('/game/my-courses'),
      ]);
      setGames(g);
      setCourses(c);
      // Pre-select the course when there's only one to choose.
      if (c.length === 1) {
        setCourseId(c[0].id);
        setImportCourseId(c[0].id);
      }
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load games');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError('');
    try {
      const game = await api.post<{ id: number }>('/host/games', {
        title,
        description,
        max_players: Number(maxPlayers),
        course_id: courseId,
      });
      navigate(`/games/${game.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create game');
    } finally {
      setSaving(false);
    }
  }

  function startEdit(g: Game) {
    setEditingGame(g);
    setEditTitle(g.title);
    setEditDescription(g.description);
    setEditMaxPlayers(String(g.max_players));
  }

  async function handleEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editingGame) return;
    setEditSaving(true);
    setError('');
    try {
      await api.put(`/host/games/${editingGame.id}`, {
        title: editTitle,
        description: editDescription,
        max_players: Number(editMaxPlayers),
      });
      setEditingGame(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update game');
    } finally {
      setEditSaving(false);
    }
  }

  async function handleImport(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setImporting(true);
    setError('');
    try {
      const form = new FormData();
      form.append('file', file);
      form.append('course_id', String(importCourseId));
      const result = await api.postForm<{ game_id: number }>('/host/games/import', form);
      navigate(`/games/${result.game_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Import failed');
    } finally {
      setImporting(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }

  // Admin only — the endpoint enforces ADMIN server-side.
  async function moveGame(id: number, newCourseId: number) {
    if (!confirm(
      "Moving this game changes which hosts can use it — hosts who don't host the new course lose "
      + 'their access to it. Past sessions keep their original course.',
    )) return;
    setError('');
    try {
      await api.put(`/admin/games/${id}`, { course_id: newCourseId });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to move game');
    }
  }

  // Admin only — the endpoint enforces ADMIN server-side.
  async function deleteGame(id: number) {
    setError('');
    try {
      await api.delete(`/admin/games/${id}`);
      setConfirmDelete(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete game');
    }
  }

  const courseOptions = courses.map((c) => (
    <option key={c.id} value={c.id}>{c.name} ({c.semester})</option>
  ));

  return (
    <div className="p-8 max-w-4xl">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold text-slate-100">Games</h2>
        {!noCourses && (
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => setShowImport(!showImport)}>
              <Upload size={14} className="mr-1" /> Import JSON
            </Button>
            <Button size="sm" onClick={() => setShowForm(!showForm)}>
              <Plus size={16} className="mr-1" /> New Game
            </Button>
          </div>
        )}
      </div>

      {error && <p className="text-red-400 mb-4 text-sm">{error}</p>}

      {showImport && !noCourses && (
        <Card className="mb-6">
          <CardHeader><h3 className="text-lg font-semibold text-slate-100">Import Game</h3></CardHeader>
          <CardContent className="space-y-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">Course</label>
              <select
                value={importCourseId}
                onChange={(e) => setImportCourseId(e.target.value === '' ? '' : Number(e.target.value))}
                className={selectClass}
              >
                <option value="">Select a course…</option>
                {courseOptions}
              </select>
            </div>
            <input ref={fileRef} type="file" accept=".json" className="hidden" onChange={handleImport} />
            <div className="flex gap-3">
              <Button onClick={() => fileRef.current?.click()} disabled={!importCourseId || importing}>
                <Upload size={14} className="mr-1" />
                {importing ? 'Importing…' : 'Choose file'}
              </Button>
              <Button variant="ghost" onClick={() => setShowImport(false)}>Cancel</Button>
            </div>
          </CardContent>
        </Card>
      )}

      {showForm && !noCourses && (
        <Card className="mb-6">
          <CardHeader><h3 className="text-lg font-semibold text-slate-100">Create Game</h3></CardHeader>
          <CardContent>
            <form onSubmit={handleCreate} className="space-y-3">
              <div>
                <label className="block text-xs text-slate-400 mb-1">Course</label>
                <select
                  value={courseId}
                  onChange={(e) => setCourseId(e.target.value === '' ? '' : Number(e.target.value))}
                  className={selectClass}
                  required
                >
                  <option value="">Select a course…</option>
                  {courseOptions}
                </select>
              </div>
              <Input placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} required />
              <textarea
                placeholder="Description (optional)"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent resize-none"
                rows={3}
              />
              <div>
                <label className="block text-xs text-slate-400 mb-1">Max players</label>
                <Input
                  type="number"
                  value={maxPlayers}
                  onChange={(e) => setMaxPlayers(e.target.value)}
                  min="1"
                  max="500"
                />
              </div>
              <div className="flex gap-3">
                <Button type="submit" disabled={saving}>{saving ? 'Creating…' : 'Create'}</Button>
                <Button type="button" variant="ghost" onClick={() => setShowForm(false)}>Cancel</Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      {editingGame && (
        <Card className="mb-6">
          <CardHeader><h3 className="text-lg font-semibold text-slate-100">Edit Game</h3></CardHeader>
          <CardContent>
            <form onSubmit={handleEdit} className="space-y-3">
              <Input placeholder="Title" value={editTitle} onChange={(e) => setEditTitle(e.target.value)} required />
              <textarea
                placeholder="Description (optional)"
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                className="w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent resize-none"
                rows={3}
              />
              <div>
                <label className="block text-xs text-slate-400 mb-1">Max players</label>
                <Input
                  type="number"
                  value={editMaxPlayers}
                  onChange={(e) => setEditMaxPlayers(e.target.value)}
                  min="1"
                  max="500"
                />
              </div>
              <div className="flex gap-3">
                <Button type="submit" disabled={editSaving}>{editSaving ? 'Saving…' : 'Save'}</Button>
                <Button type="button" variant="ghost" onClick={() => setEditingGame(null)}>Cancel</Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      {loading ? (
        <p className="text-slate-400">Loading…</p>
      ) : games.length === 0 ? (
        noCourses ? (
          <p className="text-slate-400">You don't host any courses yet — ask an administrator for HOST access.</p>
        ) : (
          <p className="text-slate-400">No games yet.</p>
        )
      ) : (
        <div className="space-y-8">
          {groupByCourse(games).map((group) => (
            <section key={group.key}>
              <h3 className="text-lg font-semibold text-slate-200 mb-3">{group.label}</h3>
              <div className="space-y-3">
                {group.games.map((g) => (
                  <Card key={g.id} className="px-6 py-4">
                    <div className="flex items-center justify-between gap-4">
                      <div className="min-w-0">
                        <p className="font-semibold text-slate-100">{g.title}</p>
                        {g.description && <p className="text-slate-400 text-sm mt-0.5 line-clamp-1">{g.description}</p>}
                        <p className="text-slate-500 text-xs mt-0.5">Max {g.max_players} players</p>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {isAdmin && (
                          <select
                            value={g.course_id ?? ''}
                            onChange={(e) => {
                              const next = Number(e.target.value);
                              if (next !== g.course_id) void moveGame(g.id, next);
                            }}
                            className="rounded-lg border border-slate-600 bg-slate-800 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                          >
                            {/* A course can be set or changed, never cleared. */}
                            {g.course_id === null && <option value="" disabled>Assign a course…</option>}
                            {courseOptions}
                          </select>
                        )}
                        <Button variant="outline" size="sm" onClick={() => startEdit(g)}>
                          <Pencil size={14} />
                        </Button>
                        <Button variant="outline" size="sm" onClick={() => navigate(`/games/${g.id}`)}>
                          <List size={14} className="mr-1" /> Questions
                        </Button>
                        {isAdmin && confirmDelete !== g.id && (
                          <Button variant="destructive" size="sm" onClick={() => setConfirmDelete(g.id)}>
                            <Trash2 size={14} />
                          </Button>
                        )}
                      </div>
                    </div>
                    {isAdmin && confirmDelete === g.id && (
                      <div className="flex items-center justify-end gap-2 mt-3">
                        <span className="text-red-400 text-xs">
                          This erases every session and score ever recorded for this game.
                        </span>
                        <Button size="sm" variant="destructive" onClick={() => void deleteGame(g.id)}>
                          Confirm
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(null)}>
                          Cancel
                        </Button>
                      </div>
                    )}
                  </Card>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
