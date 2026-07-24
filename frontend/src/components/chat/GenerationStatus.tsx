// ============================================================
// GenerationStatus — what the assistant is doing, while it does it.
//
// Three lifecycles, one component:
//   1. WORKING  — before any answer text exists, show the current phase:
//                 an animated line icon + a plain-language label
//                 ("Searching the web"), with a live elapsed timer.
//   2. STREAMING— the moment answer text starts arriving, the live status
//                 collapses out of the way into a quiet one-line summary.
//   3. DONE     — the summary stays, expandable after the fact, so the
//                 reasoning trail is available but never in the way.
//
// The phases are derived from the pipeline SSE events the backend already
// emits — this component adds no new network surface, it just translates
// engine vocabulary ("reranker") into human vocabulary ("Ranking sources").
// ============================================================

import { memo, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement } from 'react';
import type { PipelineEvent } from '../../types';
import { formatMs } from '../../lib/utils';
import {
  SparkIcon, RouteIcon, SearchIcon, GlobeIcon, RankIcon, ReadIcon,
  PenIcon, FileIcon, VerifyIcon, SaveIcon, AlertIcon, CheckIcon,
  SkipIcon, ChevronIcon,
} from './StatusIcons';

type IconComponent = (props: { className?: string; animate?: boolean }) => ReactElement;

interface PhaseDef {
  /** Present tense — shown while the phase is running. */
  active: string;
  /** Past tense — shown in the collapsed reasoning trail. */
  done: string;
  Icon: IconComponent;
}

// Engine step → human phase. Anything not listed here is not shown to the
// user (e.g. title_generation, which is housekeeping, not reasoning).
const PHASES: Record<string, PhaseDef> = {
  query_rewriter:     { active: 'Understanding your question', done: 'Understood the question', Icon: SparkIcon },
  router:             { active: 'Choosing an approach',        done: 'Chose an approach',       Icon: RouteIcon },
  retrieval:          { active: 'Searching the knowledge base',done: 'Searched the knowledge base', Icon: SearchIcon },
  web_search:         { active: 'Searching the web',           done: 'Searched the web',        Icon: GlobeIcon },
  reranker:           { active: 'Ranking sources',             done: 'Ranked sources',          Icon: RankIcon },
  evaluator:          { active: 'Reading results',             done: 'Read the results',        Icon: ReadIcon },
  response:           { active: 'Writing response',            done: 'Wrote the response',      Icon: PenIcon },
  file_generation:    { active: 'Building your file',          done: 'Built your file',         Icon: FileIcon },
  citation_validator: { active: 'Checking citations',          done: 'Checked citations',       Icon: VerifyIcon },
  memory:             { active: 'Saving to this conversation', done: 'Saved to this conversation', Icon: SaveIcon },
};

type PhaseStatus = 'active' | 'done' | 'error' | 'skipped';

interface Phase {
  key: string;
  step: string;
  label: string;
  Icon: IconComponent;
  status: PhaseStatus;
  detail?: string;
  ms?: number;
  retryNum?: number;
}

/** Fold the raw event stream into an ordered list of user-facing phases. */
function derivePhases(events: PipelineEvent[]): Phase[] {
  const order: string[] = [];
  const byKey = new Map<string, Phase>();

  for (const event of events) {
    const def = PHASES[event.step];
    if (!def) continue;
    // Token deltas carry no phase information.
    if (event.status === 'streaming') continue;

    const retry = event.retry_num ?? 0;
    const key = `${event.step}:${retry}`;

    if (!byKey.has(key)) {
      order.push(key);
      byKey.set(key, {
        key,
        step: event.step,
        label: def.active,
        Icon: def.Icon,
        status: 'active',
        retryNum: retry || undefined,
      });
    }

    const phase = byKey.get(key)!;

    switch (event.status) {
      case 'done':
        phase.status = 'done';
        phase.label = def.done;
        break;
      case 'error':
        phase.status = 'error';
        phase.label = def.active;
        break;
      case 'skipped':
        phase.status = 'skipped';
        phase.label = def.done;
        break;
      default:
        // 'active' / 'running' — leave as-is
        phase.status = phase.status === 'done' ? 'done' : 'active';
        break;
    }

    if (event.ms !== undefined) phase.ms = event.ms;
    if (event.detail && event.status !== 'active') phase.detail = event.detail;
  }

  return order.map((key) => byKey.get(key)!);
}

