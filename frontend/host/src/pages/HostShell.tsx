import { useEffect, useState } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { PlayCircle, Gamepad2, Users, History, LogOut, Loader2 } from 'lucide-react';
import { api, ApiError } from '../lib/api';
import { Me, MeContext } from '../lib/me';
import { Button } from '../components/ui/button';
import { Card, CardContent, CardHeader } from '../components/ui/card';

/**
 * Layout route for every non-game page: loads the current user, then renders the sidebar and
 * the page. Game screens sit outside it so they stay full-screen.
 */
export default function HostShell() {
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
      // Only an expired or invalid token signs the user out; a network error or 5xx (e.g. the
      // backend restarting under --reload) offers a retry instead.
      if (err instanceof ApiError && err.status === 401) {
        signOut();
        return;
      }
      setError(err instanceof Error ? err.message : 'Failed to load your account');
    }
  }

  useEffect(() => { void load(); }, []);

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader>
            <h1 className="text-lg font-semibold text-primary">Couldn't load your account.</h1>
            <p className="text-danger text-sm mt-1">{error}</p>
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
        <Loader2 size={24} className="animate-spin text-secondary" />
      </div>
    );
  }

  // Production shares one token across all three apps, so a guest joining from this machine
  // replaces the host's token.
  if (me.role === 'GUEST') {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <Card className="w-full max-w-md">
          <CardHeader>
            <p className="text-primary">
              You're signed in as a guest. Sign in with a host account to use Buzzer Host.
            </p>
          </CardHeader>
          <CardContent>
            <Button onClick={signOut}>Sign out</Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${
      isActive
        ? 'bg-selected text-primary'
        : 'text-secondary hover:bg-hover hover:text-primary'
    }`;

  return (
    <MeContext.Provider value={me}>
      <div className="flex min-h-screen">
        {/* Sticky and viewport-high so Sign out stays in view however long the page is */}
        <aside className="w-56 shrink-0 sticky top-0 h-screen bg-surface border-r border-subtle flex flex-col">
          <div className="px-6 py-5 border-b border-subtle">
            <h1 className="text-lg font-bold text-primary">Buzzer Host</h1>
          </div>
          <nav className="flex-1 overflow-y-auto p-3 space-y-1">
            <NavLink to="/home" className={linkClass}>
              <PlayCircle size={16} /> Play
            </NavLink>
            <NavLink to="/games" className={linkClass}>
              <Gamepad2 size={16} /> Games
            </NavLink>
            <NavLink to="/rosters" className={linkClass}>
              <Users size={16} /> Rosters
            </NavLink>
            <NavLink to="/sessions" className={linkClass}>
              <History size={16} /> Sessions
            </NavLink>
          </nav>
          <div className="p-3 border-t border-subtle">
            <button
              onClick={signOut}
              className="flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium text-secondary hover:bg-hover hover:text-primary w-full transition-colors"
            >
              <LogOut size={16} /> Sign out
            </button>
          </div>
        </aside>

        <main className="flex-1 overflow-auto pt-6">
          <Outlet />
        </main>
      </div>
    </MeContext.Provider>
  );
}
