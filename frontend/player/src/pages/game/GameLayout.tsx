import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { Outlet, useNavigate, useParams } from 'react-router-dom';
import { isTokenExpired } from '../../lib/utils';
import { io, Socket } from 'socket.io-client';
import type {
  AnswerRejectedPayload,
  AnswerResultPayload,
  PlayerGameOverPayload,
  PlayerJoinedPayload,
  PlayerPhase,
  PlayerResultsPayload,
  QuestionPayload,
  SyncStatePayload,
} from '../../types/game';

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

interface GameContextValue {
  phase: PlayerPhase;
  gameStatus: 'LOBBY' | 'IN_PROGRESS';
  roomCode: string;
  playerCount: number;
  hostDisconnected: boolean;
  currentQuestion: QuestionPayload | null;
  questionLocked: boolean;
  lastAnswerData: Record<string, unknown> | null;
  answerResult: AnswerResultPayload | null;
  questionResults: PlayerResultsPayload | null;
  gameOver: PlayerGameOverPayload | null;
  /** The question whose answer is on its way: sent or waiting for the socket to rejoin, and
   *  not yet acknowledged by the server. */
  pendingAnswerId: number | null;
  /** The question whose answer the server refused because it arrived too late. */
  rejectedAnswerId: number | null;
  emitAnswer: (questionId: number, answerData: Record<string, unknown>, answerTimeMs: number) => void;
}

interface PendingAnswer {
  questionId: number;
  payload: { question_id: number; answer_data: Record<string, unknown>; answer_time_ms: number };
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
  // React Router gives `navigate` a new identity on every pathname change. The socket effect
  // must not depend on it, or every screen change re-creates the socket (B1); its handlers
  // reach the current one through this ref instead.
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const socketRef = useRef<Socket | null>(null);
  // Ref mirrors phase so socket event closures (registered once) can read current value.
  const phaseRef = useRef<PlayerPhase>('lobby');
  // The socket is ready once the server has seated it in the room, which it confirms with
  // sync_state. Anything sent before that — including what socket.io buffers while
  // disconnected and flushes ahead of join_room — reaches a server that ignores it.
  const readyRef = useRef(false);
  // An answer is pending from emitAnswer until the server acknowledges it (answer_received).
  // It is sent only while the socket is ready, else after the next sync_state.
  const pendingRef = useRef<PendingAnswer | null>(null);
  const currentQuestionIdRef = useRef<number | null>(null);
  const acceptedQuestionIdRef = useRef<number | null>(null);
  // The question `lastAnswerData` answers.
  const lastAnswerQuestionRef = useRef<number | null>(null);

  const [phase, setPhase] = useState<PlayerPhase>('lobby');
  const [gameStatus, setGameStatus] = useState<'LOBBY' | 'IN_PROGRESS'>('LOBBY');
  const [playerCount, setPlayerCount] = useState(0);
  const [hostDisconnected, setHostDisconnected] = useState(false);
  const [currentQuestion, setCurrentQuestion] = useState<QuestionPayload | null>(null);
  const [questionLocked, setQuestionLocked] = useState(false);
  const [lastAnswerData, setLastAnswerData] = useState<Record<string, unknown> | null>(null);
  const [answerResult, setAnswerResult] = useState<AnswerResultPayload | null>(null);
  const [questionResults, setQuestionResults] = useState<PlayerResultsPayload | null>(null);
  const [gameOver, setGameOver] = useState<PlayerGameOverPayload | null>(null);
  const [pendingAnswerId, setPendingAnswerId] = useState<number | null>(null);
  const [rejectedAnswerId, setRejectedAnswerId] = useState<number | null>(null);
  // Connection lost after the game screen was up: socket.io is retrying. Shown as a banner, so
  // the page underneath — and a board's placements or selection — stays mounted.
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState('');

  function setPending(pending: PendingAnswer | null) {
    pendingRef.current = pending;
    setPendingAnswerId(pending?.questionId ?? null);
  }

  // The server says this question's answer didn't count (rejected as late, or dropped after a
  // reconnect): forget what the phone sent, so the results can't show it as "You answered".
  // Only that question's answer, and never one the server accepted.
  function forgetAnswer(questionId: number) {
    if (lastAnswerQuestionRef.current !== questionId) return;
    if (acceptedQuestionIdRef.current === questionId) return;
    lastAnswerQuestionRef.current = null;
    setLastAnswerData(null);
  }

