import { create } from 'zustand';
import { apiClient } from '../lib/api';
import { LAST_SESSION_KEY } from '../lib/constants';
import { useChatStore } from './chatStore';
import { useCaseStore } from './caseStore';
import { useProjectStore } from './projectStore';
import { useSessionStore } from './sessionStore';

export interface User {
  id: string;
  email: string;
  role: string;
  is_admin: boolean;
  company_name: string | null;
  plan: string;
}

interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;

  checkAuth: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, company_name?: string) => Promise<void>;
  logout: () => Promise<void>;
  clearError: () => void;
  setUnauthenticated: () => void; // for global 401 intercepts
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: false,
  isLoading: true, // Start loading as we check auth on mount
  error: null,

  checkAuth: async () => {
    try {
      set({ isLoading: true, error: null });
      const { data } = await apiClient.get<User>('/auth/me');
      set({ user: data, isAuthenticated: true, isLoading: false });
    } catch (err: any) {
      set({ user: null, isAuthenticated: false, isLoading: false });
    }
  },

  login: async (email, password) => {
    try {
      set({ isLoading: true, error: null });
      await apiClient.post('/auth/login', { email, password });
      
      // On success, fetch the user profile
      const { data } = await apiClient.get<User>('/auth/me');
      set({ user: data, isAuthenticated: true, isLoading: false });
    } catch (err: any) {
      const isNetworkError = !err.response;
      const message = isNetworkError 
        ? "Network error. The server may be offline." 
        : (err.response?.data?.detail || "Invalid email or password");
      
      set({ error: message, isLoading: false, isAuthenticated: false });
    }
  },

  register: async (email, password, company_name) => {
    try {
      set({ isLoading: true, error: null });
      await apiClient.post('/auth/register', { email, password, company_name });
      
      // Auto-login after successful registration
      await apiClient.post('/auth/login', { email, password });
      
      const { data } = await apiClient.get<User>('/auth/me');
      set({ user: data, isAuthenticated: true, isLoading: false });
    } catch (err: any) {
      const isNetworkError = !err.response;
      const message = isNetworkError 
        ? "Network error. The server may be offline." 
        : (err.response?.data?.detail || "Registration failed");
      
      set({ error: message, isLoading: false });
    }
  },

  logout: async () => {
    try {
      await apiClient.post('/auth/logout');
    } catch (err) {
      // ignore logout errors
    } finally {
      set({ user: null, isAuthenticated: false, error: null });
      // Clear every other store's state too, so a shared workstation never
      // carries one user's case/session/chat content into the next login.
      useChatStore.getState().reset();
      useCaseStore.getState().reset();
      useProjectStore.getState().reset();
      useSessionStore.getState().reset();
      localStorage.removeItem(LAST_SESSION_KEY);
    }
  },

  clearError: () => set({ error: null }),
  
  setUnauthenticated: () => set({ user: null, isAuthenticated: false }),
}));
