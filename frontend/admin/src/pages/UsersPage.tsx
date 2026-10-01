import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus } from 'lucide-react';
import { api } from '../lib/api';
import { Button } from '../components/ui/button';
import { Card, CardContent, CardHeader } from '../components/ui/card';
import { Input } from '../components/ui/input';

interface UserItem {
  id: string;
  username: string | null;
  netid: string | null;
  display_name: string | null;
  email: string | null;
  role: string;
  created_at: string;
  last_login: string | null;
}

export default function UsersPage() {
  const [users, setUsers] = useState<UserItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [saving, setSaving] = useState(false);
  const navigate = useNavigate();

  async function load() {
    try {
      const data = await api.get<UserItem[]>('/admin/users');
      setUsers(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load users');
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
      await api.post('/admin/users', {
        username,
        display_name: displayName,
        password,
        email: email || undefined,
      });
      setUsername(''); setDisplayName(''); setPassword(''); setEmail('');
      setShowForm(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create user');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="p-8 max-w-4xl">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold text-primary">Users</h2>
        <Button onClick={() => setShowForm(!showForm)} size="sm">
          <Plus size={16} className="mr-1" /> New User
        </Button>
      </div>

      {error && <p className="text-danger mb-4 text-sm">{error}</p>}

      {showForm && (
        <Card className="mb-6">
          <CardHeader><h3 className="text-lg font-semibold text-primary">Create Local Account</h3></CardHeader>
          <CardContent>
            <form onSubmit={handleCreate} className="grid grid-cols-2 gap-3">
              <Input placeholder="Username" value={username} onChange={(e) => setUsername(e.target.value)} required />
              <Input placeholder="Display name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
              <Input type="password" placeholder="Password (min 8 chars)" value={password} onChange={(e) => setPassword(e.target.value)} required />
              <Input type="email" placeholder="Email (optional)" value={email} onChange={(e) => setEmail(e.target.value)} />
              <div className="col-span-2 flex gap-3">
                <Button type="submit" disabled={saving}>{saving ? 'Creating\u2026' : 'Create User'}</Button>
                <Button type="button" variant="ghost" onClick={() => setShowForm(false)}>Cancel</Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      {loading ? (
        <p className="text-secondary">Loading\u2026</p>
      ) : (
        <div className="space-y-2">
          {users.map((u) => (
            <div
              key={u.id}
              onClick={() => navigate(`/users/${u.id}`)}
              className="flex items-center justify-between px-4 py-3 rounded-lg border border-subtle bg-surface cursor-pointer hover:bg-hover"
            >
              <div>
                <span className="font-medium text-primary">{u.display_name ?? u.username ?? u.netid}</span>
                <span className="text-secondary text-sm ml-3">{u.username ?? u.netid}</span>
                <span className={`ml-3 text-xs px-2 py-0.5 rounded-full ${u.role === 'ADMIN' ? 'bg-accent-subtle text-accent' : 'bg-raised text-secondary'}`}>
                  {u.role}
                </span>
              </div>
              <span className="text-secondary text-xs">{u.email ?? ''}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
