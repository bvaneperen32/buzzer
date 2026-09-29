import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { Outlet, useNavigate, useParams } from 'react-router-dom';
import { api } from '../../lib/api';
import { referencedImageIds } from '../../lib/questionImages';
import { useSessionImageLinks } from '../../lib/sessionImages';
import { QRCodeSVG } from 'qrcode.react';
import { io, Socket } from 'socket.io-client';
import type {
  AnswerPhaseEndedPayload,
  AnswerStatusPayload,
  HostGameOverPayload,
  HostPhase,
  HostResultsPayload,
  PlayerInfo,
  PlayerJoinedPayload,
  PlayerLeftPayload,
  QuestionPayload,
  SyncStatePayload,
} from '../../types/game';
import type { LinkEntry } from '../../types/images';

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

interface GameContextValue {
  phase: HostPhase;
  roomCode: string;
  gameTitle: string;
  players: PlayerInfo[];
  playerCount: number;
  currentQuestion: QuestionPayload | null;
  answeredCount: number;
  allAnswered: boolean;       // every player has answered (drives auto-advance)
  answerPhaseEnded: boolean;     // timer expired or all answered (drives Lock button)
  questionLocked: boolean;       // host explicitly locked the question
  lockedTimerSeconds: number | null; // remaining seconds when locked (for reconnect)
  questionResults: HostResultsPayload | null;
  gameOver: HostGameOverPayload | null;
  autoAdvance: boolean;
  setAutoAdvance: (v: boolean) => void;
  emitAdvance: () => void;
  emitLockQuestion: () => void;
  /** The session link map: every image of the game, by id as a string (T8). */
  imageLinks: Record<string, LinkEntry>;
  /** Fetch current links; calls made at the same time share one request. */
  refreshImageLinks: () => Promise<void>;
  /** The last link fetch failed (e.g. access lost): show unresolved images as unavailable. */
  imageLinksFailed: boolean;
}

const GameContext = createContext<GameContextValue | null>(null);

