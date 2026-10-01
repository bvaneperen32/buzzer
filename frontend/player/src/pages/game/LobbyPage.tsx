import { useGame } from './GameLayout';

export default function LobbyPage() {
  const { roomCode, playerCount, gameStatus } = useGame();

  const statusText = gameStatus === 'IN_PROGRESS'
    ? 'Waiting for next question…'
    : 'Waiting for host to start…';

  return (
    <div className="min-h-screen flex flex-col items-center justify-center p-6 gap-6 text-center">
      <div className="space-y-2">
        <p className="text-secondary text-sm uppercase tracking-widest">Room</p>
        <p className="text-4xl font-black font-mono tracking-widest text-primary">{roomCode}</p>
      </div>

      <div className="mt-4 space-y-2">
        <div className="flex justify-center">
          <div className="w-12 h-12 border-4 border-accent border-t-transparent rounded-full animate-spin" />
        </div>
        <p className="text-secondary text-lg font-medium">{statusText}</p>
        <p className="text-secondary text-sm">
          {playerCount} player{playerCount !== 1 ? 's' : ''} in room
        </p>
      </div>
    </div>
  );
}