  useEffect(() => {
    const go = (to: string, options?: { replace?: boolean }) => navigateRef.current(to, options);
    const token = localStorage.getItem('token');
    if (!token || isTokenExpired(token)) {
      localStorage.removeItem('token');
      go(code ? `/name/${code}` : '/join', { replace: true });
      return;
    }
    if (!code) {
      go('/join', { replace: true });
      return;
    }

    const sock = io({
      path: '/socket.io',
      auth: (cb) => cb({ token: localStorage.getItem('token') ?? '' }),
      transports: ['websocket', 'polling'],
    });
    socketRef.current = sock;
    readyRef.current = false;
    let connectedBefore = false;

    // The server has the answer: show it as locked in.
    const accepted = (questionId: number) => {
      acceptedQuestionIdRef.current = questionId;
      setPhase('feedback');
      go(`/game/${code}/feedback`);
    };

    sock.on('connect', () => {
      connectedBefore = true;
      readyRef.current = false;
      setError('');
      if (phaseRef.current !== 'gameover') {
        sock.emit('join_room', { room_code: code, role: 'PLAYER' });
      }
    });

    // `sock.active`: socket.io will keep retrying. It doesn't after our own disconnect (game
    // over, leaving) or a connection the server refused.
    sock.on('disconnect', () => {
      readyRef.current = false;
      if (sock.active) setReconnecting(true);
    });

    sock.on('connect_error', (err) => {
      if (phaseRef.current === 'gameover') return;
      // A failed retry after a lost connection isn't fatal: socket.io keeps retrying.
      if (connectedBefore && sock.active) {
        setReconnecting(true);
      } else {
        setReconnecting(false);
        setError(`Connection error: ${err.message}`);
      }
    });

    sock.on('sync_state', (data: SyncStatePayload) => {
      readyRef.current = true;
      setReconnecting(false); // back in the room, not just connected
      setHostDisconnected(Boolean(data.hostDisconnected));
      setPlayerCount(data.playerCount ?? 0);
      if (data.questionLocked) setQuestionLocked(true);
      if (data.status === 'LOBBY') {
        setGameStatus('LOBBY');
        setPhase('lobby');
        go(`/game/${code}/lobby`, { replace: true });
      } else if (data.status === 'IN_PROGRESS') {
        // Game already running — update state but don't navigate; new_question
        // (or question_results) will drive routing to the correct page.
        setGameStatus('IN_PROGRESS');
      }

      // Rejoined with an answer still pending: send it, unless it can no longer count.
      const pending = pendingRef.current;
      if (!pending) return;
      const open = data.status === 'IN_PROGRESS' && data.currentQuestion?.question_id === pending.questionId;
      if (open && data.hasAnswered) {
        // It reached the server before the connection dropped; only the reply was lost.
        setPending(null);
        accepted(pending.questionId);
      } else if (open && !data.questionLocked) {
        sock.emit('submit_answer', pending.payload);
      } else {
        // Locked or moved on while disconnected: it can't be sent, and the page says so.
        setPending(null);
        forgetAnswer(pending.questionId);
      }
    });

    sock.on('player_joined', (data: PlayerJoinedPayload) => {
      setPlayerCount(data.playerCount);
    });

    sock.on('new_question', (data: QuestionPayload) => {
      // After a reconnect the server repeats the open question to a player it has no answer
      // from. That repeat can arrive after the answer's acknowledgement; don't go back to it.
      const repeat = data.questionId === currentQuestionIdRef.current;
      if (repeat && acceptedQuestionIdRef.current === data.questionId) return;
      currentQuestionIdRef.current = data.questionId;
      setCurrentQuestion(data);
      setQuestionLocked(false);
      if (!repeat) {
        setPending(null);
        setRejectedAnswerId(null);
        acceptedQuestionIdRef.current = null;
        lastAnswerQuestionRef.current = null;
        setLastAnswerData(null);
        setAnswerResult(null);
        setQuestionResults(null);
      }
      setHostDisconnected(false);
      setPhase('question');
      go(`/game/${code}/question`);
    });

    sock.on('question_locked', () => {
      setQuestionLocked(true);
    });

    sock.on('question_unlocked', () => {
      setQuestionLocked(false);
      setRejectedAnswerId(null); // open again: the player may answer
    });

    sock.on('answer_received', (data: AnswerResultPayload) => {
      if (data.alreadyAnswered) {
        // A duplicate submit's echo. It carries no score, but it does confirm that the
        // server has an answer from us, if that one's own reply never arrived.
        if (pendingRef.current?.questionId === data.questionId) {
          setPending(null);
          accepted(data.questionId);
        }
        return;
      }
      setPending(null);
      setAnswerResult(data);
      accepted(data.questionId);
    });

    // Too late: the question locked, or moved on, before the answer arrived. Not an error — the
    // game screen stays — and never shown as accepted: the page shows the question as closed.
    sock.on('answer_rejected', (data: AnswerRejectedPayload) => {
      if (pendingRef.current?.questionId === data.questionId) setPending(null);
      forgetAnswer(data.questionId);
      if (data.questionId === currentQuestionIdRef.current) {
        setRejectedAnswerId(data.questionId);
        setQuestionLocked(true);
      }
    });

    sock.on('question_results', (data: PlayerResultsPayload) => {
      setPending(null);
      setQuestionResults(data);
      setPhase('results');
      go(`/game/${code}/results`);
    });

    sock.on('game_over', (data: PlayerGameOverPayload) => {
      phaseRef.current = 'gameover';
      setGameOver(data);
      setPhase('gameover');
      go(`/game/${code}/gameover`);
      sock.disconnect();
    });

    sock.on('host_disconnected', () => {
      setHostDisconnected(true);
    });

    // The host is back. A lock the host set by hand may still hold; question_unlocked says so.
    sock.on('host_reconnected', () => {
      setHostDisconnected(false);
    });

    sock.on('game_abandoned', () => {
      go('/join', { replace: true });
    });

    sock.on('error', (data: { message: string }) => {
      const pending = pendingRef.current;
      setPending(null); // a rejected answer is reported, never retried
      if (pending) forgetAnswer(pending.questionId);
      if (phaseRef.current !== 'gameover') {
        setError(data.message);
      }
    });

    return () => {
      // Drop the listeners first, so nothing this socket still delivers can change state.
      sock.off();
      sock.disconnect();
      if (socketRef.current === sock) socketRef.current = null;
    };
  }, [code]);

