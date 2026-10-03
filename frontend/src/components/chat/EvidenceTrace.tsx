// ============================================================
// EvidenceTrace — what the runtime actually did, step by step
//
// The backend emits one `step: "trace"` SSE event per stage: the concepts
// it matched, the names it resolved and how, every planner attempt and its
// exact rejection reason, the plan in concept LABELS, the SQL each
// operation compiled to, the rows and citations it got back, and the
// findings composition proposed with the reason each one was dropped.
//
// Those events were already arriving on the message and nothing rendered
// them — GenerationStatus keys off a fixed PHASES map and silently drops
// any step not in it, so a run showed a spinner and no explanation. This
// renders them instead, because two failures that read identically on
// screen ("the planner chose search" and "the planner emitted an invalid
// plan") need opposite fixes.
// ============================================================

import { useState } from 'react';
import type { PipelineEvent } from '../../types';

/** One recorded stage, as `trace.py` writes it. */
export interface TraceStep {
  seq: number;
  stage: string;
  title: string;
  at_ms: number;
  detail?: Record<string, unknown>;
}

interface Props {
  events: PipelineEvent[];
  /** Open by default while the answer is still streaming. */
  defaultOpen?: boolean;
}

/** Stages that mean something went wrong, so they read differently. */
const PROBLEM = new Set(['plan_rejected', 'fallback']);

const STAGE_LABEL: Record<string, string> = {
  question: 'Question',
  classification: 'Question kind',
  catalog: 'Catalog',
  entity_candidates: 'Name resolution',
  value_locations: 'Where values live',
  web_lookup: 'Open-web lookup',
  retrieval: 'Retrieval arms',
  planning: 'Planner',
  planner_call: 'Planner',
  plan: 'Plan',
  plan_rejected: 'Plan rejected',
  strategy: 'New strategy',
  payload: 'Request size',
  entity_lookup: 'Lookup',
  catalog_lookup: 'Lookup',
  clarification: 'Clarification',
  fallback: 'Fallback',
  execute: 'Execute',
  sql: 'SQL',
  search_scope: 'Search scope',
  graph: 'Graph',
  graph_seeds: 'Graph',
  result: 'Result',
  composition: 'Composition',
  answer: 'Answer',
};

function isPlain(value: unknown): value is string | number | boolean {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean';
}

/** Render one detail value without collapsing it into "[object Object]". */
function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === '') return null;
  if (isPlain(value)) return <span className="et-val">{String(value)}</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="et-muted">none</span>;
    return (
      <ul className="et-list">
        {value.slice(0, 25).map((item, i) => (
          <li key={i}>
            {isPlain(item) ? String(item) : <Fields data={item as Record<string, unknown>} inline />}
          </li>
        ))}
        {value.length > 25 && <li className="et-muted">and {value.length - 25} more</li>}
      </ul>
    );
  }
  return <Fields data={value as Record<string, unknown>} />;
}

