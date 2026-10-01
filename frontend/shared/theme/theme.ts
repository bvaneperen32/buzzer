/**
 * Framework-free theme logic shared by host, player and admin; each app's ThemeToggle imports it
 * by relative path. No imports: frontend/shared/ has no node_modules, so nothing here could
 * resolve a package (react included).
 *
 * The active theme is `<html data-theme="light|dark">`. The inline script in each app's
 * index.html sets it before first paint, using the same rule as currentTheme(): the app's stored
 * choice if valid, else the OS preference. The attribute is the source of truth; React state only
 * mirrors it. Each app has its own storage key, so in production (one origin) toggling admin
 * doesn't change the projector.
 */

export type Theme = 'light' | 'dark';

const DARK_QUERY = '(prefers-color-scheme: dark)';

function isTheme(value: unknown): value is Theme {
  return value === 'light' || value === 'dark';
}

/** The app's stored choice, or null when there is none, it's invalid, or storage throws. */
export function storedTheme(key: string): Theme | null {
  try {
    const value = localStorage.getItem(key);
    return isTheme(value) ? value : null;
  } catch {
    return null; // storage can throw in private modes
  }
}

/** The OS preference. */
export function systemTheme(): Theme {
  return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light';
}

/** The theme the app should show: the stored choice wins over the OS. */
export function currentTheme(key: string): Theme {
  return storedTheme(key) ?? systemTheme();
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

/** Stores the choice and applies it. If storage fails, it still applies for this session. */
export function setTheme(key: string, theme: Theme): void {
  try {
    localStorage.setItem(key, theme);
  } catch {
    // Storage blocked: the theme below still applies until the page is reloaded.
  }
  applyTheme(theme);
}

/**
 * Follows changes made outside this tab's toggle: the OS preference (applied only while no
 * choice is stored) and this key changing in another tab. Calls `onChange` with the new theme
 * after each change it applies. The `storage` event never fires in the tab that wrote the key,
 * so a toggle must update its own state itself. Returns the unsubscribe function.
 */
export function watchTheme(key: string, onChange: (theme: Theme) => void): () => void {
  const media = window.matchMedia(DARK_QUERY);

  function apply() {
    const theme = currentTheme(key);
    applyTheme(theme);
    onChange(theme);
  }

  function onSystemChange() {
    if (storedTheme(key) === null) apply();
  }

  function onStorage(event: StorageEvent) {
    if (event.key === key) apply();
  }

  media.addEventListener('change', onSystemChange);
  window.addEventListener('storage', onStorage);
  return () => {
    media.removeEventListener('change', onSystemChange);
    window.removeEventListener('storage', onStorage);
  };
}

/**
 * Reports every change to `<html data-theme>`, whatever made it: this tab's toggle, the OS
 * preference or another tab (all three end in applyTheme). For code that paints colours by hand,
 * such as a canvas; unlike watchTheme it applies nothing. Returns the disconnect function.
 */
export function observeTheme(onChange: (theme: Theme) => void): () => void {
  const root = document.documentElement;
  const observer = new MutationObserver(() => {
    const theme = root.dataset.theme;
    if (isTheme(theme)) onChange(theme);
  });
  observer.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
  return () => observer.disconnect();
}
