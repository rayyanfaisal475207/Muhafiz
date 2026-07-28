// ============================================================
// Theme — light/dark switching.
//
// All colors in the app are CSS variables; the `.dark` class in index.css
// redefines them. So switching themes is just toggling that class on the
// root element. The user's choice is a UI preference, persisted in
// localStorage (not app data). If they've never chosen, we follow the OS.
// ============================================================

export type Theme = 'light' | 'dark';

export const THEME_KEY = 'muhafiz_theme';

export function getStoredTheme(): Theme | null {
  const v = localStorage.getItem(THEME_KEY);
  return v === 'light' || v === 'dark' ? v : null;
}

/** Persisted choice if any, otherwise the OS preference, otherwise light. */
export function resolveInitialTheme(): Theme {
  const stored = getStoredTheme();
  if (stored) return stored;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/** Reflect the theme onto the DOM (adds/removes `.dark` on <html>). */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark');
}

/** Persist the choice and apply it. */
export function persistTheme(theme: Theme): void {
  localStorage.setItem(THEME_KEY, theme);
  applyTheme(theme);
}
