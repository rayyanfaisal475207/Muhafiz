import type { Source } from '../../types';
import { useModalA11y } from '../../hooks/useModalA11y';

interface CitationPanelProps {
  source: Source;
  onClose: () => void;
}

// A slide-in overlay, not a reserved layout column (Module 2 of
// FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md) — covers the right ~40% of
// the viewport on desktop, full width on narrow screens, without squeezing
// the chat's own width. Always mounted while a citation is active, so
// isOpen is unconditionally true here; ChatPage controls mount/unmount via
// `activeSource`.
export function CitationPanel({ source, onClose }: CitationPanelProps) {
  const dialogRef = useModalA11y(true, onClose);

  // source.type is the structural discriminator the orchestrator already
  // sets on every web source dict — sniffing the filename for a URL shape
  // was a heuristic stand-in that also misfired on a document whose
  // filename happens to look like a link. Case-evidence citations
  // (including cross-case ones) always carry a non-"web" type.
  const isWeb = source.type === 'web';

  return (
    <div
      className="fixed inset-0 z-50"
      style={{ background: 'rgba(0, 0, 0, 0.25)' }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="citation-panel-title"
        tabIndex={-1}
        className="flex flex-col h-full w-full sm:w-[420px] md:w-2/5 md:min-w-[380px] ml-auto animate-slide-in-right relative"
        style={{ background: 'var(--bg-surface-2)', boxShadow: 'var(--shadow-lg)' }}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
          <h3 id="citation-panel-title" className="text-sm font-semibold text-[var(--text-primary)] flex items-center gap-2">
            {isWeb ? '🌐 Web Source' : '📄 Document Citation'}
          </h3>
          <button onClick={onClose} aria-label="Close" className="p-1 rounded-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-surface-3)] transition-colors">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="p-4 flex-1 overflow-y-auto">
          <div className="mb-6">
            <div className="text-[11px] font-semibold text-[var(--text-faint)] mb-1 uppercase tracking-wider">Source Title</div>
            <div className="text-sm text-[var(--text-primary)] font-medium break-all">{source.filename}</div>
            {isWeb && (
              <a href={source.filename} target="_blank" rel="noreferrer" className="text-xs text-[var(--accent)] hover:underline underline-offset-2 mt-1 inline-block">
                Open Original URL ↗
              </a>
            )}
          </div>

          <div>
            <div className="text-[11px] font-semibold text-[var(--text-faint)] mb-2 uppercase tracking-wider">Extracted Context</div>
            <div className="p-3 rounded-sm" style={{ background: 'var(--bg-surface)', border: '1px solid var(--border)' }}>
              <p className="text-sm text-[var(--text-secondary)] whitespace-pre-wrap leading-relaxed">
                {(source as any).snippet || (source as any).content || 'No text snippet available.'}
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