export function useGame(): GameContextValue {
  const ctx = useContext(GameContext);
  if (!ctx) throw new Error('useGame must be used within GameLayout');
  return ctx;
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export default function GameLayout() {
  const { code = '' } = useParams<{ code: string }>();
  const navigate = useNavigate();
  const socketRef = useRef<Socket | null>(null);
  const currentQuestionIdRef = useRef<number | null>(null);

  const [phase, setPhase] = useState<HostPhase>('lobby');
  const [gameTitle, setGameTitle] = useState('');
  const [players, setPlayers] = useState<PlayerInfo[]>([]);
  const [playerCount, setPlayerCount] = useState(0);
  const [currentQuestion, setCurrentQuestion] = useState<QuestionPayload | null>(null);
  const [answeredCount, setAnsweredCount] = useState(0);
  const [allAnswered, setAllAnswered] = useState(false);
  const [answerPhaseEnded, setAnswerPhaseEnded] = useState(false);
  const [questionLocked, setQuestionLocked] = useState(false);
  const [lockedTimerSeconds, setLockedTimerSeconds] = useState<number | null>(null);
  const [questionResults, setQuestionResults] = useState<HostResultsPayload | null>(null);
  const [gameOver, setGameOver] = useState<HostGameOverPayload | null>(null);
  const [autoAdvance, setAutoAdvance] = useState(false);
  const [error, setError] = useState('');
  const [sessionId, setSessionId] = useState<string | null>(null);

  const {
    links: imageLinks,
    refresh: refreshImageLinks,
    failed: imageLinksFailed,
  } = useSessionImageLinks(sessionId);
  // The socket handlers are registered once; they reach the current refresh through a ref.
  const refreshImageLinksRef = useRef(refreshImageLinks);
  refreshImageLinksRef.current = refreshImageLinks;
  const refreshedForQuestion = useRef<number | null>(null);

  useEffect(() => {
    const token = localStorage.getItem('token');
    if (!token || !code) {
      navigate('/home', { replace: true });
      return;
    }

    api.get<{ game_title: string; session_id: string }>(`/game/rooms/${code}`)
      .then(data => {
        setGameTitle(data.game_title);
        setSessionId(data.session_id); // starts the session link map fetch
      })
      .catch(() => {}); // non-fatal — title stays blank (and images unavailable) if it fails

    const sock = io({
      path: '/socket.io',
      auth: (cb) => cb({ token: localStorage.getItem('token') ?? '' }),
      transports: ['websocket', 'polling'],
    });
    socketRef.current = sock;

    let connectedBefore = false;
    sock.on('connect', () => {
      setError('');
      sock.emit('join_room', { room_code: code, role: 'HOST' });
      // Links may have expired while the socket was down.
      if (connectedBefore) void refreshImageLinksRef.current();
      connectedBefore = true;
    });

    sock.on('connect_error', (err) => {
      setError(`Connection error: ${err.message}`);
    });

    sock.on('sync_state', (data: SyncStatePayload) => {
      setPlayers(data.players ?? []);
      setPlayerCount(data.playerCount ?? data.players?.length ?? 0);
      if (data.status === 'LOBBY') {
        setPhase('lobby');
        navigate(`/game/${code}/lobby`, { replace: true });
      } else if (data.status === 'IN_PROGRESS') {
        if (data.currentQuestion) {
          setCurrentQuestion(data.currentQuestion);
          currentQuestionIdRef.current = data.currentQuestion.questionId;
        }
        if (data.questionLocked) {
          setQuestionLocked(true);
          setLockedTimerSeconds(data.timerRemainingSeconds ?? null);
        }
        if (data.questionPhase === 'QUESTION') {
          setPhase('question');
          navigate(`/game/${code}/question`, { replace: true });
        } else if (data.questionPhase === 'RESULTS') {
          setPhase('results');
          navigate(`/game/${code}/results`, { replace: true });
        }
      }
    });

    sock.on('player_joined', (data: PlayerJoinedPayload) => {
      setPlayers(prev =>
        prev.some(p => p.userId === data.userId)
          ? prev
          : [...prev, { userId: data.userId, displayName: data.displayName }]
      );
      setPlayerCount(data.playerCount);
    });

    sock.on('player_left', (data: PlayerLeftPayload) => {
      setPlayers(prev => prev.filter(p => p.userId !== data.userId));
      setPlayerCount(data.playerCount);
    });

    sock.on('new_question', (data: QuestionPayload) => {
      currentQuestionIdRef.current = data.questionId;
      setCurrentQuestion(data);
      setAnsweredCount(0);
      setAllAnswered(false);
      setAnswerPhaseEnded(false);
      setQuestionLocked(false);
      setLockedTimerSeconds(null);
      setQuestionResults(null);
      setPhase('question');
      navigate(`/game/${code}/question`);
    });

    sock.on('answer_status', (data: AnswerStatusPayload) => {
      setAnsweredCount(data.answeredCount);
      if (data.totalPlayers) setPlayerCount(data.totalPlayers);
    });

    sock.on('answer_phase_ended', (data: AnswerPhaseEndedPayload) => {
      // Ignore stale events from a previous question's orphaned timer task
      if (data.questionId !== currentQuestionIdRef.current) return;
      setAnsweredCount(data.answeredCount);
      if (data.totalPlayers) setPlayerCount(data.totalPlayers);
      setAnswerPhaseEnded(true);
      if (data.allAnswered) setAllAnswered(true);
    });

    sock.on('question_locked', (data: { questionId: number; remainingSeconds: number }) => {
      setQuestionLocked(true);
      setLockedTimerSeconds(data.remainingSeconds);
    });

    sock.on('question_unlocked', () => {
      setQuestionLocked(false);
      setLockedTimerSeconds(null);
    });

    sock.on('question_results', (data: HostResultsPayload) => {
      setQuestionResults(data);
      setPhase('results');
      navigate(`/game/${code}/results`);
    });

    sock.on('game_over', (data: HostGameOverPayload) => {
      setGameOver(data);
      setPhase('gameover');
      navigate(`/game/${code}/gameover`);
    });

    sock.on('game_abandoned', () => {
      navigate('/home', { replace: true });
    });

    sock.on('error', (data: { message: string }) => {
      setError(data.message);
    });

    return () => {
      sock.disconnect();
      socketRef.current = null;
    };
  }, [code, navigate]);

  // A question that uses an image the map doesn't have yet (uploaded after the map was fetched)
  // gets one refresh; an id still missing afterwards is treated as no image.
  useEffect(() => {
    if (!currentQuestion || !sessionId) return;
    if (refreshedForQuestion.current === currentQuestion.questionId) return;
    const missing = referencedImageIds(currentQuestion.config).some(
      (id) => !imageLinks[String(id)],
    );
    if (missing) {
      refreshedForQuestion.current = currentQuestion.questionId;
      void refreshImageLinks();
    }
  }, [currentQuestion, sessionId, imageLinks, refreshImageLinks]);

  const emitAdvance = useCallback(() => {
    socketRef.current?.emit('host_advance', {});
  }, []);

  const emitLockQuestion = useCallback(() => {
    socketRef.current?.emit('host_lock_question', {});
  }, []);

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <div className="text-center space-y-4">
          <p className="text-red-400 text-lg">{error}</p>
          <button className="text-indigo-400 underline" onClick={() => navigate('/home')}>
            Back to Home
          </button>
        </div>
      </div>
    );
  }

  const playerJoinUrl = `${window.location.origin}/player/join?code=${code}`;

  return (
    <GameContext.Provider
      value={{ phase, roomCode: code, gameTitle, players, playerCount, currentQuestion, answeredCount, allAnswered, answerPhaseEnded, questionLocked, lockedTimerSeconds, questionResults, gameOver, autoAdvance, setAutoAdvance, emitAdvance, emitLockQuestion, imageLinks, refreshImageLinks, imageLinksFailed }}
    >
      <Outlet />

      {/* Persistent join panel — always visible in the bottom-right corner */}
      <div className="fixed bottom-4 right-4 flex flex-col items-center gap-2 bg-slate-900/90 border border-slate-700 rounded-2xl p-3 shadow-xl backdrop-blur-sm">
        <div className="bg-white rounded-lg p-1.5">
          <QRCodeSVG value={playerJoinUrl} size={96} />
        </div>
        <div className="text-center">
          <p className="text-slate-500 text-[10px] uppercase tracking-widest leading-none mb-0.5">Room Code</p>
          <p className="text-white font-mono font-black tracking-widest text-lg leading-none">{code}</p>
        </div>
      </div>
    </GameContext.Provider>
  );
}
