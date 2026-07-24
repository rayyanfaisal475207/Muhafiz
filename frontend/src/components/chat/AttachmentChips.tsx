// ============================================================
// AttachmentChips — files attached to the current conversation.
//
// These are NOT knowledge-base documents. They are read once, for this
// conversation only, and are never embedded or indexed. Knowledge-base
// ingestion is an admin function.
// ============================================================

import { useChatStore } from '../../store/chatStore';
import { formatBytes } from '../../lib/utils';

function FileGlyph({ className = 'w-3.5 h-3.5' }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M5 3.5h6l4 4v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1Z" />
      <path d="M11 3.5v4h4" />
    </svg>
  );
}

function XGlyph() {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      className="w-3 h-3"
      aria-hidden="true"
    >
      <path d="M5.5 5.5l9 9M14.5 5.5l-9 9" />
    </svg>
  );
}

export function AttachmentChips() {
  const { attachments, pendingAttachments, removeAttachment, dismissPending } = useChatStore();

  if (attachments.length === 0 && pendingAttachments.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 mb-2">
      {attachments.map((att) => {
        const failed = att.status === 'failed';
        return (
          <span
            key={att.attachment_id}
            className="group inline-flex items-center gap-1.5 pl-2 pr-1 py-1 rounded-sm text-[12px] max-w-[240px]"
            style={{
              background: failed ? 'var(--error-soft)' : 'var(--bg-surface-2)',
              border: `1px solid ${failed ? 'color-mix(in srgb, var(--error) 30%, transparent)' : 'var(--border)'}`,
              color: failed ? 'var(--error)' : 'var(--text-secondary)',
            }}
            title={
              failed
                ? att.error_message || 'This file could not be read'
                : `${att.filename} · ${formatBytes(att.file_size_bytes ?? 0)}`
            }
          >
            <FileGlyph className={`w-3.5 h-3.5 shrink-0 ${failed ? '' : 'text-[var(--accent)]'}`} />
            <span className="truncate">{att.filename}</span>
            <button
              onClick={() => removeAttachment(att.attachment_id)}
              className="shrink-0 p-0.5 rounded-xs transition-colors hover:bg-[var(--bg-surface-3)]"
              style={{ color: 'var(--text-muted)' }}
              aria-label={`Remove ${att.filename}`}
            >
              <XGlyph />
            </button>
          </span>
        );
      })}

      {pendingAttachments.map((pending) => {
        const failed = pending.status === 'failed';
        return (
          <span
            key={pending.tempId}
            className="inline-flex items-center gap-1.5 pl-2 pr-1 py-1 rounded-sm text-[12px] max-w-[240px]"
            style={{
              background: failed ? 'var(--error-soft)' : 'var(--bg-surface-2)',
              border: `1px solid ${failed ? 'color-mix(in srgb, var(--error) 30%, transparent)' : 'var(--border)'}`,
              color: failed ? 'var(--error)' : 'var(--text-muted)',
            }}
            title={failed ? pending.error : 'Reading file…'}
          >
            {failed ? (
              <FileGlyph className="w-3.5 h-3.5 shrink-0" />
            ) : (
              <svg viewBox="0 0 20 20" className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5}>
                <circle cx="10" cy="10" r="6.5" opacity={0.3} />
                <circle cx="10" cy="10" r="2.6" fill="currentColor" stroke="none">
                  <animate attributeName="r" values="2.6;4.2;2.6" dur="1.4s" repeatCount="indefinite" />
                  <animate attributeName="opacity" values="1;0.4;1" dur="1.4s" repeatCount="indefinite" />
                </circle>
              </svg>
            )}
            <span className="truncate">{pending.filename}</span>
            {failed && (
              <button
                onClick={() => dismissPending(pending.tempId)}
                className="shrink-0 p-0.5 rounded-xs transition-colors hover:bg-[var(--bg-surface-3)]"
                aria-label={`Dismiss ${pending.filename}`}
              >
                <XGlyph />
              </button>
            )}
          </span>
        );
      })}
    </div>
  );
}
