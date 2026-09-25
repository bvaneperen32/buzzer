import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import LoginPage from './pages/LoginPage';
import HostShell from './pages/HostShell';
import HomePage from './pages/HomePage';
import GamesPage from './pages/GamesPage';
import QuestionEditorPage from './pages/QuestionEditorPage';
import RostersPage from './pages/RostersPage';
import RosterPage from './pages/RosterPage';
import SessionsPage from './pages/SessionsPage';
import GameLayout from './pages/game/GameLayout';
import LobbyPage from './pages/game/LobbyPage';
import QuestionPage from './pages/game/QuestionPage';
import ResultsPage from './pages/game/ResultsPage';
import GameOverPage from './pages/game/GameOverPage';

function RequireAuth({ children }: { children: React.ReactNode }) {
  if (!localStorage.getItem('token')) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        {/* Play keeps the path /home: GameLayout and GameOverPage navigate there. */}
        <Route element={<RequireAuth><HostShell /></RequireAuth>}>
          <Route path="/home" element={<HomePage />} />
          <Route path="/games" element={<GamesPage />} />
          <Route path="/games/:gameId" element={<QuestionEditorPage />} />
          <Route path="/rosters" element={<RostersPage />} />
          <Route path="/rosters/:courseId" element={<RosterPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
        </Route>
        <Route path="/game/:code" element={<RequireAuth><GameLayout /></RequireAuth>}>
          <Route path="lobby" element={<LobbyPage />} />
          <Route path="question" element={<QuestionPage />} />
          <Route path="results" element={<ResultsPage />} />
          <Route path="gameover" element={<GameOverPage />} />
        </Route>
        {/* Also catches /. RequireAuth sends signed-out users on to /login. */}
        <Route path="*" element={<Navigate to="/home" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
