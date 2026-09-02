// ============================================================
// ChatPanel — left column: message list + input
// ============================================================

import { useEffect, useRef } from 'react';
import { useChatStore } from '../../store/chatStore';
import { useCaseStore } from '../../store/caseStore';
import { useAuthStore } from '../../store/authStore';
import { MessageBubble } from './MessageBubble';
import { ChatInput } from './ChatInput';
import { LogoMark } from '../brand/Logo';
import type { Source } from '../../types';
import { ALL_CASES_ROLES } from '../../lib/constants';

const GENERAL_SUGGESTIONS = [
  'What PPC section covers mobile phone theft?',
  'What documents are needed to file an FIR?',
  'What section applies to unlicensed weapon possession?',
  'What is the procedure for a certified copy of an FIR?',
];

// "All Cases" (supervisor+, no case selected — orchestrator.py's
// _build_retrieval_where) searches every case's evidence plus general
// reference material, and can reach cross-case tools like XGRAPH/XAGG.
// GENERAL_SUGGESTIONS alone (reference-lookup-only) undersold that scope
// entirely — a supervisor landing here with no case selected saw the same
// "ask about police procedure" framing an investigator with no cross-case
// access at all sees, with nothing suggesting the cross-case capability
// that's the actual point of being in this scope.
const ALL_CASES_SUGGESTIONS = [
  'Which cases is a named suspect connected to across the database?',
  'Has this phone number or vehicle appeared in more than one case?',
  'What PPC section covers mobile phone theft?',
  'How many cases were opened at a given police station this year?',
];

const CASE_SUGGESTIONS = [
  'Who is named as the accused in this case?',
  'Summarize the key evidence in this case',
  'What is the current status of this case?',
  'Who are the witnesses in this case, and how are they connected?',
];

const CASE_SUPERVISOR_SUGGESTION =
  'Has any person, vehicle, or phone number in this case appeared in another case?';

interface ChatPanelProps {
  onSourceClick?: (source: Source) => void;
}

export function ChatPanel({ onSourceClick }: ChatPanelProps) {
  const { messages, isStreaming, sendMessage, newSession, error, clearError } = useChatStore();
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  // Track whether the user is near the bottom; if they scrolled up to read
  // something, streaming tokens must not yank them back down.
  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const lastMessage = messages[messages.length - 1];
  useEffect(() => {
    if (!stickToBottom.current) return;
    // 'auto' during streaming — stacked smooth-scroll animations fight each
    // other on every token and cause visible jitter.
    bottomRef.current?.scrollIntoView({ behavior: isStreaming ? 'auto' : 'smooth' });
  }, [messages.length, lastMessage?.content, isStreaming]);

  return (
    <div className="flex flex-col h-full" style={{ background: 'var(--bg-surface)' }}>
      {/* Header */}
      <div
        className="flex items-center justify-between px-6 py-3 border-b shrink-0"
        style={{ borderColor: 'var(--border)' }}
      >
        <div className="flex items-center gap-2">
          <h1 className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>Chat</h1>
        </div>
        {isStreaming && (
          <div className="flex items-center gap-1.5 text-[11px]" style={{ color: 'var(--accent)' }}>
            <span
              className="animate-pulse-dot w-1.5 h-1.5 rounded-pill inline-block"
              style={{ background: 'var(--accent)' }}
            />
            Responding…
          </div>
        )}
      </div>

      {/* Messages — a comfortable measure, centered, like a document */}
      <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-6 py-8">
        {messages.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="flex flex-col gap-7 max-w-[46rem] mx-auto w-full">
            {messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} onSourceClick={onSourceClick} />
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {/* Error banner — the store recorded errors but nothing displayed them */}
      {error && (
        <div
          className="mx-6 mb-2 flex items-center justify-between gap-3 px-4 py-2.5 rounded-sm text-[13px]"
          style={{
            background: 'var(--error-soft)',
            border: '1px solid color-mix(in srgb, var(--error) 30%, transparent)',
            color: 'var(--text-secondary)',
          }}
          role="alert"
        >
          <span className="min-w-0 truncate">
            <strong className="font-semibold" style={{ color: 'var(--error)' }}>Error:</strong>{' '}
            {error}
          </span>
          <button
            onClick={clearError}
            className="shrink-0 px-1 text-[var(--text-muted)] hover:text-[var(--text-primary)]"
            aria-label="Dismiss error"
          >
            ✕
          </button>
        </div>
      )}

      {/* Input */}
      <ChatInput
        onSend={sendMessage}
        onNewSession={newSession}
        disabled={isStreaming}
      />
    </div>
  );
}

function EmptyState() {
  const activeCase = useCaseStore((s) => s.cases.find((c) => c.case_id === s.activeCaseId));
  const role = useAuthStore((s) => s.user?.role);
  const hasAllCasesScope = !!role && ALL_CASES_ROLES.includes(role);

  const suggestions = activeCase
    ? [
        ...CASE_SUGGESTIONS,
        ...(hasAllCasesScope ? [CASE_SUPERVISOR_SUGGESTION] : []),
      ]
    : hasAllCasesScope
      ? ALL_CASES_SUGGESTIONS
      : GENERAL_SUGGESTIONS;

  const heading = activeCase
    ? `Ask about ${activeCase.fir_number || activeCase.case_id}`
    : hasAllCasesScope
      ? 'Ask across every case'
      : 'Ask about police procedure';

  const subtext = activeCase
    ? "Muhafiz searches this case's evidence, checks the entity graph, and answers with the source it came from."
    : hasAllCasesScope
      ? 'Muhafiz searches every case’s evidence and the general reference material, following connections across cases, and answers with the source it came from.'
      : 'Muhafiz searches the reference material, checks its own sources, and answers with the section it came from.';

  return (
    <div className="flex flex-col items-center justify-center h-full gap-7 text-center px-8">
      <LogoMark className="w-12 h-12" />

      <div className="flex flex-col gap-2">
        <h2
          className="text-[22px] font-semibold tracking-[-0.02em]"
          style={{ color: 'var(--text-primary)' }}
        >
          {heading}
        </h2>
        <p
          className="text-sm max-w-sm leading-relaxed mx-auto"
          style={{ color: 'var(--text-muted)' }}
        >
          {subtext}
        </p>
      </div>

      <div className="grid grid-cols-1 gap-2 w-full max-w-md">
        {suggestions.map((suggestion) => (
          <button
            key={suggestion}
            className="group flex items-center justify-between gap-3 text-left text-[14px] px-4 py-3 rounded-sm transition-all duration-150 hover-glow"
            style={{
              background: 'var(--bg-surface)',
              border: '1px solid var(--border)',
              color: 'var(--text-secondary)',
            }}
            onClick={() => {
              useChatStore.getState().sendMessage(suggestion);
            }}
          >
            <span>{suggestion}</span>
            <svg
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.6}
              strokeLinecap="round"
              strokeLinejoin="round"
              className="w-4 h-4 shrink-0 opacity-0 -translate-x-1 transition-all duration-150 group-hover:opacity-100 group-hover:translate-x-0"
              style={{ color: 'var(--accent)' }}
            >
              <path d="M4 10h11" />
              <path d="m10.5 5.5 4.5 4.5-4.5 4.5" />
            </svg>
          </button>
        ))}
      </div>
    </div>
  );
}
