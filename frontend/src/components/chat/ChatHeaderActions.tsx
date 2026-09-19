// ============================================================
// ChatHeaderActions — the chat header's right-hand icon rail.
//
// Two things live here, both moved out of the sidebar on purpose:
//
//  * Export. It used to be a hover button on every Chat History row,
//    sitting under the pointer while you were browsing conversations —
//    a navigation list is the wrong place for a destructive-feeling,
//    file-producing action, and it was easy to fire by accident. It now
//    acts on the conversation you are actually looking at.
//
//  * Account (settings, theme, sign out). These were pinned to the
//    sidebar footer, which left the sidebar carrying navigation, case
//    scope, history AND account controls at once. They are secondary
//    and belong behind one menu.
// ============================================================

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '../../store/authStore';
import { useChatStore } from '../../store/chatStore';
import { useSessionStore } from '../../store/sessionStore';
import { useThemeStore } from '../../store/themeStore';
import { apiClient } from '../../lib/api';

function DownloadIcon() {
  return (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 6h16M4 12h16M4 18h16" />
    </svg>
  );
}

const itemClass =
  'flex items-center w-full gap-2.5 px-3 py-2 text-sm text-left rounded-sm transition-colors text-[var(--text-secondary)] hover:bg-[var(--bg-surface-3)] hover:text-[var(--text-primary)]';

export function ChatHeaderActions() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  const logout = useAuthStore((s) => s.logout);
  const email = useAuthStore((s) => s.user?.email);
  const sessionId = useChatStore((s) => s.sessionId);
  const messages = useChatStore((s) => s.messages);
  const sessions = useSessionStore((s) => s.sessions);
  const theme = useThemeStore((s) => s.theme);
  const toggleTheme = useThemeStore((s) => s.toggle);
  const isDark = theme === 'dark';

  // Nothing to export until the conversation actually exists server-side.
  const canExport = messages.length > 0;
  const title = sessions.find((s) => s.session_id === sessionId)?.title || 'chat-export';

  useEffect(() => {
    if (!error) return;
    const t = setTimeout(() => setError(null), 5000);
    return () => clearTimeout(t);
  }, [error]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const handleExport = async () => {
    if (!canExport || busy) return;
    setBusy(true);
    try {
      const res = await apiClient.get(`/sessions/${sessionId}/export?format=pdf`, {
        responseType: 'blob',
      });
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', `${title}.pdf`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } catch {
      setError('Failed to export this chat.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={containerRef} className="relative flex items-center gap-1">
      {error && (
        <span className="text-[11px] mr-1" style={{ color: 'var(--error)' }} role="alert">
          {error}
        </span>
      )}

      <button
        onClick={handleExport}
        disabled={!canExport || busy}
        title={canExport ? 'Export this chat as PDF' : 'Nothing to export yet'}
        aria-label="Export this chat as PDF"
        className="p-1.5 rounded-sm transition-colors text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)] disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:text-[var(--text-muted)] disabled:hover:bg-transparent"
      >
        <DownloadIcon />
      </button>

      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Menu"
        aria-label="Menu"
        className="p-1.5 rounded-sm transition-colors text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)]"
      >
        <MenuIcon />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Account"
          className="absolute right-0 top-full mt-2 w-56 p-1.5 rounded-sm border shadow-lg z-30"
          style={{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }}
        >
          {email && (
            <div
              className="px-3 py-2 text-[11px] truncate border-b mb-1"
              style={{ color: 'var(--text-faint)', borderColor: 'var(--border)' }}
            >
              {email}
            </div>
          )}

          <button
            role="menuitem"
            className={itemClass}
            onClick={() => {
              setOpen(false);
              navigate('/settings');
            }}
          >
            <svg className="w-4 h-4 opacity-70" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
            </svg>
            Profile &amp; Settings
          </button>

          <button role="menuitem" className={itemClass} onClick={toggleTheme}>
            {isDark ? (
              <svg className="w-4 h-4 opacity-70" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
              </svg>
            ) : (
              <svg className="w-4 h-4 opacity-70" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
              </svg>
            )}
            {isDark ? 'Light mode' : 'Dark mode'}
          </button>

          <button
            role="menuitem"
            className="flex items-center w-full gap-2.5 px-3 py-2 text-sm text-left rounded-sm transition-colors text-[var(--text-secondary)] hover:bg-[var(--error-soft)] hover:text-[var(--error)]"
            onClick={() => {
              setOpen(false);
              logout();
            }}
          >
            <svg className="w-4 h-4 opacity-70" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
            </svg>
            Sign Out
          </button>
        </div>
      )}
    </div>
  );
}
