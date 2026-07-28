import { create } from 'zustand';
import { apiClient } from '../lib/api';
import { useProjectStore } from './projectStore';
import { useCaseStore } from './caseStore';

export interface Session {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  project_id?: string | null;
  case_id?: string | null;
}

interface SessionState {
  sessions: Session[];
  isLoading: boolean;
  error: string | null;

  fetchSessions: (signal?: AbortSignal) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  
  // Optimistically prepend a new session (called from ChatPage when user sends first message)
  addSessionOptimistic: (session: Session, projectId?: string | null, caseId?: string | null) => void;
  // Optimistically update a session's title (called when title_generation event arrives)
  updateSessionTitleOptimistic: (id: string, title: string) => void;
  reset: () => void;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: [],
  isLoading: false,
  error: null,

  fetchSessions: async (signal?: AbortSignal) => {
    try {
      set({ isLoading: true, error: null });
      const activeProjectId = useProjectStore.getState().activeProjectId;
      const activeCaseId = useCaseStore.getState().activeCaseId;
      const params = new URLSearchParams();
      if (activeProjectId) params.set('project_id', activeProjectId);
      if (activeCaseId) params.set('case_id', activeCaseId);
      const queryParams = params.toString() ? `?${params.toString()}` : '';
      const { data } = await apiClient.get<Session[]>(`/sessions${queryParams}`, { signal });
      set({ sessions: data, isLoading: false });
    } catch (err: any) {
      if (err.name === 'CanceledError' || err.message === 'canceled') return;
      set({ error: err.response?.data?.detail || 'Failed to fetch sessions', isLoading: false });
    }
  },

  deleteSession: async (id: string) => {
    try {
      await apiClient.delete(`/sessions/${id}`);
      // Remove from UI after successful backend deletion (not optimistic, confirmed per plan)
      set({ sessions: get().sessions.filter((s) => s.session_id !== id) });
    } catch (err: any) {
      set({ error: err.response?.data?.detail || 'Failed to delete session' });
      throw err; // So UI can show error
    }
  },

  renameSession: async (id: string, title: string) => {
    try {
      await apiClient.patch(`/sessions/${id}`, { title });
      set({
        sessions: get().sessions.map((s) =>
          s.session_id === id ? { ...s, title } : s
        ),
      });
    } catch (err: any) {
      set({ error: err.response?.data?.detail || 'Failed to rename session' });
      throw err;
    }
  },
  
  addSessionOptimistic: (session: Session, projectId?: string | null, caseId?: string | null) => {
    // Dedupe by session_id: this fires whenever the first message of a
    // conversation is sent, including into a session that is already in the
    // list (e.g. an empty session restored from the URL). Prepending blindly
    // produced duplicate rows that all matched the active id, so every one of
    // them rendered as "active".
    const existing = get().sessions.filter((s) => s.session_id !== session.session_id);
    set({ sessions: [{ ...session, project_id: projectId, case_id: caseId }, ...existing] });
  },
  
  updateSessionTitleOptimistic: (id: string, title: string) => {
    set({
      sessions: get().sessions.map((s) =>
        s.session_id === id ? { ...s, title } : s
      ),
    });
  },

  reset: () => {
    set({ sessions: [], isLoading: false, error: null });
  },
}));
