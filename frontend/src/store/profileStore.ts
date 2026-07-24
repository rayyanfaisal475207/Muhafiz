import { create } from 'zustand';
import { apiClient } from '../lib/api';

export interface UserContextProfile {
  context_text: string;
  preferred_language: string;
  llm_mode: string;
}

interface ProfileState {
  profile: UserContextProfile | null;
  isLoading: boolean;
  error: string | null;
  clearError: () => void;
  loadProfile: () => Promise<void>;
  updateProfile: (data: UserContextProfile) => Promise<void>;
}

export const useProfileStore = create<ProfileState>((set) => ({
  profile: null,
  isLoading: false,
  error: null,
  
  clearError: () => set({ error: null }),
  
  loadProfile: async () => {
    set({ isLoading: true, error: null });
    try {
      const { data } = await apiClient.get<UserContextProfile>('/profile');
      set({ profile: data, isLoading: false });
    } catch (err: any) {
      set({ 
        error: err.response?.data?.detail || 'Failed to load profile',
        isLoading: false 
      });
    }
  },
  
  updateProfile: async (profileData) => {
    set({ isLoading: true, error: null });
    try {
      const { data } = await apiClient.put<UserContextProfile>('/profile', profileData);
      set({ profile: data, isLoading: false });
    } catch (err: any) {
      set({ 
        error: err.response?.data?.detail || 'Failed to update profile',
        isLoading: false 
      });
      throw err;
    }
  }
}));
