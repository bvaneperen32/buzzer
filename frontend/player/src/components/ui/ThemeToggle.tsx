import { useEffect, useState } from 'react';
import { Moon, Sun } from 'lucide-react';
import {
  currentTheme,
  setTheme as storeTheme,
  watchTheme,
  type Theme,
} from '../../../../shared/theme/theme';

// Must match the key in index.html's inline theme script. Each app has its own key.
const THEME_KEY = 'buzzer-theme-player';

/**
 * The light/dark switch, fixed top-right on every route (App.tsx renders it once, outside the
 * routes). The first visit follows the OS; a click stores the choice, which from then on wins
 * over the OS. `<html data-theme>` is the source of truth: this state only mirrors it for the
 * icon and label, which name the theme a click switches *to*. 44px here (touch target), 40px in
 * host and admin. z-40 keeps it under the "Host disconnected" banner (z-50) while that shows.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => currentTheme(THEME_KEY));

  useEffect(() => watchTheme(THEME_KEY, setTheme), []);

  function toggle() {
    const next: Theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    storeTheme(THEME_KEY, next);
    // Required: `storage` events fire only in other tabs, so watchTheme never reports this click.
    setTheme(next);
  }

  const label = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
  const Icon = theme === 'dark' ? Sun : Moon;

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={label}
      title={label}
      className="fixed top-3 right-3 z-40 flex h-11 w-11 items-center justify-center rounded-full border border-subtle bg-surface text-secondary shadow transition-colors hover:bg-hover hover:text-primary"
    >
      <Icon size={18} aria-hidden="true" />
    </button>
  );
}
