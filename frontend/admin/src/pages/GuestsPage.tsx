import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import { Button } from '../components/ui/button';
import { Card, CardContent, CardHeader } from '../components/ui/card';
import { Input } from '../components/ui/input';

interface GuestUser {
  id: string;
  display_name: string | null;
  email: string | null;
  created_at: string;
}

export default function GuestsPage() {
  const [guests, setGuests] = useState<GuestUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [mergeGuestId, setMergeGuestId] = useState('');
  const [mergeNetid, setMergeNetid] = useState('');
  const [merging, setMerging] = useState(false);
  const [mergeSuccess, setMergeSuccess] = useState('');

  async function load() {
    try {
      const data = await api.get<GuestUser[]>('/admin/users/guests');
      setGuests(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load guests');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  async function handleMerge(e: React.FormEvent) {
    e.preventDefault();
    setMerging(true);
    setError('');
    setMergeSuccess('');
    try {
      await api.post('/admin/users/merge-guest', {
        guest_user_id: mergeGuestId,
        target_netid: mergeNetid,
      });
      setMergeSuccess(`Guest merged into ${mergeNetid}`);
      setMergeGuestId('');
      setMergeNetid('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Merge failed');
    } finally {
      setMerging(false);
    }
  }

  async function deleteGuest(id: string) {
    if (!confirm('Delete this guest user and all their scores?')) return;
    try {
      await api.delete(`/admin/users/${id}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
    }
  }

  return (
    <div className="p-8 max-w-4xl space-y-6">
      <h2 className="text-2xl font-bold text-primary">Guest Users</h2>

      {error && <p className="text-danger text-sm">{error}</p>}
      {mergeSuccess && <p className="text-success text-sm">{mergeSuccess}</p>}

      <Card>
        <CardHeader><h3 className="font-semibold text-primary">Merge Guest into Real User</h3></CardHeader>
        <CardContent>
          <form onSubmit={handleMerge} className="flex gap-3 flex-wrap">
            <Input
              placeholder="Guest user ID (UUID)"
              value={mergeGuestId}
              onChange={(e) => setMergeGuestId(e.target.value)}
              className="flex-1 min-w-64 font-mono text-sm"
              required
            />
            <Input
              placeholder="Target netid"
              value={mergeNetid}
              onChange={(e) => setMergeNetid(e.target.value)}
              className="flex-1 min-w-40"
              required
            />
            <Button type="submit" disabled={merging}>{merging ? 'Merging\u2026' : 'Merge'}</Button>
          </form>
        </CardContent>
      </Card>

      {loading ? (
        <p className="text-secondary">Loading\u2026</p>
      ) : guests.length === 0 ? (
        <p className="text-secondary">No guest users.</p>
      ) : (
        <div className="space-y-2">
          {guests.map((g) => (
            <div key={g.id} className="flex items-center justify-between px-4 py-3 rounded-lg border border-subtle bg-surface">
              <div>
                <span className="font-medium text-primary">{g.display_name}</span>
                <span className="text-secondary text-sm ml-3">{g.email}</span>
                <span className="text-secondary text-xs ml-3 font-mono">{g.id}</span>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setMergeGuestId(g.id)}
                >
                  Use ID
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => void deleteGuest(g.id)}
                >
                  Delete
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
