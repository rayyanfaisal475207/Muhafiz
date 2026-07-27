import React, { useEffect, useState } from 'react';
import { useProfileStore } from '../store/profileStore';
import type { UserContextProfile } from '../store/profileStore';
import { useAuthStore } from '../store/authStore';
import { useNavigate } from 'react-router-dom';

export const SettingsPage: React.FC = () => {
  const { user } = useAuthStore();
  const navigate = useNavigate();
  const { profile, isLoading, error, loadProfile, updateProfile } = useProfileStore();
  
  const [formData, setFormData] = useState<UserContextProfile>({
    context_text: '',
    preferred_language: 'auto',
    llm_mode: 'cloud'
  });
  const [saveSuccess, setSaveSuccess] = useState(false);

  useEffect(() => {
    if (!user) {
      navigate('/login');
      return;
    }
    loadProfile();
  }, [user, navigate, loadProfile]);

  useEffect(() => {
    if (profile) {
      setFormData(profile);
    }
  }, [profile]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
    setFormData({ ...formData, [e.target.name]: e.target.value });
    setSaveSuccess(false);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await updateProfile(formData);
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
    } catch (err) {
      // Error handled by store
    }
  };

  return (
    <div className="h-full overflow-y-auto bg-[var(--bg-base)]">
      <div className="max-w-3xl mx-auto p-8 pt-12">
        <h1 className="text-3xl font-bold text-[var(--text-primary)] mb-2">Profile & Settings</h1>
        <p className="text-[var(--text-secondary)] mb-4">
          Tell Muhafiz about your context so it can personalize its answers automatically.
        </p>
        <p className="text-sm text-[var(--text-muted)] mb-8 border-b border-[var(--border)] pb-4">
          Logged in as: <strong>{user?.email}</strong>
        </p>

        <form onSubmit={handleSubmit} className="bg-[var(--bg-surface)] rounded-xl shadow-sm border border-[var(--border)] overflow-hidden">
            <div className="p-6 space-y-6">
              
              {/* Context Text */}
              <div>
                <label htmlFor="context_text" className="block text-sm font-semibold text-[var(--text-primary)] mb-1">
                  Context
                </label>
                <p className="text-xs text-[var(--text-muted)] mb-3">
                  E.g., "I'm a duty officer at the Aabpara station, mainly handling FIR intake and traffic complaints."
                </p>
                <textarea
                  id="context_text"
                  name="context_text"
                  rows={4}
                  value={formData.context_text}
                  onChange={handleChange}
                  className="w-full rounded-md border border-[var(--border)] bg-[var(--bg-base)] text-[var(--text-primary)] px-3 py-2 text-sm focus:outline-none focus:border-[var(--accent)]"
                  placeholder="Enter your personal context here..."
                />
              </div>

              {/* Language */}
              <div>
                <label htmlFor="preferred_language" className="block text-sm font-semibold text-[var(--text-primary)] mb-1">
                  Preferred Language
                </label>
                <p className="text-xs text-[var(--text-muted)] mb-3">
                  "Auto" replies in whatever language you ask your question in (Urdu or English), regardless of what language the source documents are written in. Pick a fixed language only if you always want answers in that one language.
                </p>
                <select
                  id="preferred_language"
                  name="preferred_language"
                  value={formData.preferred_language}
                  onChange={handleChange}
                  className="w-full max-w-xs rounded-md border border-[var(--border)] bg-[var(--bg-base)] text-[var(--text-primary)] px-3 py-2 text-sm focus:outline-none focus:border-[var(--accent)]"
                >
                  <option value="auto">Auto (match my question)</option>
                  <option value="english">English (always)</option>
                  <option value="urdu">Urdu (always)</option>
                </select>
              </div>

              {/* LLM Mode */}
              <div>
                <label htmlFor="llm_mode" className="block text-sm font-semibold text-[var(--text-primary)] mb-1">
                  AI Model Mode
                </label>
                <select
                  id="llm_mode"
                  name="llm_mode"
                  value={formData.llm_mode}
                  onChange={handleChange}
                  className="w-full max-w-xs rounded-md border border-[var(--border)] bg-[var(--bg-base)] text-[var(--text-primary)] px-3 py-2 text-sm focus:outline-none focus:border-[var(--accent)]"
                >
                  <option value="cloud">Cloud (High Performance)</option>
                  <option value="local">Local / Private (Phase 9)</option>
                </select>
              </div>

            </div>

            <div className="bg-[var(--bg-surface-2)] px-6 py-4 border-t border-[var(--border)] flex items-center justify-between">
              <div className="text-sm">
                {error && <span className="text-[var(--error)]">{error}</span>}
                {saveSuccess && <span className="text-green-600 font-medium">Settings saved successfully!</span>}
              </div>
              <button
                type="submit"
                disabled={isLoading}
                className="btn-accent px-5 py-2 text-sm"
              >
                {isLoading ? 'Saving...' : 'Save Changes'}
              </button>
            </div>
          </form>
        </div>
    </div>
  );
};
