// ============================================================
// MessageBubble — renders a single chat message
// Memoized: during streaming the store updates on every token, and an
// unmemoized bubble re-rendered (and re-parsed) every message each token.
// ============================================================

import { memo, useMemo } from 'react';
import type { ReactNode } from 'react';
import type { ChatMessage, Source } from '../../types';
import { GenerationStatus } from './GenerationStatus';
import { AlertIcon, GlobeIcon, ReadIcon } from './StatusIcons';
import { FileResultCard } from './FileResultCard';
import { QueryChecks } from '../pipeline/QueryChecks';

interface Props {
  message: ChatMessage;
  onSourceClick?: (source: Source) => void;
}

// Inline parsing: [Document N] citation chips, **bold**, and `code`.
// Deliberately NOT a full markdown/HTML renderer — no dangerouslySetInnerHTML,
// so model output can never inject markup (the answer text is untrusted).
function parseInline(text: string, keyPrefix: string) {
  // Split on citations first so a citation inside bold still renders as a chip.
  return text.split(/(\[Document \d+\])/g).map((part, i) => {
    if (part.match(/^\[Document \d+\]$/)) {
      return (
        <span
          key={`${keyPrefix}-cite-${i}`}
          className="text-accent text-xs font-semibold px-1 py-0.5 bg-accent/10 rounded mx-0.5 inline-block"
        >
          {part}
        </span>
      );
    }
    // Then **bold**, *emphasis* and `code` within the non-citation segments.
    // [Scenario-test Finding B] Single-asterisk *emphasis* was previously
    // unhandled and rendered as literal asterisks ("A *30-bore pistol*"),
    // because only the ** form was matched. The ** alternative MUST stay
    // first in this pattern so a bold run is consumed as bold rather than
    // being mis-split into two single-asterisk fragments. The single-*
    // alternative requires a non-asterisk, non-space char right after the
    // opening * so it can't match a "**" boundary or a bare bullet/maths
    // asterisk, and disallows * inside the run so it stops at its own
    // closing delimiter.
    return (
      <span key={`${keyPrefix}-seg-${i}`}>
        {part
          // `_italic_` is matched only when the underscores sit on a word
          // boundary, so identifiers that legitimately contain underscores
          // (snake_case field names, some record IDs) are left alone — the
          // backend's own degradation notes use the _..._ form, and those
          // were rendering with literal underscores (verify-log Finding P).
          .split(/(\*\*[\s\S]*?\*\*|\*[^*\s][^*]*\*|(?<![A-Za-z0-9])_[^_\s][^_]*_(?![A-Za-z0-9])|`[^`]+`)/g)
          .map((sub, j) => {
            if (sub.length > 4 && sub.startsWith('**') && sub.endsWith('**')) {
              return <strong key={j}>{sub.slice(2, -2)}</strong>;
            }
            if (
              sub.length > 2 &&
              sub.startsWith('*') &&
              sub.endsWith('*') &&
              !sub.startsWith('**')
            ) {
              return <em key={j}>{sub.slice(1, -1)}</em>;
            }
            if (sub.length > 2 && sub.startsWith('_') && sub.endsWith('_')) {
              return <em key={j}>{sub.slice(1, -1)}</em>;
            }
            if (sub.startsWith('`') && sub.endsWith('`') && sub.length > 1) {
              return <code key={j}>{sub.slice(1, -1)}</code>;
            }
            return sub;
          })}
      </span>
    );
  });
}

// Block-level markdown → real React elements: headings (#..######), bullet
// lists (- / *), ordered lists (1.), and paragraphs. Consecutive list items
// are grouped into a single <ul>/<ol>. Blank lines separate paragraphs.
function parseContent(content: string) {
  const lines = content.split('\n');
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[]; start?: number } | null = null;
  let para: string[] = [];
  let key = 0;

  const flushPara = () => {
    if (para.length) {
      const text = para.join(' ');
      blocks.push(<p key={`b-${key++}`}>{parseInline(text, `p-${key}`)}</p>);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      const items = list.items.map((it, i) => (
        <li key={i}>{parseInline(it, `li-${key}-${i}`)}</li>
      ));
      blocks.push(
        list.ordered ? (
          <ol key={`b-${key++}`} start={list.start ?? 1}>
            {items}
          </ol>
        ) : (
          <ul key={`b-${key++}`}>{items}</ul>
        ),
      );
      list = null;
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const ordered = line.match(/^\s*(\d+)\.\s+(.*)$/);

    if (heading) {
      flushPara();
      flushList();
      const level = Math.min(heading[1].length, 6);
      const text = heading[2];
      const Tag = (`h${Math.min(level + 1, 6)}` as 'h2' | 'h3' | 'h4' | 'h5' | 'h6');
      blocks.push(<Tag key={`b-${key++}`}>{parseInline(text, `h-${key}`)}</Tag>);
    } else if (bullet) {
      flushPara();
      if (!list || list.ordered) {
        flushList();
        list = { ordered: false, items: [] };
      }
      list.items.push(bullet[1]);
    } else if (ordered) {
      flushPara();
      if (!list || !list.ordered) {
        flushList();
        // [Scenario-test Finding I] Carry the marker's own number as the
        // list's `start`. A numbered list that gets split (by an intervening
        // blank line, sub-bullet, or paragraph) previously restarted every
        // fragment at "1.", so a ranked list rendered as 1./1./1. — losing
        // the ranking, which in that answer WAS the information. The source
        // markdown was correct (verified: it emits 1./2./3.); the split was
        // ours.
        list = { ordered: true, items: [], start: parseInt(ordered[1], 10) || 1 };
      }
      list.items.push(ordered[2]);
    } else if (line.trim() === '') {
      // [Scenario-test Finding I] A blank line no longer terminates a list.
      // Models routinely put blank lines between list items (and between an
      // item and its own sub-bullets); treating that as "list over" was what
      // fragmented ordered lists into many single-item <ol>s. Paragraphs
      // still break here, and any non-list line below still closes the list.
      flushPara();
    } else {
      flushList();
      para.push(line);
    }
  }
  flushPara();
  flushList();
  return blocks;
}

function safeSourceLabel(filename: string): string {
  if (filename.startsWith('http')) {
    try {
      return new URL(filename).hostname.replace('www.', '');
    } catch {
      return filename;
    }
  }
  return filename;
}

export const MessageBubble = memo(function MessageBubble({ message, onSourceClick }: Props) {
  const isUser = message.role === 'user';

  const parsedContent = useMemo(
    () => (isUser ? null : parseContent(message.content)),
    [isUser, message.content],
  );

  const fileErrors = useMemo(
    () =>
      message.isStreaming
        ? []
        : (message.pipelineEvents ?? []).filter(
            (e) => e.step === 'file_generation' && e.status === 'error',
          ),
    [message.isStreaming, message.pipelineEvents],
  );

  if (isUser) {
    return (
      <div className="flex justify-end animate-slide-in-right">
        <div
          className="max-w-[75%] px-4 py-2.5 rounded-lg"
          style={{
            background: 'var(--bg-surface-3)',
            border: '1px solid var(--border)',
          }}
        >
          <p className="text-[var(--text-primary)] text-[15px] leading-relaxed whitespace-pre-wrap">
            {message.content}
          </p>
        </div>
      </div>
    );
  }

  // Assistant message — no bubble chrome. The answer is the page's main
  // content, so it reads as a document, not as a chat bubble (the way Claude
  // presents its responses). Only the user's turn gets a container.
  return (
    <div className="flex justify-start animate-slide-in-left">
      <div className="flex gap-3 w-full min-w-0">
        {/* Avatar — the logo mark, quietly */}
        <div
          className="w-7 h-7 rounded-sm flex items-center justify-center shrink-0 mt-0.5"
          style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}
          aria-hidden="true"
        >
          <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
            <path d="M12 3.5 14.4 9.6 20.5 12l-6.1 2.4L12 20.5 9.6 14.4 3.5 12l6.1-2.4L12 3.5Z" />
          </svg>
        </div>

        <div className="flex flex-col min-w-0 flex-1 pt-0.5">
          {/* Live generation status → collapses into "Show reasoning" */}
          <GenerationStatus
            events={message.pipelineEvents ?? []}
            isStreaming={!!message.isStreaming}
            hasContent={message.content.length > 0}
          />

          {message.content ? (
            <div
              className={`prose-chat text-[15px] text-[var(--text-primary)] leading-relaxed ${
                message.isStreaming ? 'streaming-cursor' : ''
              }`}
            >
              {parsedContent}
            </div>
          ) : null}
          {/* What was checked for this query. Deliberately BELOW the answer and
              outside the prose block: it describes how the answer was produced,
              not what the evidence says, so it must never read as part of the
              content. Renders nothing while streaming or when no trace exists. */}
          {!message.isStreaming && <QueryChecks trace={message.degradationTrace} />}
          {/* File Download Block */}
          {!message.isStreaming &&
            message.pipelineEvents?.some((e) => e.step === 'file_generation' && e.status === 'done') && (
              <div className="mt-3 mb-2 flex flex-col gap-2">
                {message.pipelineEvents
                  .filter((e) => e.step === 'file_generation' && e.status === 'done' && e.sources)
                  .flatMap((e) => e.sources || [])
                  .map((file, i) => (
                    <FileResultCard key={i} file={file} />
                  ))}
              </div>
            )}
          {/* File generation failures — previously swallowed entirely */}
          {fileErrors.length > 0 && (
            <div className="mt-2 mb-1 flex flex-col gap-2">
              {fileErrors.map((e, i) => (
                <div
                  key={i}
                  className="flex items-start gap-2 px-3 py-2.5 rounded-md text-[12.5px] leading-relaxed"
                  style={{
                    background: 'var(--error-soft)',
                    border: '1px solid var(--error)',
                    borderColor: 'color-mix(in srgb, var(--error) 30%, transparent)',
                    color: 'var(--text-secondary)',
                  }}
                >
                  <AlertIcon className="w-4 h-4 shrink-0 mt-px" />
                  <span>
                    <strong className="font-semibold" style={{ color: 'var(--error)' }}>
                      File generation failed.
                    </strong>{' '}
                    {e.detail || 'The document could not be created. Please try again.'}
                  </span>
                </div>
              ))}
            </div>
          )}
          {/* Source citations and status tags */}
          {!message.isStreaming && message.sources && message.sources.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-3">
              <span className="text-[11px] self-center" style={{ color: 'var(--text-faint)' }}>
                Sources
              </span>
              {message.sources.map((src, i) => {
                // Structural discriminator (src.type), not a filename/URL
                // heuristic — a case-evidence citation whose source
                // filename happens to look like a link must never render
                // as an external "web" reference.
                const isWeb = src.type === 'web';
                const label = safeSourceLabel(src.filename);
                return (
                  <button
                    key={i}
                    onClick={() => onSourceClick && onSourceClick(src)}
                    className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-pill text-[11px] cursor-pointer transition-colors hover:border-hover"
                    style={{
                      background: 'var(--bg-surface-2)',
                      border: '1px solid var(--border)',
                      color: 'var(--text-secondary)',
                    }}
                    title={src.filename}
                  >
                    {isWeb ? <GlobeIcon className="w-3 h-3" /> : <ReadIcon className="w-3 h-3" />}
                    <span className="max-w-[180px] truncate">{label}</span>
                  </button>
                );
              })}

              {/* Web Search Tag */}
              {message.pipelineEvents?.some((e) => e.step === 'web_search' && e.status === 'done') && (
                <span
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-pill text-[11px] ml-1"
                  style={{
                    background: 'var(--accent-soft)',
                    color: 'var(--accent)',
                    border: '1px solid var(--accent-border)',
                  }}
                >
                  <GlobeIcon className="w-3 h-3" /> Web search
                </span>
              )}

              {/* Cross-case finding tag — an XGRAPH/XAGG answer draws on
                  evidence outside the case currently being viewed, so it
                  must never read as an ordinary case-scoped citation. */}
              {message.pipelineEvents?.some(
                (e) => e.step === 'cross_case_finding' && e.status === 'done',
              ) && (
                <span
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-pill text-[11px] ml-1"
                  style={{
                    background: 'var(--warning-soft)',
                    color: 'var(--warning)',
                    border: '1px solid color-mix(in srgb, var(--warning) 30%, transparent)',
                  }}
                  title="This finding draws on evidence from other cases, structurally separate from this case's own evidence."
                >
                  <AlertIcon className="w-3 h-3" /> Cross-case finding
                </span>
              )}

              {/* Citation Warning Tag */}
              {message.pipelineEvents?.some(
                (e) => e.step === 'citation_validator' && e.detail?.includes('unverified'),
              ) && (
                <span
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-pill text-[11px] ml-1"
                  style={{
                    background: 'var(--warning-soft)',
                    color: 'var(--warning)',
                    border: '1px solid color-mix(in srgb, var(--warning) 30%, transparent)',
                  }}
                >
                  <AlertIcon className="w-3 h-3" /> Claims flagged
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
});
