// ============================================================
// ChatInput — composer: attach, textarea, send.
//
// The attach button adds a file to THIS conversation only (see
// AttachmentChips). It is not an ingestion pipeline: nothing here reaches the
// shared knowledge base, which only admins can add to.
// ============================================================

import { useState, useRef, useCallback } from 'react';
import { useChatStore } from '../../store/chatStore';
import { useCaseStore } from '../../store/caseStore';
import { useAuthStore } from '../../store/authStore';
import { ALL_CASES_ROLES } from '../../lib/constants';
import { AttachmentChips } from './AttachmentChips';

interface Props {
  onSend: (text: string) => void;
  onNewSession: () => void;
  disabled: boolean;
}

const ACCEPTED =
  '.pdf,.txt,.md,.csv,.xlsx,.xls,.html,.htm,.docx,.png,.jpg,.jpeg,.webp';

// Same three scopes ChatPanel's empty state reflects — the placeholder
// went stale the same way that landing copy did: a supervisor+ user with
// no case selected is in a real cross-case search scope now, not just a
// reference-lookup one, and this text never mentioned it.
function placeholderFor(hasActiveCase: boolean, hasAllCasesScope: boolean): string {
  if (hasActiveCase) return "Ask about this case's evidence, entities, or timeline…";
  if (hasAllCasesScope) return 'Ask about any case, connection, or SOP…';
  return 'Ask about any section, procedure, or SOP…';
}

