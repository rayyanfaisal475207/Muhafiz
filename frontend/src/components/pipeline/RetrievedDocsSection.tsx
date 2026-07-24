// ============================================================
// RetrievedDocsSection — shows retrieved document chips
// ============================================================

import type { PipelineEvent } from '../../types';

interface Props {
  events: PipelineEvent[];
}

export function RetrievedDocsSection({ events }: Props) {
  // Find retrieval event
  const retrievalEvent = events.find(
    (e) => e.step === 'retrieval' && e.status === 'done',
  );
  const rerankerEvent = events.find(
    (e) => e.step === 'reranker' && e.status === 'done',
  );
  const evaluatorEvent = events.find(
    (e) => e.step === 'evaluator' && e.status === 'done',
  );

  if (!retrievalEvent && !rerankerEvent) return null;

  const isRelevant = evaluatorEvent?.detail?.toLowerCase().includes('relevant: true') ||
                     evaluatorEvent?.detail?.toLowerCase().includes('relevant: yes');
  const isNotRelevant = evaluatorEvent?.detail?.toLowerCase().includes('relevant: false') ||
                        evaluatorEvent?.detail?.toLowerCase().includes('relevant: no');

  return (
    <div
      className="mt-3 rounded-lg p-3 border"
      style={{
        background: 'var(--bg-surface)',
        borderColor: 'rgba(255,255,255,0.06)',
      }}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] font-medium text-[var(--text-muted)] uppercase tracking-wide">
          Retrieved Docs
        </span>
        {evaluatorEvent && (
          <span
            className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${
              isRelevant
                ? 'bg-success/15 text-success'
                : isNotRelevant
                ? 'bg-error/15 text-error'
                : 'bg-black/[0.06] text-[var(--text-muted)]'
            }`}
          >
            {isRelevant ? '✓ Relevant' : isNotRelevant ? '✗ Not relevant' : 'Evaluated'}
          </span>
        )}
      </div>

      <div className="flex flex-col gap-1 text-[11px] text-[var(--text-secondary)]">
        {retrievalEvent?.detail && (
          <div className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-accent/60 shrink-0" />
            <span>Semantic: {retrievalEvent.detail}</span>
            {retrievalEvent.ms !== undefined && (
              <span className="text-[var(--text-muted)]">({retrievalEvent.ms}ms)</span>
            )}
          </div>
        )}
        {rerankerEvent?.detail && (
          <div className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-success/60 shrink-0" />
            <span>After RRF: {rerankerEvent.detail}</span>
            {rerankerEvent.ms !== undefined && (
              <span className="text-[var(--text-muted)]">({rerankerEvent.ms}ms)</span>
            )}
          </div>
        )}
        {evaluatorEvent?.detail && (
          <div className="flex items-center gap-1.5 mt-0.5">
            <span
              className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                isRelevant ? 'bg-success' : isNotRelevant ? 'bg-error' : 'bg-warning/60'
              }`}
            />
            <span
              className={
                isRelevant ? 'text-success/80' : isNotRelevant ? 'text-error/80' : ''
              }
              title={evaluatorEvent.detail}
            >
              {evaluatorEvent.detail.length > 60
                ? evaluatorEvent.detail.slice(0, 60) + '…'
                : evaluatorEvent.detail}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
