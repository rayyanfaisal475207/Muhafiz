// ============================================================
// ChatPage — full-width chat; a clicked citation opens as a slide-in
// overlay rather than reserving a permanent right column (Module 2 of
// FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md — the pipeline trace this
// column used to show permanently now lives inline per-message in
// GenerationStatus, see Module 1).
// ============================================================

import { useEffect, useState } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { ChatPanel } from '../components/chat/ChatPanel';
import { CitationPanel } from '../components/chat/CitationPanel';
import { useChatStore } from '../store/chatStore';
import type { Source } from '../types';

import { LAST_SESSION_KEY } from '../lib/constants';

export function ChatPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const { loadSession, newSession } = useChatStore();
  const [activeSource, setActiveSource] = useState<Source | null>(null);

  useEffect(() => {
    // Clear any open citation on every navigation into this page (session
    // switch, case/project switch, explicit New Chat) so cross-case evidence
    // never lingers on screen after the underlying conversation has changed.
    setActiveSource(null);

    if (id) {
      // Persist this session so a refresh on '/' can restore it
      localStorage.setItem(LAST_SESSION_KEY, id);
      loadSession(id);
    } else if ((location.state as { fresh?: boolean } | null)?.fresh) {
      // Explicit "New Chat" from the sidebar (or a case/project switch, which
      // reuses this same mechanism): the store was already reset by the
      // handler. Do NOT restore the last session — that bounce-back was
      // exactly why the sidebar button appeared to do nothing.
    } else {
      // No session in URL — try to restore the last active session
      const lastId = localStorage.getItem(LAST_SESSION_KEY);
      if (lastId) {
        navigate(`/chat/${lastId}`, { replace: true });
      } else {
        newSession();
      }
    }
  }, [id, location.key]);

  return (
    <div className="flex justify-center h-full py-6 px-6" style={{ background: 'var(--bg-base)' }}>
      <div
        className="flex w-full max-w-7xl h-full rounded-lg overflow-hidden relative"
        style={{
          background: 'var(--bg-surface)',
          border: '1px solid var(--border)',
          boxShadow: 'var(--shadow-md)',
        }}
      >
        <div className="flex flex-col flex-1 min-w-0">
          <ChatPanel onSourceClick={(s) => setActiveSource(s)} />
        </div>
      </div>

      {/* Citation viewer — an on-demand overlay, not a reserved column.
          Mounted only while a citation is active. */}
      {activeSource && (
        <CitationPanel source={activeSource} onClose={() => setActiveSource(null)} />
      )}
    </div>
  );
}