  function emitAnswer(questionId: number, answerData: Record<string, unknown>, answerTimeMs: number) {
    if (pendingRef.current?.questionId === questionId) return; // already on its way
    const pending: PendingAnswer = {
      questionId,
      payload: { question_id: questionId, answer_data: answerData, answer_time_ms: answerTimeMs },
    };
    lastAnswerQuestionRef.current = questionId;
    setLastAnswerData(answerData);
    setPending(pending);
    const sock = socketRef.current;
    if (sock && readyRef.current) sock.emit('submit_answer', pending.payload);
    // Otherwise the socket is reconnecting: sync_state sends it once the room has us again.
  }

  if (error && phase !== 'gameover') {
    return (
      <div className="min-h-screen flex items-center justify-center p-4">
        <div className="text-center space-y-4">
          <p className="text-red-400 text-lg">{error}</p>
          <button className="text-indigo-400 underline" onClick={() => navigate('/join')}>
            Back to Join
          </button>
        </div>
      </div>
    );
  }

  return (
    <GameContext.Provider
      value={{ phase, gameStatus, roomCode: code, playerCount, hostDisconnected, currentQuestion, questionLocked, lastAnswerData, answerResult, questionResults, gameOver, pendingAnswerId, rejectedAnswerId, emitAnswer }}
    >
      {reconnecting && phase !== 'gameover' ? (
        <div role="status" className="fixed top-0 inset-x-0 bg-slate-700/95 text-slate-100 text-center py-2 text-sm z-50">
          Connection lost — reconnecting…
        </div>
      ) : hostDisconnected && phase !== 'gameover' && (
        <div className="fixed top-0 inset-x-0 bg-yellow-600/90 text-yellow-100 text-center py-2 text-sm z-50">
          Host disconnected — waiting for them to reconnect…
        </div>
      )}
      <Outlet />
    </GameContext.Provider>
  );
}
