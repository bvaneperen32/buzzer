import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { ThemeToggle } from './components/ui/ThemeToggle';
import JoinPage from './pages/JoinPage';
import LoginPage from './pages/LoginPage';
import NamePage from './pages/NamePage';
import GameLayout from './pages/game/GameLayout';
import LobbyPage from './pages/game/LobbyPage';
import QuestionPage from './pages/game/QuestionPage';
import FeedbackPage from './pages/game/FeedbackPage';
import ResultsPage from './pages/game/ResultsPage';
import GameOverPage from './pages/game/GameOverPage';

export default function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/join" element={<JoinPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/name/:code" element={<NamePage />} />
        <Route path="/game/:code" element={<GameLayout />}>
          <Route path="lobby" element={<LobbyPage />} />
          <Route path="question" element={<QuestionPage />} />
          <Route path="feedback" element={<FeedbackPage />} />
          <Route path="results" element={<ResultsPage />} />
          <Route path="gameover" element={<GameOverPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/join" replace />} />
      </Routes>
      {/* Outside the routes, so every screen has it (join, game screens, errors) */}
      <ThemeToggle />
    </BrowserRouter>
  );
}
