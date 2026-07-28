// ============================================================
// ChatPage — two-column layout (60% chat, 40% pipeline)
// ============================================================

import { useEffect, useState } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { ChatPanel } from '../components/chat/ChatPanel';
import { PipelinePanel } from '../components/pipeline/PipelinePanel';
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

  // Layout is unchanged: chat on the left, pipeline/citation on the right.
  // Only the surface treatment changed — one soft shadow, warm border.
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
        {/* Left: Chat */}
        <div
          className="flex flex-col border-r"
          style={{ flex: '1 1 60%', minWidth: 0, borderColor: 'var(--border)' }}
        >
          <ChatPanel onSourceClick={(s) => setActiveSource(s)} />
        </div>

        {/* Right: Pipeline Trace or Citation */}
        <div
          className="flex flex-col"
          style={{ flex: '0 0 40%', minWidth: '350px', background: 'var(--bg-surface-2)' }}
        >
          {activeSource ? (
            <CitationPanel source={activeSource} onClose={() => setActiveSource(null)} />
          ) : (
            <PipelinePanel />
          )}
        </div>
      </div>
    </div>
  );
}
