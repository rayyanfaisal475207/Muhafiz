import { useState, useEffect } from 'react';
import { NavLink, useNavigate, useParams } from 'react-router-dom';
import { useAuthStore } from '../../store/authStore';
import { useProjectStore } from '../../store/projectStore';
import { ProjectSettingsModal } from './ProjectSettingsModal';
import { useCaseStore } from '../../store/caseStore';
import { CaseSettingsModal } from './CaseSettingsModal';
import { CaseSelector } from './CaseSelector';
import { useSessionStore } from '../../store/sessionStore';
import { useChatStore } from '../../store/chatStore';
import { ThemeToggle } from './ThemeToggle';
import { apiClient } from '../../lib/api';
import { LAST_SESSION_KEY, SIDEBAR_COLLAPSED_KEY, ALL_CASES_ROLES } from '../../lib/constants';
import { LogoLockup, LogoMark } from '../brand/Logo';

// New Chat icon, kept alongside the (now button) control below.
const NewChatIcon = (
  <svg viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5">
    <path d="M2 5a2 2 0 012-2h7a2 2 0 012 2v4a2 2 0 01-2 2H9l-3 3v-3H4a2 2 0 01-2-2V5z" />
    <path d="M15 7v2a4 4 0 01-4 4H9.828l-1.766 1.767c.28.149.599.233.938.233h2l3 3v-3h2a2 2 0 002-2V9a2 2 0 00-2-2h-1z" />
  </svg>
);

// Points left when the sidebar is expanded (click to collapse) and right
// when collapsed (click to expand) — a single chevron, no separate icon set.
function CollapseToggleIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      className={`w-4 h-4 transition-transform duration-200 ${collapsed ? 'rotate-180' : ''}`}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 19l-7-7 7-7" />
    </svg>
  );
}

const navItems = [
  {
    to: '/settings',
    label: 'Profile & Settings',
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5">
        <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
      </svg>
    ),
  },
];

function groupSessions(sessions: any[]) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const lastWeek = new Date(today);
  lastWeek.setDate(lastWeek.getDate() - 7);

  const groups: Record<string, any[]> = {
    'Today': [],
    'Yesterday': [],
    'Previous 7 Days': [],
    'Older': []
  };

  sessions.forEach(s => {
    const d = new Date(s.updated_at || s.created_at);
    if (d >= today) groups['Today'].push(s);
    else if (d >= yesterday) groups['Yesterday'].push(s);
    else if (d >= lastWeek) groups['Previous 7 Days'].push(s);
    else groups['Older'].push(s);
  });

  return groups;
}

