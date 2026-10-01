import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../lib/api';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Card, CardContent, CardHeader } from '../components/ui/card';

export default function JoinPage() {
  const [searchParams] = useSearchParams();
  const [roomCode, setRoomCode] = useState(() => searchParams.get('code')?.toUpperCase() ?? '');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  async function handleJoin(e?: React.FormEvent) {
    e?.preventDefault();
    const code = roomCode.trim().toUpperCase();
    if (code.length !== 6) {
      setError('Room code must be 6 characters');
      return;
    }
    setError('');
    setLoading(true);
    try {
      await api.get(`/game/rooms/${code}/ping`);
      navigate(`/name/${code}`);
    } catch {
      setError('Room not found. Check the code and try again.');
    } finally {
      setLoading(false);
    }
  }

  // Auto-submit when the page loads with a pre-filled code from QR scan
  useEffect(() => {
    if (searchParams.get('code')) {
      handleJoin();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <h1 className="text-2xl font-bold text-primary text-center">Buzzer</h1>
          <p className="text-secondary text-sm text-center mt-1">Enter your room code to join</p>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleJoin} className="space-y-4">
            <Input
              type="text"
              value={roomCode}
              onChange={e => setRoomCode(e.target.value.toUpperCase())}
              placeholder="XXXXXX"
              maxLength={6}
              className="text-center text-3xl font-mono tracking-widest uppercase"
              autoCapitalize="characters"
              autoComplete="off"
              autoFocus
            />
            {error && <p className="text-danger text-sm text-center">{error}</p>}
            <Button type="submit" className="w-full" size="lg" disabled={loading || roomCode.length < 6}>
              {loading ? 'Checking…' : 'Join Game'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
