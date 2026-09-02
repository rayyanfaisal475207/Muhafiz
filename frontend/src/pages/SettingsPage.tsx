import React, { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useProfileStore } from '../store/profileStore';
import type { UserContextProfile } from '../store/profileStore';
import { useAuthStore } from '../store/authStore';
import { useNavigate } from 'react-router-dom';
import { ReadIcon, GlobeIcon, SparkIcon, CheckIcon } from '../components/chat/StatusIcons';

// ============================================================
// SettingsPage — Module 7 of FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md.
//
// Decision recorded there before this file changed: Settings stays a
// single small form (three fields, no other account-level section planned
// anywhere in the roadmap) rather than growing into the reference
// screenshot's two-pane category/detail layout — that would be
// speculative structure for content that doesn't exist yet. This is a
// restyle of the existing three fields into the reference's per-item row
// shape (leading icon, label+description, trailing control), plus
// per-field save confirmation replacing the old page-level banner.
// ============================================================

const FIELD_ICON_WRAP = 'flex items-center justify-center w-8 h-8 rounded-sm shrink-0';

/** One row: icon, label (+ a "Saved" badge once this field's own save is
 * confirmed), description, and either a `trailing` control on the same
 * line (select dropdowns) or `children` full-width beneath the header
 * (the Context textarea, which doesn't fit a single-line trailing slot). */
function SettingsRow({
  icon, label, description, htmlFor, saved, trailing, children,
}: {
  icon: ReactNode;
  label: string;
  description?: string;
  htmlFor: string;
  saved: boolean;
  trailing?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="px-6 py-5" data-testid={`settings-row-${htmlFor}`}>
      <div className="flex items-start gap-3">
        <span className={FIELD_ICON_WRAP} style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}>
          {icon}
        </span>
        <div className="flex-1 min-w-0 flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <label htmlFor={htmlFor} className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
                {label}
              </label>
              {saved && (
                <span
                  className="inline-flex items-center gap-1 text-[11px] font-medium animate-fade-in"
                  style={{ color: 'var(--success)' }}
                >
                  <CheckIcon className="w-3 h-3" /> Saved
                </span>
              )}
            </div>
            {description && (
              <p className="text-xs mt-1 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                {description}
              </p>
            )}
          </div>
          {trailing && <div className="shrink-0 pt-0.5">{trailing}</div>}
        </div>
      </div>
      {children && <div className="mt-3 ml-11">{children}</div>}
    </div>
  );
}

const SELECT_CLASS =
  'w-full max-w-[220px] rounded-md border border-[var(--border)] bg-[var(--bg-base)] text-[var(--text-primary)] px-3 py-2 text-sm focus:outline-none focus:border-[var(--accent)]';

export const SettingsPage: React.FC = () => {
  const { user } = useAuthStore();
  const navigate = useNavigate();
  const { profile, isLoading, error, loadProfile, updateProfile } = useProfileStore();

  const [formData, setFormData] = useState<UserContextProfile>({
    context_text: '',
    preferred_language: 'auto',
    llm_mode: 'cloud'
  });
  // Which fields the most recent successful submit actually changed —
  // compared against the profile as it was BEFORE that submit, not just
  // "the submit succeeded" — so a save that touched only one field shows
  // confirmation on that one field, not a blanket page-level banner.
  const [savedFields, setSavedFields] = useState<Set<keyof UserContextProfile>>(new Set());

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
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
    // Editing a field again invalidates just that field's confirmation,
    // not the others still showing from the same submit.
    setSavedFields((prev) => {
      if (!prev.has(name as keyof UserContextProfile)) return prev;
      const next = new Set(prev);
      next.delete(name as keyof UserContextProfile);
      return next;
    });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // Snapshot the profile as it stood before this submit — updateProfile
    // below replaces the store's `profile` with the server's response, so
    // this is the only point the "before" values are still available to
    // diff against.
    const before = profile;
    try {
      await updateProfile(formData);
      const changed = new Set<keyof UserContextProfile>();
      (Object.keys(formData) as (keyof UserContextProfile)[]).forEach((key) => {
        if (!before || before[key] !== formData[key]) changed.add(key);
      });
      setSavedFields(changed);
      if (changed.size > 0) {
        setTimeout(() => setSavedFields(new Set()), 3000);
      }
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
          <div className="divide-y divide-[var(--border)]">

            <SettingsRow
              icon={<ReadIcon className="w-4 h-4" />}
              label="Context"
              description={'E.g., "I\'m a duty officer at the Aabpara station, mainly handling FIR intake and traffic complaints."'}
              htmlFor="context_text"
              saved={savedFields.has('context_text')}
            >
              <textarea
                id="context_text"
                name="context_text"
                rows={4}
                value={formData.context_text}
                onChange={handleChange}
                className="w-full rounded-md border border-[var(--border)] bg-[var(--bg-base)] text-[var(--text-primary)] px-3 py-2 text-sm focus:outline-none focus:border-[var(--accent)]"
                placeholder="Enter your personal context here..."
              />
            </SettingsRow>

            <SettingsRow
              icon={<GlobeIcon className="w-4 h-4" />}
              label="Preferred Language"
              description='"Auto" replies in whatever language you ask your question in (Urdu or English), regardless of what language the source documents are written in. Pick a fixed language only if you always want answers in that one language.'
              htmlFor="preferred_language"
              saved={savedFields.has('preferred_language')}
              trailing={
                <select
                  id="preferred_language"
                  name="preferred_language"
                  value={formData.preferred_language}
                  onChange={handleChange}
                  className={SELECT_CLASS}
                >
                  <option value="auto">Auto (match my question)</option>
                  <option value="english">English (always)</option>
                  <option value="urdu">Urdu (always)</option>
                </select>
              }
            />

            <SettingsRow
              icon={<SparkIcon className="w-4 h-4" />}
              label="AI Model Mode"
              htmlFor="llm_mode"
              saved={savedFields.has('llm_mode')}
              trailing={
                <select
                  id="llm_mode"
                  name="llm_mode"
                  value={formData.llm_mode}
                  onChange={handleChange}
                  className={SELECT_CLASS}
                >
                  <option value="cloud">Cloud (High Performance)</option>
                  <option value="local">Local / Private (Phase 9)</option>
                </select>
              }
            />

          </div>

          <div className="bg-[var(--bg-surface-2)] px-6 py-4 border-t border-[var(--border)] flex items-center justify-between">
            <div className="text-sm">
              {error && <span className="text-[var(--error)]">{error}</span>}
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