function Fields({ data, inline }: { data: Record<string, unknown>; inline?: boolean }) {
  const entries = Object.entries(data ?? {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== '' &&
      !(Array.isArray(v) && v.length === 0),
  );
  if (entries.length === 0) return null;
  if (inline) {
    return (
      <span>
        {entries.map(([k, v], i) => (
          <span key={k}>
            {i > 0 && '  '}
            <span className="et-key">{k}</span>=<Value value={v} />
          </span>
        ))}
      </span>
    );
  }
  return (
    <div className="et-fields">
      {entries.map(([k, v]) => (
        <div className="et-field" key={k}>
          <div className="et-key">{k}</div>
          <div className="et-value"><Value value={v} /></div>
        </div>
      ))}
    </div>
  );
}

function Step({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(false);
  const detail = step.detail ?? {};
  const problem = PROBLEM.has(step.stage);
  // Technical details are available on deliberate expansion.
  const sql = step.stage === 'sql' ? String(detail.sql ?? '') : '';

  return (
    <li className={`et-step${problem ? ' et-step-problem' : ''}`}>
      <button type="button" className="et-head" onClick={() => setOpen((o) => !o)}>
        <span className="et-seq">{step.seq}</span>
        <span className="et-stage">{STAGE_LABEL[step.stage] ?? step.stage}</span>
        <span className="et-title">{step.title}</span>
        <span className="et-ms">{step.at_ms} ms</span>
      </button>
      {open && (
        <div className="et-body">
          {sql ? (
            <>
              <pre className="et-sql">{sql}</pre>
              {Array.isArray(detail.params) && (detail.params as unknown[]).length > 0 && (
                <div className="et-field">
                  <div className="et-key">params</div>
                  <div className="et-value"><Value value={detail.params} /></div>
                </div>
              )}
            </>
          ) : (
            <Fields data={detail} />
          )}
        </div>
      )}
    </li>
  );
}

export function EvidenceTrace({ events, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen);

  // A step can arrive live (one per event) and again in the final response
  // payload, so the last writer for a given seq wins and order is restored.
  const bySeq = new Map<number, TraceStep>();
  for (const event of events) {
    const payload = event.trace;
    if (!payload) continue;
    // A live event carries ONE step; the final response carries them all.
    const incoming: TraceStep[] = Array.isArray(payload) ? payload : [payload];
    for (const s of incoming) {
      if (s && typeof s.seq === 'number') bySeq.set(s.seq, s);
    }
  }
  const steps = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  if (steps.length === 0) return null;

  const problems = steps.filter((s) => PROBLEM.has(s.stage)).length;

  return (
    <div className="et">
      <button type="button" className="et-toggle" onClick={() => setOpen((o) => !o)}>
        {open ? '▾' : '▸'} Trace · {steps.length} steps
        {problems > 0 && <span className="et-badge">{problems} rejected</span>}
      </button>
      {open && <ol className="et-steps">{steps.map((s) => <Step key={s.seq} step={s} />)}</ol>}

      <style>{`
        .et { margin-top: 10px; font-size: 12px; }
        .et-toggle {
          background: none; border: none; padding: 2px 0; cursor: pointer;
          color: #6b7280; font-size: 12px; font-family: inherit;
        }
        .et-toggle:hover { color: #111827; }
        .et-badge {
          margin-left: 8px; padding: 1px 6px; border-radius: 9px;
          background: #fee2e2; color: #991b1b; font-size: 11px;
        }
        .et-steps { list-style: none; margin: 6px 0 0; padding: 0;
                    border-left: 2px solid #e5e7eb; }
        .et-step { margin: 0; padding: 0 0 0 10px; }
        .et-step-problem { border-left: 2px solid #ef4444; margin-left: -2px; }
        .et-head {
          display: flex; gap: 8px; align-items: baseline; width: 100%;
          background: none; border: none; padding: 3px 0; cursor: pointer;
          text-align: left; font-family: inherit; font-size: 12px; color: #374151;
        }
        .et-head:hover { color: #111827; }
        .et-seq { color: #9ca3af; min-width: 18px; font-variant-numeric: tabular-nums; }
        .et-stage {
          min-width: 108px; color: #4b5563; font-weight: 600;
          text-transform: uppercase; font-size: 10px; letter-spacing: .04em;
        }
        .et-title { flex: 1; }
        .et-ms { color: #9ca3af; font-variant-numeric: tabular-nums; }
        .et-body { padding: 4px 0 8px 26px; }
        .et-fields { display: flex; flex-direction: column; gap: 4px; }
        .et-field { display: flex; gap: 8px; align-items: baseline; }
        .et-key { color: #6b7280; min-width: 128px; }
        .et-value, .et-val { color: #111827; word-break: break-word; }
        .et-muted { color: #9ca3af; }
        .et-list { margin: 0; padding-left: 14px; }
        .et-list li { margin: 1px 0; }
        .et-sql {
          margin: 4px 0; padding: 6px 8px; background: #f9fafb;
          border: 1px solid #e5e7eb; border-radius: 4px;
          white-space: pre-wrap; word-break: break-word;
          font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
          color: #1f2937;
        }
        .et-sql-peek {
          max-height: 3.6em; overflow: hidden; opacity: .75; margin-left: 26px;
        }
        @media (prefers-color-scheme: dark) {
          .et-toggle { color: #9ca3af; }
          .et-toggle:hover { color: #f3f4f6; }
          .et-steps { border-left-color: #374151; }
          .et-head { color: #d1d5db; }
          .et-head:hover { color: #f9fafb; }
          .et-stage { color: #9ca3af; }
          .et-key, .et-muted, .et-seq, .et-ms { color: #9ca3af; }
          .et-value, .et-val { color: #f3f4f6; }
          .et-sql { background: #111827; border-color: #374151; color: #e5e7eb; }
          .et-badge { background: #7f1d1d; color: #fecaca; }
        }
      `}</style>
    </div>
  );
}
