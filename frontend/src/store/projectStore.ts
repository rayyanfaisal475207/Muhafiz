import { create } from 'zustand';
import { apiClient } from '../lib/api';
import { useAuthStore } from './authStore';

export interface Project {
  id: string;
  user_id: string;
  name: string;
  description?: string;
  domain_context?: string;
  created_at: string;
  updated_at: string;
}

interface ProjectState {
  projects: Project[];
  activeProjectId: string | null;
  isLoading: boolean;
  error: string | null;

  fetchProjects: (signal?: AbortSignal) => Promise<void>;
  createProject: (data: { name: string; description?: string; domain_context?: string }) => Promise<Project>;
  updateProject: (id: string, data: { name?: string; description?: string; domain_context?: string }) => Promise<void>;
  deleteProject: (id: string) => Promise<void>;
  setActiveProject: (id: string | null) => void;
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  projects: [],
  activeProjectId: null,
  isLoading: false,
  error: null,

  fetchProjects: async (signal?: AbortSignal) => {
    try {
      set({ isLoading: true, error: null });
      const user = useAuthStore.getState().user;
      if (!user) {
        set({ projects: [], isLoading: false });
        return;
      }
      
      const { data } = await apiClient.get<Project[]>('/projects/', { signal });
      set({ projects: data, isLoading: false });
    } catch (err: any) {
      if (err.name === 'CanceledError' || err.message === 'canceled') return;
      set({ error: err.response?.data?.detail || 'Failed to fetch projects', isLoading: false });
    }
  },

  createProject: async (payload) => {
    try {
      const user = useAuthStore.getState().user;
      if (!user) throw new Error("Unauthenticated");

      const { data } = await apiClient.post<Project>('/projects/', { ...payload, user_id: user.id });
      set({ projects: [...get().projects, data] });
      return data;
    } catch (err: any) {
      set({ error: err.response?.data?.detail || 'Failed to create project' });
      throw err;
    }
  },

  updateProject: async (id, payload) => {
    try {
      const { data } = await apiClient.put<Project>(`/projects/${id}`, payload);
      set({
        projects: get().projects.map((p) => (p.id === id ? data : p)),
      });
    } catch (err: any) {
      set({ error: err.response?.data?.detail || 'Failed to update project' });
      throw err;
    }
  },

  deleteProject: async (id: string) => {
    try {
      await apiClient.delete(`/projects/${id}`);
      set({ projects: get().projects.filter((p) => p.id !== id) });
      if (get().activeProjectId === id) {
        set({ activeProjectId: null });
      }
    } catch (err: any) {
      set({ error: err.response?.data?.detail || 'Failed to delete project' });
      throw err;
    }
  },

  setActiveProject: (id: string | null) => {
    set({ activeProjectId: id });
  }
}));
