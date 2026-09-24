import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Trash2, Pencil } from 'lucide-react';
import { api } from '../lib/api';
import { Button } from '../components/ui/button';
import { Card, CardContent, CardHeader } from '../components/ui/card';
import { Input } from '../components/ui/input';

interface CourseAccess { course_id: number; role: string }
interface UserDetail {
  id: string;
  username: string | null;
  netid: string | null;
  display_name: string | null;
  email: string | null;
  role: string;
  created_at: string;
  last_login: string | null;
  course_access: CourseAccess[];
  game_access: number[];
}
interface Course { id: number; name: string; semester: string }
interface Game { id: number; title: string; course_id: number | null }

interface CourseSelection { id: number; role: 'HOST' | 'PLAYER'; checked: boolean }
interface GameSelection { id: number; checked: boolean }

export default function UserDetailPage() {
  const { userId } = useParams<{ userId: string }>();
  const navigate = useNavigate();
  const [user, setUser] = useState<UserDetail | null>(null);
  const [courses, setCourses] = useState<Course[]>([]);
  const [games, setGames] = useState<Game[]>([]);
  const [error, setError] = useState('');
  const [showCoursePanel, setShowCoursePanel] = useState(false);
  const [courseSelections, setCourseSelections] = useState<CourseSelection[]>([]);
  const [grantingCourses, setGrantingCourses] = useState(false);
  const [showGamePanel, setShowGamePanel] = useState(false);
  const [gameSelections, setGameSelections] = useState<GameSelection[]>([]);
  const [grantingGames, setGrantingGames] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editUsername, setEditUsername] = useState('');
  const [editDisplayName, setEditDisplayName] = useState('');
  const [editEmail, setEditEmail] = useState('');
  const [editPassword, setEditPassword] = useState('');
  const [editRole, setEditRole] = useState('');
  const [editSaving, setEditSaving] = useState(false);

  async function load() {
    try {
      const [u, cs, gs] = await Promise.all([
        api.get<UserDetail>(`/admin/users/${userId}`),
        api.get<Course[]>('/admin/courses'),
        api.get<Game[]>('/admin/games'),
      ]);
      setUser(u);
      setCourses(cs);
      setGames(gs);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load user');
    }
  }

  useEffect(() => { void load(); }, [userId]);

  function openCoursePanel(availableCourses: Course[]) {
    setCourseSelections(availableCourses.map((c) => ({ id: c.id, role: 'HOST', checked: false })));
    setShowCoursePanel(true);
  }

  async function grantSelectedCourses() {
    const selected = courseSelections.filter((s) => s.checked);
    if (selected.length === 0) return;
    setGrantingCourses(true);
    setError('');
    try {
      await Promise.all(
        selected.map((s) => api.post(`/admin/users/${userId}/course-access`, { course_id: s.id, role: s.role }))
      );
      setShowCoursePanel(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to grant access');
    } finally {
      setGrantingCourses(false);
    }
  }

  async function revokeCourseAccess(courseId: number, role: string) {
    // The server also removes the user's grants on the course's games when a HOST row goes.
    if (role === 'HOST' && !confirm("This also removes the user's access to this course's games.")) return;
    try {
      await api.delete(`/admin/users/${userId}/course-access/${courseId}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke access');
    }
  }

  function openGamePanel(availableGames: Game[]) {
    setGameSelections(availableGames.map((g) => ({ id: g.id, checked: false })));
    setShowGamePanel(true);
  }

  async function grantSelectedGames() {
    const selected = gameSelections.filter((s) => s.checked);
    if (selected.length === 0) return;
    setGrantingGames(true);
    setError('');
    try {
      await Promise.all(
        selected.map((s) => api.post(`/admin/users/${userId}/game-access`, { game_id: s.id }))
      );
      setShowGamePanel(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to grant game access');
    } finally {
      setGrantingGames(false);
    }
  }

  async function revokeGameAccess(gameId: number) {
    try {
      await api.delete(`/admin/users/${userId}/game-access/${gameId}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke game access');
    }
  }

  function startEdit(u: UserDetail) {
    setEditUsername(u.username ?? '');
    setEditDisplayName(u.display_name ?? '');
    setEditEmail(u.email ?? '');
    setEditPassword('');
    setEditRole(u.role);
    setEditing(true);
  }

  async function saveEdit() {
    if (!user) return;
    setEditSaving(true);
    setError('');
    try {
      const body: Record<string, string> = {};
      if (user.username !== null && editUsername !== user.username) body.username = editUsername;
      if (editDisplayName !== (user.display_name ?? '')) body.display_name = editDisplayName;
      if (editEmail !== (user.email ?? '')) body.email = editEmail;
      if (editPassword) body.password = editPassword;
      if (editRole !== user.role) body.role = editRole;
      await api.put(`/admin/users/${userId}`, body);
      setEditing(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update user');
    } finally {
      setEditSaving(false);
    }
  }

  async function deleteUser() {
    if (!confirm('Delete this user? This cannot be undone.')) return;
    try {
      await api.delete(`/admin/users/${userId}`);
      navigate('/users');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete user');
    }
  }

  if (!user) return <div className="p-8 text-slate-400">{error || 'Loading\u2026'}</div>;

  const courseMap = Object.fromEntries(courses.map((c) => [c.id, c]));
  const gameMap = Object.fromEntries(games.map((g) => [g.id, g]));

  const grantedCourseIds = new Set(user.course_access.map((ca) => ca.course_id));
  const grantedGameIds = new Set(user.game_access);
  const availableCourses = courses.filter((c) => !grantedCourseIds.has(c.id));
  // A game can be granted only within a course the user HOSTs (the server returns 409 otherwise),
  // so unassigned games and games of other courses are never offered.
  const hostCourseIds = new Set(
    user.course_access.filter((ca) => ca.role === 'HOST').map((ca) => ca.course_id),
  );
  const availableGames = games.filter(
    (g) => g.course_id !== null && hostCourseIds.has(g.course_id) && !grantedGameIds.has(g.id),
  );

  const selectedCourseCount = courseSelections.filter((s) => s.checked).length;
  const selectedGameCount = gameSelections.filter((s) => s.checked).length;

  return (
    <div className="p-8 max-w-3xl space-y-6">
      <button
        onClick={() => navigate('/users')}
        className="flex items-center gap-2 text-slate-400 hover:text-slate-100 text-sm"
      >
        <ArrowLeft size={14} /> Back to Users
      </button>

      {error && <p className="text-red-400 text-sm">{error}</p>}

      <Card>
        <CardHeader>
          <div className="flex items-start justify-between">
            <div>
              <h2 className="text-xl font-bold text-slate-100">{user.display_name ?? user.username ?? user.netid}</h2>
              <p className="text-slate-400 text-sm mt-0.5">
                {user.username && <span className="mr-3">@{user.username}</span>}
                {user.netid && <span className="mr-3">netid: {user.netid}</span>}
                <span className="px-2 py-0.5 rounded-full text-xs bg-slate-700 text-slate-300">{user.role}</span>
              </p>
            </div>
            <div className="flex gap-2">
              {!editing && (
                <Button variant="outline" size="sm" onClick={() => startEdit(user)}>
                  <Pencil size={14} className="mr-1" /> Edit
                </Button>
              )}
              {user.role !== 'ADMIN' && (
                <Button variant="destructive" size="sm" onClick={() => void deleteUser()}>
                  <Trash2 size={14} className="mr-1" /> Delete
                </Button>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {editing ? (
            <div className="space-y-3 pt-1">
              {user.username !== null && (
                <div>
                  <label className="block text-xs text-slate-400 mb-1">Username</label>
                  <Input
                    value={editUsername}
                    onChange={(e) => setEditUsername(e.target.value)}
                    placeholder="username"
                    className="text-sm"
                  />
                </div>
              )}
              <div>
                <label className="block text-xs text-slate-400 mb-1">Display name</label>
                <Input
                  value={editDisplayName}
                  onChange={(e) => setEditDisplayName(e.target.value)}
                  placeholder="Display name"
                  className="text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-slate-400 mb-1">Email</label>
                <Input
                  type="email"
                  value={editEmail}
                  onChange={(e) => setEditEmail(e.target.value)}
                  placeholder="email@example.com"
                  className="text-sm"
                />
              </div>
              {user.username !== null && (
                <div>
                  <label className="block text-xs text-slate-400 mb-1">New password <span className="text-slate-500">(leave blank to keep current)</span></label>
                  <Input
                    type="password"
                    value={editPassword}
                    onChange={(e) => setEditPassword(e.target.value)}
                    placeholder="New password"
                    className="text-sm"
                  />
                </div>
              )}
              <div>
                <label className="block text-xs text-slate-400 mb-1">Role</label>
                <select
                  className="rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-slate-100 text-sm"
                  value={editRole}
                  onChange={(e) => setEditRole(e.target.value)}
                >
                  <option value="USER">USER</option>
                  <option value="ADMIN">ADMIN</option>
                </select>
              </div>
              <div className="flex gap-2 pt-1">
                <Button size="sm" onClick={() => void saveEdit()} disabled={editSaving}>
                  {editSaving ? 'Saving\u2026' : 'Save'}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
              </div>
            </div>
          ) : (
            <div className="space-y-1">
              {user.email && <p className="text-slate-400 text-xs">{user.email}</p>}
              {user.netid && <p className="text-slate-500 text-xs">netid: {user.netid} (OAuth2 — not editable)</p>}
              <p className="text-slate-500 text-xs">
                Created {new Date(user.created_at).toLocaleDateString()}
                {user.last_login && ` \u00b7 Last login ${new Date(user.last_login).toLocaleDateString()}`}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Course access */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-slate-100">Course Access</h3>
            {availableCourses.length > 0 && !showCoursePanel && (
              <Button size="sm" variant="outline" onClick={() => openCoursePanel(availableCourses)}>
                Add Courses
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {user.course_access.length === 0 && !showCoursePanel && (
            <p className="text-slate-500 text-sm">No course access granted.</p>
          )}
          {user.course_access.map((ca) => {
            const course = courseMap[ca.course_id];
            return (
              <div key={ca.course_id} className="flex items-center justify-between">
                <span className="text-slate-200 text-sm">
                  {course ? `${course.name} (${course.semester})` : `Course ${ca.course_id}`}
                  <span className="ml-2 text-xs text-slate-400">{ca.role}</span>
                </span>
                <Button variant="ghost" size="sm" onClick={() => void revokeCourseAccess(ca.course_id, ca.role)}>
                  <Trash2 size={12} />
                </Button>
              </div>
            );
          })}

          {showCoursePanel && (
            <div className="rounded-lg border border-slate-600 bg-slate-800/50 p-3 space-y-1">
              <div className="flex items-center gap-2 pb-1 mb-1 border-b border-slate-700">
                <input
                  type="checkbox"
                  className="rounded"
                  checked={courseSelections.every((s) => s.checked)}
                  onChange={(e) =>
                    setCourseSelections((prev) => prev.map((s) => ({ ...s, checked: e.target.checked })))
                  }
                />
                <span className="text-xs text-slate-400 flex-1">Select all</span>
                <span className="text-xs text-slate-500 w-20 text-center">Role</span>
              </div>
              {courseSelections.map((sel, i) => {
                const course = courseMap[sel.id];
                return (
                  <div key={sel.id} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      className="rounded"
                      checked={sel.checked}
                      onChange={(e) =>
                        setCourseSelections((prev) =>
                          prev.map((s, j) => j === i ? { ...s, checked: e.target.checked } : s)
                        )
                      }
                    />
                    <span className="flex-1 text-sm text-slate-200">
                      {course ? `${course.name} (${course.semester})` : `Course ${sel.id}`}
                    </span>
                    <div className="flex rounded overflow-hidden border border-slate-600 text-xs">
                      <button
                        className={`px-2 py-1 ${sel.role === 'HOST' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-slate-200'}`}
                        onClick={() => setCourseSelections((prev) => prev.map((s, j) => j === i ? { ...s, role: 'HOST' } : s))}
                      >HOST</button>
                      <button
                        className={`px-2 py-1 ${sel.role === 'PLAYER' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-slate-200'}`}
                        onClick={() => setCourseSelections((prev) => prev.map((s, j) => j === i ? { ...s, role: 'PLAYER' } : s))}
                      >PLAYER</button>
                    </div>
                  </div>
                );
              })}
              <div className="flex gap-2 pt-2">
                <Button
                  size="sm"
                  onClick={() => void grantSelectedCourses()}
                  disabled={selectedCourseCount === 0 || grantingCourses}
                >
                  {grantingCourses ? 'Granting\u2026' : `Grant Selected (${selectedCourseCount})`}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setShowCoursePanel(false)}>Cancel</Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Game access */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-slate-100">Game Access</h3>
            {hostCourseIds.size === 0 ? (
              <p className="text-slate-500 text-xs text-right max-w-xs">
                Grant HOST on a course first — games can only be granted within courses the user hosts.
              </p>
            ) : availableGames.length > 0 && !showGamePanel && (
              <Button size="sm" variant="outline" onClick={() => openGamePanel(availableGames)}>
                Add Games
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {user.game_access.length === 0 && !showGamePanel && (
            <p className="text-slate-500 text-sm">No game access granted.</p>
          )}
          {user.game_access.map((gid) => {
            const game = gameMap[gid];
            return (
              <div key={gid} className="flex items-center justify-between">
                <span className="text-slate-200 text-sm">{game ? game.title : `Game ${gid}`}</span>
                <Button variant="ghost" size="sm" onClick={() => void revokeGameAccess(gid)}>
                  <Trash2 size={12} />
                </Button>
              </div>
            );
          })}

          {showGamePanel && (
            <div className="rounded-lg border border-slate-600 bg-slate-800/50 p-3 space-y-1">
              <div className="flex items-center gap-2 pb-1 mb-1 border-b border-slate-700">
                <input
                  type="checkbox"
                  className="rounded"
                  checked={gameSelections.every((s) => s.checked)}
                  onChange={(e) =>
                    setGameSelections((prev) => prev.map((s) => ({ ...s, checked: e.target.checked })))
                  }
                />
                <span className="text-xs text-slate-400">Select all</span>
              </div>
              {gameSelections.map((sel, i) => {
                const game = gameMap[sel.id];
                const course = game && game.course_id !== null ? courseMap[game.course_id] : undefined;
                return (
                  <div key={sel.id} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      className="rounded"
                      checked={sel.checked}
                      onChange={(e) =>
                        setGameSelections((prev) =>
                          prev.map((s, j) => j === i ? { ...s, checked: e.target.checked } : s)
                        )
                      }
                    />
                    <span className="text-sm text-slate-200">
                      {game ? game.title : `Game ${sel.id}`}
                      {course && ` (${course.name}, ${course.semester})`}
                    </span>
                  </div>
                );
              })}
              <div className="flex gap-2 pt-2">
                <Button
                  size="sm"
                  onClick={() => void grantSelectedGames()}
                  disabled={selectedGameCount === 0 || grantingGames}
                >
                  {grantingGames ? 'Granting\u2026' : `Grant Selected (${selectedGameCount})`}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setShowGamePanel(false)}>Cancel</Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
