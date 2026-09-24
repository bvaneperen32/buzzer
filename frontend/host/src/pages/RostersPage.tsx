import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';
import { api } from '../lib/api';
import { Card } from '../components/ui/card';

interface Course { id: number; name: string; semester: string }

/** Courses the user HOSTs (every course, for an admin), each linking to its roster. */
export default function RostersPage() {
  const [courses, setCourses] = useState<Course[]>([]);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');

  async function load() {
    try {
      setCourses(await api.get<Course[]>('/game/my-courses'));
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load courses');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  return (
    <div className="p-8 max-w-4xl">
      <h2 className="text-2xl font-bold text-slate-100 mb-6">Rosters</h2>

      {error && <p className="text-red-400 mb-4 text-sm">{error}</p>}

      {loading ? (
        <p className="text-slate-400">Loading…</p>
      ) : loaded && courses.length === 0 ? (
        <p className="text-slate-400">You don't host any courses yet — ask an administrator for HOST access.</p>
      ) : (
        <div className="space-y-3">
          {courses.map((c) => (
            <Link key={c.id} to={`/rosters/${c.id}`} className="block">
              <Card className="flex items-center justify-between px-6 py-4 hover:bg-slate-700/60 transition-colors">
                <span className="font-semibold text-slate-100">{c.name} ({c.semester})</span>
                <ChevronRight size={16} className="text-slate-400" />
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
