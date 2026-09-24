import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { Button } from '../components/ui/button';
import { Card, CardContent, CardHeader } from '../components/ui/card';

interface Course { id: number; name: string; semester: string }
interface Game { id: number; title: string; course_id: number | null }
interface ActiveSession {
  session_id: string;
  room_code: string;
  status: string;
  game_title: string;
  course_name: string;
  course_semester: string;
}

export default function HomePage() {
  const [courses, setCourses] = useState<Course[]>([]);
  const [games, setGames] = useState<Game[]>([]);
  const [activeSessions, setActiveSessions] = useState<ActiveSession[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [courseId, setCourseId] = useState<number | ''>('');
  const [gameId, setGameId] = useState<number | ''>('');
  const [allowGuests, setAllowGuests] = useState(true);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const noCourses = loaded && courses.length === 0;
  // Unassigned games (course_id null) never match a course, so they never appear here.
  const courseGames = games.filter(g => g.course_id === courseId);

  /** Select a course and reset the quiz, auto-selecting it when the course has exactly one. */
  function pickCourse(id: number | '', gameList: Game[]) {
    setCourseId(id);
    const options = gameList.filter(g => g.course_id === id);
    setGameId(options.length === 1 ? options[0].id : '');
  }

  useEffect(() => {
    Promise.all([
      api.get<Course[]>('/game/my-courses'),
      api.get<Game[]>('/game/my-games'),
      api.get<ActiveSession[]>('/game/my-active-sessions'),
    ]).then(([c, g, s]) => {
      setCourses(c);
      setGames(g);
      setActiveSessions(s);
      if (c.length === 1) pickCourse(c[0].id, g);
      setLoaded(true);
    }).catch(() => setError('Failed to load courses or games. Are you still logged in?'));
  }, []);

  async function deleteSession(sessionId: string) {
    try {
      await api.delete(`/game/sessions/${sessionId}`);
      setActiveSessions(prev => prev.filter(s => s.session_id !== sessionId));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete session');
    } finally {
      setConfirmDelete(null);
    }
  }

  async function createRoom() {
    if (!courseId || !gameId) return;
    setError('');
    setLoading(true);
    try {
      const data = await api.post<{ room_code: string }>('/game/rooms', {
        course_id: courseId,
        game_id: gameId,
        allow_guests: allowGuests,
      });
      navigate(`/game/${data.room_code}/lobby`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create room');
      setLoading(false);
    }
  }

  return (
    <div className="p-8 flex flex-col gap-4">
      {activeSessions.length > 0 && (
        <Card className="w-full max-w-lg border-amber-500/40">
          <CardHeader>
            <h2 className="text-lg font-semibold text-amber-400">Active Sessions</h2>
            <p className="text-slate-400 text-sm">You have running games — rejoin one or create a new room below.</p>
          </CardHeader>
          <CardContent className="space-y-2">
            {activeSessions.map(s => (
              <div
                key={s.session_id}
                className="flex items-center justify-between rounded-lg bg-slate-800 border border-slate-700 px-4 py-3"
              >
                <div>
                  <p className="text-slate-100 font-medium">{s.game_title}</p>
                  <p className="text-slate-400 text-sm">{s.course_name} ({s.course_semester})</p>
                  <p className="text-slate-500 text-xs mt-0.5">
                    Room <span className="font-mono text-slate-300">{s.room_code}</span>
                    {' · '}
                    <span className={s.status === 'IN_PROGRESS' ? 'text-green-400' : 'text-amber-400'}>
                      {s.status === 'IN_PROGRESS' ? 'In Progress' : 'Lobby'}
                    </span>
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {confirmDelete === s.session_id ? (
                    <>
                      <span className="text-red-400 text-xs">Delete all data?</span>
                      <Button size="sm" variant="destructive" onClick={() => deleteSession(s.session_id)}>
                        Confirm
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(null)}>
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button size="sm" variant="outline" onClick={() => navigate(`/game/${s.room_code}/lobby`)}>
                        Rejoin
                      </Button>
                      <Button size="sm" variant="destructive" onClick={() => setConfirmDelete(s.session_id)}>
                        Delete
                      </Button>
                    </>
                  )}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card className="w-full max-w-lg">
        <CardHeader>
          <h1 className="text-2xl font-bold text-slate-100">Create Game Room</h1>
          <p className="text-slate-400 text-sm mt-1">Select a course and quiz to begin</p>
        </CardHeader>
        <CardContent className="space-y-4">
          {noCourses ? (
            <p className="text-slate-400 text-sm">
              You don't host any courses yet — ask an administrator for HOST access.
            </p>
          ) : (
            <>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Course</label>
                <select
                  value={courseId}
                  onChange={e => pickCourse(e.target.value === '' ? '' : Number(e.target.value), games)}
                  className="w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option value="">Select a course…</option>
                  {courses.map(c => (
                    <option key={c.id} value={c.id}>{c.name} ({c.semester})</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Quiz</label>
                <select
                  value={gameId}
                  onChange={e => setGameId(e.target.value === '' ? '' : Number(e.target.value))}
                  disabled={!courseId}
                  className="w-full rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50"
                >
                  <option value="">Select a quiz…</option>
                  {courseGames.map(g => (
                    <option key={g.id} value={g.id}>{g.title}</option>
                  ))}
                </select>
                {courseId !== '' && courseGames.length === 0 && (
                  <p className="text-slate-400 text-sm mt-2">
                    No games in this course yet — create or import one on the{' '}
                    <Link to="/games" className="text-indigo-400 underline">Games page</Link>.
                  </p>
                )}
              </div>
              <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={allowGuests}
                  onChange={e => setAllowGuests(e.target.checked)}
                  className="rounded border-slate-600 bg-slate-800 accent-indigo-500"
                />
                Allow guests
              </label>
            </>
          )}
          {error && <p className="text-red-400 text-sm">{error}</p>}
          {!noCourses && (
            <Button
              className="w-full"
              size="lg"
              onClick={createRoom}
              disabled={!courseId || !gameId || loading}
            >
              {loading ? 'Creating room…' : 'Create Room'}
            </Button>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