export function ChatInput({ onSend, onNewSession, disabled }: Props) {
  const [text, setText] = useState('');
  const [isDraggingOver, setIsDraggingOver] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachFile = useChatStore((s) => s.attachFile);
  const webSearchEnabled = useChatStore((s) => s.webSearchEnabled);
  const setWebSearchEnabled = useChatStore((s) => s.setWebSearchEnabled);
  const hasActiveCase = useCaseStore((s) => !!s.activeCaseId);
  const role = useAuthStore((s) => s.user?.role);
  const placeholder = placeholderFor(hasActiveCase, !!role && ALL_CASES_ROLES.includes(role));

  const handleFiles = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      Array.from(files).forEach((file) => attachFile(file));
    },
    [attachFile],
  );

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDraggingOver(false);
    handleFiles(e.dataTransfer.files);
  };

  const handleSend = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText('');
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [text, disabled, onSend]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    // Auto-expand textarea
    const ta = e.target;
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
  };

  const canSend = !disabled && text.trim().length > 0;

  return (
    <div className="px-6 pb-5 pt-2" style={{ background: 'var(--bg-surface)' }}>
      <div className="max-w-[46rem] mx-auto w-full">
        {/* Files attached to this conversation */}
        <AttachmentChips />

      {/* The composer floats on the surface: one soft shadow, generous radius,
          navy ring only on focus. Dropping a file anywhere on it attaches it. */}
      <div
        onDragOver={(e) => { e.preventDefault(); setIsDraggingOver(true); }}
        onDragLeave={() => setIsDraggingOver(false)}
        onDrop={handleDrop}
        className="flex items-end gap-2 px-3 py-2.5 rounded-lg transition-shadow duration-150 focus-within:shadow-md"
        style={{
          background: isDraggingOver ? 'var(--accent-soft)' : 'var(--bg-surface)',
          border: `1px solid ${isDraggingOver ? 'var(--accent)' : 'var(--border-strong)'}`,
          boxShadow: 'var(--shadow-sm)',
        }}
      >
        {/* Attach a file to this conversation */}
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPTED}
          multiple
          className="hidden"
          onChange={(e) => {
            handleFiles(e.target.files);
            e.target.value = '';  // let the same file be picked twice
          }}
        />
        <button
          id="attach-btn"
          onClick={() => fileInputRef.current?.click()}
          title="Attach a file to this conversation"
          aria-label="Attach a file"
          className="shrink-0 w-8 h-8 rounded-sm flex items-center justify-center transition-colors text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)]"
        >
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
            <path d="M15.5 9.2 10 14.7a3.4 3.4 0 0 1-4.8-4.8l6-6a2.3 2.3 0 0 1 3.2 3.2l-6 6a1.1 1.1 0 0 1-1.6-1.6l5.4-5.4" />
          </svg>
        </button>

        {/* Explicit, per-message web-search opt-in. Never triggered
            automatically — only reachable this way or by the router
            classifying the question as general/off-corpus knowledge. */}
        <button
          id="web-search-toggle-btn"
          type="button"
          onClick={() => setWebSearchEnabled(!webSearchEnabled)}
          title={webSearchEnabled ? 'Web search on for your next message (click to turn off)' : 'Search the web for this question'}
          aria-pressed={webSearchEnabled}
          aria-label="Toggle web search for this message"
          className="shrink-0 w-8 h-8 rounded-sm flex items-center justify-center transition-colors"
          style={{
            color: webSearchEnabled ? 'var(--accent-on)' : 'var(--text-muted)',
            background: webSearchEnabled ? 'var(--accent)' : 'transparent',
          }}
        >
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
            <circle cx="10" cy="10" r="7" />
            <path d="M3 10h14M10 3a11 11 0 0 1 0 14M10 3a11 11 0 0 0 0 14" />
          </svg>
        </button>

        {/* New Session */}
        <button
          id="new-session-btn"
          onClick={onNewSession}
          title="Start a new conversation"
          className="shrink-0 w-8 h-8 rounded-sm flex items-center justify-center transition-colors text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--accent-soft)]"
        >
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" className="w-4 h-4">
            <path d="M10 4.5v11M4.5 10h11" />
          </svg>
        </button>

        {/* Textarea — never disabled: the user can compose their next question
            while the current answer is still streaming. */}
        <textarea
          ref={textareaRef}
          id="chat-input"
          value={text}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          rows={1}
          className="flex-1 bg-transparent text-[15px] text-[var(--text-primary)] placeholder-[var(--text-faint)] resize-none outline-none leading-relaxed py-1"
          style={{ maxHeight: '160px' }}
        />

        {/* Send */}
        <button
          id="send-btn"
          onClick={handleSend}
          disabled={!canSend}
          title={disabled ? 'Waiting for the current response' : 'Send'}
          className="shrink-0 w-8 h-8 rounded-sm flex items-center justify-center transition-all duration-150 disabled:cursor-not-allowed"
          style={{
            background: canSend ? 'var(--accent)' : 'var(--bg-surface-3)',
            color: canSend ? 'var(--accent-on)' : 'var(--text-faint)',
          }}
        >
          {disabled ? (
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} className="w-4 h-4">
              <circle cx="10" cy="10" r="6.5" opacity={0.3} />
              <circle cx="10" cy="10" r="2.6" fill="currentColor" stroke="none">
                <animate attributeName="r" values="2.6;4.2;2.6" dur="1.6s" repeatCount="indefinite" />
                <animate attributeName="opacity" values="1;0.4;1" dur="1.6s" repeatCount="indefinite" />
              </circle>
            </svg>
          ) : (
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
              <path d="M10 16V4.5" />
              <path d="m5.5 9 4.5-4.5L14.5 9" />
            </svg>
          )}
        </button>
      </div>

      <p className="text-center text-[11px] mt-2" style={{ color: 'var(--text-faint)' }}>
        <kbd className="px-1 py-0.5 rounded-xs text-[10px]" style={{ background: 'var(--bg-surface-3)', border: '1px solid var(--border)' }}>Enter</kbd> to send ·{' '}
        <kbd className="px-1 py-0.5 rounded-xs text-[10px]" style={{ background: 'var(--bg-surface-3)', border: '1px solid var(--border)' }}>Shift+Enter</kbd> for a new line ·
        attachments stay in this conversation
      </p>
      </div>
    </div>
  );
}