/** A live-ticking elapsed timer, so a long phase never looks frozen. */
function useElapsed(active: boolean): number {
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef<number | null>(null);

  useEffect(() => {
    if (!active) {
      startedAt.current = null;
      setElapsed(0);
      return;
    }
    startedAt.current = Date.now();
    const id = window.setInterval(() => {
      if (startedAt.current) setElapsed(Date.now() - startedAt.current);
    }, 100);
    return () => window.clearInterval(id);
  }, [active]);

  return elapsed;
}

interface Props {
  events: PipelineEvent[];
  isStreaming: boolean;
  hasContent: boolean;
}

export const GenerationStatus = memo(function GenerationStatus({
  events, isStreaming, hasContent,
}: Props) {
  const phases = useMemo(() => derivePhases(events), [events]);

  // The live phase: whatever is currently running (or the last thing that
  // errored, so a failure never scrolls silently past).
  const livePhase = useMemo(() => {
    const active = [...phases].reverse().find((p) => p.status === 'active');
    if (active) return active;
    return [...phases].reverse().find((p) => p.status === 'error');
  }, [phases]);

  const isWorking = isStreaming && !hasContent;
  const elapsed = useElapsed(isWorking && !!livePhase);

  const [expanded, setExpanded] = useState(false);
  // A new response always starts collapsed, whatever the user did last time.
  useEffect(() => {
    if (isWorking) setExpanded(false);
  }, [isWorking]);

  const visible = phases.filter((p) => p.status !== 'skipped');
  const totalMs = visible.reduce((sum, p) => sum + (p.ms ?? 0), 0);
  const hasError = phases.some((p) => p.status === 'error');

  if (phases.length === 0) return null;

  // ── 1. WORKING — the live, animated status line ────────────────────────
  if (isWorking && livePhase) {
    const { Icon, label, status } = livePhase;
    const isError = status === 'error';

    return (
      <div
        className="flex items-center gap-2.5 mb-3 animate-fade-in"
        role="status"
        aria-live="polite"
      >
        {/* Icon in a soft navy well, with a halo pulsing outward while it works */}
        <span className="relative flex items-center justify-center w-7 h-7 shrink-0">
          {!isError && (
            <span
              className="absolute inset-0 rounded-pill"
              style={{ background: 'var(--accent-soft)', animation: 'status-halo 1.8s ease-out infinite' }}
            />
          )}
          <span
            className="relative flex items-center justify-center w-7 h-7 rounded-pill"
            style={{
              background: isError ? 'var(--error-soft)' : 'var(--accent-soft)',
              color: isError ? 'var(--error)' : 'var(--accent)',
            }}
          >
            {/* keyed so the icon animates in whenever the phase changes */}
            <span key={livePhase.key} className="animate-status-icon flex">
              <Icon animate={!isError} />
            </span>
          </span>
        </span>

        <span
          className={`text-[13px] font-medium ${isError ? '' : 'animate-status-breathe'}`}
          style={{ color: isError ? 'var(--error)' : 'var(--text-secondary)' }}
        >
          {isError ? `${label} failed` : label}
        </span>

        {elapsed > 400 && (
          <span className="text-[11px] tabular-nums" style={{ color: 'var(--text-faint)' }}>
            {formatMs(elapsed)}
          </span>
        )}

        {/* Breadcrumb of phases already finished — quiet progress, no bar */}
        {visible.length > 1 && (
          <span className="flex items-center gap-1 ml-0.5">
            {visible.slice(0, -1).map((p) => (
              <span
                key={p.key}
                title={p.label}
                className="w-1 h-1 rounded-pill"
                style={{ background: p.status === 'error' ? 'var(--error)' : 'var(--accent-border)' }}
              />
            ))}
          </span>
        )}
      </div>
    );
  }

  // ── 2 & 3. STREAMING / DONE — collapsed summary, expandable ────────────
  return (
    <div className="mb-3">
      <button
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="group inline-flex items-center gap-1.5 px-2 py-1 -ml-2 rounded-sm text-[12px] transition-colors"
        style={{ color: hasError ? 'var(--error)' : 'var(--text-muted)' }}
      >
        <ChevronIcon
          className={`w-3 h-3 transition-transform duration-200 ${expanded ? 'rotate-90' : ''}`}
        />
        <span className="group-hover:underline underline-offset-2">
          {hasError ? 'Some steps failed' : expanded ? 'Hide reasoning' : 'Show reasoning'}
        </span>
        <span style={{ color: 'var(--text-faint)' }}>
          · {visible.length} step{visible.length === 1 ? '' : 's'}
          {totalMs > 0 && ` · ${formatMs(totalMs)}`}
        </span>
      </button>

      {expanded && (
        <ol
          className="mt-2 ml-1 pl-4 flex flex-col gap-2.5 animate-fade-in"
          style={{ borderLeft: '1px solid var(--border)' }}
        >
          {phases.map((phase) => {
            const { Icon } = phase;
            const isError = phase.status === 'error';
            const isSkipped = phase.status === 'skipped';

            return (
              <li key={phase.key} className="flex items-start gap-2.5">
                <span
                  className="flex items-center justify-center w-5 h-5 rounded-sm shrink-0 mt-px"
                  style={{
                    background: isError
                      ? 'var(--error-soft)'
                      : isSkipped
                        ? 'transparent'
                        : 'var(--accent-soft)',
                    color: isError
                      ? 'var(--error)'
                      : isSkipped
                        ? 'var(--text-faint)'
                        : 'var(--accent)',
                  }}
                >
                  {isError ? (
                    <AlertIcon className="w-3.5 h-3.5" />
                  ) : isSkipped ? (
                    <SkipIcon className="w-3.5 h-3.5" />
                  ) : (
                    <Icon className="w-3.5 h-3.5" />
                  )}
                </span>

                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span
                      className="text-[12.5px] font-medium"
                      style={{
                        color: isError
                          ? 'var(--error)'
                          : isSkipped
                            ? 'var(--text-faint)'
                            : 'var(--text-secondary)',
                      }}
                    >
                      {isSkipped ? `${phase.label} — skipped` : phase.label}
                    </span>
                    {phase.retryNum ? (
                      <span
                        className="text-[10px] px-1.5 rounded-pill"
                        style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}
                      >
                        retry {phase.retryNum}
                      </span>
                    ) : null}
                    {phase.ms !== undefined && (
                      <span className="text-[11px] tabular-nums ml-auto shrink-0" style={{ color: 'var(--text-faint)' }}>
                        {formatMs(phase.ms)}
                      </span>
                    )}
                  </div>

                  {phase.detail && (
                    <p
                      className="text-[11.5px] mt-0.5 leading-relaxed break-words"
                      style={{ color: 'var(--text-faint)' }}
                    >
                      {phase.detail}
                    </p>
                  )}
                </div>
              </li>
            );
          })}
          {/* Completed phases get a check at the end of the trail */}
          <li className="flex items-center gap-2.5" style={{ color: 'var(--text-faint)' }}>
            <span className="flex items-center justify-center w-5 h-5 shrink-0">
              <CheckIcon className="w-3.5 h-3.5" />
            </span>
            <span className="text-[12px]">Answer delivered</span>
          </li>
        </ol>
      )}
    </div>
  );
});
