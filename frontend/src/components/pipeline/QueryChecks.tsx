import { useState } from 'react';
import type { DegradationTrace } from '../../types';

/**
 * "What I checked" — per-query transparency for the investigator, on their own
 * answer, in their own chat.
 *
 * Sourced from the degradation trace the harness supervisor builds once per
 * query and persists on the assistant's message (migration 019), so it is
 * present both while the answer streams AND after a page reload. Previously
 * this kind of detail lived only in the live SSE stream and vanished on
 * refresh, and the durable copy was admin-only on the Run History page.
 *
 * ALWAYS-ON BY DESIGN, not only when something failed. If the element appeared
 * only on degradation, its presence would read as an alarm and its absence
 * would be indistinguishable from "the feature didn't render" — so a clean run
 * would tell the investigator nothing. Showing "Checked: document search,
 * case-graph search" every time is what makes the degraded case legible by
 * contrast.
 *
 * Renders NOTHING when there is no trace at all (a legacy-path or pre-harness
 * message). That is a different fact from a clean run and must not be shown as
 * one.
 *
 * All source names come pre-rendered from the backend in `trace.labels`. Do
 * not map raw identifiers here — the canonical label map lives in
 * contracts.py, and a client-side copy is the drift risk this deliberately
 * avoids.
 */
export function QueryChecks({ trace }: { trace?: DegradationTrace }) {
  const [expanded, setExpanded] = useState(false);

  if (!trace) return null;

  const { labels, caveats } = trace;
  const contributed = labels?.contributed_only ?? [];
  const partial = labels?.degraded_and_contributed ?? [];
  const failed = labels?.degraded_only ?? [];

  const nothingRecorded =
    contributed.length === 0 && partial.length === 0 && failed.length === 0;
  if (nothingRecorded && (caveats?.length ?? 0) === 0) return null;

  const hasIssue = partial.length > 0 || failed.length > 0 || (caveats?.length ?? 0) > 0;
  const worked = [...contributed, ...partial];

  // One compact line. The summary states what was consulted; anything that
  // went wrong is named rather than hinted at.
  const summary = worked.length > 0
    ? `Checked: ${worked.join(', ')}`
    : 'No sources were available for this answer';

  return (
    <div className="query-checks" data-has-issue={hasIssue || undefined}>
      <button
        type="button"
        className="query-checks-summary"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className="query-checks-dot" aria-hidden="true" />
        <span className="query-checks-text">{summary}</span>
        {failed.length > 0 && (
          <span className="query-checks-flag">
            {failed.length === 1 ? '1 source unavailable' : `${failed.length} sources unavailable`}
          </span>
        )}
        <span className="query-checks-chevron" aria-hidden="true">
          {expanded ? '▾' : '▸'}
        </span>
      </button>

      {expanded && (
        <div className="query-checks-detail">
          {contributed.map((label) => (
            <div key={`ok-${label}`} className="query-checks-row">
              <span className="query-checks-badge is-ok">used</span> {label}
            </div>
          ))}
          {partial.map((label) => (
            <div key={`partial-${label}`} className="query-checks-row">
              <span className="query-checks-badge is-partial">partial</span> {label}
              <span className="query-checks-note">
                — contributed, but not fully checked
              </span>
            </div>
          ))}
          {failed.map((label) => (
            <div key={`failed-${label}`} className="query-checks-row">
              <span className="query-checks-badge is-failed">unavailable</span> {label}
            </div>
          ))}
          {(caveats ?? []).map((c, i) => (
            <div key={`caveat-${i}`} className="query-checks-caveat">
              {c}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default QueryChecks;
