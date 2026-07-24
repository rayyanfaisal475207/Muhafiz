import { create } from 'zustand';
import { apiClient } from '../lib/api';
import { useAuthStore } from './authStore';

export interface Case {
  case_id: string;
  fir_number?: string;
  crime_category?: string;
  investigation_officer?: string;
  police_station?: string;
  incident_date?: string;
  investigation_status?: string;
  location?: string;
  description?: string;
  created_at: string;
  updated_at: string;
}

export interface CaseCreatePayload {
  case_id?: string;
  fir_number?: string;
  crime_category?: string;
  investigation_officer?: string;
  police_station?: string;
  incident_date?: string;
  investigation_status?: string;
  location?: string;
  description?: string;
}

interface CaseState {
  cases: Case[];
  activeCaseId: string | null;
  isLoading: boolean;
  error: string | null;

  fetchCases: (signal?: AbortSignal) => Promise<void>;
  createCase: (data: CaseCreatePayload) => Promise<Case>;
  updateCase: (id: string, data: Partial<CaseCreatePayload>) => Promise<void>;
  deleteCase: (id: string) => Promise<void>;
  setActiveCase: (id: string | null) => void;
}

export const useCaseStore = create<CaseState>((set, get) => ({
  cases: [],
  activeCaseId: null,
  isLoading: false,
  error: null,

  fetchCases: async (signal?: AbortSignal) => {
    try {
      set({ isLoading: true, error: null });
      const user = useAuthStore.getState().user;
      if (!user) {
        set({ cases: [], isLoading: false });
        return;
      }

      const { data } = await apiClient.get<Case[]>('/cases/', { signal });
      set({ cases: data, isLoading: false });
    } catch (err: any) {
      if (err.name === 'CanceledError' || err.message === 'canceled') return;
      set({ error: err.response?.data?.detail || 'Failed to fetch cases', isLoading: false });
    }
  },

  createCase: async (payload) => {
    try {
      const { data } = await apiClient.post<Case>('/cases/', payload);
      set({ cases: [data, ...get().cases] });
      return data;
    } catch (err: any) {
      set({ error: err.response?.data?.detail || 'Failed to create case' });
      throw err;
    }
  },

  updateCase: async (id, payload) => {
    try {
      const { data } = await apiClient.put<Case>(`/cases/${id}`, payload);
      set({
        cases: get().cases.map((c) => (c.case_id === id ? data : c)),
      });
    } catch (err: any) {
      set({ error: err.response?.data?.detail || 'Failed to update case' });
      throw err;
    }
  },

  deleteCase: async (id: string) => {
    try {
      await apiClient.delete(`/cases/${id}`);
      set({ cases: get().cases.filter((c) => c.case_id !== id) });
      if (get().activeCaseId === id) {
        set({ activeCaseId: null });
      }
    } catch (err: any) {
      set({ error: err.response?.data?.detail || 'Failed to delete case' });
      throw err;
    }
  },

  setActiveCase: (id: string | null) => {
    set({ activeCaseId: id });
  },
}));
