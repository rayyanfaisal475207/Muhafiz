// ============================================================
// Theme Store — Zustand
// Holds the current theme so any component can read it / toggle it,
// and keeps the DOM class + localStorage in sync.
// ============================================================

import { create } from 'zustand';
import { resolveInitialTheme, persistTheme, type Theme } from '../lib/theme';

interface ThemeState {
  theme: Theme;
  toggle: () => void;
  setTheme: (theme: Theme) => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: resolveInitialTheme(),
  toggle: () => {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark';
    persistTheme(next);
    set({ theme: next });
  },
  setTheme: (theme) => {
    persistTheme(theme);
    set({ theme });
  },
}));