export function Sidebar() {
  const { logout, isAuthenticated, user } = useAuthStore();
  const { sessions, deleteSession, renameSession, error: sessionsError, isLoading: sessionsLoading } = useSessionStore();
  const newSession = useChatStore((s) => s.newSession);
  const { projects, activeProjectId, fetchProjects, setActiveProject, error: projectsError, isLoading: projectsLoading } = useProjectStore();
  const { cases, activeCaseId, fetchCases, setActiveCase, error: casesError, isLoading: casesLoading } = useCaseStore();
  const [isProjectModalOpen, setIsProjectModalOpen] = useState(false);
  const [isCaseModalOpen, setIsCaseModalOpen] = useState(false);
  const navigate = useNavigate();
  const { id: currentSessionId } = useParams();

  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');

  // Collapse/expand — a per-browser preference, same plain-localStorage-key
  // pattern as LAST_SESSION_KEY (read once on mount, written on toggle).
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true';
    } catch {
      return false;
    }
  });
  const toggleCollapsed = () => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next));
      } catch {
        // Storage unavailable (private mode, quota) — the toggle still
        // works for this session, it just won't survive a reload.
      }
      return next;
    });
  };

  // Delete/rename/export failures used to be swallowed with only
  // console.error — nothing told the user the action didn't work.
  const [actionError, setActionError] = useState<string | null>(null);
  useEffect(() => {
    if (!actionError) return;
    const t = setTimeout(() => setActionError(null), 5000);
    return () => clearTimeout(t);
  }, [actionError]);

  useEffect(() => {
    if (!isAuthenticated) return;
    const controller = new AbortController();
    fetchProjects(controller.signal);
    return () => controller.abort();
  }, [fetchProjects, isAuthenticated]);

  useEffect(() => {
    if (!isAuthenticated) return;
    const controller = new AbortController();
    fetchCases(controller.signal);
    return () => controller.abort();
  }, [fetchCases, isAuthenticated]);

  useEffect(() => {
    if (!isAuthenticated) return;
    const controller = new AbortController();
    useSessionStore.getState().fetchSessions(controller.signal);
    return () => controller.abort();
  }, [activeProjectId, activeCaseId, isAuthenticated]);


  const groups = groupSessions(sessions);

  // Mirror the composer's "+" button: reset the chat store to a fresh session,
  // then land on a clean '/'. The `fresh` flag stops ChatPage from restoring
  // the last session (the bounce-back that made this button appear dead).
  const handleNewChat = () => {
    newSession();
    navigate('/', { state: { fresh: true } });
  };

  const handleDelete = async (id: string) => {
    try {
      setDeleteConfirmId(null);
      await deleteSession(id);
      if (currentSessionId === id) {
        // Clear localStorage so refresh doesn't try to restore deleted session
        if (localStorage.getItem(LAST_SESSION_KEY) === id) {
          localStorage.removeItem(LAST_SESSION_KEY);
        }
        navigate('/', { replace: true });
      }
    } catch (e) {
      console.error(e);
      setActionError('Failed to delete session. Please try again.');
    }
  };

  const handleRename = async (id: string, oldTitle: string) => {
    if (editTitle.trim() && editTitle.trim() !== oldTitle) {
      try {
        await renameSession(id, editTitle.trim());
      } catch (e) {
        console.error(e);
        setActionError('Failed to rename session. Please try again.');
      }
    }
    setEditingId(null);
  };

  const handleDownload = async (id: string, title: string) => {
    try {
      const res = await apiClient.get(`/sessions/${id}/export?format=pdf`, { responseType: 'blob' });
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', `${title || 'chat-export'}.pdf`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } catch (e) {
      console.error('Export failed', e);
      setActionError('Failed to export session. Please try again.');
    }
  };

  return (
    <>
    <aside
      className={`flex flex-col h-full bg-[var(--bg-surface-2)] border-r border-[var(--border)] py-4 shrink-0 z-10 transition-[width] duration-200 ${collapsed ? 'w-14' : 'w-64'}`}
    >
      {/* Logo + collapse toggle */}
      <div className={`flex items-center mb-6 ${collapsed ? 'flex-col gap-3 px-2' : 'justify-between px-4'}`}>
        {collapsed ? <LogoMark className="w-7 h-7" /> : <LogoLockup />}
        <button
          onClick={toggleCollapsed}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className="p-1.5 rounded-xs text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)] transition-colors"
        >
          <CollapseToggleIcon collapsed={collapsed} />
        </button>
      </div>

      {/* Workspace and Case selectors need label/search text to be usable
          at all (unlike a static nav icon) — they hide entirely when
          collapsed rather than shrinking to a broken icon form. One click
          on the toggle above always brings them back. */}
      {!collapsed && (
      <>
      {/* Project Selector */}
      <div className="px-4 mb-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Workspace</span>
          <button onClick={() => setIsProjectModalOpen(true)} title="New project" aria-label="New project" className="p-1 rounded-xs text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)] transition-colors">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 4v16m8-8H4" /></svg>
          </button>
        </div>
        <select
          aria-label="Workspace"
          className="w-full bg-[var(--bg-surface)] border border-[var(--border)] rounded-sm px-2.5 py-1.5 text-sm text-[var(--text-primary)] transition-colors hover:border-[var(--border-hover)] focus:outline-none focus:border-[var(--accent)]"
          value={activeProjectId || ''}
          onChange={(e) => {
            setActiveProject(e.target.value || null);
            newSession();
            navigate('/', { state: { fresh: true } });
          }}
        >
          <option value="">Global (All Projects)</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        {projectsLoading && projects.length === 0 && !projectsError && (
          <p className="mt-1 text-[10.5px]" style={{ color: 'var(--text-faint)' }}>Loading workspaces…</p>
        )}
        {projectsError && (
          <p className="mt-1 text-[11px]" style={{ color: 'var(--error)' }} role="alert">Failed to load workspaces: {projectsError}</p>
        )}
        <p className="mt-1 text-[10.5px] leading-snug" style={{ color: 'var(--text-faint)' }}>
          Personal workspace — notes &amp; general chats, not tied to an investigation.
        </p>
      </div>

      {/* Case Selector */}
      <div className="px-4 mb-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Case</span>
          <button onClick={() => setIsCaseModalOpen(true)} title="New case" aria-label="New case" className="p-1 rounded-xs text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)] transition-colors">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 4v16m8-8H4" /></svg>
          </button>
        </div>
        <CaseSelector
          cases={cases}
          activeCaseId={activeCaseId}
          allCasesLabel={ALL_CASES_ROLES.includes(user?.role || '') ? 'All Cases' : 'No Case'}
          onSelect={(caseId) => {
            setActiveCase(caseId);
            newSession();
            navigate('/', { state: { fresh: true } });
          }}
        />
        {casesLoading && cases.length === 0 && !casesError && (
          <p className="mt-1 text-[10.5px]" style={{ color: 'var(--text-faint)' }}>Loading cases…</p>
        )}
        {casesError && (
          <p className="mt-1 text-[11px]" style={{ color: 'var(--error)' }} role="alert">Failed to load cases: {casesError}</p>
        )}
        <p className="mt-1 text-[10.5px] leading-snug" style={{ color: 'var(--text-faint)' }}>
          {activeCaseId
            ? 'Formal investigation — evidence & entities scoped to this case.'
            : ALL_CASES_ROLES.includes(user?.role || '')
              ? 'Searches every case’s evidence plus general reference material.'
              : 'General reference material only — select a case to search its evidence.'}
        </p>
      </div>
      </>
      )}

      {/* Main Nav */}

      <nav className={`flex flex-col gap-1 mb-6 ${collapsed ? 'px-2 items-center' : 'px-3'}`}>
        <button
          onClick={handleNewChat}
          title="New Chat"
          aria-label="New Chat"
          className={
            collapsed
              ? 'flex items-center justify-center w-9 h-9 rounded-sm transition-colors text-[var(--text-secondary)] hover:bg-[var(--bg-surface-3)] hover:text-[var(--text-primary)]'
              : 'flex items-center px-3 py-2 rounded-sm transition-colors text-sm font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-surface-3)] hover:text-[var(--text-primary)]'
          }
        >
          <span className={collapsed ? '' : 'mr-3 opacity-70'}>{NewChatIcon}</span>
          {!collapsed && 'New Chat'}
        </button>
      </nav>

      {/* Chat History — hidden collapsed, same reasoning as the Workspace/
          Case selectors above: session titles need to be readable to be
          useful, an icon can't stand in for them. */}
      {!collapsed && (
      <div className="flex-1 overflow-y-auto px-3">
        <div className="text-[11px] font-semibold uppercase tracking-wider mb-2 pl-3" style={{ color: 'var(--text-faint)' }}>
          Chat History
        </div>

        {actionError && (
          <div
            className="mx-1 mb-2 flex items-center justify-between gap-2 px-3 py-2 rounded-sm text-[12px]"
            style={{
              background: 'var(--error-soft)',
              border: '1px solid color-mix(in srgb, var(--error) 30%, transparent)',
              color: 'var(--text-secondary)',
            }}
            role="alert"
          >
            <span className="min-w-0 truncate">{actionError}</span>
            <button
              onClick={() => setActionError(null)}
              className="shrink-0 px-1 text-[var(--text-muted)] hover:text-[var(--text-primary)]"
              aria-label="Dismiss error"
            >
              ✕
            </button>
          </div>
        )}

        {sessionsError && (
          <p className="mb-2 pl-3 text-[11px]" style={{ color: 'var(--error)' }} role="alert">
            Failed to load chat history: {sessionsError}
          </p>
        )}
        {sessionsLoading && sessions.length === 0 && !sessionsError && (
          <p className="mb-2 pl-3 text-[11px]" style={{ color: 'var(--text-faint)' }}>Loading chat history…</p>
        )}

        {Object.entries(groups).map(([label, items]) => (
          items.length > 0 && (
            <div key={label} className="mb-4">
              <div className="text-[10px] font-medium mb-1 pl-3" style={{ color: 'var(--text-faint)' }}>{label}</div>
              {items.map(s => {
                const isActive = currentSessionId === s.session_id;
                return (
                  <div key={s.session_id} className="relative group flex flex-col">
                    <div 
                      className={`flex items-center px-3 py-2 rounded-sm text-sm cursor-pointer transition-colors ${
                        isActive
                          ? 'bg-[var(--accent-soft)] text-[var(--accent)] font-medium'
                          : 'text-[var(--text-secondary)] hover:bg-[var(--bg-surface-3)] hover:text-[var(--text-primary)]'
                      }`}
                      onClick={() => {
                        localStorage.setItem(LAST_SESSION_KEY, s.session_id);
                        navigate(`/chat/${s.session_id}`);
                      }}
                    >
                      {editingId === s.session_id ? (
                        <input
                          type="text"
                          className="flex-1 bg-[var(--bg-surface)] border border-[var(--accent)] outline-none text-[var(--text-primary)] px-1.5 py-0.5 text-sm rounded-xs"
                          value={editTitle}
                          onChange={(e) => setEditTitle(e.target.value)}
                          onBlur={() => handleRename(s.session_id, s.title)}
                          onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }}
                          autoFocus
                          onClick={(e) => e.stopPropagation()}
                        />
                      ) : (
                        <div className="truncate flex-1 pr-8">{s.title || 'New Chat'}</div>
                      )}

                      {/* Action Icons */}
                      {!editingId && (
                        <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center opacity-0 group-hover:opacity-100 transition-opacity gap-1">
                          <button
                            className="p-1 rounded-xs text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)] transition-colors"
                            title="Export as PDF"
                            aria-label="Export as PDF"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDownload(s.session_id, s.title);
                            }}
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
                          </button>
                          <button
                            className="p-1 rounded-xs text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)] transition-colors"
                            title="Rename"
                            aria-label="Rename session"
                            onClick={(e) => {
                              e.stopPropagation();
                              setEditTitle(s.title);
                              setEditingId(s.session_id);
                            }}
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"></path></svg>
                          </button>
                          <button
                            className="p-1 rounded-xs text-[var(--text-muted)] hover:text-[var(--error)] hover:bg-[var(--error-soft)] transition-colors"
                            title="Delete session"
                            aria-label="Delete session"
                            onClick={(e) => {
                              e.stopPropagation();
                              setDeleteConfirmId(s.session_id);
                            }}
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                          </button>
                        </div>
                      )}
                    </div>
                    
                    {/* Delete Confirmation */}
                    {deleteConfirmId === s.session_id && (
                      <div className="px-3 py-2 rounded-sm mt-1 mx-2 flex flex-col gap-2" style={{ background: 'var(--error-soft)', border: '1px solid color-mix(in srgb, var(--error) 28%, transparent)' }}>
                        <span className="text-xs" style={{ color: 'var(--text-secondary)' }}>Delete this session?</span>
                        <div className="flex gap-2">
                          <button 
                            className="text-xs px-2.5 py-1 rounded-xs font-medium text-white transition-opacity hover:opacity-90" style={{ background: 'var(--error)' }}
                            onClick={(e) => { e.stopPropagation(); handleDelete(s.session_id); }}
                          >Confirm</button>
                          <button 
                            className="text-xs px-2.5 py-1 rounded-xs font-medium transition-colors" style={{ background: 'var(--bg-surface-3)', color: 'var(--text-secondary)' }}
                            onClick={(e) => { e.stopPropagation(); setDeleteConfirmId(null); }}
                          >Cancel</button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )
        ))}
      </div>
      )}
      {collapsed && <div className="flex-1" />}

      {/* Account — Settings, theme, sign out grouped together at the bottom */}
      <div className={`mt-auto pt-4 border-t border-[var(--border)] flex flex-col gap-1 ${collapsed ? 'px-2 items-center' : 'px-4'}`}>
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            title={item.label}
            aria-label={item.label}
            className={({ isActive }) =>
              collapsed
                ? `flex items-center justify-center w-9 h-9 rounded-sm transition-colors ${
                    isActive
                      ? 'bg-[var(--accent-soft)] text-[var(--accent)]'
                      : 'text-[var(--text-secondary)] hover:bg-[var(--bg-surface-3)] hover:text-[var(--text-primary)]'
                  }`
                : `flex items-center px-3 py-2 rounded-sm transition-colors text-sm font-medium ${
                    isActive
                      ? 'bg-[var(--accent-soft)] text-[var(--accent)]'
                      : 'text-[var(--text-secondary)] hover:bg-[var(--bg-surface-3)] hover:text-[var(--text-primary)]'
                  }`
            }
          >
            <span className={collapsed ? '' : 'mr-3 opacity-70'}>{item.icon}</span>
            {!collapsed && item.label}
          </NavLink>
        ))}
        <ThemeToggle collapsed={collapsed} />
        <button
          onClick={() => logout()}
          title="Sign Out"
          aria-label="Sign Out"
          className={
            collapsed
              ? 'flex items-center justify-center w-9 h-9 text-sm font-medium rounded-sm text-[var(--text-secondary)] hover:bg-[var(--bg-surface-3)] hover:text-[var(--text-primary)] transition-colors'
              : 'flex items-center w-full px-3 py-2 text-sm font-medium rounded-sm text-[var(--text-secondary)] hover:bg-[var(--bg-surface-3)] hover:text-[var(--text-primary)] transition-colors'
          }
        >
          <svg className={collapsed ? 'w-4 h-4' : 'w-4 h-4 mr-3'} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"></path>
          </svg>
          {!collapsed && 'Sign Out'}
        </button>
      </div>
    </aside>
    <ProjectSettingsModal isOpen={isProjectModalOpen} onClose={() => setIsProjectModalOpen(false)} editProject={null} />
    <CaseSettingsModal isOpen={isCaseModalOpen} onClose={() => setIsCaseModalOpen(false)} />
    </>
  );
}
