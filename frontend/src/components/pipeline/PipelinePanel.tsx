// ============================================================
// PipelinePanel — right column: live pipeline trace
// ============================================================

import { useChatStore } from '../../store/chatStore';
import { PipelineStepCard } from './PipelineStepCard';
import { RetrievedDocsSection } from './RetrievedDocsSection';

export function PipelinePanel() {
  const { currentSteps, currentEvents, isStreaming } = useChatStore();

  const hasStarted = currentSteps.some((s) => s.status !== 'waiting');
  const retryEvents = currentEvents.filter(
    (e) => e.retry_num !== undefined && e.retry_num > 0,
  );
  const maxRetry = retryEvents.length > 0
    ? Math.max(...retryEvents.map((e) => e.retry_num ?? 0))
    : 0;

  return (
    <div
      className="flex flex-col h-full border-l"
      style={{
        borderColor: 'var(--border)',
        background: 'var(--bg-base)',
      }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-3 border-b shrink-0"
        style={{ borderColor: 'var(--border)' }}
      >
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-[var(--text-primary)]">
            Pipeline Trace
          </span>
          {isStreaming && (
            <span className="animate-pulse-dot w-1.5 h-1.5 rounded-full bg-accent inline-block" />
          )}
        </div>
        {maxRetry > 0 && (
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-warning/15 text-warning border border-warning/25 font-medium">
            {maxRetry} retry{maxRetry > 1 ? 's' : ''}
          </span>
        )}
      </div>

      {/* Steps */}
      <div className="flex-1 overflow-y-auto px-3 py-3">
        {!hasStarted ? (
          <IdleState />
        ) : (
          <div className="flex flex-col gap-1.5">
            {currentSteps.map((step) => (
              <PipelineStepCard key={step.name} step={step} />
            ))}

            {/* Retry indicator */}
            {maxRetry > 0 && (
              <div
                className="mt-1 px-3 py-2 rounded-sm border text-[11px]"
                style={{
                  background: 'var(--warning-soft)',
                  borderColor: 'color-mix(in srgb, var(--warning) 28%, transparent)',
                  color: 'var(--warning)',
                }}
              >
                <div className="font-medium mb-0.5">Retry loop triggered</div>
                <div style={{ color: 'var(--text-muted)' }}>
                  {maxRetry} attempt{maxRetry > 1 ? 's' : ''} — query improved from evaluator feedback
                </div>
              </div>
            )}

            {/* Retrieved docs section */}
            <RetrievedDocsSection events={currentEvents} />

            {/* Timing summary */}
            <TimingSummary events={currentEvents} />
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="px-4 py-2.5 border-t border-[var(--border)] shrink-0 bg-[var(--bg-surface)]">
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {[
            { color: 'bg-[var(--text-muted)]', label: 'Waiting' },
            { color: 'bg-accent', label: 'Active' },
            { color: 'bg-success', label: 'Done' },
            { color: 'bg-warning', label: 'Retry' },
            { color: 'bg-error', label: 'Error' },
          ].map((item) => (
            <div key={item.label} className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${item.color}`} />
              <span className="text-[10px] font-medium text-[var(--text-muted)]">{item.label}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function IdleState() {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-4 gap-4">
      <div className="w-12 h-12 rounded-xl border border-[var(--border)] bg-[var(--bg-surface)] shadow-sm flex items-center justify-center">
        <svg viewBox="0 0 24 24" fill="none" className="w-6 h-6 text-[var(--text-muted)]">
          <path d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </div>
      <div>
        <p className="text-xs font-semibold text-[var(--text-primary)]">Pipeline idle</p>
        <p className="text-[11px] text-[var(--text-muted)] mt-1">
          Steps animate here when you send a message
        </p>
      </div>
      <div className="w-full border-t border-[var(--border)]" />
      <div className="flex flex-col gap-2 w-full text-left">
        {['Query Rewriter', 'Router', 'Retrieval', 'Re-ranker', 'Evaluator', 'Response', 'Memory'].map((step) => (
          <div key={step} className="flex items-center gap-2 px-2 py-1.5 rounded-md border border-[var(--border)] bg-[var(--bg-surface)] shadow-sm">
            <span className="w-1.5 h-1.5 rounded-full bg-[var(--text-muted)]" />
            <span className="text-[11px] font-medium text-[var(--text-secondary)]">{step}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function TimingSummary({ events }: { events: import('../../types').PipelineEvent[] }) {
  const doneEvents = events.filter((e) => e.status === 'done' && e.ms !== undefined);
  if (doneEvents.length === 0) return null;

  const totalMs = doneEvents.reduce((sum, e) => sum + (e.ms ?? 0), 0);

  return (
    <div
      className="mt-2 px-3 py-2 rounded-lg text-[11px] border"
      style={{
        background: 'var(--bg-surface)',
        borderColor: 'var(--border)',
        color: 'var(--text-muted)',
      }}
    >
      <span className="font-medium text-[var(--text-secondary)]">Total pipeline: </span>
      {totalMs < 1000 ? `${totalMs}ms` : `${(totalMs / 1000).toFixed(1)}s`}
    </div>
  );
}
