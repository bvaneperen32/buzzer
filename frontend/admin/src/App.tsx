import { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Outlet, Route, Routes, useNavigate, NavLink } from 'react-router-dom';
import { BookOpen, Users, UserX, LogOut, ExternalLink, Loader2 } from 'lucide-react';
import { api, ApiError } from './lib/api';
import { hostAppUrl } from './lib/hostApp';
import { Button } from './components/ui/button';
import { Card, CardContent, CardHeader } from './components/ui/card';
import LoginPage from './pages/LoginPage';
import CoursesPage from './pages/CoursesPage';
import UsersPage from './pages/UsersPage';
import UserDetailPage from './pages/UserDetailPage';
import GuestsPage from './pages/GuestsPage';

interface Me {
  id: string;
  username: string | null;
  netid: string | null;
  display_name: string | null;
  role: string;
}

/**
 * Checks the role with the server on entry (never the token's claim or a stored copy). This
 * is display only: every /api/admin endpoint enforces ADMIN itself.
 */
function RequireAdmin() {
  const hasToken = !!localStorage.getItem('token');
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState('');
  const navigate = useNavigate();

  function signOut() {
    localStorage.removeItem('token');
    navigate('/login');
  }

  async function load() {
    setError('');
    try {
      setMe(await api.get<Me>('/auth/me'));
    } catch (err) {
      // Only an expired or invalid token signs the user out; anything else offers a retry.
      if (err instanceof ApiError && err.status === 401) {
        signOut();
        return;
      }
      setError(err instanceof Error ? err.message : 'Failed to load your account');
    }
  }

  useEffect(() => { if (hasToken) void load(); }, []);

  if (!hasToken) return <Navigate to="/login" replace />;

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader>
            <h1 className="text-lg font-semibold text-slate-100">Couldn't load your account.</h1>
            <p className="text-red-400 text-sm mt-1">{error}</p>
          </CardHeader>
          <CardContent>
            <Button onClick={() => void load()}>Retry</Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!me) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 size={24} className="animate-spin text-slate-400" />
      </div>
    );
  }

  if (me.role !== 'ADMIN') {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader>
            <p className="text-slate-100">This console is for administrators.</p>
          </CardHeader>
          <CardContent className="flex gap-3">
            <a
              href={hostAppUrl('/home')}
              className="inline-flex items-center justify-center rounded-lg font-semibold transition-colors px-4 py-2 text-base bg-indigo-600 text-white hover:bg-indigo-500 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-slate-900 focus:ring-indigo-500"
            >
              Open Buzzer Host
            </a>
            <Button variant="ghost" onClick={signOut}>Sign out</Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return <Outlet />;
}

function AdminLayout() {
  const navigate = useNavigate();

  function logout() {
    localStorage.removeItem('token');
    navigate('/login');
  }

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${
      isActive
        ? 'bg-indigo-600 text-white'
        : 'text-slate-300 hover:bg-slate-700 hover:text-slate-100'
    }`;

  // Host work (games, questions, rosters, session downloads) lives in the host app. These open
  // it in a new tab so the console stays open.
  const hostTools = [
    { label: 'Play', path: '/home' },
    { label: 'Games', path: '/games' },
    { label: 'Rosters', path: '/rosters' },
    { label: 'Sessions', path: '/sessions' },
  ];

  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="w-56 shrink-0 bg-slate-900 border-r border-slate-700 flex flex-col">
        <div className="px-6 py-5 border-b border-slate-700">
          <h1 className="text-lg font-bold text-white">Buzzer Admin</h1>
        </div>
        <nav className="flex-1 p-3 space-y-1">
          <NavLink to="/users" className={linkClass}>
            <Users size={16} /> Users
          </NavLink>
          <NavLink to="/courses" className={linkClass}>
            <BookOpen size={16} /> Courses
          </NavLink>
          <NavLink to="/guests" className={linkClass}>
            <UserX size={16} /> Guests
          </NavLink>

          <div className="pt-3 mt-3 border-t border-slate-700">
            <p className="px-4 pb-1 text-xs font-medium uppercase tracking-wider text-slate-500">Host tools</p>
            {hostTools.map(({ label, path }) => (
              <a
                key={path}
                href={hostAppUrl(path)}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center justify-between px-4 py-2 rounded-lg text-xs text-slate-400 hover:bg-slate-800 hover:text-slate-200 transition-colors"
              >
                <span>{label}</span>
                <ExternalLink size={12} />
              </a>
            ))}
          </div>
        </nav>
        <div className="p-3 border-t border-slate-700">
          <button
            onClick={logout}
            className="flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-slate-700 hover:text-slate-100 w-full transition-colors"
          >
            <LogOut size={16} /> Logout
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAdmin />}>
          <Route element={<AdminLayout />}>
            <Route path="/users" element={<UsersPage />} />
            <Route path="/users/:userId" element={<UserDetailPage />} />
            <Route path="/courses" element={<CoursesPage />} />
            <Route path="/guests" element={<GuestsPage />} />
          </Route>
        </Route>
        {/* Must agree with LoginPage's post-login redirect. */}
        <Route path="*" element={<Navigate to="/users" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
