// ============================================================
// MessageBubble — renders a single chat message
// Memoized: during streaming the store updates on every token, and an
// unmemoized bubble re-rendered (and re-parsed) every message each token.
// ============================================================

import { memo, useMemo, Children } from 'react';
import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Components } from 'react-markdown';
import type { ChatMessage, Source } from '../../types';
import { GenerationStatus } from './GenerationStatus';
import { AlertIcon, GlobeIcon, ReadIcon } from './StatusIcons';
import { FileResultCard } from './FileResultCard';
import { QueryChecks } from '../pipeline/QueryChecks';

interface Props {
  message: ChatMessage;
  onSourceClick?: (source: Source) => void;
}

// ── Markdown rendering ───────────────────────────────────────────────────
//
// react-markdown + remark-gfm (Module 5 of FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md)
// replaced a hand-rolled regex parser here. react-markdown renders to real
// React elements, not dangerouslySetInnerHTML, and no rehype-raw (or any
// raw-HTML plugin) is registered below — so the original parser's own
// safety property survives exactly: model output can never inject markup,
// because there is no code path anywhere in this file that turns a string
// into HTML. remark-gfm adds table support, which the hand-rolled parser
// had no handling for at all.
//
// [Document N] citation chips have no library equivalent, so they're
// implemented as a small post-render pass over react-markdown's own
// element tree (see withCitationChips) — not by keeping any part of the
// old parser around as a patch on top of the library.
//
// ── Streaming-reveal treatment (Module 6) ───────────────────────────────
//
// The original plan assumed wiring .animate-token-in into the old
// hand-rolled parseContent loop, which produced discrete text segments one
// at a time — a natural "new token" boundary to animate. react-markdown
// re-parses the FULL, growing message.content string into a fresh AST on
// every SSE chunk; there is no equivalent per-token boundary exposed by
// the library, and animating individual characters through a full markdown
// re-render on every chunk would mean re-triggering (or hand-rolling a
// diff against) a tree that mostly hasn't semantically changed — real
// over-engineering for a "whisper of a fade."
//
// What DOES create a genuine, non-retriggering DOM boundary is a new
// top-level block appearing — a new paragraph, list, heading, or table
// starting. React's own reconciliation already patches an existing
// block's text in place without remounting it as more tokens stream into
// it (same type, same position in the tree), so a CSS rule keyed to
// ":last-child" only ever plays on a block's first paint, never on every
// keystroke within it: see `.token-stream > *:last-child` in index.css.
// This reuses .animate-token-in's own keyframe with zero new JS
// bookkeeping — no manual diffing, no per-character spans — and reads as
// each new chunk of the answer's structure easing in as it lands, which is
// the considered middle ground between a flat re-render and animating
// through the AST.

const CITATION_SPLIT = /(\[Document \d+\])/g;
const CITATION_MATCH = /^\[Document \d+\]$/;

/** Splits `[Document N]` markers out of a text-containing element's
 * children into citation-chip spans, leaving everything else (including
 * already-rendered nested elements like <strong>/<em>) untouched. Applied
 * per text-bearing component override below, so a citation nested inside
 * bold/italic text is still caught by that element's own override. */
function withCitationChips(children: ReactNode): ReactNode[] {
  const out: ReactNode[] = [];
  Children.toArray(children).forEach((child, i) => {
    if (typeof child !== 'string') {
      out.push(child);
      return;
    }
    const parts = child.split(CITATION_SPLIT);
    if (parts.length === 1) {
      out.push(child);
      return;
    }
    parts.forEach((part, j) => {
      out.push(
        CITATION_MATCH.test(part) ? (
          <span
            key={`cite-${i}-${j}`}
            className="text-accent text-xs font-semibold px-1 py-0.5 bg-accent/10 rounded mx-0.5 inline-block"
          >
            {part}
          </span>
        ) : (
          <span key={`txt-${i}-${j}`}>{part}</span>
        ),
      );
    });
  });
  return out;
}

/** Model markdown headings (#..######) are demoted one level, same as the
 * old hand-rolled parser did (h(level+1), capped at h6) — so an answer's
 * own heading never competes with the page's own hierarchy. */
function headingComponent(level: 1 | 2 | 3 | 4 | 5 | 6) {
  const Tag = (`h${Math.min(level + 1, 6)}` as 'h2' | 'h3' | 'h4' | 'h5' | 'h6');
  return function Heading({ children }: { children?: ReactNode }) {
    return <Tag>{withCitationChips(children)}</Tag>;
  };
}

const markdownComponents: Components = {
  p: ({ children }) => <p>{withCitationChips(children)}</p>,
  li: ({ children }) => <li>{withCitationChips(children)}</li>,
  strong: ({ children }) => <strong>{withCitationChips(children)}</strong>,
  em: ({ children }) => <em>{withCitationChips(children)}</em>,
  td: ({ children }) => <td>{withCitationChips(children)}</td>,
  th: ({ children }) => <th>{withCitationChips(children)}</th>,
  h1: headingComponent(1),
  h2: headingComponent(2),
  h3: headingComponent(3),
  h4: headingComponent(4),
  h5: headingComponent(5),
  h6: headingComponent(6),
};

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
            // `token-stream` (only while isStreaming) is the streaming-reveal
            // treatment — see the file-level comment above for why this
            // isn't a per-character animation.
            <div
              className={`prose-chat text-[15px] text-[var(--text-primary)] leading-relaxed ${
                message.isStreaming ? 'streaming-cursor token-stream' : ''
              }`}
            >
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {message.content}
              </ReactMarkdown>
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
